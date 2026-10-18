import io
import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from django.contrib.auth.hashers import check_password

from myapp.models import SupportProfile

URL = '/store/dashboard/chats/profile/'


def png():
    buf = io.BytesIO()
    Image.new('RGB', (40, 40), (10, 120, 200)).save(buf, 'PNG')
    return SimpleUploadedFile('me.png', buf.getvalue())


class SupportProfileTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.staff = User.objects.create_user(username='boss', email='boss@example.com',
                                              password='old-pass-123!', is_staff=True)
        self.client.force_login(self.staff)

    def test_sidebar_link_and_page(self):
        page = self.client.get(URL).content.decode()
        self.assertIn('Update profile', page)
        self.assertIn('Chat profile', page)

    def test_chat_profile_with_photo_shows_on_chats_page(self):
        self.client.post(URL, {'section': 'chat', 'display_name': 'Vidhyora Help', 'about': 'Here 9 to 6',
                               'welcome': 'Ask us anything!', 'photo': png()})
        profile = SupportProfile.load()
        self.assertEqual(profile.display_name, 'Vidhyora Help')
        self.assertTrue(profile.photo)
        shown = self.client_class().get('/chats/support-photo/')
        self.assertEqual(shown.status_code, 200)
        shown.close()
        self.client.post('/chats/api/signup/', {'name': 'Asha', 'email': 'a@example.com', 'phone': '9876543210',
                                                'state': 'Uttar Pradesh', 'city': 'Lucknow', 'password': 'secret-pass-1'},
                         content_type='application/json')
        body = self.client.get('/chats/').content.decode()
        for part in ('Vidhyora Help', 'Here 9 to 6', 'Ask us anything!', '/chats/support-photo/'):
            self.assertIn(part, body)
        self.client.post(URL, {'section': 'chat', 'display_name': 'Vidhyora Help', 'remove_photo': '1'})
        self.assertEqual(self.client.get('/chats/support-photo/').status_code, 404)

    def test_bad_photo_is_refused(self):
        self.client.post(URL, {'section': 'chat', 'display_name': 'X Team',
                               'photo': SimpleUploadedFile('a.png', b'not an image')})
        self.assertEqual(SupportProfile.load().display_name, 'Support')

    def test_details_and_password(self):
        self.client.post(URL, {'section': 'account', 'first_name': 'Rudra', 'last_name': 'T', 'email': 'new@example.com'})
        self.staff.refresh_from_db()
        self.assertEqual((self.staff.first_name, self.staff.email), ('Rudra', 'new@example.com'))
        self.client.post(URL, {'section': 'password', 'current': 'wrong', 'new': 'Brand-new-pass-9', 'confirm': 'Brand-new-pass-9'})
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password('old-pass-123!'))
        self.client.post(URL, {'section': 'password', 'current': 'old-pass-123!', 'new': 'Brand-new-pass-9', 'confirm': 'Brand-new-pass-9'})
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password('Brand-new-pass-9'))
        self.assertEqual(self.client.get(URL).status_code, 200)  # still logged in

    def test_non_staff_cannot_open(self):
        self.assertNotEqual(self.client_class().get(URL).status_code, 200)


class ChatUserProfileTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.client.post('/chats/api/signup/', {'name': 'Asha', 'email': 'a@example.com', 'phone': '9876543210',
                                                'state': 'Uttar Pradesh', 'city': 'Lucknow', 'password': 'secret-pass-1'},
                         content_type='application/json')

    def test_menu_and_profile_update(self):
        from myapp.models import ChatUser
        body = self.client.get('/chats/').content.decode()
        self.assertIn('id="openProfile"', body)
        self.assertIn('id="logoutBtn"', body)
        base = {'name': 'Asha Rao', 'phone': '9123456780', 'state': 'Maharashtra', 'city': 'Pune'}
        res = self.client.post('/chats/api/profile/', {**base, 'photo': png()}).json()
        self.assertTrue(res['ok'])
        user = ChatUser.objects.get()
        self.assertEqual((user.name, user.phone, user.city), ('Asha Rao', '9123456780', 'Pune'))
        shown = self.client.get(f'/chats/user-photo/{user.pk}/')
        self.assertEqual(shown.status_code, 200)
        shown.close()
        self.assertEqual(self.client_class().get(f'/chats/user-photo/{user.pk}/').status_code, 404)
        bad = self.client.post('/chats/api/profile/', {**base, 'current_password': 'nope', 'new_password': 'another-pass'}).json()
        self.assertEqual(bad['field'], 'current_password')
        self.assertTrue(self.client.post('/chats/api/profile/', {**base, 'current_password': 'secret-pass-1',
                                                                 'new_password': 'another-pass'}).json()['ok'])
        user.refresh_from_db()
        self.assertTrue(check_password('another-pass', user.password))
        self.assertIn('id="composer"', self.client.get('/chats/').content.decode())  # still logged in
        self.assertEqual(self.client.post('/chats/api/profile/', {**base, 'city': 'Lucknow'}).json()['field'], 'city')


class ChatUserManagementTests(TestCase):
    def setUp(self):
        self.client.post('/chats/api/signup/', {'name': 'Asha', 'email': 'a@example.com', 'phone': '9876543210',
                                                'state': 'Uttar Pradesh', 'city': 'Lucknow', 'password': 'secret-pass-1'},
                         content_type='application/json')
        from myapp.models import ChatUser
        self.user = ChatUser.objects.get()
        self.staff = self.client_class()
        self.staff.force_login(User.objects.create_user(username='boss', password='x', is_staff=True))
        self.url = f'/store/dashboard/chats/users/{self.user.pk}/action/'

    def test_status_page_and_sidebar_count(self):
        self.client.get('/chats/api/messages/')  # marks the user online
        page = self.staff.get('/store/dashboard/chats/status/').content.decode()
        self.assertIn('User status', page)
        self.assertIn('id="navOnline"><i></i>1</span>', page)
        data = self.staff.get('/store/dashboard/chats/status/?format=json').json()
        self.assertEqual((data['online'], data['users'][0]['name']), (1, 'Asha'))

    def test_deactivate_logs_out_and_blocks_login(self):
        self.staff.post(self.url, {'action': 'toggle_active'})
        self.assertEqual(self.client.get('/chats/api/messages/').status_code, 401)
        res = self.client.post('/chats/api/login/', {'email': 'a@example.com', 'password': 'secret-pass-1'},
                               content_type='application/json')
        self.assertEqual(res.status_code, 403)
        self.staff.post(self.url, {'action': 'toggle_active'})
        res = self.client.post('/chats/api/login/', {'email': 'a@example.com', 'password': 'secret-pass-1'},
                               content_type='application/json')
        self.assertTrue(res.json()['ok'])

    def test_edit_verify_clear_and_delete(self):
        from myapp.models import ChatMessage, ChatUser
        self.staff.post(self.url, {'action': 'edit', 'name': 'Asha R', 'email': 'asha@example.com', 'phone': '9123456780',
                                   'state': 'Maharashtra', 'city': 'Pune', 'new_password': 'brand-new-1'})
        self.user.refresh_from_db()
        self.assertEqual((self.user.name, self.user.email, self.user.city), ('Asha R', 'asha@example.com', 'Pune'))
        self.assertTrue(check_password('brand-new-1', self.user.password))
        self.staff.post(self.url, {'action': 'toggle_verified'})
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)
        self.assertIn('Verified', self.staff.get('/store/dashboard/chats/signups/').content.decode())
        ChatMessage.objects.create(user=self.user, sender='user', body='hi')
        self.staff.post(self.url, {'action': 'clear_chat'})
        self.assertFalse(ChatMessage.objects.exists())
        self.staff.post(self.url, {'action': 'delete'})
        self.assertFalse(ChatUser.objects.exists())

    def test_only_staff_can_manage(self):
        self.client_class().post(self.url, {'action': 'delete'})
        from myapp.models import ChatUser
        self.assertTrue(ChatUser.objects.exists())
