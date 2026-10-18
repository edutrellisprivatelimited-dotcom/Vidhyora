"""Keeps what the chat model is sent short without losing what matters.

Two separate jobs, both done by the fast Quick model:

* A very long message is condensed before the model answers it. Its opening
  and closing stay word-for-word — that is almost always where the actual
  question or instruction is — and only the middle is shortened. The saved
  message is never changed; only the copy sent to the model is.
* A long chat keeps a running summary of its older messages on the
  conversation. It is brought up to date in the background after a reply is
  saved, so nobody waits for it, and the newest messages always go as-is.

Every step falls back to sending the original text when anything fails.
"""
import hashlib
import logging
import threading

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# ~1,000 words. AI_CHAT_MAX_MESSAGE_CHARS (16,000) is the hard cap on one message.
LONG_MESSAGE_CHARS = 6000
KEEP_HEAD_CHARS = 700
KEEP_TAIL_CHARS = 700
CONDENSE_TIMEOUT = 20.0
CONDENSED_CACHE_SECONDS = 7 * 24 * 60 * 60

# The newest messages always go to the model word-for-word.
KEEP_RECENT_MESSAGES = 6
# Older messages are folded into the summary once there are this many of them
# waiting, or once the messages waiting add up to this many characters.
FOLD_AFTER_MESSAGES = 6
FOLD_AFTER_CHARS = 24000
FOLD_INPUT_CHARS = 60000      # most a single summary update reads
FOLD_MESSAGE_CHARS = 6000     # most of any one message it reads
SUMMARY_MAX_TOKENS = 1500
SUMMARY_TIMEOUT = 60.0
FOLD_LOCK_SECONDS = 180


def enabled():
    return getattr(settings, 'AI_CONTEXT_COMPRESSION_ENABLED', True)


def _complete(system, user_content, *, max_tokens, timeout):
    """One plain-text call to the Quick model, hidden reasoning off."""
    from myapp import ai_chat
    cfg = ai_chat.MODELS['quick']
    kwargs = dict(
        model=cfg['id'],
        messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user_content}],
        temperature=0.2, top_p=ai_chat.TOP_P, max_tokens=max_tokens, timeout=timeout, stream=True,
    )
    if cfg.get('reasoning'):
        kwargs['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False, 'force_nonempty_content': True}}
    client = ai_chat._client_for_cfg(cfg.get('api_key_setting'), cfg)
    return ai_chat.strip_think_tags(''.join(ai_chat._stream_content(client, kwargs))).strip()


# ── one long message ─────────────────────────────────────────────────────────

_CONDENSE_SYSTEM = (
    "You shorten the middle part of a long message a user sent to an AI "
    "tutor, so the tutor can still answer it fully. Keep every name, number, "
    "date, formula, equation, code identifier, quoted phrase, question and "
    "instruction exactly. Drop repetition, filler and decoration. Write in the "
    "same language as the text. Output only the shortened text — no heading, "
    "no comment about what you did, and do not answer or reply to it."
)


def _split_at_space(text, index, *, forward):
    """Move a cut point to the nearest line break or space so no word is split."""
    window = 200
    if forward:
        part = text[index:index + window]
        found = [spot for spot in (part.find('\n'), part.find(' ')) if spot >= 0]
        return index + min(found) + 1 if found else index
    part = text[max(0, index - window):index]
    cut = max(part.rfind('\n'), part.rfind(' '))
    return index - len(part) + cut + 1 if cut >= 0 else index


def _skip_condensing(text):
    """Requests that need the user's exact words: rewriting, translating,
    proofreading, or anything with code in it."""
    from myapp import ai_chat
    return ai_chat.is_rewrite_request(text) or ai_chat.is_code_request(text) or '```' in text


def _condensed_key(text):
    return 'ai-condensed-' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def cached_condensed(text):
    """The condensed copy of an earlier long message, if one was made."""
    if not text or len(text) <= LONG_MESSAGE_CHARS:
        return None
    try:
        return cache.get(_condensed_key(text))
    except Exception:
        return None


def condense_long_message(text):
    """The long message with its middle shortened, or None to send it as it is."""
    if not enabled() or not isinstance(text, str) or len(text) <= LONG_MESSAGE_CHARS:
        return None
    if _skip_condensing(text):
        return None
    cached = cached_condensed(text)
    if cached:
        return cached
    head_end = _split_at_space(text, KEEP_HEAD_CHARS, forward=True)
    tail_start = _split_at_space(text, len(text) - KEEP_TAIL_CHARS, forward=False)
    head, middle, tail = text[:head_end], text[head_end:tail_start], text[tail_start:]
    if len(middle) < 2000:
        return None
    try:
        short = _complete(_CONDENSE_SYSTEM, middle, max_tokens=1200, timeout=CONDENSE_TIMEOUT)
    except Exception as exc:
        logger.warning('Could not condense a long message, sending it in full: %s', exc)
        return None
    # A summary that barely saves anything isn't worth the detail it may lose.
    if not short or len(short) > len(middle) * 0.7:
        return None
    condensed = (
        f"{head.rstrip()}\n\n[The middle of this long message has been shortened; "
        f"names, numbers and facts were kept.]\n{short}\n[End of shortened part.]\n\n{tail.lstrip()}"
    )
    try:
        cache.set(_condensed_key(text), condensed, CONDENSED_CACHE_SECONDS)
    except Exception:
        pass
    logger.info('Condensed a long message: %d -> %d chars', len(text), len(condensed))
    return condensed


# ── a long chat ──────────────────────────────────────────────────────────────

_SUMMARY_SYSTEM = (
    "You keep the memory of a long chat between a user and an AI assistant. "
    "You are given the summary so far (it may be empty) and the next messages. "
    "Write one updated summary that replaces the old one. Keep: what the user "
    "is working on and why, every specific fact they gave (names, numbers, "
    "dates, places, class or exam, preferences, constraints), questions asked "
    "and the answers or decisions reached, code/file/function names, and "
    "anything still open or promised. Drop greetings, small talk and wording "
    "that does not change the meaning. Write short bullet points in the "
    "language the chat mostly uses, saying 'The user…' and 'The assistant…'. "
    "Output only the summary."
)


def summary_note(summary):
    """The system message that stands in for the summarised messages."""
    return (
        "Summary of the earlier part of this conversation (those messages are not "
        "shown below). Use it as context the user already gave you; never mention "
        "that a summary exists:\n" + summary
    )


def _message_for_summary(row):
    from myapp.models import AIMessage
    speaker = 'User' if row.role == AIMessage.ROLE_USER else 'Assistant'
    text = row.content or ''
    if row.document_name:
        doc = (row.document_text or '')[:FOLD_MESSAGE_CHARS // 2]
        text = f"[Attached document: {row.document_name}]\n{doc}\n---\n{text}"
    if row.image_data and row.role == AIMessage.ROLE_USER:
        text = '[Sent an image] ' + text + (f"\nText in the image: {row.document_text}" if row.document_text and not row.document_name else '')
    if len(text) > FOLD_MESSAGE_CHARS:
        text = text[:FOLD_MESSAGE_CHARS] + ' […]'
    return f"{speaker}: {text}"


def _messages_to_fold(conversation):
    """The live messages after the summary that are old enough to fold, or []
    when it isn't worth an update yet."""
    rows = list(
        conversation.messages.filter(superseded=False, pk__gt=conversation.context_summary_until)
        .order_by('pk').only('pk', 'role', 'content', 'image_data', 'document_name', 'document_text')
    )
    if len(rows) <= KEEP_RECENT_MESSAGES:
        return []
    cut = len(rows) - KEEP_RECENT_MESSAGES
    # Start the word-for-word part on a user message, so it never opens with
    # an answer whose question was folded away.
    from myapp.models import AIMessage
    while cut > 0 and rows[cut].role != AIMessage.ROLE_USER:
        cut -= 1
    older = rows[:cut]
    if not older:
        return []
    chars = sum(len(row.content or '') + len(row.document_text or '') for row in older)
    if len(older) < FOLD_AFTER_MESSAGES and chars < FOLD_AFTER_CHARS:
        return []
    return older


def fold_conversation(conversation_id):
    """Fold the older messages of one conversation into its summary. True
    when the summary was updated."""
    from myapp import privacy
    from myapp.models import AIConversation
    conversation = AIConversation.objects.filter(pk=conversation_id).first()
    if conversation is None:
        return False
    older = _messages_to_fold(conversation)
    if not older:
        return False
    lines = [_message_for_summary(row) for row in older]
    # Too much for one call: keep the newest part, as plain trimming would.
    while len(lines) > 1 and sum(len(line) for line in lines) > FOLD_INPUT_CHARS:
        lines.pop(0)
    previous = conversation.context_summary
    prompt = (
        f"Summary so far:\n{previous or '(none yet)'}\n\nNext messages:\n" + '\n\n'.join(lines)
    )
    try:
        summary = _complete(_SUMMARY_SYSTEM, privacy.redact(prompt),
                            max_tokens=SUMMARY_MAX_TOKENS, timeout=SUMMARY_TIMEOUT)
    except Exception as exc:
        logger.warning('Could not update the summary of conversation %s: %s', conversation_id, exc)
        return False
    if not summary:
        return False
    # Saved only if nothing changed the summary meanwhile (an edit resets it).
    updated = AIConversation.objects.filter(
        pk=conversation_id, context_summary_until=conversation.context_summary_until,
    ).update(context_summary=summary, context_summary_until=older[-1].pk)
    if updated:
        logger.info('Conversation %s summary now covers messages up to %s (%d chars)',
                    conversation_id, older[-1].pk, len(summary))
    return bool(updated)


def schedule_fold(conversation):
    """After a reply is saved: update the summary in the background when
    enough older messages have piled up. Cheap to call on every turn."""
    if not enabled() or conversation is None:
        return
    try:
        if not _messages_to_fold(conversation):
            return
        lock = f'ai-summary-lock-{conversation.pk}'
        if not cache.add(lock, 1, FOLD_LOCK_SECONDS):
            return
    except Exception:
        logger.exception('Could not check whether conversation %s needs a summary', conversation.pk)
        return

    def run():
        try:
            fold_conversation(conversation.pk)
        except Exception:
            logger.exception('Summary update failed for conversation %s', conversation.pk)
        finally:
            cache.delete(lock)
            from django.db import connection
            connection.close()

    threading.Thread(target=run, name='ai-summary', daemon=True).start()


def forget_summary(conversation, from_pk):
    """An edit or retry replaces message `from_pk` and everything after it.
    A summary that already covers it describes things that are no longer
    there, so it is dropped and rebuilt later."""
    from myapp.models import AIConversation
    if conversation.context_summary_until and from_pk <= conversation.context_summary_until:
        AIConversation.objects.filter(pk=conversation.pk).update(context_summary='', context_summary_until=0)
        conversation.context_summary = ''
        conversation.context_summary_until = 0
