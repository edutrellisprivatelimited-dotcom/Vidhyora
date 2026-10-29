"""The security brakes: the real visitor IP, private media, the developer
API's limits and the password-guessing brake on every login."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from myapp import login_guard
from myapp.devices import client_ip
from myapp.models import AIAPIAccess, AIAPIKey, AIBlock, AICodingKey, StoreProfile
from myapp.views import AI_FREE_MESSAGE_LIMIT


class ClientIpTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_a_forwarded_for_header_typed_by_the_visitor_is_ignored(self):
        request = self.factory.get('/', HTTP_X_FORWARDED_FOR='1.2.3.4', REMOTE_ADDR='10.0.0.7')
        self.assertEqual(client_ip(request), '10.0.0.7')

    def test_the_edge_set_real_ip_wins(self):
        request = self.factory.get(
            '/', HTTP_X_REAL_IP='203.0.113.9', HTTP_X_FORWARDED_FOR='1.2.3.4', REMOTE_ADDR='10.0.0.7',
        )
        self.assertEqual(client_ip(request), '203.0.113.9')

    def test_garbage_is_not_an_ip(self):
        request = self.factory.get('/', HTTP_X_REAL_IP='not-an-ip', REMOTE_ADDR='')
        self.assertEqual(client_ip(request), '')

    def test_chat_rate_limit_cannot_be_dodged_with_a_fake_header(self):
        cache.clear()
        self.addCleanup(cache.clear)
        with patch('myapp.views.AI_CHAT_RATE_LIMIT', 1):
            self.client.post('/AI/api/send/', data='{}', content_type='application/json',
                             HTTP_X_FORWARDED_FOR='1.1.1.1')
            second = self.client.post('/AI/api/send/', data='{}', content_type='application/json',
                                      HTTP_X_FORWARDED_FOR='2.2.2.2')
        self.assertEqual(second.status_code, 429)


class PrivateMediaTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        root = Path(self.media.name)
        (root / 'ai_calls' / '1').mkdir(parents=True)
        (root / 'ai_calls' / '1' / '1.webm').write_bytes(b'secret recording')
        (root / 'avatars').mkdir()
        (root / 'avatars' / 'me.png').write_bytes(b'png')
        override = override_settings(MEDIA_ROOT=root)
        override.enable()
        self.addCleanup(override.disable)

    def _get(self, path):
        response = self.client.get(path)
        response.close()  # releases the file so Windows can delete the temp folder
        return response

    def test_call_recordings_are_not_served_from_media(self):
        self.assertEqual(self._get('/media/ai_calls/1/1.webm').status_code, 404)

    def test_public_pictures_are_still_served(self):
        self.assertEqual(self._get('/media/avatars/me.png').status_code, 200)

    def test_no_climbing_out_of_a_public_folder(self):
        self.assertEqual(self._get('/media/avatars/../ai_calls/1/1.webm').status_code, 404)
        self.assertEqual(self._get('/media/avatars/..%5Cai_calls%5C1%5C1.webm').status_code, 404)


@patch('myapp.views.ai_chat.is_model_enabled', return_value=True)
@patch('myapp.views.ai_chat.stream_chat', return_value=iter(['Hello']))
class DeveloperApiLimitTests(TestCase):
    URL = '/api/v1/chat/'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user('dev@example.com', email='dev@example.com', password='pw-123456')
        StoreProfile.objects.create(user=self.user)
        AIAPIAccess.objects.create(user=self.user, model_keys='quick')
        self.raw_key = AIAPIKey.generate_for(self.user)

    def _call(self, **extra):
        return self.client.post(
            self.URL, data=json.dumps({'model': 'quick', 'message': 'hi'}), content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.raw_key}', **extra,
        )

    def test_a_free_account_uses_its_free_messages(self, stream, enabled):
        self.assertEqual(self._call().status_code, 200)
        self.assertEqual(StoreProfile.objects.get(user=self.user).ai_free_messages_used, 1)

    def test_a_free_account_past_its_allowance_is_refused(self, stream, enabled):
        StoreProfile.objects.filter(user=self.user).update(ai_free_messages_used=AI_FREE_MESSAGE_LIMIT)
        response = self._call()
        self.assertEqual(response.status_code, 403)
        stream.assert_not_called()

    def test_a_subscriber_is_not_counted(self, stream, enabled):
        StoreProfile.objects.filter(user=self.user).update(
            ai_free_messages_used=AI_FREE_MESSAGE_LIMIT, ai_subscription_until=timezone.now() + timedelta(days=30),
        )
        self.assertEqual(self._call().status_code, 200)

    def test_a_blocked_account_is_refused(self, stream, enabled):
        AIBlock.objects.create(user=self.user)
        self.assertEqual(self._call().status_code, 403)
        stream.assert_not_called()

    def test_a_blocked_ip_is_refused(self, stream, enabled):
        AIBlock.objects.create(ip_address='198.51.100.4')
        self.assertEqual(self._call(HTTP_X_REAL_IP='198.51.100.4').status_code, 403)

    def test_a_deactivated_account_key_stops_working(self, stream, enabled):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self._call().status_code, 401)

    @override_settings(DEVELOPER_API_RATE_PER_MINUTE=2)
    def test_requests_per_minute_are_limited(self, stream, enabled):
        StoreProfile.objects.filter(user=self.user).update(ai_subscription_until=timezone.now() + timedelta(days=30))
        stream.side_effect = lambda *a, **k: iter(['Hello'])
        self.assertEqual(self._call().status_code, 200)
        self.assertEqual(self._call().status_code, 200)
        limited = self._call()
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited['Retry-After'], '60')


class CodingApiBlockTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user('coder2', password='pw')
        StoreProfile.objects.update_or_create(
            user=self.user, defaults={'ai_subscription_until': timezone.now() + timedelta(days=30)},
        )
        self.raw_key = AICodingKey.generate_for(self.user, label='LAPTOP')

    def test_a_blocked_account_cannot_use_code_x(self):
        AIBlock.objects.create(user=self.user)
        response = self.client.get('/api/v1/code/models', HTTP_AUTHORIZATION=f'Bearer {self.raw_key}')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'account_blocked')


class LoginGuardTests(TestCase):
    URL = '/AI/api/login/'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user('member@example.com', email='member@example.com', password='right-pass-123')
        StoreProfile.objects.create(user=self.user, phone='9000000099')

    def _login(self, identifier, password, ip='203.0.113.1'):
        return self.client.post(
            self.URL, data=json.dumps({'identifier': identifier, 'password': password}),
            content_type='application/json', HTTP_X_REAL_IP=ip,
        )

    def test_unknown_account_and_wrong_password_look_the_same(self):
        unknown = self._login('nobody@example.com', 'whatever')
        wrong = self._login('member@example.com', 'wrong')
        self.assertEqual(unknown.status_code, wrong.status_code)
        self.assertEqual(unknown.json()['detail'], wrong.json()['detail'])

    def test_an_account_locks_after_too_many_wrong_passwords(self):
        for n in range(login_guard.MAX_FAILURES_PER_ACCOUNT):
            self._login('member@example.com', 'wrong', ip=f'203.0.113.{n + 10}')
        # Even the right password, from a fresh IP, waits out the lock.
        locked = self._login('member@example.com', 'right-pass-123', ip='203.0.113.200')
        self.assertEqual(locked.status_code, 429)
        self.assertIn('Too many wrong attempts', locked.json()['detail'])

    def test_one_ip_is_stopped_from_trying_many_accounts(self):
        for n in range(login_guard.MAX_FAILURES_PER_IP):
            self._login(f'guess{n}@example.com', 'wrong')
        self.assertEqual(self._login('member@example.com', 'right-pass-123').status_code, 429)
        # Another visitor is unaffected.
        self.assertEqual(self._login('member@example.com', 'right-pass-123', ip='198.51.100.1').status_code, 200)

    def test_a_good_login_clears_the_account_count(self):
        for _ in range(login_guard.MAX_FAILURES_PER_ACCOUNT - 1):
            self._login('member@example.com', 'wrong')
        self.assertEqual(self._login('member@example.com', 'right-pass-123').status_code, 200)
        self.client.logout()
        self._login('member@example.com', 'wrong', ip='198.51.100.2')
        self.assertEqual(self._login('member@example.com', 'right-pass-123', ip='198.51.100.3').status_code, 200)

    def test_the_admin_login_has_the_same_brake(self):
        staff = User.objects.create_user('boss', password='boss-pass-123', is_staff=True, is_superuser=True)
        for n in range(login_guard.MAX_FAILURES_PER_ACCOUNT):
            self.client.post('/admin/login/', {'username': 'boss', 'password': 'wrong'}, HTTP_X_REAL_IP=f'203.0.113.{n + 50}')
        response = self.client.post(
            '/admin/login/', {'username': 'boss', 'password': 'boss-pass-123'}, HTTP_X_REAL_IP='198.51.100.9',
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Too many wrong passwords')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertTrue(staff.is_active)
