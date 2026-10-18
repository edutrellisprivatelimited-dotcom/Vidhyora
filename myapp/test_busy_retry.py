from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from myapp import ai_chat


class Busy(Exception):
    status_code = 503

    def __init__(self):
        super().__init__('ResourceExhausted: Worker local total request limit reached (16/16)')


class BadRequest(Exception):
    status_code = 400


def _answer(text):
    return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])])


def _always_busy(**kwargs):
    raise Busy()


def _ask(create, model_key='terra', backups=None):
    """Run one turn. backups=None leaves the real backup list out (no backups),
    a list of model ids makes those the backups, tried in that order."""
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def fake_backups(cfg, key, kwargs, power):
        return [({'id': model_id, 'reasoning': False, 'vision': False}, 'BACKUP_KEY',
                 dict(kwargs, model=model_id), False) for model_id in backups or ()]

    with patch.object(ai_chat, '_client_for_cfg', return_value=client), \
            patch.object(ai_chat, '_fallback_backends', side_effect=fake_backups), \
            patch('myapp.ai_chat.time.sleep') as sleep:
        try:
            return ''.join(ai_chat._stream_chat_impl(
                [{'role': 'user', 'content': 'Say OK'}], model_key)), sleep
        except Exception as exc:
            return exc, sleep


class BusyRetryTests(SimpleTestCase):
    def test_a_busy_model_is_given_time_to_recover(self):
        create = Mock(side_effect=[Busy(), Busy(), Busy(), _answer('OK')])
        reply, sleep = _ask(create)
        self.assertEqual(reply, 'OK')
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0, 8.0])

    def test_it_gives_up_after_the_last_wait(self):
        create = Mock(side_effect=_always_busy)
        error, sleep = _ask(create)
        self.assertIsInstance(error, Busy)
        self.assertEqual(create.call_count, len(ai_chat.BUSY_RETRY_DELAYS) + 1)
        self.assertEqual(sum(c.args[0] for c in sleep.call_args_list), sum(ai_chat.BUSY_RETRY_DELAYS))

    def test_the_total_wait_is_capped(self):
        create = Mock(side_effect=_always_busy)
        with patch.object(ai_chat, 'BUSY_RETRY_BUDGET_SECONDS', 5.0):
            error, sleep = _ask(create)
        self.assertIsInstance(error, Busy)
        # 2 s and 4 s fit in 5 s from the start; 8 s more would not.
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])

    def test_a_real_error_is_not_retried(self):
        create = Mock(side_effect=BadRequest('invalid request'))
        error, sleep = _ask(create)
        self.assertIsInstance(error, BadRequest)
        self.assertEqual(create.call_count, 1)
        sleep.assert_not_called()

    def test_sol_and_terra_wait_longer_for_the_first_word(self):
        create = Mock(side_effect=lambda **kw: _answer('OK'))
        for key in (ai_chat.SOL_MODEL_KEY, ai_chat.TERRA_MODEL_KEY):
            _ask(create, key)
            self.assertEqual(create.call_args.kwargs['timeout'], 40.0)


class BackupModelTests(SimpleTestCase):
    def _answers_only(self, model_id, text='Answer from the backup.'):
        def create(**kwargs):
            if kwargs['model'] == model_id:
                return _answer(text)
            raise Busy()
        return Mock(side_effect=create)

    def test_a_failing_model_hands_over_to_the_next_one(self):
        create = self._answers_only('backup/one')
        reply, sleep = _ask(create, backups=['backup/one'])
        self.assertEqual(reply, 'Answer from the backup.')
        # With a backup waiting, the selected model gets the short schedule.
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(ai_chat.FALLBACK_FIRST_DELAYS))

    def test_the_third_model_answers_when_two_fail(self):
        create = self._answers_only('backup/two', 'Third time lucky.')
        reply, _ = _ask(create, backups=['backup/one', 'backup/two'])
        self.assertEqual(reply, 'Third time lucky.')
        tried = [c.kwargs['model'] for c in create.call_args_list]
        self.assertEqual(tried[-1], 'backup/two')
        self.assertIn('backup/one', tried)

    def test_the_backup_gets_the_same_prompt_and_identity(self):
        create = self._answers_only('backup/one')
        _ask(create, backups=['backup/one'])
        first, last = create.call_args_list[0].kwargs, create.call_args_list[-1].kwargs
        self.assertNotEqual(first['model'], last['model'])
        self.assertEqual(first['messages'], last['messages'])
        self.assertIn(ai_chat.MODELS['terra']['label'], last['messages'][0]['content'])

    def test_the_last_error_is_reported_when_every_model_fails(self):
        create = Mock(side_effect=_always_busy)
        error, _ = _ask(create, backups=['backup/one', 'backup/two'])
        self.assertIsInstance(error, Busy)
        self.assertIn('backup/two', [c.kwargs['model'] for c in create.call_args_list])

    def test_a_rejected_request_is_not_sent_to_other_models(self):
        create = Mock(side_effect=BadRequest('prompt is too long'))
        error, _ = _ask(create, backups=['backup/one'])
        self.assertIsInstance(error, BadRequest)
        self.assertEqual(create.call_count, 1)


class BackupListTests(SimpleTestCase):
    def _backups(self, model_key, enabled=lambda key: True, keys=True):
        cfg, key = ai_chat._backend_for(model_key)
        with patch('myapp.provider_keys.get_key', return_value='a-key' if keys else ''), \
                patch.object(ai_chat, 'nvidia_key_pool', return_value=['a-key'] if keys else []), \
                patch('myapp.model_controls.is_enabled', side_effect=enabled):
            return cfg, key, ai_chat._fallback_backends(cfg, key, {'model': cfg['id'], 'timeout': 15.0}, 'medium')

    def test_never_the_backend_already_tried_and_never_twice(self):
        cfg, key, backups = self._backups('terra')
        pairs = [(b[0]['id'], b[1] or '') for b in backups]
        self.assertNotIn((cfg['id'], key or ''), pairs)
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertTrue(backups)

    def test_a_switched_off_model_is_not_used(self):
        _, _, backups = self._backups('terra', enabled=lambda key: key != 'sol')
        self.assertNotIn(ai_chat._backend_for('sol')[1], [b[1] for b in backups])

    def test_no_key_means_no_backup(self):
        _, _, backups = self._backups('terra', keys=False)
        self.assertEqual(backups, [])
