import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from myapp import context_compress
from myapp.models import AIConversation, AIMessage


def _long_text(question='What is the main reason the empire declined?'):
    opening = 'Please read my notes on the Roman Empire carefully. ' * 15
    middle = 'In 285 AD Diocletian split the empire, and taxes rose sharply over many years. ' * 120
    return f'{opening}\n{middle}\n{question}'


@override_settings(AI_CONTEXT_COMPRESSION_ENABLED=True)
class LongMessageTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_middle_is_shortened_and_the_question_stays_word_for_word(self):
        text = _long_text()
        with patch.object(context_compress, '_complete', return_value='Diocletian split the empire in 285 AD; taxes rose.') as call:
            condensed = context_compress.condense_long_message(text)
        self.assertIn('What is the main reason the empire declined?', condensed)
        self.assertTrue(condensed.startswith('Please read my notes on the Roman Empire carefully.'))
        self.assertIn('Diocletian split the empire in 285 AD; taxes rose.', condensed)
        self.assertLess(len(condensed), len(text) / 3)
        self.assertNotIn('What is the main reason', call.call_args.args[1])

    def test_the_condensed_copy_is_reused_for_later_turns(self):
        text = _long_text()
        with patch.object(context_compress, '_complete', return_value='Short version.') as call:
            first = context_compress.condense_long_message(text)
            second = context_compress.condense_long_message(text)
        self.assertEqual(first, second)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(context_compress.cached_condensed(text), first)

    def test_messages_that_need_the_exact_words_are_left_alone(self):
        with patch.object(context_compress, '_complete') as call:
            self.assertIsNone(context_compress.condense_long_message('short question'))
            self.assertIsNone(context_compress.condense_long_message(_long_text('Please translate this into Hindi.')))
            self.assertIsNone(context_compress.condense_long_message(_long_text('Fix this:\n```python\nprint(1)\n```')))
        call.assert_not_called()

    def test_a_failure_or_a_poor_summary_sends_the_original(self):
        text = _long_text()
        with patch.object(context_compress, '_complete', side_effect=RuntimeError('busy')):
            self.assertIsNone(context_compress.condense_long_message(text))
        with patch.object(context_compress, '_complete', return_value='x' * len(text)):
            self.assertIsNone(context_compress.condense_long_message(text))

    @override_settings(AI_CONTEXT_COMPRESSION_ENABLED=False)
    def test_switched_off_does_nothing(self):
        with patch.object(context_compress, '_complete') as call:
            self.assertIsNone(context_compress.condense_long_message(_long_text()))
        call.assert_not_called()


@override_settings(AI_CONTEXT_COMPRESSION_ENABLED=True)
class RunningSummaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.conversation = AIConversation.objects.create(title='Long chat')

    def _chat(self, turns):
        rows = []
        for number in range(turns):
            rows.append(AIMessage.objects.create(conversation=self.conversation, role=AIMessage.ROLE_USER, content=f'question {number}'))
            rows.append(AIMessage.objects.create(conversation=self.conversation, role=AIMessage.ROLE_ASSISTANT, content=f'answer {number}'))
        return rows

    def test_a_short_chat_is_not_summarised(self):
        self._chat(4)
        with patch.object(context_compress, '_complete') as call:
            self.assertFalse(context_compress.fold_conversation(self.conversation.pk))
        call.assert_not_called()

    def test_older_messages_are_folded_and_the_newest_kept(self):
        rows = self._chat(7)
        with patch.object(context_compress, '_complete', return_value='- The user asked questions 0-3.') as call:
            self.assertTrue(context_compress.fold_conversation(self.conversation.pk))
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.context_summary, '- The user asked questions 0-3.')
        # 14 messages, the last 6 kept word-for-word: 8 folded, ending on an answer.
        self.assertEqual(self.conversation.context_summary_until, rows[7].pk)
        sent = call.call_args.args[1]
        self.assertIn('User: question 0', sent)
        self.assertIn('Assistant: answer 3', sent)
        self.assertNotIn('question 4', sent)

    def test_the_next_update_builds_on_the_previous_summary(self):
        self._chat(7)
        with patch.object(context_compress, '_complete', return_value='first summary'):
            context_compress.fold_conversation(self.conversation.pk)
        self._chat(4)
        with patch.object(context_compress, '_complete', return_value='second summary') as call:
            self.assertTrue(context_compress.fold_conversation(self.conversation.pk))
        self.assertIn('first summary', call.call_args.args[1])
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.context_summary, 'second summary')

    def test_a_failed_update_keeps_the_old_summary(self):
        self._chat(7)
        with patch.object(context_compress, '_complete', side_effect=RuntimeError('busy')):
            self.assertFalse(context_compress.fold_conversation(self.conversation.pk))
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.context_summary_until, 0)

    def test_editing_a_summarised_message_drops_the_summary(self):
        rows = self._chat(7)
        with patch.object(context_compress, '_complete', return_value='summary'):
            context_compress.fold_conversation(self.conversation.pk)
        self.conversation.refresh_from_db()
        context_compress.forget_summary(self.conversation, rows[12].pk)
        self.assertEqual(AIConversation.objects.get(pk=self.conversation.pk).context_summary, 'summary')
        context_compress.forget_summary(self.conversation, rows[2].pk)
        self.assertEqual(AIConversation.objects.get(pk=self.conversation.pk).context_summary_until, 0)


@override_settings(AI_CONTEXT_COMPRESSION_ENABLED=True)
class ChatSendCompressionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='long@example.com', email='long@example.com',
                                             password='test-password-123', is_staff=True)
        self.client.force_login(self.user)

    def _send(self, message, conversation=None):
        payload = {'message': message, 'model': 'quick'}
        if conversation:
            payload['conversation_id'] = conversation.pk
        with patch('myapp.views.ai_chat.stream_chat', return_value=iter(['ok'])) as stream_chat, \
                patch.object(context_compress, 'schedule_fold'):
            response = self.client.post('/AI/api/send/', data=json.dumps(payload), content_type='application/json')
            b''.join(response.streaming_content)
        self.assertEqual(response.status_code, 200)
        return stream_chat.call_args.args[0]

    def test_the_model_gets_the_summary_instead_of_the_folded_messages(self):
        conversation = AIConversation.objects.create(user=self.user, title='Chat')
        rows = []
        for number in range(5):
            rows.append(AIMessage.objects.create(conversation=conversation, role=AIMessage.ROLE_USER, content=f'old question {number}'))
            rows.append(AIMessage.objects.create(conversation=conversation, role=AIMessage.ROLE_ASSISTANT, content=f'old answer {number}'))
        conversation.context_summary = '- The user is preparing for a history exam.'
        conversation.context_summary_until = rows[5].pk
        conversation.save()

        sent = self._send('and what came next?', conversation)

        self.assertEqual(sent[0]['role'], 'system')
        self.assertIn('preparing for a history exam', sent[0]['content'])
        texts = [m['content'] for m in sent[1:]]
        self.assertNotIn('old question 0', texts)
        self.assertNotIn('old answer 2', texts)
        self.assertIn('old question 3', texts)
        self.assertEqual(texts[-1], 'and what came next?')
        saved = AIMessage.objects.filter(conversation=conversation, role=AIMessage.ROLE_USER).order_by('pk')
        self.assertEqual(saved.first().content, 'old question 0')

    def test_a_long_message_is_sent_condensed_but_saved_in_full(self):
        text = _long_text()
        with patch.object(context_compress, '_complete', return_value='Diocletian split the empire; taxes rose.'):
            sent = self._send(text)
        self.assertIn('Diocletian split the empire; taxes rose.', sent[-1]['content'])
        self.assertIn('What is the main reason the empire declined?', sent[-1]['content'])
        self.assertLess(len(sent[-1]['content']), len(text))
        self.assertEqual(AIMessage.objects.get(role=AIMessage.ROLE_USER).content, text)
