"""A reasoning model's private reasoning never reaches the user, even when only
its closing </think> tag shows up (Sol, Terra and Luna run on Nemotron)."""
from django.test import SimpleTestCase

from myapp import ai_chat

LEAK = ('The user is asking which AI model I am. According to the system prompt, I am currently '
        'running as "ChatGPT 6 Sol" - the flagship GPT-6 model from OpenAI. I should answer with '
        'this name and description.</think>I’m **ChatGPT 6 Sol** — OpenAI’s flagship GPT-6 model.')
ANSWER = 'I’m **ChatGPT 6 Sol** — OpenAI’s flagship GPT-6 model.'


def pieces(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


def filtered(text, size=7):
    return ''.join(ai_chat._without_think(ai_chat._without_leading_reasoning(pieces(text, size))))


class LeadingReasoningTests(SimpleTestCase):
    def test_reasoning_before_a_lone_closing_tag_is_dropped(self):
        for size in (1, 3, 7, 50, len(LEAK)):
            self.assertEqual(filtered(LEAK, size), ANSWER)

    def test_an_ordinary_reply_comes_through_unchanged_and_starts_streaming_early(self):
        reply = 'Hello! How are you today? 😊 It’s nice to meet you — I’m ChatGPT 6 Sol.' * 3
        self.assertEqual(filtered(reply), reply)
        stream = ai_chat._without_leading_reasoning(iter(pieces(reply, 10)))
        first = next(stream)
        self.assertLess(len(first), 60)  # not held back until the end

    def test_a_real_answer_that_opens_like_reasoning_is_still_shown_in_full(self):
        reply = 'Let me explain how photosynthesis works. Plants take in light, water and carbon dioxide.'
        self.assertEqual(filtered(reply), reply)

    def test_a_short_reply_is_kept(self):
        self.assertEqual(filtered('Hi! 👋'), 'Hi! 👋')

    def test_a_proper_think_block_is_still_removed(self):
        self.assertEqual(filtered('<think>Plan the reply.</think>Here you go.'), 'Here you go.')
        # The closing tag arriving in a later piece than the opening one.
        chunks = ['<think>let me wor', 'k it out</think>The answer is 4.']
        self.assertEqual(''.join(ai_chat._without_think(ai_chat._without_leading_reasoning(chunks))),
                         'The answer is 4.')

    def test_whole_reply_cleanup_handles_the_lone_closing_tag_too(self):
        self.assertEqual(ai_chat.strip_think_tags(LEAK), ANSWER)
        self.assertEqual(ai_chat.strip_think_tags('<think>x</think>Done.'), 'Done.')
        self.assertEqual(ai_chat.strip_think_tags('No tags here.'), 'No tags here.')
