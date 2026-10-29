"""Video tutorials: staff upload in pieces on the dashboard, logged-in users
watch in the AI page's pop-up, and nobody gets the file as a download."""
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from myapp import tutorials
from myapp.models import TutorialVideo

VIDEO = bytes(range(256)) * 20  # 5,120 bytes standing in for an MP4


class TutorialTestCase(TestCase):
    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.media = Path(media.name)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        small = patch('myapp.tutorials.CHUNK_BYTES', 2048)
        small.start()
        self.addCleanup(small.stop)
        no_dropbox = patch('myapp.dropbox_images.enqueue_tutorial_video')
        self.enqueue = no_dropbox.start()
        self.addCleanup(no_dropbox.stop)
        self.staff = User.objects.create_user('boss', email='boss@example.com', password='pw', is_staff=True)
        self.member = User.objects.create_user('member', email='member@example.com', password='pw')

    def upload(self, title='How to chat', content=VIDEO, name='how-to-chat.mp4'):
        self.client.force_login(self.staff)
        start = self.client.post('/store/dashboard/tutorials/upload/start/', {
            'title': title, 'description': 'The basics', 'name': name, 'size': len(content),
        })
        self.assertEqual(start.status_code, 200, start.content)
        pk = start.json()['id']
        offset = 0
        while offset < len(content):
            piece = content[offset:offset + 2048]
            response = self.client.post(f'/store/dashboard/tutorials/{pk}/upload/chunk/', {
                'offset': offset, 'chunk': SimpleUploadedFile('chunk', piece),
            })
            self.assertEqual(response.status_code, 200, response.content)
            offset = response.json()['received']
        finish = self.client.post(f'/store/dashboard/tutorials/{pk}/upload/finish/', {
            'poster': 'data:image/jpeg;base64,AAAA', 'duration': '75',
        })
        self.assertEqual(finish.status_code, 200, finish.content)
        return TutorialVideo.objects.get(pk=pk)


class UploadTests(TutorialTestCase):
    def test_a_video_uploaded_in_pieces_is_ready_and_queued_for_dropbox(self):
        video = self.upload()
        self.assertEqual(video.status, TutorialVideo.STATUS_READY)
        self.assertEqual((self.media / video.local_name).read_bytes(), VIDEO)
        self.assertEqual(video.duration_seconds, 75)
        self.enqueue.assert_called_once_with(video.pk)

    def test_a_repeated_piece_is_answered_with_where_to_resume(self):
        self.client.force_login(self.staff)
        pk = self.client.post('/store/dashboard/tutorials/upload/start/', {
            'title': 'T', 'name': 'a.mp4', 'size': len(VIDEO),
        }).json()['id']
        self.client.post(f'/store/dashboard/tutorials/{pk}/upload/chunk/', {'offset': 0, 'chunk': SimpleUploadedFile('c', VIDEO[:2048])})
        again = self.client.post(f'/store/dashboard/tutorials/{pk}/upload/chunk/', {'offset': 0, 'chunk': SimpleUploadedFile('c', VIDEO[:2048])})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()['received'], 2048)
        early = self.client.post(f'/store/dashboard/tutorials/{pk}/upload/finish/')
        self.assertEqual(early.status_code, 400)

    def test_wrong_type_and_too_big_are_refused_in_plain_words(self):
        self.client.force_login(self.staff)
        wrong = self.client.post('/store/dashboard/tutorials/upload/start/', {'title': 'T', 'name': 'clip.avi', 'size': 10})
        self.assertIn('MP4', wrong.json()['detail'])
        big = self.client.post('/store/dashboard/tutorials/upload/start/', {
            'title': 'T', 'name': 'clip.mp4', 'size': tutorials.MAX_BYTES + 1,
        })
        self.assertEqual(big.status_code, 400)
        self.assertIn('720p', big.json()['detail'])

    def test_members_cannot_upload(self):
        self.client.force_login(self.member)
        response = self.client.post('/store/dashboard/tutorials/upload/start/', {'title': 'T', 'name': 'a.mp4', 'size': 10})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TutorialVideo.objects.exists())


class WatchingTests(TutorialTestCase):
    def test_members_see_ready_visible_videos_in_order(self):
        second = self.upload('Second')
        first = self.upload('First')
        self.client.post(f'/store/dashboard/tutorials/{first.pk}/move/up/')
        hidden = self.upload('Hidden')
        TutorialVideo.objects.filter(pk=hidden.pk).update(is_active=False)
        self.client.force_login(self.member)
        videos = self.client.get('/AI/api/tutorials/').json()['videos']
        self.assertEqual([v['title'] for v in videos], ['First', 'Second'])
        self.assertTrue(all(v['ready'] for v in videos))
        self.assertEqual(videos[1]['length'], '1:15')
        self.assertNotEqual(second.pk, first.pk)

    def test_the_video_plays_in_pieces_and_is_never_a_download(self):
        video = self.upload()
        self.client.force_login(self.member)
        with patch('myapp.tutorials.RANGE_BYTES', 1000):
            response = self.client.get(f'/AI/tutorials/{video.token}/video', HTTP_RANGE='bytes=0-')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response['Content-Range'], f'bytes 0-999/{len(VIDEO)}')
        self.assertEqual(b''.join(response.streaming_content), VIDEO[:1000])
        self.assertEqual(response['Content-Disposition'], 'inline')
        tail = self.client.get(f'/AI/tutorials/{video.token}/video', HTTP_RANGE='bytes=5000-')
        self.assertEqual(b''.join(tail.streaming_content), VIDEO[5000:])

    def test_logged_out_visitors_and_hidden_videos(self):
        video = self.upload()
        self.client.logout()
        self.assertEqual(self.client.get(f'/AI/tutorials/{video.token}/video').status_code, 403)
        self.assertEqual(self.client.get('/AI/api/tutorials/').status_code, 401)
        TutorialVideo.objects.filter(pk=video.pk).update(is_active=False)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(f'/AI/tutorials/{video.token}/video').status_code, 404)
        self.client.force_login(self.staff)  # staff can still preview it
        self.assertEqual(self.client.get(f'/AI/tutorials/{video.token}/video', HTTP_RANGE='bytes=0-9').status_code, 206)

    def test_the_file_is_not_reachable_through_media(self):
        video = self.upload()
        self.assertEqual(self.client.get(f'/media/{video.local_name}').status_code, 404)

    def test_delete_removes_the_file(self):
        video = self.upload()
        path = self.media / video.local_name
        self.client.post(f'/store/dashboard/tutorials/{video.pk}/delete/')
        self.assertFalse(TutorialVideo.objects.exists())
        self.assertFalse(path.exists())

    def test_a_video_lost_from_the_server_comes_back_from_dropbox(self):
        video = self.upload()
        TutorialVideo.objects.filter(pk=video.pk).update(dropbox_path='/Vidhyora/Tutorials/x.mp4')
        (self.media / video.local_name).unlink()

        class FakeDropbox:
            def files_download_to_file(self, target, path):
                Path(target).write_bytes(VIDEO)

        self.client.force_login(self.member)
        with patch('myapp.dropbox_images._thread_client', return_value=FakeDropbox()):
            first = self.client.get('/AI/api/tutorials/').json()['videos'][0]
            self.assertFalse(first['ready'])
            for _ in range(50):
                if (self.media / video.local_name).exists():
                    break
                time.sleep(.05)
            again = self.client.get('/AI/api/tutorials/').json()['videos'][0]
        self.assertTrue(again['ready'])


class DashboardPageTests(TutorialTestCase):
    def test_the_page_lists_videos_for_staff(self):
        self.upload('Getting started')
        response = self.client.get('/store/dashboard/tutorials/')
        self.assertContains(response, 'Getting started')
        self.assertContains(response, 'Upload a new video')

    def test_ai_page_has_the_menu_item_and_player(self):
        self.client.force_login(self.member)
        response = self.client.get('/')
        self.assertContains(response, 'id="tutorialsMenuBtn"')
        self.assertContains(response, 'controlslist="nodownload')
