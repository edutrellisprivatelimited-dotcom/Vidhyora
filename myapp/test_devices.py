"""Connected devices: how many devices an account may use, the device list,
logging devices out, and one-time device login links."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.test import TestCase
from django.utils import timezone

from myapp import devices
from myapp.device_links import describe
from myapp.models import ActiveUserSession, DeviceLoginLink, StoreProfile

PASSWORD = 'test-password-123'
PHONE_UA = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36'


class DeviceTestCase(TestCase):
    def make_user(self, email, **extra):
        user = User.objects.create_user(username=email, email=email, password=PASSWORD, **extra)
        StoreProfile.objects.create(user=user)
        return user

    def login(self, user, **headers):
        client = self.client_class(**headers)
        response = client.post('/AI/api/login/', data=json.dumps({'identifier': user.email, 'password': PASSWORD}),
                               content_type='application/json')
        self.assertEqual(response.status_code, 200)
        return client

    def signed_in(self, client):
        return client.get('/AI/api/account/').status_code == 200

    def post(self, client, url, body=None):
        return client.post(url, data=json.dumps(body or {}), content_type='application/json')

    def use_link(self, owner_client, **headers):
        url = self.post(owner_client, '/AI/api/devices/link/').json()['url']
        device = self.client_class(**headers)
        path = url.split('testserver', 1)[1]
        self.assertEqual(device.get(path).status_code, 200)
        device.post(path)
        return device, path


class SuperuserDeviceTests(DeviceTestCase):
    def test_a_superuser_stays_logged_in_on_ten_devices_and_the_eleventh_logs_out_the_oldest(self):
        owner = self.make_user('owner@example.com', is_staff=True, is_superuser=True)
        clients = [self.login(owner) for _ in range(devices.SUPERUSER_DEVICE_LIMIT)]
        self.assertTrue(all(self.signed_in(c) for c in clients))
        self.assertEqual(ActiveUserSession.objects.filter(user=owner).count(), 10)

        eleventh = self.login(owner)
        self.assertTrue(self.signed_in(eleventh))
        self.assertFalse(self.signed_in(clients[0]))   # used longest ago
        self.assertTrue(all(self.signed_in(c) for c in clients[1:]))
        self.assertEqual(ActiveUserSession.objects.filter(user=owner).count(), 10)


class NormalUserDeviceTests(DeviceTestCase):
    def setUp(self):
        self.user = self.make_user('asha@example.com')

    def test_a_link_adds_a_second_device_and_a_password_login_still_replaces_the_main_one(self):
        main = self.login(self.user)
        second, _ = self.use_link(main, HTTP_USER_AGENT=PHONE_UA)
        self.assertTrue(self.signed_in(main))
        self.assertTrue(self.signed_in(second))
        linked = ActiveUserSession.objects.get(user=self.user, added_by_link=True)
        self.assertEqual(linked.session_key, second.session.session_key)

        third = self.login(self.user)
        self.assertTrue(self.signed_in(third))
        self.assertFalse(self.signed_in(main))
        self.assertTrue(self.signed_in(second))
        self.assertEqual(ActiveUserSession.objects.filter(user=self.user).count(), devices.DEVICE_LIMIT)

    def test_no_link_is_made_once_the_account_is_on_two_devices(self):
        main = self.login(self.user)
        self.use_link(main)
        response = self.post(main, '/AI/api/devices/link/')
        self.assertEqual(response.status_code, 400)
        self.assertIn('2 devices', response.json()['detail'])
        listing = main.get('/AI/api/devices/').json()
        self.assertFalse(listing['can_add'])
        self.assertEqual(listing['limit'], 2)

    def test_a_link_works_once_only_and_opening_it_does_not_use_it(self):
        main = self.login(self.user)
        url = self.post(main, '/AI/api/devices/link/').json()['url']
        path = url.split('testserver', 1)[1]
        visitor = self.client_class()
        page = visitor.get(path)  # e.g. a chat app's link preview
        self.assertContains(page, 'Log in on this device?')
        self.assertContains(page, 'asha@example.com')
        self.assertFalse(self.signed_in(visitor))
        visitor.post(path)
        self.assertTrue(self.signed_in(visitor))
        late = self.client_class()
        self.assertContains(late.post(path), 'already used', status_code=410)
        self.assertFalse(self.signed_in(late))

    def test_an_expired_or_unknown_link_cannot_log_in(self):
        main = self.login(self.user)
        url = self.post(main, '/AI/api/devices/link/').json()['url']
        DeviceLoginLink.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        visitor = self.client_class()
        self.assertContains(visitor.post(url.split('testserver', 1)[1]), 'expired', status_code=410)
        self.assertContains(visitor.get('/device/not-a-real-token/'), 'not valid', status_code=410)
        self.assertFalse(self.signed_in(visitor))

    def test_a_new_link_replaces_the_unused_one(self):
        main = self.login(self.user)
        first = self.post(main, '/AI/api/devices/link/').json()['url']
        self.post(main, '/AI/api/devices/link/')
        self.assertContains(self.client_class().get(first.split('testserver', 1)[1]), 'not valid', status_code=410)

    def test_the_list_shows_each_device_and_another_one_can_be_logged_out(self):
        main = self.login(self.user)
        second, _ = self.use_link(main, HTTP_USER_AGENT=PHONE_UA)
        listing = main.get('/AI/api/devices/').json()
        self.assertEqual([d['current'] for d in listing['devices']], [True, False])
        phone = listing['devices'][1]
        self.assertEqual((phone['name'], phone['kind'], phone['by_link']), ('Chrome on Android', 'phone', True))

        self.assertEqual(self.post(main, f"/AI/api/devices/{listing['devices'][0]['id']}/remove/").status_code, 400)
        self.assertEqual(self.post(main, f"/AI/api/devices/{phone['id']}/remove/").status_code, 200)
        self.assertFalse(self.signed_in(second))
        self.assertTrue(self.signed_in(main))
        self.assertTrue(main.get('/AI/api/devices/').json()['can_add'])

    def test_someone_elses_device_cannot_be_logged_out(self):
        other = self.make_user('other@example.com')
        other_client = self.login(other)
        other_device = ActiveUserSession.objects.get(user=other)
        main = self.login(self.user)
        self.assertEqual(self.post(main, f'/AI/api/devices/{other_device.pk}/remove/').status_code, 404)
        self.assertTrue(self.signed_in(other_client))

    def test_a_device_whose_session_ended_is_not_listed_or_counted(self):
        main = self.login(self.user)
        second, _ = self.use_link(main)
        Session.objects.filter(session_key=second.session.session_key).delete()
        listing = main.get('/AI/api/devices/').json()
        self.assertEqual(len(listing['devices']), 1)
        self.assertTrue(listing['can_add'])

    def test_changing_the_password_logs_the_other_devices_out(self):
        main = self.login(self.user)
        second, _ = self.use_link(main)
        response = self.post(main, '/AI/api/profile/password/', {'current_password': PASSWORD, 'new_password': 'brand-new-pass'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.signed_in(main))
        self.assertFalse(self.signed_in(second))
        self.assertEqual(list(ActiveUserSession.objects.filter(user=self.user).values_list('session_key', flat=True)),
                         [main.session.session_key])

    def test_signed_out_visitors_get_nothing(self):
        self.assertEqual(self.client.get('/AI/api/devices/').status_code, 401)
        self.assertEqual(self.post(self.client, '/AI/api/devices/link/').status_code, 401)


class DescribeTests(TestCase):
    def test_common_browsers(self):
        cases = {
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36 Edg/129.0':
                ('Edge on Windows', 'desktop'),
            PHONE_UA: ('Chrome on Android', 'phone'),
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1':
                ('Safari on iPhone', 'phone'),
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5; rv:128.0) Gecko/20100101 Firefox/128.0': ('Firefox on Mac', 'desktop'),
            '': ('Browser', 'desktop'),
        }
        for agent, expected in cases.items():
            self.assertEqual(describe(agent), expected, agent)


class SupportChatRemovedTests(TestCase):
    def test_the_chat_pages_are_gone(self):
        staff = User.objects.create_user('staff@example.com', password=PASSWORD, is_staff=True)
        self.client.force_login(staff)
        for path in ('/chats/', '/chats/api/messages/', '/store/dashboard/chats/', '/store/dashboard/chats/signups/'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        sidebar = self.client.get('/store/dashboard/')
        for gone in ('/store/dashboard/chats/', 'Total signups', 'User status', 'IP addresses'):
            self.assertNotContains(sidebar, gone)
