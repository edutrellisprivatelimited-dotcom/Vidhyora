import json
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from myapp.models import ChatMessage, ChatUser

PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
SIGNUP = {
    'name': 'Asha Rao', 'email': 'asha@example.com', 'phone': '9876543210',
    'state': 'Uttar Pradesh', 'city': 'Lucknow', 'password': 'secret-pass-1',
}


class ChatRichMessageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.staff_user = User.objects.create_user(username='staff3@example.com', email='staff3@example.com',
                                                   password='staff-pass-123', is_staff=True)
        self.post('/chats/api/signup/', SIGNUP)
        self.user = ChatUser.objects.get()

    def post(self, url, data=None, client=None):
        return (client or self.client).post(url, data=json.dumps(data or {}), content_type='application/json')

    def staff(self):
        client = self.client_class()
        client.force_login(self.staff_user)
        return client

    def upload(self, name, content=PNG, client=None, url='/chats/api/upload/', **extra):
        return (client or self.client).post(url, {'file': SimpleUploadedFile(name, content), 'body': 'look'}, **extra)

    def test_a_photo_is_sent_and_only_the_owner_and_staff_can_open_it(self):
        message = self.upload('me.png').json()['message']
        self.assertEqual((message['kind'], message['body'], message['name']), ('image', 'look', 'me.png'))
        self.assertEqual(self.client.get(message['url']).status_code, 200)
        self.assertEqual(self.client_class().get(message['url']).status_code, 404)
        other = self.client_class()
        self.post('/chats/api/signup/', {**SIGNUP, 'email': 'other@example.com'}, client=other)
        self.assertEqual(other.get(message['url']).status_code, 404)
        self.assertEqual(self.staff().get(message['url']).status_code, 200)

    def test_unsafe_or_huge_files_are_refused(self):
        self.assertIn('not supported', self.upload('page.html', b'<script>alert(1)</script>').json()['error'])
        self.assertIn('not supported', self.upload('logo.svg', b'<svg/>').json()['error'])
        with patch('myapp.support_chat.MAX_UPLOAD_MB', 0):
            self.assertIn('up to', self.upload('big.png').json()['error'])
        self.assertFalse(ChatMessage.objects.exists())

    def test_a_document_downloads_and_admin_can_send_files(self):
        message = self.upload('notes.pdf', b'%PDF-1.4 test').json()['message']
        self.assertEqual(message['kind'], 'file')
        self.assertIn('attachment', self.client.get(message['url'])['Content-Disposition'])
        reply = self.upload('answer.png', client=self.staff(), url=f'/store/dashboard/chats/{self.user.pk}/',
                            HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertEqual(reply['message']['from'], 'admin')
        self.assertEqual(self.client.get(reply['message']['url']).status_code, 200)

    def test_live_location_moves_and_stops(self):
        sent = self.post('/chats/api/location/', {'lat': 26.85, 'lng': 80.95, 'live_minutes': 15}).json()['message']
        self.assertTrue(sent['live'])
        self.post('/chats/api/location/', {'id': sent['id'], 'lat': 26.86, 'lng': 80.96})
        live = self.staff().get(f'/store/dashboard/chats/{self.user.pk}/poll/').json()['locations']
        self.assertEqual((live[0]['lat'], live[0]['lng']), (26.86, 80.96))
        self.post('/chats/api/location/', {'id': sent['id'], 'stop': True})
        self.assertFalse(self.client.get('/chats/api/messages/').json()['locations'][0]['live'])
        self.assertEqual(self.post('/chats/api/location/', {'lat': 200, 'lng': 0}).status_code, 400)

    def test_a_call_rings_connects_and_is_logged(self):
        offer = {'type': 'offer', 'sdp': 'v=0'}
        call = self.post('/chats/api/call/', {'action': 'start', 'kind': 'video', 'offer': offer}).json()['call']
        self.post('/chats/api/call/', {'action': 'ice', 'id': call['id'], 'candidate': {'candidate': 'a'}})
        staff = self.staff()
        self.assertIn('calling', staff.get('/store/dashboard/chats/').content.decode())
        seen = staff.get(f'/store/dashboard/chats/{self.user.pk}/call/').json()['call']
        self.assertEqual((seen['status'], seen['offer'], seen['ice']), ('ringing', offer, [{'candidate': 'a'}]))
        answer = {'type': 'answer', 'sdp': 'v=0'}
        self.post(f'/store/dashboard/chats/{self.user.pk}/call/', {'action': 'answer', 'id': call['id'], 'answer': answer}, client=staff)
        mine = self.client.get('/chats/api/messages/').json()['call']
        self.assertEqual((mine['status'], mine['answer']), ('active', answer))
        self.post('/chats/api/call/', {'action': 'end', 'id': call['id']})
        self.assertTrue(ChatMessage.objects.get(kind='call').body.startswith('Video call · 0:0'))

    def test_an_unanswered_call_is_logged_as_missed(self):
        offer = {'type': 'offer', 'sdp': 'x'}
        self.post('/chats/api/call/', {'action': 'start', 'kind': 'audio', 'offer': offer})
        self.assertEqual(self.post('/chats/api/call/', {'action': 'start', 'offer': offer}).status_code, 409)
        with patch('myapp.support_chat.CALL_RING_SECONDS', -1):
            state = self.client.get('/chats/api/call/').json()['call']
        self.assertEqual((state['status'], state['reason']), ('ended', 'missed'))
        self.assertEqual(ChatMessage.objects.get(kind='call').body, 'Missed voice call')

    def test_signups_page_and_sidebar_dropdown(self):
        page = self.staff().get('/store/dashboard/chats/signups/').content.decode()
        self.assertIn('asha@example.com', page)
        self.assertIn('Lucknow, Uttar Pradesh', page)
        self.assertIn('Total signups <span class="nav-count">1</span>', page)
        self.assertIn('Conversations', page)

    def test_the_chat_page_is_watermarked_and_has_calls_and_attachments(self):
        body = self.client.get('/chats/').content.decode()
        for part in ('id="voiceCall"', 'id="videoCall"', 'id="attachMenu"', 'id="pickLocation"', 'id="shield"'):
            self.assertIn(part, body)
        self.assertNotIn('id="watermark"', body)


class PhotoSavingTests(ChatRichMessageTests):
    def test_only_staff_can_download_photos(self):
        url = self.upload('me.png').json()['message']['url']
        mine = self.client.get(url + '?download=1')
        self.assertNotIn('attachment', mine.get('Content-Disposition', ''))
        self.assertIn('no-store', mine['Cache-Control'])
        mine.close()
        staff = self.staff().get(url + '?download=1')
        self.assertIn('attachment', staff['Content-Disposition'])
        staff.close()
