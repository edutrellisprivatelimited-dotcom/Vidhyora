import io
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from myapp import dropbox_backup as backup


class BackupBundleTests(SimpleTestCase):
    def test_missing_avatar_is_reported_without_blocking_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite3'
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE example (id INTEGER)')
                connection.commit()
            storage = Mock()
            storage.open.side_effect = [FileNotFoundError('avatars/ll.png'), io.BytesIO(b'icon')]
            dbx = Mock()
            missing = []
            with patch.object(backup, 'db_path', return_value=path), patch.object(backup, '_client', return_value=dbx), patch.object(backup, '_image_files', return_value=[('avatars/ll.png', storage), ('pwa/icon.png', storage)]):
                backup.create_backup(SimpleNamespace(), missing_images=missing)
            self.assertEqual(missing, ['avatars/ll.png'])
            with zipfile.ZipFile(io.BytesIO(dbx.files_upload.call_args_list[0].args[0])) as archive:
                self.assertEqual(archive.read('media/pwa/icon.png'), b'icon')
                self.assertNotIn('media/avatars/ll.png', archive.namelist())
                self.assertEqual(json.loads(archive.read('backup_manifest.json'))['missing_images'], missing)

    def test_legacy_database_backup_still_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite3'
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE legacy (id INTEGER)')
                connection.commit()
            content = path.read_bytes()
            path.write_bytes(b'changed')
            dbx = Mock()
            dbx.files_download.return_value = (None, SimpleNamespace(content=content))
            with patch.object(backup, '_client', return_value=dbx), patch.object(backup, 'db_path', return_value=path):
                backup.restore_backup(SimpleNamespace(), 'db_old.sqlite3')
            self.assertEqual(path.read_bytes(), content)

    def test_round_trip_restores_database_and_branding_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / 'db.sqlite3'
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('CREATE TABLE example (value TEXT)')
                connection.execute("INSERT INTO example VALUES ('original')")
                connection.commit()
            storage = Mock()
            storage.open.side_effect = lambda name, mode: io.BytesIO(b'image-' + name.encode())
            dbx = Mock()
            with override_settings(MEDIA_ROOT=root / 'media'), patch.object(backup, 'db_path', return_value=database), patch.object(backup, '_client', return_value=dbx), patch.object(backup, '_image_files', return_value=[('pwa/icon.png', storage), ('branding/favicon.ico', storage), ('branding/social/share.png', storage)]):
                filename = backup.create_backup(SimpleNamespace())
                self.assertEqual(dbx.files_upload.call_count, 2)
                self.assertEqual(dbx.files_upload.call_args_list[0].args[1], f'{backup.backup_folder()}/{filename}')
                self.assertEqual(dbx.files_upload.call_args_list[1].args[1], f'{backup.backup_folder()}/latest.json')
                self.assertLess(len(dbx.files_upload.call_args_list[1].args[0]), 200)
                content = dbx.files_upload.call_args_list[0].args[0]
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    self.assertIn('media/pwa/icon.png', archive.namelist())
                    self.assertIn('media/branding/favicon.ico', archive.namelist())
                database.write_bytes(b'changed')
                dbx.files_download.return_value = (None, SimpleNamespace(content=content))
                backup.restore_backup(SimpleNamespace(), filename)
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute('SELECT value FROM example').fetchone()[0], 'original')
                self.assertEqual((root / 'media/pwa/icon.png').read_bytes(), b'image-pwa/icon.png')

    def test_unsafe_archive_is_rejected_before_database_changes(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('db.sqlite3', b'SQLite format 3\x00')
            archive.writestr('media/../../outside.png', b'bad')
        dbx = Mock()
        dbx.files_download.return_value = (None, SimpleNamespace(content=data.getvalue()))
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder), patch.object(backup, '_client', return_value=dbx):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(SimpleNamespace(), 'backup.zip')


class LargeBackupTests(SimpleTestCase):
    def test_a_large_file_is_uploaded_in_pieces_read_from_disk(self):
        data = bytes(range(256)) * 10
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'big.zip'
            path.write_bytes(data)
            dbx = Mock()
            dbx.files_upload_session_start.return_value = SimpleNamespace(session_id='s1')
            progress = []
            with patch.object(backup, 'UPLOAD_CHUNK_BYTES', 1000):
                backup._upload_file(dbx, path, '/x/big.zip', backup.dropbox.files.WriteMode.add, progress=progress.append)
        sent = dbx.files_upload_session_start.call_args.args[0]
        sent += b''.join(call.args[0] for call in dbx.files_upload_session_append_v2.call_args_list)
        sent += dbx.files_upload_session_finish.call_args.args[0]
        self.assertEqual(sent, data)
        dbx.files_upload.assert_not_called()
        self.assertTrue(progress)

    def test_a_failed_piece_is_retried(self):
        data = b'x' * 2500
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'big.zip'
            path.write_bytes(data)
            dbx = Mock()
            dbx.files_upload_session_start.return_value = SimpleNamespace(session_id='s1')
            dbx.files_upload_session_append_v2.side_effect = [OSError('reset'), None, None]
            with patch.object(backup, 'UPLOAD_CHUNK_BYTES', 1000), patch.object(backup.time, 'sleep'):
                backup._upload_file(dbx, path, '/x/big.zip', backup.dropbox.files.WriteMode.add)
        self.assertEqual(dbx.files_upload_session_append_v2.call_count, 2)

    def test_the_snapshot_leaves_out_free_space(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'db.sqlite3'
            with closing(sqlite3.connect(source)) as connection:
                connection.execute('CREATE TABLE blobs (data BLOB)')
                connection.executemany('INSERT INTO blobs VALUES (?)', [(b'x' * 50000,)] * 40)
                connection.commit()
                connection.execute('DELETE FROM blobs')
                connection.commit()
            copy = Path(folder) / 'copy.sqlite3'
            backup._snapshot(source, copy)
            self.assertLess(copy.stat().st_size, source.stat().st_size / 4)
            with closing(sqlite3.connect(copy)) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM blobs').fetchone()[0], 0)

    def test_the_background_job_reports_progress_and_the_result(self):
        with tempfile.TemporaryDirectory() as folder:
            status_file = Path(folder) / 'status.json'

            def fake_create(settings_obj, *, missing_images=None, progress=None, automatic=False):
                progress('Uploading to Dropbox: 5 of 10 MB')
                missing_images.append('avatars/gone.png')
                return 'backup_x.zip'

            with patch.object(backup, 'STATUS_FILE', status_file), patch.object(backup, 'create_backup', side_effect=fake_create):
                self.assertTrue(backup.start_backup_job(SimpleNamespace()))
                for thread in __import__('threading').enumerate():
                    if thread.name == 'dropbox-backup':
                        thread.join(5)
                status = backup.job_status()
        self.assertEqual(status['state'], 'done')
        self.assertIn('backup_x.zip', status['message'])
        self.assertEqual(status['skipped'], ['avatars/gone.png'])

    def test_a_second_backup_cannot_start_while_one_is_running(self):
        with tempfile.TemporaryDirectory() as folder:
            status_file = Path(folder) / 'status.json'
            import time as _time
            status_file.write_text(json.dumps({'state': 'running', 'started': _time.time(), 'message': 'Uploading'}))
            with patch.object(backup, 'STATUS_FILE', status_file), patch.object(backup, 'create_backup') as create:
                self.assertFalse(backup.start_backup_job(SimpleNamespace()))
            create.assert_not_called()

    def test_a_backup_that_never_finished_is_not_shown_as_running_forever(self):
        with tempfile.TemporaryDirectory() as folder:
            status_file = Path(folder) / 'status.json'
            status_file.write_text(json.dumps({'state': 'running', 'started': 1, 'message': 'Uploading'}))
            with patch.object(backup, 'STATUS_FILE', status_file):
                self.assertEqual(backup.job_status()['state'], 'error')

    def test_cancel_frees_the_page_and_the_worker_stops_without_reporting(self):
        import threading
        with tempfile.TemporaryDirectory() as folder:
            status_file = Path(folder) / 'status.json'
            reached, release, outcome = threading.Event(), threading.Event(), {}

            def fake_create(settings_obj, *, missing_images=None, progress=None, automatic=False):
                progress('Packing')
                reached.set()
                release.wait(5)
                try:
                    progress('Uploading')
                except backup.BackupCancelled:
                    outcome['stopped'] = True
                    raise
                return 'backup_x.zip'

            with patch.object(backup, 'STATUS_FILE', status_file), patch.object(backup, 'create_backup', side_effect=fake_create):
                self.assertTrue(backup.start_backup_job(SimpleNamespace()))
                reached.wait(5)
                self.assertTrue(backup.cancel_backup_job())
                self.assertEqual(backup.job_status()['state'], 'cancelled')
                release.set()
                for thread in threading.enumerate():
                    if thread.name == 'dropbox-backup':
                        thread.join(5)
                status = backup.job_status()
                self.assertFalse(backup.cancel_backup_job())
        self.assertTrue(outcome.get('stopped'))
        self.assertEqual(status['state'], 'cancelled')

    def test_a_backup_whose_progress_went_silent_is_not_shown_as_running(self):
        import time as _time
        with tempfile.TemporaryDirectory() as folder:
            status_file = Path(folder) / 'status.json'
            now = _time.time()
            status_file.write_text(json.dumps({'state': 'running', 'started': now - 1200, 'updated': now - 1200, 'message': 'Packing'}))
            with patch.object(backup, 'STATUS_FILE', status_file):
                self.assertEqual(backup.job_status()['state'], 'error')


class AutomaticBackupTests(SimpleTestCase):
    def _database(self, folder):
        path = Path(folder) / 'db.sqlite3'
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE example (id INTEGER)')
            connection.commit()
        return path

    def _listing(self, *names):
        return SimpleNamespace(entries=[SimpleNamespace(name=name) for name in names], has_more=False)

    def test_an_automatic_backup_replaces_only_the_previous_automatic_one(self):
        with tempfile.TemporaryDirectory() as folder:
            dbx = Mock()
            uploaded = []
            dbx.files_upload.side_effect = lambda content, path, mode: uploaded.append(path)
            dbx.files_list_folder.side_effect = lambda path: self._listing(
                'auto_backup_20260101_000000_000000.zip', 'backup_20260102_000000_000000.zip',
                'latest.json', *(p.rsplit('/', 1)[1] for p in uploaded))
            with patch.object(backup, 'db_path', return_value=self._database(folder)), patch.object(backup, '_client', return_value=dbx), patch.object(backup, '_image_files', return_value=[]):
                filename = backup.create_backup(SimpleNamespace(), automatic=True)
        self.assertRegex(filename, r'^Vidhyora_Auto-Backup_\d\d-[A-Z][a-z]{2}-\d{4}_\d\d-\d\d-\d\d-[AP]M\.zip$')
        dbx.files_delete_v2.assert_called_once_with(f'{backup.backup_folder()}/auto_backup_20260101_000000_000000.zip')

    def test_the_previous_automatic_backup_is_kept_when_the_upload_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            dbx = Mock()
            dbx.files_upload.side_effect = RuntimeError('connection reset')
            dbx.files_list_folder.return_value = self._listing('auto_backup_20260101_000000_000000.zip')
            with patch.object(backup, 'db_path', return_value=self._database(folder)), patch.object(backup, '_client', return_value=dbx), patch.object(backup, '_image_files', return_value=[]):
                with self.assertRaises(backup.BackupError):
                    backup.create_backup(SimpleNamespace(), automatic=True)
        dbx.files_delete_v2.assert_not_called()

    def test_a_manual_backup_never_removes_anything(self):
        with tempfile.TemporaryDirectory() as folder:
            dbx = Mock()
            dbx.files_list_folder.return_value = self._listing('auto_backup_20260101_000000_000000.zip')
            with patch.object(backup, 'db_path', return_value=self._database(folder)), patch.object(backup, '_client', return_value=dbx), patch.object(backup, '_image_files', return_value=[]):
                filename = backup.create_backup(SimpleNamespace())
        self.assertTrue(filename.startswith('Vidhyora_Backup_'))
        self.assertFalse(backup.is_automatic(filename))
        dbx.files_delete_v2.assert_not_called()

    def _run_if_due(self, entries, enabled=True):
        settings_obj = SimpleNamespace(auto_backup_enabled=enabled, is_configured=True)
        with patch('myapp.models.DropboxSettings.get_solo', return_value=settings_obj), \
                patch.object(backup, '_last_auto_attempt', 0.0), \
                patch.object(backup, 'list_backup_entries', return_value=entries), \
                patch.object(backup, 'start_backup_job', return_value=True) as start:
            started = backup.run_automatic_backup_if_due()
        return started, start

    def test_a_backup_starts_when_none_exists_or_the_last_is_a_day_old(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        for entries in ([], [('auto_backup_old.zip', now - datetime.timedelta(hours=25))]):
            started, start = self._run_if_due(entries + [('backup_manual.zip', now)])
            self.assertTrue(started)
            start.assert_called_once_with(start.call_args.args[0], automatic=True)

    def test_no_backup_when_the_last_one_is_recent_or_it_is_switched_off(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        started, start = self._run_if_due([('auto_backup_new.zip', now - datetime.timedelta(hours=3))])
        self.assertFalse(started)
        started, start = self._run_if_due([], enabled=False)
        self.assertFalse(started)
        start.assert_not_called()

    def test_the_timer_does_not_run_under_tests(self):
        with patch.object(backup.threading, 'Thread') as thread:
            self.assertFalse(backup.start_scheduler())
        thread.assert_not_called()


class BackupPageActionTests(TestCase):
    """Restore, Delete and Download on the Backup & Restore page."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.staff = User.objects.create_superuser('boss@example.com', 'boss@example.com', 'pw')
        self.client.force_login(self.staff)
        folder = tempfile.mkdtemp()
        self.status_file = Path(folder) / 'status.json'
        patcher = patch.object(backup, 'STATUS_FILE', self.status_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch('myapp.views.call_command')
    @patch('myapp.views.dropbox_backup.restore_backup')
    def test_restoring_a_backup_made_during_this_login_works_and_keeps_you_logged_in(self, restore, migrate):
        # The test login's session row already exists, just as it does in a
        # backup taken during the same login, so creating it again fails.
        response = self.client.post('/store/dashboard/backup/restore/', {'filename': 'backup_x.zip'}, follow=True)
        self.assertContains(response, 'Database restored from')
        self.assertNotContains(response, 'could not be updated')
        self.assertEqual(int(self.client.session['_auth_user_id']), self.staff.pk)

    @patch('myapp.views.call_command', side_effect=RuntimeError('table already exists'))
    @patch('myapp.views.dropbox_backup.restore_backup')
    def test_a_failed_upgrade_says_what_went_wrong(self, restore, migrate):
        response = self.client.post('/store/dashboard/backup/restore/', {'filename': 'backup_x.zip'}, follow=True)
        self.assertContains(response, 'RuntimeError: table already exists')

    @patch('myapp.views.dropbox_backup.restore_backup')
    def test_no_restore_while_a_backup_is_running(self, restore):
        import time as _time
        self.status_file.write_text(json.dumps({'state': 'running', 'started': _time.time(), 'message': 'Packing'}))
        response = self.client.post('/store/dashboard/backup/restore/', {'filename': 'backup_x.zip'}, follow=True)
        self.assertContains(response, 'A backup is running right now')
        restore.assert_not_called()

    def test_backup_now_explains_when_the_automatic_backup_is_busy(self):
        import time as _time
        self.status_file.write_text(json.dumps({'state': 'running', 'started': _time.time(), 'message': 'Packing', 'automatic': True}))
        with patch('myapp.models.DropboxSettings.is_configured', True), \
                patch.object(backup, 'account_info', return_value={
                    'name': 'Asha Rao', 'email': 'asha@example.com', 'used': None, 'allocated': None,
                    'limited': False, 'folder': backup.backup_folder(),
                }), \
                patch.object(backup, 'list_backup_entries', return_value=[]):
            response = self.client.post('/store/dashboard/backup/run/', follow=True)
        self.assertContains(response, 'automatic daily backup is running right now')

    def test_deleting_one_backup(self):
        dbx = Mock()
        with patch.object(backup, '_client', return_value=dbx):
            response = self.client.post('/store/dashboard/backup/delete/', {'filename': 'backup_1.zip'}, follow=True)
            self.client.post('/store/dashboard/backup/delete/', {'filename': '../secret.zip'})
        dbx.files_delete_v2.assert_called_once_with(f'{backup.backup_folder()}/backup_1.zip')
        self.assertContains(response, 'Deleted')

    def test_download_goes_straight_to_dropbox_or_gives_just_the_database(self):
        database = b'SQLite format 3\x00' + b'rest'
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w') as archive:
            archive.writestr('db.sqlite3', database)
            archive.writestr('media/pwa/icon.png', b'icon')
        dbx = Mock()
        dbx.files_get_temporary_link.return_value = SimpleNamespace(link='https://dl.dropboxusercontent.com/x')
        dbx.files_download.return_value = (None, SimpleNamespace(content=bundle.getvalue()))
        with patch.object(backup, '_client', return_value=dbx):
            direct = self.client.get('/store/dashboard/backup/download/', {'filename': 'backup_1.zip'})
            only_db = self.client.get('/store/dashboard/backup/download/', {'filename': 'backup_1.zip', 'part': 'db'})
        self.assertRedirects(direct, 'https://dl.dropboxusercontent.com/x', fetch_redirect_response=False)
        self.assertEqual(only_db.content, database)
        self.assertIn('backup_1.sqlite3', only_db['Content-Disposition'])


class BackupNameTests(TestCase):
    def test_backups_are_named_after_the_ai_with_a_readable_local_date_and_time(self):
        import datetime
        from myapp.models import SiteCustomization
        site = SiteCustomization.get_solo()
        site.ai_brand_name = 'Study Buddy_auto'
        site.save()
        when = datetime.datetime(2026, 10, 24, 12, 4, 55, tzinfo=datetime.timezone.utc)  # 05:34:55 PM in India
        self.assertEqual(backup.backup_filename(when=when), 'Study-Buddy-auto_Backup_24-Oct-2026_05-34-55-PM.zip')
        automatic = backup.backup_filename(automatic=True, when=when)
        self.assertEqual(automatic, 'Study-Buddy-auto_Auto-Backup_24-Oct-2026_05-34-55-PM.zip')
        self.assertTrue(backup.is_automatic(automatic))
        self.assertFalse(backup.is_automatic(backup.backup_filename(when=when)))
        self.assertTrue(backup.is_automatic('auto_backup_20261024_173423_273766.zip'))  # older name
        self.assertFalse(backup.is_automatic('backup_20261016_212929_599011.zip'))

    def test_a_blank_ai_name_falls_back_to_vidhyora(self):
        from myapp.models import SiteCustomization
        site = SiteCustomization.get_solo()
        site.ai_brand_name = '  '
        site.save()
        self.assertTrue(backup.backup_filename().startswith('Vidhyora_Backup_'))
