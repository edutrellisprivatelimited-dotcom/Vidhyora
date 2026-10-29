"""Uploads images to Dropbox off the request's hot path, and reads them back.

* AI-generated images (see myapp.image_store) go to
  "/<AI name>/AI Images/<email>/<YYYY-MM>/" as compressed WebP. The page never
  links to Dropbox; myapp.image_store serves them from the site itself.
* The pictures attached to a user's bug report (views.ai_report_submit) go to
  "/<AI name>/Reports/<email>/", so whoever reviews the report can still see
  what the user was looking at.

Speed is the binding constraint, so uploading is fire-and-forget:

* The enqueue functions do no network, disk or database work. They put a job
  on an in-memory queue and return, so a reply never waits on Dropbox.
* One daemon worker thread runs the jobs.
* Every failure is logged and swallowed. Dropbox being down must never turn a
  generated image into an error the user sees; an image that failed to upload
  stays on the server and image_store.upload_pending queues it again later.

The queue is bounded (MAX_QUEUED): past that, jobs are dropped with a log line
rather than growing until the process runs out of memory.
"""

import base64
import binascii
import logging
import queue
import re
import threading
import time

from django.conf import settings
from django.utils import timezone

from myapp import dropbox_layout

logger = logging.getLogger(__name__)

MAX_QUEUED = 200
# Generous, because this is off the critical path; still bounded so a hung
# connection cannot wedge the worker forever.
UPLOAD_TIMEOUT_SECONDS = 60

# Only the header is matched, so working out the file type costs nothing even
# for a multi-megabyte data URI; the body is decoded on the worker thread.
_DATA_URI_HEADER_RE = re.compile(r'^data:image/([a-z0-9.+-]+);base64,', re.IGNORECASE)

_queue = None
_worker = None
_worker_lock = threading.Lock()

# Touched only by the worker thread, so it needs no lock of its own.
_client = None
_client_credentials = None
# Request threads that read an image back each get their own client.
_local = threading.local()

_CREDENTIAL_SETTINGS = ('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN')
_CREDENTIAL_TTL_SECONDS = 30
_saved_credentials = (0.0, ('', '', ''))


def _setting(name):
    return (getattr(settings, name, '') or '').strip()


def _dashboard_credentials():
    """The App Key/Secret/Refresh Token saved on the dashboard's Backup &
    Restore page, re-read at most every 30 seconds (or right after a change
    through reset_credentials) so this module never queries per image."""
    global _saved_credentials
    stamp, value = _saved_credentials
    if stamp and time.monotonic() - stamp < _CREDENTIAL_TTL_SECONDS:
        return value
    value = ('', '', '')
    try:
        from .models import DropboxSettings
        row = DropboxSettings.objects.filter(pk=1).values_list('app_key', 'app_secret', 'refresh_token').first()
        if row:
            value = tuple((part or '').strip() for part in row)
    except Exception:
        logger.debug('Could not read the saved Dropbox credentials', exc_info=True)
    _saved_credentials = (time.monotonic(), value)
    return value


def _credentials():
    """(app key, app secret, refresh token): what the dashboard saved wins,
    and the server settings fill whatever it left blank."""
    saved = _dashboard_credentials()
    return tuple(saved[i] or _setting(name) for i, name in enumerate(_CREDENTIAL_SETTINGS))


def reset_credentials():
    """Called when the dashboard changes the Dropbox account: the next upload
    reads the new credentials and builds a fresh client."""
    global _client, _client_credentials, _saved_credentials
    _saved_credentials = (0.0, ('', '', ''))
    _client = None
    _client_credentials = None
    _local.__dict__.clear()


def is_enabled():
    """False when uploading is switched off outright, chiefly during tests.

    Kept separate from is_configured() because "no credentials" and "asked not
    to upload" are different situations: only the first is worth warning about.
    """
    return bool(getattr(settings, 'DROPBOX_IMAGE_ARCHIVE_ENABLED', True))


def is_configured():
    """True when all three Dropbox credentials are present."""
    return all(_credentials())


def _build_client():
    try:
        import dropbox
    except ImportError:  # pragma: no cover - the package ships in requirements
        logger.warning('Dropbox images are off: the dropbox package is not installed')
        return None
    if not is_enabled():
        # The worker thread outlives a single request, so this is checked
        # again here rather than trusting the check made at enqueue time.
        return None
    if not is_configured():
        logger.warning('Dropbox images are off: credentials are not set')
        return None
    # A refresh token (not a short-lived access token) so the client renews
    # itself indefinitely without anyone re-authorising the app by hand.
    app_key, app_secret, refresh_token = _credentials()
    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token, app_key=app_key, app_secret=app_secret,
        timeout=UPLOAD_TIMEOUT_SECONDS,
    )


def _worker_client():
    global _client, _client_credentials
    credentials = _credentials()
    if _client is None or _client_credentials != credentials:
        _client = _build_client()
        _client_credentials = credentials
    return _client


def upload_bytes(content, path):
    """Upload on the worker thread; the folder's AI name is checked first so
    a renamed AI's files land in its renamed folder. Returns the path used,
    or None when Dropbox isn't set up."""
    import dropbox
    client = _worker_client()
    if client is None:
        return None
    dropbox_layout.sync_root(client)
    # files_upload creates missing parent folders itself. autorename guards a
    # name collision; mute stops Dropbox notifying the owner for every image.
    result = client.files_upload(content, path, mode=dropbox.files.WriteMode.add, autorename=True, mute=True)
    return getattr(result, 'path_display', None) or path


def _thread_client():
    """A client for the calling thread (a request, or the backup scheduler)."""
    if not is_enabled() or not is_configured():
        return None
    credentials = _credentials()
    if getattr(_local, 'credentials', None) != credentials:
        _local.client = _build_client()
        _local.credentials = credentials
    return _local.client


def download(path):
    """The bytes of one Dropbox file, for a request that needs an image the
    server no longer has. None when Dropbox isn't set up or the file is gone."""
    client = _thread_client() if path else None
    if client is None:
        return None
    try:
        _, response = client.files_download(path)
        return response.content
    except Exception as exc:
        _local.__dict__.clear()
        logger.warning('Could not download %s from Dropbox: %s', path, exc)
        return None


def list_names(folder):
    """The file names in one Dropbox folder ([] when it doesn't exist)."""
    client = _thread_client()
    if client is None:
        return []
    return [entry.name for entry in dropbox_layout._list_files(client, folder)]


def _run():
    global _client
    while True:
        job, args = _queue.get()
        try:
            job(*args)
        except Exception as exc:
            # Expired credentials, rate limits, a full Dropbox and network
            # blips all land here. Drop the cached client so the next job
            # rebuilds it rather than reusing one that may be in a bad state.
            _client = None
            logger.warning('Dropbox upload failed (%s): %s', getattr(job, '__name__', job), exc)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _queue, _worker
    with _worker_lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=MAX_QUEUED)
        # Restart defensively: _run swallows everything, so a dead thread
        # should be impossible, but silently never uploading again would be a
        # bad way to find out otherwise.
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name='dropbox-images', daemon=True)
            _worker.start()
        return _queue


def _submit(job, *args):
    """Queue one job. Never raises: the callers sit in a request the user is
    waiting on, and the image is already saved on the server by then."""
    try:
        if not is_enabled() or not is_configured():
            return False
        _ensure_worker().put_nowait((job, args))
        return True
    except queue.Full:
        logger.warning('Dropbox upload queue is full; skipping one job')
        return False
    except Exception:
        logger.exception('Could not queue a Dropbox upload')
        return False


def enqueue_stored_image(pk):
    """Queue one image_store.StoredImage for upload. Never raises."""
    from myapp import image_store
    return _submit(image_store.upload_stored, pk)


def enqueue_tutorial_video(pk):
    """Queue one finished tutorials.TutorialVideo for upload. Never raises."""
    from myapp import tutorials
    return _submit(tutorials.upload_to_dropbox, pk)


def _decode_data_uri(value):
    match = _DATA_URI_HEADER_RE.match((value or '')[:64])
    if not match:
        return None
    try:
        # validate=False: browsers produce data URIs with newlines in them.
        return base64.b64decode(value[match.end():], validate=False) or None
    except (ValueError, binascii.Error):
        return None


def _upload_report_image(value, email, name):
    from myapp import image_store
    content = _decode_data_uri(value)
    if not content:
        logger.warning('Skipped report evidence %s: no usable image data', name)
        return None
    content, extension = image_store.compress(content)
    stamp = timezone.localtime().strftime('%Y-%m-%d_%H-%M-%S')
    path = upload_bytes(content, dropbox_layout.report_path(email, f'{stamp}_{name}.{extension}'))
    if path:
        logger.info('Saved report evidence to Dropbox: %s', path)
    return path


def enqueue_report_images(report_id, email, user_image='', reply_image=''):
    """Queue a bug report's pictures. Never raises; returns how many.

    ``user_image`` is what the person attached, ``reply_image`` what the AI sent
    back, both as the data URIs ``views._snapshot_ai_report_image`` produces.
    Either can instead be a bare URL (when the picture could not be read);
    there are no bytes behind those, so they are skipped.
    """
    queued = 0
    for label, value in (('user', user_image), ('reply', reply_image)):
        if isinstance(value, str) and value.startswith('data:image/'):
            queued += _submit(_upload_report_image, value, email, f'report-{report_id}-{label}')
    return queued


def flush(timeout=10):
    """Block until queued uploads finish. For tests and shutdown hooks only.

    Returns True if the queue drained within the timeout.
    """
    if _queue is None:
        return True
    deadline = threading.Event()
    waiter = threading.Thread(target=lambda: (_queue.join(), deadline.set()), daemon=True)
    waiter.start()
    return deadline.wait(timeout)
