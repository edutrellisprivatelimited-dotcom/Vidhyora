import json
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from myapp.models import ChatMessage, ChatUser

SIGNUP = {
    'name': 'Asha Rao', 'email': 'asha@example.com', 'phone': '+91 98765 43210',
    'state': 'Uttar Pradesh', 'city': 'Lucknow', 'password': 'secret-pass-1',
}


class ChatsTestCase(TestCase):
    def setUp(self):
        cache.clear()

    def post(self, url, data=None):
        return self.client.post(url, data=json.dumps(data or {}), content_type='application/json')

    def signup(self, **change):
        return self.post('/chats/api/signup/', {**SIGNUP, **change})


class ChatAccountTests(ChatsTestCase):
    def test_a_new_visitor_starts_a_guest_chat_with_login_and_signup_on_top(self):
        first = self.client.get('/chats/').content.decode()
        self.assertIn('Starting chat', first)
        self.assertNotIn('id="loginForm"', first)
        self.assertFalse(ChatUser.objects.exists())        # nothing saved until the page's script asks
        self.assertTrue(self.post('/chats/api/guest/').json()['ok'])
        body = self.client.get('/chats/').content.decode()
        for part in ('id="composer"', 'id="openLogin"', 'id="openSignup"', 'id="loginForm"', 'id="signupForm"',
                     'id="forgotCard"', 'may need verification', 'Uttar Pradesh'):
            self.assertIn(part, body)
        self.assertNotIn('id="openProfile"', body)
        self.assertTrue(ChatUser.objects.get().is_guest)

    def test_guest_messages_carry_over_on_signup_and_login(self):
        self.post('/chats/api/guest/')
        self.post('/chats/api/messages/', {'body': 'hello as guest'})
        self.signup()
        user = ChatUser.objects.get()
        self.assertFalse(user.is_guest)
        self.assertEqual(user.messages.get().body, 'hello as guest')
        self.post('/chats/api/logout/')
        self.post('/chats/api/guest/')
        self.post('/chats/api/messages/', {'body': 'second visit'})
        self.post('/chats/api/login/', {'email': 'asha@example.com', 'password': SIGNUP['password']})
        self.assertEqual(ChatUser.objects.count(), 1)
        self.assertEqual(list(user.messages.values_list('body', flat=True).order_by('pk')), ['hello as guest', 'second visit'])

    def test_signup_saves_every_field_and_logs_in(self):
        self.assertEqual(self.signup().json()['ok'], True)
        user = ChatUser.objects.get()
        self.assertEqual((user.name, user.email, user.phone, user.state, user.city),
                         ('Asha Rao', 'asha@example.com', '9876543210', 'Uttar Pradesh', 'Lucknow'))
        self.assertFalse(user.is_verified)
        self.assertNotEqual(user.password, SIGNUP['password'])
        self.assertIn('id="composer"', self.client.get('/chats/').content.decode())

    def test_every_signup_field_is_required_and_checked(self):
        for change, field in (({'name': ''}, 'name'), ({'email': 'nope'}, 'email'),
                              ({'phone': '12345'}, 'phone'), ({'state': 'Atlantis'}, 'state'),
                              ({'city': 'Mumbai'}, 'city'), ({'password': 'short'}, 'password')):
            response = self.signup(**change)
            self.assertEqual(response.status_code, 400, field)
            self.assertEqual(response.json()['field'], field)
        self.assertFalse(ChatUser.objects.exists())

    def test_the_same_email_cannot_sign_up_twice(self):
        self.signup()
        self.post('/chats/api/logout/')
        self.assertEqual(self.signup(email='ASHA@example.com').json()['field'], 'email')

    def test_login_and_wrong_password(self):
        self.signup()
        self.post('/chats/api/logout/')
        self.assertFalse(self.post('/chats/api/login/', {'email': 'asha@example.com', 'password': 'wrong-one'}).json()['ok'])
        self.assertTrue(self.post('/chats/api/login/', {'email': 'Asha@example.com', 'password': SIGNUP['password']}).json()['ok'])

    def test_it_is_separate_from_the_main_login(self):
        User.objects.create_user(username='main@example.com', email='main@example.com', password='main-pass-123')
        self.assertFalse(self.post('/chats/api/login/', {'email': 'main@example.com', 'password': 'main-pass-123'}).json()['ok'])
        self.signup()
        self.assertFalse(self.client.session.get('_auth_user_id'))
        self.client.login(username='main@example.com', password='main-pass-123')
        self.post('/chats/api/logout/')
        self.assertIn('Starting chat', self.client.get('/chats/').content.decode())

    def test_forgot_password_shows_the_support_email_and_sends_nothing(self):
        from myapp.models import SupportProfile
        self.post('/chats/api/guest/')
        page = self.client.get('/chats/').content.decode()
        self.assertIn('mailto:shifn32@gmail.com', page)
        self.assertNotIn('Send reset link', page)
        with patch('myapp.emailing.send_store_email') as send:
            res = self.post('/chats/api/forgot/', {'email': 'asha@example.com'})
        self.assertEqual(res.status_code, 410)
        send.assert_not_called()
        SupportProfile.objects.update(support_email='help@example.com')
        self.assertIn('help@example.com', self.client.get('/chats/').content.decode())

class ChatMessagingTests(ChatsTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(username='staff@example.com', email='staff@example.com',
                                              password='staff-pass-123', is_staff=True)

    def test_user_sends_admin_sees_badge_replies_and_user_gets_it(self):
        self.signup()
        self.assertTrue(self.post('/chats/api/messages/', {'body': 'Hello, I need help'}).json()['ok'])
        self.post('/chats/api/messages/', {'body': 'Are you there?'})
        user = ChatUser.objects.get()

        staff = self.client_class()
        staff.force_login(self.staff)
        page = staff.get('/store/dashboard/chats/').content.decode()
        self.assertIn('Asha Rao', page)
        self.assertRegex(page, r'class="nav-badge"[^>]*>1<')
        staff.post(f'/store/dashboard/chats/{user.pk}/', {'body': 'Hi Asha, how can we help?'})
        page = staff.get('/store/dashboard/chats/').content.decode()
        self.assertNotIn('class="nav-badge"', page)

        messages = self.client.get('/chats/api/messages/').json()['messages']
        self.assertEqual([(m['from'], m['body']) for m in messages], [
            ('user', 'Hello, I need help'), ('user', 'Are you there?'), ('admin', 'Hi Asha, how can we help?'),
        ])
        newer = self.client.get(f'/chats/api/messages/?after={messages[1]["id"]}').json()['messages']
        self.assertEqual(len(newer), 1)

    def test_badge_counts_each_waiting_chat(self):
        for n in range(3):
            self.signup(email=f'user{n}@example.com')
            self.post('/chats/api/messages/', {'body': f'help {n}'})
            self.post('/chats/api/logout/')
        self.client.force_login(self.staff)
        self.assertRegex(self.client.get('/store/dashboard/backup/').content.decode(), r'class="nav-badge"[^>]*>3<')

    def test_logged_out_people_cannot_read_or_send(self):
        self.assertEqual(self.client.get('/chats/api/messages/').status_code, 401)
        self.assertEqual(self.post('/chats/api/messages/', {'body': 'hi'}).status_code, 401)
        self.assertFalse(ChatMessage.objects.exists())

    def test_non_staff_cannot_open_the_admin_chats(self):
        self.signup()
        self.post('/chats/api/messages/', {'body': 'private'})
        self.assertNotIn(b'private', self.client.get('/store/dashboard/chats/').content)


class ChatTicksAndPresenceTests(ChatsTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(username='staff2@example.com', email='staff2@example.com',
                                              password='staff-pass-123', is_staff=True)

    def test_blue_ticks_and_online_status_on_both_sides(self):
        self.signup()
        sent = self.post('/chats/api/messages/', {'body': 'Hello'}).json()
        self.assertEqual(sent['read_upto'], 0)                 # delivered, not seen
        self.assertFalse(sent['support']['online'])
        user = ChatUser.objects.get()

        staff = self.client_class()
        staff.force_login(self.staff)
        staff.get(f'/store/dashboard/chats/{user.pk}/')        # admin opens the chat
        poll = self.client.get('/chats/api/messages/').json()
        self.assertEqual(poll['read_upto'], sent['message']['id'])   # blue ticks
        self.assertTrue(poll['support']['online'])

        reply = staff.post(f'/store/dashboard/chats/{user.pk}/', {'body': 'Hi!'},
                           HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertTrue(reply['ok'])
        admin_poll = staff.get(f'/store/dashboard/chats/{user.pk}/poll/').json()
        self.assertEqual(admin_poll['seen_upto'], 0)
        self.assertTrue(admin_poll['user']['online'])
        self.client.get('/chats/api/messages/')                # user's open chat loads the reply
        admin_poll = staff.get(f'/store/dashboard/chats/{user.pk}/poll/').json()
        self.assertEqual(admin_poll['seen_upto'], reply['message']['id'])

    def test_last_seen_after_going_quiet(self):
        from datetime import timedelta
        from django.utils import timezone
        self.signup()
        user = ChatUser.objects.get()
        ChatUser.objects.filter(pk=user.pk).update(last_seen_at=timezone.now() - timedelta(minutes=5))
        staff = self.client_class()
        staff.force_login(self.staff)
        status = staff.get(f'/store/dashboard/chats/{user.pk}/poll/').json()['user']
        self.assertFalse(status['online'])
        self.assertTrue(status['last_seen'])


class ResendTests(TestCase):
    def test_a_retried_message_is_saved_once(self):
        import json as _json
        from myapp.models import ChatMessage
        self.client.post('/chats/api/signup/', _json.dumps({'name': 'Asha', 'email': 'a@example.com', 'phone': '9876543210',
                         'state': 'Uttar Pradesh', 'city': 'Lucknow', 'password': 'secret-pass-1'}), content_type='application/json')
        send = lambda: self.client.post('/chats/api/messages/', _json.dumps({'body': 'hey', 'cid': 'abc123'}),
                                        content_type='application/json').json()
        first, again = send(), send()
        self.assertEqual(first['message']['id'], again['message']['id'])
        self.assertEqual(ChatMessage.objects.count(), 1)


class GuestReturnAndIpTests(ChatsTestCase):
    def test_a_guest_who_comes_back_continues_the_same_chat(self):
        self.post('/chats/api/guest/')
        self.post('/chats/api/messages/', {'body': 'left halfway'})
        self.client.session.flush()                 # e.g. logged out of the main site
        self.client.cookies.pop('sessionid', None)
        msgs = self.client.get('/chats/api/messages/').json()['messages']
        self.assertEqual([m['body'] for m in msgs], ['left halfway'])
        self.assertEqual(ChatUser.objects.count(), 1)

    def test_ip_is_saved_and_shared_ips_are_grouped(self):
        for _ in range(2):
            client = self.client_class(REMOTE_ADDR='203.0.113.7')
            client.post('/chats/api/guest/', content_type='application/json')
            client.post('/chats/api/messages/', json.dumps({'body': 'hi'}), content_type='application/json')
        self.assertEqual(set(ChatUser.objects.values_list('ip', flat=True)), {'203.0.113.7'})
        staff = self.client_class()
        staff.force_login(User.objects.create_user(username='boss', password='x', is_staff=True))
        page = staff.get('/store/dashboard/chats/').content.decode()
        self.assertIn('2 chats from this IP', page)
        self.assertIn('IP addresses <span class="nav-count">1</span>', page)
        ips = staff.get('/store/dashboard/chats/ips/').content.decode()
        self.assertIn('203.0.113.7', ips)
        self.assertIn('2 chats', ips)
        only = staff.get('/store/dashboard/chats/?ip=203.0.113.7').content.decode()
        self.assertIn('IP 203.0.113.7 <i class="fas fa-xmark">', only)


class ConversationFilterTests(ChatsTestCase):
    def setUp(self):
        super().setUp()
        from myapp.models import ChatMessage
        self.signup()
        self.account = ChatUser.objects.get()
        ChatMessage.objects.create(user=self.account, sender='user', body='need a refund', kind='text', read_by_admin=True)
        ChatMessage.objects.create(user=self.account, sender='admin', body='sure')
        self.post('/chats/api/logout/')
        self.post('/chats/api/guest/')
        self.post('/chats/api/messages/', {'body': 'just looking'})
        self.staff = self.client_class()
        self.staff.force_login(User.objects.create_user(username='boss', password='x', is_staff=True))

    def names(self, query=''):
        page = self.staff.get('/store/dashboard/chats/' + query).context
        return sorted(u.name for u in page['chat_users'])

    def test_filters(self):
        guest = ChatUser.objects.get(is_guest=True).name
        self.assertEqual(self.names(), sorted(['Asha Rao', guest]))
        self.assertEqual(self.names('?tab=guests'), [guest])
        self.assertEqual(self.names('?tab=accounts'), ['Asha Rao'])
        self.assertEqual(self.names('?tab=waiting'), [guest])
        self.assertEqual(self.names('?reply=replied'), ['Asha Rao'])
        self.assertEqual(self.names('?reply=never'), [guest])
        self.assertEqual(self.names('?q=refund'), ['Asha Rao'])
        self.assertEqual(self.names('?state=Uttar+Pradesh'), ['Asha Rao'])
        self.assertEqual(self.names('?has=image'), [])
        self.assertEqual(self.names('?when=today'), sorted(['Asha Rao', guest]))
        self.assertEqual(self.names('?when=custom&from=2000-01-01&to=2000-01-02'), [])
        self.assertEqual(self.names('?sort=name&who=accounts&account=unverified'), ['Asha Rao'])
        page = self.staff.get('/store/dashboard/chats/?reply=replied').content.decode()
        self.assertIn('You replied last', page)
        self.assertIn('Clear all', page)
