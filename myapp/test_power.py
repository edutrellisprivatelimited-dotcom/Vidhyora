import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from myapp import ai_chat


def _sent_kwargs(power, **extra):
    """The request _stream_chat_impl would send upstream at this power."""
    captured = {}

    def fake_stream(client, kwargs):
        captured.update(kwargs)
        yield 'answer'

    with patch.object(ai_chat, '_stream_content', side_effect=fake_stream), \
            patch.object(ai_chat, '_client_for_cfg', return_value=object()):
        reply = ''.join(ai_chat._stream_chat_impl(
            [{'role': 'user', 'content': 'What is 2 + 2?'}], 'quick', power=power, **extra))
    assert reply == 'answer'
    return captured


class PowerLevelTests(TestCase):
    def _thinking(self, kwargs):
        return kwargs['extra_body']['chat_template_kwargs']['enable_thinking']

    def _reminder(self, kwargs):
        return kwargs['messages'][-2]['content']

    def test_medium_is_the_unchanged_default(self):
        kwargs = _sent_kwargs('medium')
        self.assertFalse(self._thinking(kwargs))
        self.assertEqual(kwargs['max_tokens'], ai_chat.MAX_TOKENS)
        self.assertNotIn('Power is set', self._reminder(kwargs))

    def test_high_and_extra_high_think_first_with_room_for_it(self):
        for power, extra in ai_chat.POWER_THINKING_TOKENS.items():
            kwargs = _sent_kwargs(power)
            self.assertTrue(self._thinking(kwargs), power)
            self.assertEqual(kwargs['max_tokens'], ai_chat.MAX_TOKENS + extra)
            self.assertEqual(kwargs['timeout'], ai_chat.STREAM_TIMEOUT_LONG)
            self.assertIn('Power is set to', self._reminder(kwargs))

    def test_low_keeps_replies_short_but_never_cuts_a_file(self):
        kwargs = _sent_kwargs('low')
        self.assertFalse(self._thinking(kwargs))
        self.assertEqual(kwargs['max_tokens'], ai_chat.LOW_POWER_MAX_TOKENS)
        self.assertIn('briefly', self._reminder(kwargs))
        self.assertEqual(_sent_kwargs('low', max_tokens=6000)['max_tokens'], 6000)

    def test_an_unknown_power_falls_back_to_medium(self):
        kwargs = _sent_kwargs('turbo')
        self.assertFalse(self._thinking(kwargs))
        self.assertEqual(kwargs['max_tokens'], ai_chat.MAX_TOKENS)

    def test_thinking_text_written_inline_never_reaches_the_user(self):
        def fake_stream(client, kwargs):
            yield '<think>let me wor'
            yield 'k it out</think>The answer is 4.'

        with patch.object(ai_chat, '_stream_content', side_effect=fake_stream), \
                patch.object(ai_chat, '_client_for_cfg', return_value=object()):
            reply = ''.join(ai_chat._stream_chat_impl(
                [{'role': 'user', 'content': 'What is 2 + 2?'}], 'quick', power='high'))
        self.assertEqual(reply, 'The answer is 4.')


class PowerChatPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='power@example.com', email='power@example.com',
                                             password='test-password-123', is_staff=True)
        self.client.force_login(self.user)

    def _send(self, **payload):
        payload.setdefault('message', 'Explain photosynthesis.')
        payload.setdefault('model', 'quick')
        with patch('myapp.views.ai_chat.stream_chat', return_value=iter(['ok'])) as stream_chat:
            response = self.client.post('/AI/api/send/', data=json.dumps(payload), content_type='application/json')
            b''.join(response.streaming_content)
        return stream_chat.call_args.kwargs['power']

    def test_the_chosen_power_is_passed_to_the_model(self):
        self.assertEqual(self._send(power='xhigh'), 'xhigh')
        self.assertEqual(self._send(power='low'), 'low')

    def test_a_missing_or_bad_power_means_medium(self):
        self.assertEqual(self._send(), 'medium')
        self.assertEqual(self._send(power='<script>'), 'medium')

    def test_the_picker_shows_the_power_control_on_chat_models_only(self):
        body = self.client.get('/').content.decode()
        self.assertIn('data-power-for="quick"', body)
        self.assertIn('data-power="xhigh"', body)
        self.assertNotIn('data-power-for="flux-klein-4b"', body)
