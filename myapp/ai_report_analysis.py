"""Deterministic triage for user reports about AI replies.

The functions in this module deliberately use only evidence saved on an
``AIReport``-like object.  They do not call an AI model, inspect remote files,
or claim that a reported failure has been proven.  Their purpose is to turn
explicit wording in a report into stable, explainable queues for a human or an
engineering team.

An ``AIReport``-like value may be either an object with attributes or a mapping
with the same keys.  The useful fields are ``user_prompt``, ``reported_reply``,
``explanation``, ``user_image``, and ``reported_image``; missing fields are
treated as empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Pattern


_EVIDENCE_LIMIT = 220


@dataclass(frozen=True)
class _Category:
    """Immutable presentation and remediation details for one issue queue."""

    key: str
    label: str
    severity: str
    fix: str


_IMAGE_ROUTING = _Category(
    key="image_generation_edit_routing",
    label="Image generation, editing, or routing",
    severity="high",
    fix=(
        "Route explicit image create/edit requests to the image pipeline, carry "
        "all source images into edit jobs, and require a saved image result before "
        "claiming completion. Return a clear capability or retry message when the "
        "pipeline cannot produce one."
    ),
)

_IMAGE_QUALITY = _Category(
    key="image_quality_prompt_adherence",
    label="Image quality or prompt adherence",
    severity="medium",
    fix=(
        "Pass the complete prompt and reference image to generation, preserve "
        "requested subjects, text, style, and composition, and offer a regeneration "
        "path that keeps the user's stated constraints."
    ),
)

_SERVICE_RELIABILITY = _Category(
    key="service_reliability_no_response",
    label="Service reliability or no response",
    severity="high",
    fix=(
        "Capture the upstream status and request identifier, retry only transient "
        "failures with a limit, and show an actionable retry message without saving "
        "an empty or falsely successful assistant turn."
    ),
)

_FILE_DELIVERY = _Category(
    key="downloadable_file_delivery",
    label="Downloadable PDF or Word file delivery",
    severity="high",
    fix=(
        "Create and persist the requested real file format, verify its signature, "
        "MIME type, and download response, and add the working download link only "
        "after storage succeeds."
    ),
)

_CONTEXT_LOSS = _Category(
    key="instruction_context_loss",
    label="Instruction or conversation context loss",
    severity="medium",
    fix=(
        "Send the relevant prior turns, attachments, and explicit constraints to the "
        "selected model, then add a regression case using this reported turn so later "
        "messages continue to honor them."
    ),
)

_INCOMPLETE_OUTPUT = _Category(
    key="incomplete_output",
    label="Incomplete or cut-off output",
    severity="medium",
    fix=(
        "Track every requested part as a response checklist, reserve enough output "
        "budget for all parts, and provide a continuation path when generation is cut "
        "off instead of presenting the partial result as complete."
    ),
)

_ACCURACY = _Category(
    key="accuracy_relevance",
    label="Accuracy or relevance",
    severity="high",
    fix=(
        "Re-run the reported prompt with appropriate grounding or calculation checks, "
        "remove unsupported claims, and add a regression test for the corrected fact, "
        "calculation, or requested scope."
    ),
)

_SAFETY_TONE = _Category(
    key="safety_tone",
    label="Safety or inappropriate tone",
    severity="high",
    fix=(
        "Review the reply against the safety and tone policy, strengthen the relevant "
        "input/output guardrail, and test a sanitized version of this case for a calm, "
        "respectful, and appropriately bounded response."
    ),
)

_MANUAL_REVIEW = _Category(
    key="manual_review",
    label="Manual review required",
    severity="medium",
    fix=(
        "Compare the saved prompt, reply, explanation, and image snapshots manually; "
        "record the confirmed cause and add a narrower deterministic rule only if the "
        "same evidence pattern recurs."
    ),
)


def _patterns(*expressions: str) -> tuple[Pattern[str], ...]:
    """Compile case-insensitive evidence patterns once at import time."""

    return tuple(re.compile(expression, re.IGNORECASE) for expression in expressions)


_IMAGE_NOUN = (
    r"(?:images?|photos?|pictures?|posters?|logos?|wallpapers?|banners?|graphics?|"
    r"artworks?|thumbnails?|flyers?|illustrations?|drawings?|diagrams?|icons?|avatars?|"
    r"stickers?|backgrounds?|cards?|greeting cards?|birthday cards?|imgs?|"
    r"posts?|instagram posts?|insta posts?|social media posts?|stories?)"
)
_IMAGE_ACTION = (
    r"(?:generate|gen|create|make|made|design|draw|produce|render|edit|change|update|remove|"
    r"replace|retouch|enhance|convert|turn(?:\s+(?:this|it|that))?\s+in\s*to|"
    r"banao|banado|bana do|banaiye|banwao|banwa do|bna|bnao|"
    r"bna do|bna ke do|bana ke do|bnado|bnaiye|"
    r"dikhao|dikha do|dikhaiye|dikhana|daalo|dalo|daal do|dal do|"
    r"jodo|jod do|lagao|laga do|nikaal|nikalo|nikal do|nikal ke do)"
)

_IMAGE_REQUEST_PATTERNS = _patterns(
    rf"\b{_IMAGE_ACTION}\b[\s\S]{{0,60}}\b{_IMAGE_NOUN}\b",
    rf"\b{_IMAGE_NOUN}\b[\s\S]{{0,60}}\b{_IMAGE_ACTION}\b",
)

_IMAGE_ROUTING_REPORT_PATTERNS = _patterns(
    rf"\b(?:no|missing|fake|nonexistent)\s+(?:generating\s+|generated\s+|edited\s+)?"
    rf"{_IMAGE_NOUN}\b",
    rf"\b{_IMAGE_NOUN}\b.{{0,70}}\b(?:was\s+not|wasn't|is\s+not|isn't|never)\s+"
    r"(?:generated|created|made|produced|rendered|edited|changed|shown|displayed|attached)",
    rf"\b(?:did\s+not|didn't|failed\s+to|never|could\s+not|couldn't)\s+{_IMAGE_ACTION}\b"
    rf".{{0,60}}\b{_IMAGE_NOUN}\b",
    rf"\b{_IMAGE_NOUN}\b.{{0,60}}\b(?:did\s+not|didn't|failed\s+to|never|"
    rf"could\s+not|couldn't)\s+{_IMAGE_ACTION}\b",
    rf"\b(?:unable|refused)\s+to\s+{_IMAGE_ACTION}\b.{{0,60}}\b{_IMAGE_NOUN}\b",
    rf"\b(?:image|photo|picture)\s+(?:generation|editing|edit)\b.{{0,45}}"
    r"\b(?:failed|did\s+not\s+work|didn't\s+work|was\s+not\s+done)\b",
    rf"\b(?:image\s+generator|{_IMAGE_NOUN})\b.{{0,55}}\b(?:not\s+working|"
    r"does\s+not\s+work|doesn't\s+work|nahi|nahin|nai)\b",
    rf"\b{_IMAGE_NOUN}\b.{{0,55}}\b(?:nahi|nahin|nai)\b.{{0,25}}"
    r"\b(?:ban|bana|bna|generate|edit|change|kar|ker|ho)\w*\b",
    rf"\b{_IMAGE_NOUN}\b.{{0,50}}\b(?:only\s+one|one\s+at\s+a\s+time|"
    r"multiple|more\s+than\s+one|10)\b.{0,35}\bupload\b",
    rf"\b(?:only\s+one|one\s+at\s+a\s+time|multiple|more\s+than\s+one|10)\b"
    rf".{{0,35}}\b{_IMAGE_NOUN}\b.{{0,25}}\bupload\b",
    r"\b(?:make|create)\b.{0,45}\b(?:birthday|greeting)\s+card\b.{0,45}"
    r"\bnot\s+(?:only\s+)?(?:matter|text|words?)\b",
    rf"\b(?:cannot|can't|cant|unable\s+to)\b.{{0,35}}\b{_IMAGE_ACTION}\b"
    rf".{{0,35}}\b{_IMAGE_NOUN}\b",
    rf"\b(?:gave|returned|showed)\s+(?:me\s+)?(?:only\s+)?(?:text|instructions?|a\s+description)"
    rf"\s+instead\s+of\s+(?:an?\s+)?{_IMAGE_NOUN}\b",
    rf"\b(?:wrong|text|chat)\s+(?:model|route|routing)\b.{{0,50}}\b{_IMAGE_NOUN}\b",
    rf"\b{_IMAGE_NOUN}\b.{{0,50}}\b(?:wrong|text|chat)\s+(?:model|route|routing)\b",
    r"\b(?:image|photo|picture)\s+(?:link|attachment)\b.{0,45}\b(?:fake|broken|missing|doesn't work|does not work)\b",
)

_IMAGE_ROUTING_CONTEXTUAL_REPORT_PATTERNS = _patterns(
    rf"\b(?:did\s+not|didn't|does\s+not|doesn't|do\s+not|don't|failed\s+to|never|"
    rf"could\s+not|couldn't|cannot|can't|cant)\s+{_IMAGE_ACTION}\b",
    rf"\b(?:unable|refused)\s+to\s+{_IMAGE_ACTION}\b",
    r"\b(?:fake|useless)\s+(?:work(?:ing)?|response|reply|result)\b",
)

_IMAGE_ROUTING_REPLY_PATTERNS = _patterns(
    rf"\b(?:i\s+)?(?:cannot|can't|am\s+unable\s+to|am\s+not\s+able\s+to|"
    rf"do\s+not\s+have\s+the\s+ability\s+to)\s+(?:directly\s+)?{_IMAGE_ACTION}\b",
    r"\b(?:do\s+not|don't)\s+have\b.{0,55}\bimage[- ]generation\s+capability\b",
    r"\bas\s+(?:an?\s+)?(?:text[- ]based|language)\s+(?:ai|model)\b",
    r"\b(?:design\s+brief|ascii/markdown|ascii\s+(?:art|diagram|drawing))\b",
    r"!\[[^\]]*\]\(attachment://[^)]+\)",
)

_IMAGE_QUALITY_PATTERNS = _patterns(
    r"\b(?:blurry|blurred|pixelated|distorted|deformed|grainy|low[- ]quality|"
    r"low[- ]resolution|poor[- ]quality|artifacts?|artefacts?)\b",
    rf"\b{_IMAGE_NOUN}\b.{{0,70}}\b(?:does\s+not|doesn't|did\s+not|didn't|fails?\s+to)"
    r"\s+(?:match|follow|respect|reflect|include|show)\b",
    r"\b(?:does\s+not|doesn't|did\s+not|didn't|fails?\s+to)\s+"
    rf"(?:match|follow|respect|reflect)\b.{{0,45}}\b(?:the\s+)?(?:prompt|request|instructions?|{_IMAGE_NOUN})\b",
    rf"\b(?:wrong|missing|extra|unwanted)\s+(?:face|faces|hands?|fingers?|text|words?|"
    rf"colors?|colours?|objects?|people|person|subject|style|layout|composition|{_IMAGE_NOUN})\b",
    rf"\b{_IMAGE_NOUN}\b.{{0,55}}\b(?:looks?\s+wrong|poor|bad|unusable|inaccurate)\b",
    r"\b(?:not\s+what\s+i\s+asked|did\s+not\s+follow\s+(?:my\s+)?prompt|"
    r"didn't\s+follow\s+(?:my\s+)?prompt)\b",
)

_SERVICE_REPORT_PATTERNS = _patterns(
    r"\b(?:no|zero|empty|blank)\s+(?:answer|response|reply|output)\b",
    r"\b(?:did\s+not|didn't|does\s+not|doesn't|failed\s+to|never)\s+respond\b",
    r"\b(?:stuck|kept)\s+(?:on\s+)?(?:loading|thinking|generating|retrying)\b",
    r"\b(?:timed?\s*out|timeout|server\s+error|service\s+unavailable|temporarily\s+unavailable|"
    r"connection\s+(?:error|failed|lost|dropped)|network\s+error|disconnected|overloaded|"
    r"rate[- ]limit(?:ed)?|crashed|something\s+went\s+wrong)\b",
    r"\b(?:response|reply|generation|request)\b.{0,45}\b(?:failed|stopped|froze|crashed)\b",
)

_SERVICE_REPLY_PATTERNS = _patterns(
    r"\b(?:currently|temporarily)\s+(?:unavailable|disconnected|overloaded)\b",
    r"\b(?:timed?\s*out|service\s+unavailable|connection\s+(?:error|failed|lost)|"
    r"failed\s+to\s+(?:generate|complete)|something\s+went\s+wrong)\b",
    r"\btry\s+again\s+(?:in\s+a\s+(?:few\s+)?moments?|later)\b",
)

_FILE_NOUN = r"(?:files?|pdfs?|\.pdf|docx?|\.docx|word\s+(?:files?|documents?)|documents?)"
_FILE_ACTION = r"(?:create|generate|make|prepare|produce|export|convert|save|provide|give|send|share)"

_FILE_REQUEST_PATTERNS = _patterns(
    rf"\b{_FILE_ACTION}\b[\s\S]{{0,75}}\b{_FILE_NOUN}\b",
    rf"\b{_FILE_NOUN}\b[\s\S]{{0,60}}\b(?:download|downloadable|{_FILE_ACTION})\b",
    r"\bdownloadable\s+(?:pdf|word|docx?|file|document)\b",
    r"\b(?:in|as)\s+(?:a\s+)?(?:pdf|word|docx?)\s+(?:file|document)\b",
)

_FILE_REPORT_PATTERNS = _patterns(
    rf"\b{_FILE_NOUN}\b.{{0,75}}\b(?:not\s+(?:created|generated|made|attached|downloadable)|"
    r"missing|fake|broken|invalid|corrupt(?:ed)?|empty|won't\s+open|will\s+not\s+open)\b",
    rf"\b(?:not\s+(?:created|generated|made|attached|downloadable)|missing|fake|broken|"
    rf"invalid|corrupt(?:ed)?|empty)\b.{{0,75}}\b{_FILE_NOUN}\b",
    r"\bdownload\s+(?:link|button)\b.{0,55}\b(?:missing|fake|broken|invalid|"
    r"does\s+not\s+work|doesn't\s+work|not\s+working)\b",
    r"\b(?:no|missing)\s+download\s+(?:link|button)\b",
    rf"\b(?:did\s+not|didn't|failed\s+to|could\s+not|couldn't|unable\s+to)\s+"
    rf"(?:create|generate|make|attach|provide|download|open)\b.{{0,55}}\b{_FILE_NOUN}\b",
    rf"\b(?:cannot|can't|could\s+not|couldn't|unable\s+to)\s+(?:download|open)\b"
    rf".{{0,55}}\b{_FILE_NOUN}\b",
    r"\b(?:plain|only)\s+text\b.{0,45}\b(?:instead\s+of|not)\b.{0,35}\b(?:pdf|word|docx?|file|document)\b",
    r"\b(?:pdf|docx?|word\s+(?:file|document))\b.{0,50}\b(?:plain|only)\s+text\b",
)

_FILE_REPLY_PATTERNS = _patterns(
    r"\b(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:directly\s+)?"
    r"(?:create|generate|make|attach|provide|send|export)\b.{0,100}"
    r"\b(?:pdf|word|docx?|file|document)\b",
    r"\b(?:i\s+)?(?:cannot|can't|am\s+unable\s+to)\s+(?:attach|provide)\s+(?:a\s+)?"
    r"download\s+link\b",
)

_CONTEXT_PATTERNS = _patterns(
    r"\b(?:ignored|forgot|lost|missed|did\s+not\s+follow|didn't\s+follow|failed\s+to\s+follow)"
    r"\s+(?:my\s+|the\s+|previous\s+|earlier\s+)?(?:instruction|instructions|context|constraint|"
    r"constraints|request|requirements?|conversation|message|messages)\b",
    r"\b(?:forgot|lost|ignored|does\s+not\s+remember|doesn't\s+remember|did\s+not\s+remember|"
    r"didn't\s+remember)\b.{0,60}\b(?:previous|earlier|last|above|conversation|context)\b",
    r"\b(?:previous|earlier|last|above)\b.{0,60}\b(?:was\s+ignored|were\s+ignored|"
    r"not\s+remembered|forgotten|lost)\b",
    r"\b(?:asked\s+again|repeated\s+(?:the\s+)?(?:same\s+)?question|started\s+over|"
    r"lost\s+the\s+thread)\b",
    r"\b(?:did\s+not|didn't|failed\s+to)\s+(?:use|read|consider|remember)\b.{0,55}"
    r"\b(?:attachment|uploaded\s+file|image|document|details?|information)\b",
    r"\b(?:forgot|ignored|missed)\b.{0,55}\b(?:what\s+i\s+(?:said|told|asked)|"
    r"my\s+(?:preferences?|format|language|word\s+limit|requirements?))\b",
    r"\b(?:did\s+not|didn't|not)\s+(?:take|taking|read|use|using|consider)\b.{0,40}"
    r"\b(?:the\s+)?(?:whole|full|complete)\s+prompt\b",
)

_CONTEXT_REPLY_PATTERNS = _patterns(
    r"\bi\s+(?:do\s+not|don't)\s+(?:have\s+access\s+to|remember|retain)\b.{0,60}"
    r"\b(?:previous|earlier|prior|conversation|messages?|context|attachment)\b",
    r"\bplease\s+(?:repeat|resend|upload\s+again)\b.{0,45}\b(?:instruction|details?|"
    r"message|file|image|attachment)\b",
)

_INCOMPLETE_PATTERNS = _patterns(
    r"\b(?:incomplete|unfinished|partial|cut\s+off|truncated)\b",
    r"\b(?:stopped|ended)\s+(?:halfway|mid[- ]sentence|before\s+finishing|too\s+early)\b",
    r"\b(?:did\s+not|didn't|failed\s+to)\s+(?:finish|complete|answer|cover|include)\b",
    r"\b(?:missing|skipped|omitted)\s+(?:a\s+|an\s+|the\s+|some\s+)?(?:part|parts|"
    r"section|sections|step|steps|question|questions|point|points|details?|ending|rest)\b",
    r"\b(?:only|just)\s+(?:answered|covered|completed|gave)\b.{0,35}\b(?:one|first|part)\b",
    r"\b(?:only|just)\s+(?:answered|covered|completed)\b.{0,25}\b\d+\s+(?:of|out\s+of)\s+\d+\b",
    r"\b(?:answer|reply|response|output)\b.{0,45}\b(?:incomplete|unfinished|partial|"
    r"cut\s+off|truncated)\b",
    r"\b(?:not\s+(?:a\s+)?(?:clear|complete)\s+and\s+detailed|not\s+detailed\s+enough|"
    r"lacks?\s+(?:important\s+)?detail)\b",
    r"\b(?:beech|beach|bich|beech\s+mein|beach\s+me)\b.{0,35}\b(?:atak|ruk)\w*\b",
)

_ACCURACY_PATTERNS = _patterns(
    r"\b(?:wrong|incorrect|inaccurate|false|untrue|misleading)\s+"
    r"(?:answer|response|reply|result|fact|facts|information|calculation|solution|explanation|data)\b",
    r"\b(?:answer|response|reply|result|fact|facts|information|calculation|solution|explanation|data)"
    r"\b.{0,35}\b(?:is|are|was|were|seems?|looks?)\s+(?:wrong|incorrect|inaccurate|false|"
    r"untrue|misleading)\b",
    r"\b(?:made[- ]up|fabricated|hallucinated|invented)\b",
    r"\b(?:factual|calculation|maths?|mathematical|logic|citation|source)\s+(?:error|mistake)\b",
    r"\b(?:irrelevant|unrelated|off[- ]topic|not\s+relevant|does\s+not\s+answer|"
    r"doesn't\s+answer|did\s+not\s+answer|didn't\s+answer)\b",
    r"\b(?:not\s+what\s+i\s+asked|answered\s+(?:a\s+)?different\s+question)\b",
)

_GENERIC_WRONG_PATTERNS = _patterns(
    r"^\s*(?:it(?:'s|\s+is|\s+was)?\s+)?(?:completely\s+|totally\s+|very\s+)?"
    r"(?:wrong|incorrect|inaccurate|irrelevant|bad)\s*[.!]*\s*$",
    r"\bneeds?\s+(?:a\s+)?correction\b",
)

_SAFETY_PATTERNS = _patterns(
    r"\b(?:abusive|offensive|harmful|unsafe|dangerous|hateful|racist|sexist|sexual|profane|vulgar|"
    r"rude|disrespectful|threatening|inappropriate|insulting|hostile|toxic|biased|"
    r"discriminatory|harassing)\b",
    r"\b(?:hate\s+speech|personal\s+attack|racial\s+slur|sexual\s+content|unsafe\s+advice|"
    r"encouraged\s+(?:harm|violence|self[- ]harm))\b",
    r"\b(?:tone|language|wording)\b.{0,40}\b(?:aggressive|offensive|rude|inappropriate|"
    r"hostile|unprofessional)\b",
    r"\b(?:swore|insulted|threatened|harassed)\b.{0,30}\b(?:at\s+me|me|the\s+user)\b",
)


def _field(report: Any, name: str) -> Any:
    """Read a field from either a mapping or an attribute-based report."""

    if isinstance(report, Mapping):
        return report.get(name, "")
    return getattr(report, name, "")


def _text_field(report: Any, name: str) -> str:
    """Return a report field as text without allowing ``None`` to leak."""

    value = _field(report, name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _has_value(report: Any, name: str) -> bool:
    """Check snapshot presence without copying large image data into evidence."""

    value = _field(report, name)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _first_match(patterns: tuple[Pattern[str], ...], text: str) -> re.Match[str] | None:
    """Return the earliest configured match, preserving rule priority."""

    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match
    return None


def _clean_excerpt(text: str, limit: int = _EVIDENCE_LIMIT) -> str:
    """Make a compact, bounded excerpt suitable for an admin report."""

    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _matchable(text: str) -> str:
    """Normalize typography for matching while preserving original evidence."""

    return (text or "").translate(
        str.maketrans(
            {
                "‘": "'",
                "’": "'",
                "‛": "'",
                "‐": "-",
                "‑": "-",
                "–": "-",
                "—": "-",
                "\u00a0": " ",
                "\u202f": " ",
            }
        )
    )


def _quoted(source: str, text: str) -> str:
    """Label a saved-text excerpt so a reviewer can reproduce the match."""

    return f'{source}: “{_clean_excerpt(text)}”'


def _combined_evidence(prompt: str, evidence_source: str, evidence_text: str) -> str:
    """Show both requested intent and the explicit failure signal."""

    prompt_excerpt = _clean_excerpt(prompt, 95)
    evidence_excerpt = _clean_excerpt(evidence_text, 125)
    return f'User prompt: “{prompt_excerpt}”; {evidence_source}: “{evidence_excerpt}”'


def _issue(category: _Category, evidence: str) -> dict[str, str]:
    """Build the stable public shape used by per-report analysis."""

    return {
        "key": category.key,
        "label": category.label,
        "severity": category.severity,
        "evidence": evidence,
        "fix": category.fix,
    }


def analyze_report(report: Any) -> list[dict[str, str]]:
    """Classify explicit failure signals on one ``AIReport``-like value.

    The returned list contains one dictionary per matched category, in stable
    category order.  Each dictionary has ``key``, ``label``, ``severity``,
    ``evidence``, and ``fix``.  A match is a triage signal, not a factual
    adjudication of the complaint.  If no category-specific evidence is found,
    one ``manual_review`` issue is returned instead of guessing.
    """

    prompt = _text_field(report, "user_prompt")
    reply = _text_field(report, "reported_reply")
    explanation = _text_field(report, "explanation")
    has_user_image = _has_value(report, "user_image")
    has_reported_image = _has_value(report, "reported_image")

    matchable_prompt = _matchable(prompt)
    matchable_reply = _matchable(reply)
    matchable_explanation = _matchable(explanation)

    image_request = bool(_first_match(_IMAGE_REQUEST_PATTERNS, matchable_prompt))
    image_context = image_request or has_user_image or has_reported_image
    file_request = bool(_first_match(_FILE_REQUEST_PATTERNS, matchable_prompt))

    issues: list[dict[str, str]] = []

    # Image generation/edit/routing needs an image-shaped context plus an
    # explicit non-delivery, refusal, bad-route, or fake-attachment signal.
    routing_report_match = _first_match(
        _IMAGE_ROUTING_REPORT_PATTERNS, matchable_explanation
    )
    routing_context_match = (
        _first_match(_IMAGE_ROUTING_CONTEXTUAL_REPORT_PATTERNS, matchable_explanation)
        if image_context
        else None
    )
    routing_reply_match = _first_match(_IMAGE_ROUTING_REPLY_PATTERNS, matchable_reply)
    if (image_context or routing_report_match) and (
        routing_report_match or routing_context_match or routing_reply_match
    ):
        if routing_report_match or routing_context_match:
            evidence = (
                _combined_evidence(prompt, "User report", explanation)
                if prompt
                else _quoted("User report", explanation)
            )
        else:
            evidence = (
                _combined_evidence(prompt, "Assistant reply", reply)
                if prompt
                else _quoted("Assistant reply", reply)
            )
        issues.append(_issue(_IMAGE_ROUTING, evidence))

    # Quality/adherence signals can be stated without a stored image because
    # older reports may not have snapshotted media.  They still need explicit
    # image context; a generic "bad" report alone is insufficient.
    image_quality_match = _first_match(_IMAGE_QUALITY_PATTERNS, matchable_explanation)
    if image_context and image_quality_match:
        evidence = (
            _combined_evidence(prompt, "User report", explanation)
            if prompt
            else _quoted("User report", explanation)
        )
        issues.append(_issue(_IMAGE_QUALITY, evidence))

    service_report_match = _first_match(_SERVICE_REPORT_PATTERNS, matchable_explanation)
    service_reply_match = _first_match(_SERVICE_REPLY_PATTERNS, matchable_reply)
    if service_report_match or service_reply_match:
        if service_report_match:
            evidence = _quoted("User report", explanation)
        else:
            evidence = _quoted("Assistant reply", reply)
        issues.append(_issue(_SERVICE_RELIABILITY, evidence))

    file_report_match = _first_match(_FILE_REPORT_PATTERNS, matchable_explanation)
    file_reply_match = _first_match(_FILE_REPLY_PATTERNS, matchable_reply)
    if (file_request or file_report_match) and (file_report_match or file_reply_match):
        if file_report_match:
            evidence = (
                _combined_evidence(prompt, "User report", explanation)
                if prompt
                else _quoted("User report", explanation)
            )
        else:
            evidence = (
                _combined_evidence(prompt, "Assistant reply", reply)
                if prompt
                else _quoted("Assistant reply", reply)
            )
        issues.append(_issue(_FILE_DELIVERY, evidence))

    context_report_match = _first_match(_CONTEXT_PATTERNS, matchable_explanation)
    context_reply_match = _first_match(_CONTEXT_REPLY_PATTERNS, matchable_reply)
    if context_report_match or context_reply_match:
        evidence = (
            _quoted("User report", explanation)
            if context_report_match
            else _quoted("Assistant reply", reply)
        )
        issues.append(_issue(_CONTEXT_LOSS, evidence))

    incomplete_match = _first_match(_INCOMPLETE_PATTERNS, matchable_explanation)
    if incomplete_match:
        issues.append(_issue(_INCOMPLETE_OUTPUT, _quoted("User report", explanation)))

    accuracy_match = _first_match(_ACCURACY_PATTERNS, matchable_explanation)
    generic_wrong_match = _first_match(_GENERIC_WRONG_PATTERNS, matchable_explanation)
    # A bare "wrong" attached to an image/file task is too ambiguous to call
    # a factual failure; its specific category or manual review is safer.
    if accuracy_match or (generic_wrong_match and not image_context and not file_request):
        issues.append(_issue(_ACCURACY, _quoted("User report", explanation)))

    safety_match = _first_match(_SAFETY_PATTERNS, matchable_explanation)
    if safety_match:
        issues.append(_issue(_SAFETY_TONE, _quoted("User report", explanation)))

    if not issues:
        if explanation.strip():
            evidence = (
                "No category-specific wording was detected. "
                + _quoted("User report", explanation)
            )
        elif prompt.strip() and reply.strip():
            evidence = (
                "No explanation or category-specific failure wording was saved. "
                + _combined_evidence(prompt, "Assistant reply", reply)
            )
        elif reply.strip():
            evidence = (
                "No explanation or prompt was saved. " + _quoted("Assistant reply", reply)
            )
        elif prompt.strip():
            evidence = (
                "No explanation or assistant reply was saved. " + _quoted("User prompt", prompt)
            )
        else:
            evidence = (
                "The report contains no saved text evidence; inspect its linked "
                "conversation or message if one is available."
            )
        issues.append(_issue(_MANUAL_REVIEW, evidence))

    return issues


def classify_report(report: Any) -> list[dict[str, str]]:
    """Alias for :func:`analyze_report` using classification terminology."""

    return analyze_report(report)


def aggregate_report_issues(reports: Iterable[Any]) -> list[dict[str, Any]]:
    """Group report classifications into deterministic actionable totals.

    Each report contributes at most once to a category.  Results contain
    ``key``, ``label``, ``severity``, ``count``, and ``fix`` and are sorted by
    descending count, then case-insensitive label and stable key.  The function
    accepts any iterable, including a Django queryset, but imports no Django
    code and performs no I/O of its own.
    """

    grouped: dict[str, dict[str, Any]] = {}
    for report in reports:
        seen: set[str] = set()
        for issue in analyze_report(report):
            key = issue["key"]
            if key in seen:
                continue
            seen.add(key)
            if key not in grouped:
                grouped[key] = {
                    "key": key,
                    "label": issue["label"],
                    "severity": issue["severity"],
                    "count": 0,
                    "fix": issue["fix"],
                }
            grouped[key]["count"] += 1

    return sorted(
        grouped.values(),
        key=lambda item: (-item["count"], item["label"].casefold(), item["key"]),
    )


def aggregate_reports(reports: Iterable[Any]) -> list[dict[str, Any]]:
    """Short alias for :func:`aggregate_report_issues`."""

    return aggregate_report_issues(reports)


__all__ = [
    "aggregate_report_issues",
    "aggregate_reports",
    "analyze_report",
    "classify_report",
]
