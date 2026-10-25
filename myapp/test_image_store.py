"""AI images: compressed to WebP, saved in Dropbox under the AI's folder, shown
and downloaded through the site, and older images moved across."""
import io
import json
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from PIL import Image

from myapp import ai_chat, dropbox_images, dropbox_layout, image_generation, image_store
from myapp.models import AIConversation, AIMessage, AIUserImage, DropboxSettings, SiteCustomization, StoredImage


def png(size=(256, 256), mode='RGB', color=(30, 140, 90)):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, 'PNG')
    return out.getvalue()


def noisy_png():
    """A picture with real detail, so the size comparison means something."""
    image = Image.effect_noise((256, 256), 60).convert('RGB')
    out = io.BytesIO()
    image.save(out, 'PNG')
    return out.getvalue()


def uploaded_to(paths):
    return lambda content, path, **kwargs: (paths.append((path, content)), SimpleNamespace(path_display=path))[1]


class StoreTestCase(TestCase):
    def setUp(self):
        cache.clear()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        settings_patch = override_settings(MEDIA_ROOT=media)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        site = SiteCustomization.get_solo()
        site.ai_brand_name = 'Vidhyantra'
        site.save()
        DropboxSettings.objects.update_or_create(pk=1, defaults={'dropbox_root': '/Vidhyantra'})


class CompressTests(StoreTestCase):
    def test_a_png_becomes_a_much_smaller_webp(self):
        original = noisy_png()
        data, extension = image_store.compress(original)
        self.assertEqual(extension, 'webp')
        self.assertTrue(data[:4] == b'RIFF' and data[8:12] == b'WEBP')
        self.assertLess(len(data), len(original) / 2)

    def test_transparency_is_kept(self):
        data, _ = image_store.compress(png(mode='RGBA', color=(0, 0, 0, 0)))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.mode, 'RGBA')

    def test_something_that_is_not_a_picture_is_kept_as_it_is(self):
        self.assertEqual(image_store.compress(b'not an image'), (b'not an image', 'png'))


class SaveAndShowTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('asha', 'Asha@Example.com', 'pw-123456')

    def test_a_new_image_is_saved_as_webp_and_queued_for_dropbox(self):
        with patch.object(dropbox_images, 'enqueue_stored_image') as enqueue:
            image = image_store.save_generated(png(), self.user, self.user.email)
        enqueue.assert_called_once_with(image.pk)
        self.assertTrue(image.local_name.endswith('.webp'))
        self.assertTrue(default_storage.exists(image.local_name))
        self.assertEqual((image.width, image.height), (256, 256))
        self.assertEqual(image_store.url_for(image), f'/AI/images/{image.token}.webp')

    def test_the_upload_goes_to_the_ai_folder_by_email_and_month(self):
        image = image_store.save_generated(png(), self.user, self.user.email)
        paths, client = [], Mock()
        client.files_upload.side_effect = uploaded_to(paths)
        with patch.object(dropbox_images, '_worker_client', return_value=client):
            image_store.upload_stored(image.pk)
        path, content = paths[0]
        month = image.created_at.astimezone().strftime('%Y-%m')
        self.assertTrue(path.startswith(f'/Vidhyantra/AI Images/asha@example.com/{month}/'), path)
        self.assertTrue(path.endswith('.webp'))
        self.assertEqual(content[8:12], b'WEBP')
        image.refresh_from_db()
        self.assertEqual(image.dropbox_path, path)
        self.assertIsNotNone(image.uploaded_at)

    def test_a_guest_image_goes_to_the_guests_folder(self):
        image = image_store.save_generated(png(), None, '')
        paths, client = [], Mock()
        client.files_upload.side_effect = uploaded_to(paths)
        with patch.object(dropbox_images, '_worker_client', return_value=client):
            image_store.upload_stored(image.pk)
        self.assertIn('/AI Images/guests/', paths[0][0])

    def test_the_image_is_shown_and_downloaded_from_the_site(self):
        image = image_store.save_generated(png(), self.user, self.user.email)
        url = image_store.url_for(image)
        shown = self.client.get(url)
        self.assertEqual(shown.status_code, 200)
        self.assertEqual(shown['Content-Type'], 'image/webp')
        self.assertNotIn('Content-Disposition', shown)
        saved = self.client.get(url + '?download=1')
        self.assertRegex(saved['Content-Disposition'], r'attachment; filename="Vidhyantra-AI-image-\d{4}-\d\d-\d\d_\d\d-\d\d\.webp"')
        self.assertEqual(self.client.get('/AI/images/nothing-here-1234.webp').status_code, 404)

    def test_when_the_server_copy_is_gone_it_comes_back_from_dropbox(self):
        image = image_store.save_generated(png(), self.user, self.user.email)
        with default_storage.open(image.local_name, 'rb') as stored:
            content = stored.read()
        StoredImage.objects.filter(pk=image.pk).update(dropbox_path='/Vidhyantra/AI Images/x.webp')
        default_storage.delete(image.local_name)  # e.g. a redeploy emptied media/
        with patch.object(dropbox_images, 'download', return_value=content) as download:
            response = self.client.get(image_store.url_for(image))
            again = self.client.get(image_store.url_for(image))
        self.assertEqual(response.content, content)
        self.assertEqual(again.content, content)
        download.assert_called_once_with('/Vidhyantra/AI Images/x.webp')  # cached on the server again

    def test_unfinished_uploads_are_queued_again(self):
        image = image_store.save_generated(png(), self.user, self.user.email)
        StoredImage.objects.filter(pk=image.pk).update(created_at=image.created_at.replace(year=2020))
        with patch.object(dropbox_images, 'enqueue_stored_image', return_value=True) as enqueue:
            self.assertEqual(image_store.upload_pending(), 1)
        enqueue.assert_called_once_with(image.pk)


class GeneratedImageFlowTests(StoreTestCase):
    @patch('myapp.views.image_generation.generate_image')
    def test_a_generated_image_reply_links_to_the_site_not_to_dropbox_or_media(self, generate):
        user = User.objects.create_user('maker', 'maker@example.com', 'pw-123456', is_staff=True)
        self.client.force_login(user)
        generate.return_value = image_generation.GeneratedImage(png(), 'png')
        with patch.object(dropbox_images, 'enqueue_stored_image') as enqueue:
            response = self.client.post('/AI/api/send/', data=json.dumps({
                'message': 'A futuristic learning robot', 'model': ai_chat.FLUX_KLEIN_4B_MODEL_KEY,
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        url = response['X-Generated-Image-Url']
        self.assertRegex(url, r'^/AI/images/[\w-]+\.webp$')
        self.assertEqual(AIMessage.objects.get(role=AIMessage.ROLE_ASSISTANT).image_data, url)
        self.assertEqual(AIUserImage.objects.get().url, url)
        image = StoredImage.objects.get()
        self.assertEqual((image.user, image.owner_email), (user, 'maker@example.com'))
        enqueue.assert_called_once_with(image.pk)
        self.assertEqual(self.client.get(url)['Content-Type'], 'image/webp')

    @patch('myapp.views.image_generation.generate_image')
    def test_a_broken_dropbox_still_returns_the_image(self, generate):
        user = User.objects.create_user('maker2', 'maker2@example.com', 'pw-123456', is_staff=True)
        self.client.force_login(user)
        generate.return_value = image_generation.GeneratedImage(png(), 'png')
        with override_settings(DROPBOX_IMAGE_ARCHIVE_ENABLED=True, DROPBOX_APP_KEY='k', DROPBOX_APP_SECRET='s',
                               DROPBOX_REFRESH_TOKEN='t'), \
                patch.object(dropbox_images, '_ensure_worker', side_effect=RuntimeError('boom')):
            response = self.client.post('/AI/api/send/', data=json.dumps({
                'message': 'A red car', 'model': ai_chat.FLUX_KLEIN_4B_MODEL_KEY,
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(response['X-Generated-Image-Url']).status_code, 200)


class OlderImageTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('old', 'old@example.com', 'pw-123456')
        self.conversation = AIConversation.objects.create(user=self.user, title='old')

    def _legacy(self, name, content=None):
        if content is not None:
            default_storage.save(name, ContentFile(content))
        url = f'/media/{name}'
        AIMessage.objects.create(conversation=self.conversation, role=AIMessage.ROLE_ASSISTANT, content='', image_data=url)
        AIUserImage.objects.create(user=self.user, conversation=self.conversation, url=url)
        return url

    def test_an_image_still_on_the_server_is_moved_and_every_link_updated(self):
        old_url = self._legacy('ai_generated/2026/10/01/abc123.png', png())
        with patch.object(dropbox_images, 'enqueue_stored_image') as enqueue:
            self.assertEqual(image_store.adopt_legacy(), (1, 0))
        new_url = AIUserImage.objects.get().url
        self.assertRegex(new_url, r'^/AI/images/[\w-]+\.webp$')
        self.assertEqual(AIMessage.objects.get().image_data, new_url)
        self.assertFalse(default_storage.exists('ai_generated/2026/10/01/abc123.png'))
        self.assertEqual(StoredImage.objects.get().owner_email, 'old@example.com')
        enqueue.assert_called_once()
        self.assertIsNone(image_store.read_url(old_url))
        self.assertEqual(image_store.read_url(new_url)[1], 'image/webp')

    def test_a_lost_image_is_found_in_the_old_dropbox_copy(self):
        self._legacy('ai_generated/2026/09/05/deadbeef.png')
        names = ['20260905-101010-deadbeef.png', '20260905-101011-other.png']
        with patch.object(dropbox_images, 'list_names', return_value=names) as listing, \
                patch.object(dropbox_images, 'download', return_value=png()) as download, \
                patch.object(dropbox_images, 'enqueue_stored_image'):
            self.assertEqual(image_store.adopt_legacy(), (1, 0))
        listing.assert_called_once_with('/vidhyora/old@example.com')
        download.assert_called_once_with('/vidhyora/old@example.com/20260905-101010-deadbeef.png')
        self.assertTrue(AIUserImage.objects.get().url.startswith('/AI/images/'))

    def test_an_image_found_nowhere_is_left_and_not_retried_straight_away(self):
        url = self._legacy('ai_generated/gone.png')
        with patch.object(dropbox_images, 'list_names', return_value=[]) as listing:
            self.assertEqual(image_store.adopt_legacy(), (0, 1))
            self.assertEqual(image_store.adopt_legacy(), (0, 0))
        self.assertEqual(listing.call_count, 1)
        self.assertEqual(AIUserImage.objects.get().url, url)


@override_settings(DROPBOX_AUTO_BACKUP_ENABLED=True)  # as on the live server
class FolderLayoutTests(StoreTestCase):
    def _listing(self, *names):
        import dropbox
        entries = [dropbox.files.FileMetadata(name=name) for name in names]
        return SimpleNamespace(entries=entries, has_more=False)

    def test_folder_names_follow_the_ai_name(self):
        self.assertEqual(dropbox_layout.root(), '/Vidhyantra')
        self.assertEqual(dropbox_layout.backups_folder(), '/Vidhyantra/Backups')
        self.assertEqual(dropbox_layout.report_path('A@B.com', 'x.webp'), '/Vidhyantra/Reports/a@b.com/x.webp')
        self.assertEqual(dropbox_layout.folder_for('a/b\\c:d?e*f'), 'a_b_c_d_e_f')
        for empty in ('', None, '   ', '...'):
            self.assertEqual(dropbox_layout.folder_for(empty), dropbox_layout.GUEST_FOLDER)

    def test_the_first_time_only_this_sites_backups_leave_the_shared_old_folder(self):
        DropboxSettings.objects.filter(pk=1).update(dropbox_root='')
        dbx = Mock()
        dbx.files_list_folder.return_value = self._listing(
            'Vidhyora_Backup_24-Oct-2026_05-34-55-PM.zip', 'auto_backup_20261024_173423_273766.zip',
            'backup_20261016_212929_599011.zip', 'db_20261021_183918.sqlite3', 'customize_1.zip', 'latest.json')
        self.assertEqual(dropbox_layout.sync_root(dbx), '/Vidhyantra')
        moved = sorted(call.args[1] for call in dbx.files_move_v2.call_args_list)
        self.assertEqual(moved, [
            '/Vidhyantra/Backups/Vidhyora_Backup_24-Oct-2026_05-34-55-PM.zip',
            '/Vidhyantra/Backups/auto_backup_20261024_173423_273766.zip',
            '/Vidhyantra/Backups/backup_20261016_212929_599011.zip',
        ])
        self.assertEqual(DropboxSettings.objects.get(pk=1).dropbox_root, '/Vidhyantra')
        dbx.reset_mock()
        dropbox_layout.sync_root(dbx)
        dbx.files_move_v2.assert_not_called()

    def test_renaming_the_ai_moves_the_folder_and_keeps_every_image_link_working(self):
        image = StoredImage.objects.create(local_name='x.webp', dropbox_path='/Vidhyantra/AI Images/a/2026-10/x.webp')
        site = SiteCustomization.get_solo()
        site.ai_brand_name = 'Study Buddy'
        site.save()
        dbx = Mock()
        self.assertEqual(dropbox_layout.sync_root(dbx), '/Study-Buddy')
        dbx.files_move_v2.assert_called_once_with('/Vidhyantra', '/Study-Buddy')
        image.refresh_from_db()
        self.assertEqual(image.dropbox_path, '/Study-Buddy/AI Images/a/2026-10/x.webp')

    def test_a_local_copy_of_the_site_never_moves_anything(self):
        DropboxSettings.objects.filter(pk=1).update(dropbox_root='')
        dbx = Mock()
        with override_settings(DROPBOX_AUTO_BACKUP_ENABLED=False):
            self.assertEqual(dropbox_layout.sync_root(dbx), '/Vidhyantra')
        dbx.files_list_folder.assert_not_called()
        dbx.files_move_v2.assert_not_called()
        self.assertEqual(DropboxSettings.objects.get(pk=1).dropbox_root, '')

    def test_the_old_image_archive_folder_is_never_moved_by_a_rename(self):
        DropboxSettings.objects.filter(pk=1).update(dropbox_root='/Vidhyora')  # same folder as /vidhyora
        dbx = Mock()
        dbx.files_list_folder.return_value = self._listing('Vidhyora_Backup_1-Oct-2026_01-00-00-AM.zip')
        dropbox_layout.sync_root(dbx)
        dbx.files_move_v2.assert_called_once_with(
            '/Vidhyora/Backups/Vidhyora_Backup_1-Oct-2026_01-00-00-AM.zip',
            '/Vidhyantra/Backups/Vidhyora_Backup_1-Oct-2026_01-00-00-AM.zip', autorename=True)

    def test_if_the_new_name_is_taken_the_backups_still_come_across(self):
        image = StoredImage.objects.create(local_name='x.webp', dropbox_path='/Vidhyantra/AI Images/a/x.webp')
        site = SiteCustomization.get_solo()
        site.ai_brand_name = 'Study Buddy'
        site.save()
        dbx = Mock()
        dbx.files_move_v2.side_effect = [RuntimeError('to/conflict/folder'), None]
        dbx.files_list_folder.return_value = self._listing('Vidhyantra_Backup_1-Oct-2026_01-00-00-AM.zip')
        dropbox_layout.sync_root(dbx)
        self.assertEqual(dbx.files_move_v2.call_args.args[1], '/Study-Buddy/Backups/Vidhyantra_Backup_1-Oct-2026_01-00-00-AM.zip')
        image.refresh_from_db()
        self.assertEqual(image.dropbox_path, '/Vidhyantra/AI Images/a/x.webp')  # left where it is


class ReportEvidenceTests(StoreTestCase):
    def test_report_pictures_go_to_the_reports_folder_as_webp(self):
        import base64
        data_uri = 'data:image/png;base64,' + base64.b64encode(png()).decode()
        paths = []
        with patch.object(dropbox_images, 'upload_bytes', side_effect=lambda content, path: (paths.append((path, content)), path)[1]):
            dropbox_images._upload_report_image(data_uri, 'Owner@Example.com', 'report-42-user')
            dropbox_images._upload_report_image('data:image/png;base64,!!!', 'owner@example.com', 'report-42-reply')
        self.assertEqual(len(paths), 1)
        path, content = paths[0]
        self.assertRegex(path, r'^/Vidhyantra/Reports/owner@example\.com/\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d_report-42-user\.webp$')
        self.assertEqual(content[8:12], b'WEBP')

    def test_a_report_with_only_links_queues_nothing(self):
        with override_settings(DROPBOX_IMAGE_ARCHIVE_ENABLED=True, DROPBOX_APP_KEY='k', DROPBOX_APP_SECRET='s',
                               DROPBOX_REFRESH_TOKEN='t'), \
                patch.object(dropbox_images, '_ensure_worker') as worker:
            dropbox_images._saved_credentials = (0.0, ('', '', ''))
            self.assertEqual(dropbox_images.enqueue_report_images(9, 'a@b.com', user_image='/media/x.png'), 0)
        worker.assert_not_called()
