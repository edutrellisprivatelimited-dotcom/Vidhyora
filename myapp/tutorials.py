"""Video tutorials: short clips staff upload on the dashboard and users
watch in a pop-up on the AI page (profile menu → Video tutorials).

    Dashboard  /store/dashboard/tutorials/                    list, edit, reorder, delete
               /store/dashboard/tutorials/upload/start/       a new clip: title, name, size
               /store/dashboard/tutorials/<id>/upload/chunk/  one piece at a time
               /store/dashboard/tutorials/<id>/upload/finish/ cover frame + length → ready
    AI page    /AI/api/tutorials/                             the clips to show
               /AI/tutorials/<token>/video                    the clip itself

Uploads come in CHUNK_BYTES pieces, so a long video never runs into the
server's 120-second request limit and a dropped connection only costs one
piece. The file sits under media/tutorials/ (never served from /media/) and
is copied to Dropbox "/<AI name>/Tutorials/" on the image upload worker.
After a redeploy has emptied the server's disk, it is brought back from
Dropbox in the background the first time someone opens the list.

Playback answers each byte-range request with at most RANGE_BYTES: the
browser simply asks for the next piece, and no viewer holds one of the
server's few threads for the length of a video.
"""
import logging
import re
import threading
from datetime import timedelta
from pathlib import Path

from django.contrib import messages
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Max
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from myapp import dropbox_images, dropbox_layout
from myapp.models import TutorialVideo
from myapp.views import dashboard_staff_required

logger = logging.getLogger(__name__)

CHUNK_BYTES = 4 * 1024 * 1024
MAX_BYTES = 500 * 1024 * 1024
RANGE_BYTES = 2 * 1024 * 1024
MAX_POSTER_CHARS = 400_000
FOLDER = 'tutorials'
DROPBOX_FOLDER = 'Tutorials'
CONTENT_TYPES = {
    'mp4': 'video/mp4',
    'm4v': 'video/mp4',
    'webm': 'video/webm',
    'mov': 'video/quicktime',
}
_POSTER_RE = re.compile(r'^data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}$')
_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')

_restoring = set()
_restoring_lock = threading.Lock()


def _error(detail, status=400, **extra):
    return JsonResponse({'status': 'error', 'detail': detail, **extra}, status=status)


def _local_path(video):
    return Path(default_storage.path(video.local_name))


def _on_server(video):
    path = _local_path(video)
    return path.is_file() and path.stat().st_size == video.size


def _human_size(size):
    if size >= 1024 * 1024:
        return f'{size / (1024 * 1024):.1f} MB'
    return f'{max(1, size // 1024)} KB'


def _human_length(seconds):
    if not seconds:
        return ''
    minutes, seconds = divmod(int(seconds), 60)
    return f'{minutes}:{seconds:02d}'


# ---- Dropbox ------------------------------------------------------------------

def upload_to_dropbox(pk):
    """Copy one finished clip to Dropbox. Runs on the image upload worker."""
    import dropbox
    from myapp import dropbox_backup
    video = TutorialVideo.objects.filter(pk=pk, status=TutorialVideo.STATUS_READY, dropbox_path='').first()
    if video is None or not _on_server(video):
        return
    client = dropbox_images._worker_client()
    if client is None:
        return
    dropbox_layout.sync_root(client)
    remote = f'{dropbox_layout.root()}/{DROPBOX_FOLDER}/{video.token}.{video.extension}'
    result = dropbox_backup._upload_file(client, str(_local_path(video)), remote, dropbox.files.WriteMode.overwrite)
    TutorialVideo.objects.filter(pk=pk).update(dropbox_path=getattr(result, 'path_display', None) or remote)


def queue_missing_uploads():
    """Queue every finished clip not yet in Dropbox (a redeploy can drop the queue)."""
    for video in TutorialVideo.objects.filter(status=TutorialVideo.STATUS_READY, dropbox_path=''):
        if _on_server(video):
            dropbox_images.enqueue_tutorial_video(video.pk)


def _restore(pk, path, dropbox_path):
    """Download one clip back to the server (no database work on this thread)."""
    try:
        client = dropbox_images._thread_client()
        if client is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(path.suffix + '.part')
        client.files_download_to_file(str(part), dropbox_path)
        part.replace(path)
    except Exception:
        logger.exception('Could not bring tutorial video %s back from Dropbox', pk)
    finally:
        with _restoring_lock:
            _restoring.discard(pk)


def _ensure_on_server(video):
    """True when the clip can be played now; otherwise starts bringing it
    back from Dropbox (once) and returns False."""
    if _on_server(video):
        return True
    if not video.dropbox_path:
        return False
    with _restoring_lock:
        if video.pk in _restoring:
            return False
        _restoring.add(video.pk)
    threading.Thread(
        target=_restore, args=(video.pk, _local_path(video), video.dropbox_path),
        name=f'tutorial-restore-{video.pk}', daemon=True,
    ).start()
    return False


def _delete_files(video):
    try:
        _local_path(video).unlink(missing_ok=True)
    except Exception:
        logger.exception('Could not delete the server copy of tutorial %s', video.pk)
    if video.dropbox_path:
        client = dropbox_images._thread_client()
        if client is not None:
            try:
                client.files_delete_v2(video.dropbox_path)
            except Exception as exc:
                logger.warning('Could not delete tutorial %s from Dropbox: %s', video.pk, exc)


# ---- AI page --------------------------------------------------------------------

def api_list(request):
    """The clips the pop-up shows, in the order staff set."""
    if not request.user.is_authenticated:
        return _error('Log in to watch the video tutorials.', 401)
    videos = TutorialVideo.objects.filter(status=TutorialVideo.STATUS_READY, is_active=True)
    items = [{
        'id': video.pk,
        'title': video.title,
        'description': video.description,
        'poster': video.poster,
        'length': _human_length(video.duration_seconds),
        'src': reverse('tutorial_video', args=[video.token]),
        'ready': _ensure_on_server(video),
    } for video in videos]
    response = JsonResponse({'status': 'ok', 'videos': items})
    response['Cache-Control'] = 'private, no-store'
    return response


def _file_part(path, start, length):
    with open(path, 'rb') as handle:
        handle.seek(start)
        while length > 0:
            block = handle.read(min(256 * 1024, length))
            if not block:
                break
            length -= len(block)
            yield block


def video(request, token):
    """The clip, for logged-in users only, played in the page and never
    offered as a download. Staff can also play hidden clips (preview)."""
    if not request.user.is_authenticated:
        return HttpResponse('Log in to watch this video.', status=403, content_type='text/plain')
    clip = TutorialVideo.objects.filter(token=token, status=TutorialVideo.STATUS_READY).first()
    is_staff = request.user.is_staff or request.user.is_superuser
    if clip is None or not (clip.is_active or is_staff):
        return HttpResponse('This video is not available.', status=404, content_type='text/plain')
    if not _ensure_on_server(clip):
        response = HttpResponse('This video is being prepared. Try again in a minute.', status=503, content_type='text/plain')
        response['Retry-After'] = '10'
        return response

    path = _local_path(clip)
    size = path.stat().st_size
    content_type = CONTENT_TYPES.get(clip.extension, 'video/mp4')
    match = _RANGE_RE.match(request.META.get('HTTP_RANGE', '').strip())
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
        else:
            start, end = max(0, size - int(match.group(2))), size - 1
        end = min(end, size - 1, start + RANGE_BYTES - 1)
        if start >= size or start > end:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
        response = StreamingHttpResponse(_file_part(path, start, end - start + 1), status=206, content_type=content_type)
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        response['Content-Length'] = str(end - start + 1)
    else:
        response = FileResponse(open(path, 'rb'), content_type=content_type)
        response['Content-Length'] = str(size)
    response['Accept-Ranges'] = 'bytes'
    response['Content-Disposition'] = 'inline'
    response['Cache-Control'] = 'private, max-age=3600'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


# ---- dashboard ------------------------------------------------------------------

@dashboard_staff_required
def dashboard_page(request):
    # Uploads left unfinished for a day (tab closed, connection lost) go.
    stale = TutorialVideo.objects.filter(
        status=TutorialVideo.STATUS_UPLOADING, updated_at__lt=timezone.now() - timedelta(days=1),
    )
    for video in stale:
        _delete_files(video)
        video.delete()
    queue_missing_uploads()
    videos = list(TutorialVideo.objects.all())
    for video in videos:
        video.size_label = _human_size(video.size)
        video.length_label = _human_length(video.duration_seconds)
        video.on_server = _on_server(video)
        video.percent = int(video.received * 100 / video.size) if video.size else 0
    return render(request, 'dashboard/tutorials.html', {
        'active': 'tutorials',
        'videos': videos,
        'visible_count': sum(1 for v in videos if v.is_active and v.status == TutorialVideo.STATUS_READY),
        'max_mb': MAX_BYTES // (1024 * 1024),
        'chunk_bytes': CHUNK_BYTES,
        'extensions': ', '.join(f'.{ext}' for ext in CONTENT_TYPES),
        'dropbox_on': dropbox_images.is_enabled() and dropbox_images.is_configured(),
    })


@dashboard_staff_required
def upload_start(request):
    if request.method != 'POST':
        return _error('Invalid request method.', 405)
    title = (request.POST.get('title') or '').strip()
    description = (request.POST.get('description') or '').strip()
    name = (request.POST.get('name') or '').strip()
    extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    try:
        size = int(request.POST.get('size') or 0)
    except ValueError:
        size = 0
    if not title:
        return _error('Give the video a title, so users know what it shows.')
    if len(title) > 120:
        return _error('Keep the title under 120 characters.')
    if len(description) > 300:
        return _error('Keep the description under 300 characters.')
    if extension not in CONTENT_TYPES:
        return _error('That file type can\'t be played in browsers. Choose an MP4 (best), WebM or MOV video.')
    if size <= 0:
        return _error('That file looks empty. Choose the video again.')
    if size > MAX_BYTES:
        return _error(
            f'That video is {_human_size(size)}; the limit is {MAX_BYTES // (1024 * 1024)} MB. '
            'Export it again at 720p, or shorten it, and try once more.'
        )
    video = TutorialVideo(
        title=title, description=description, original_name=name[:255], extension=extension, size=size,
        sort_order=(TutorialVideo.objects.aggregate(top=Max('sort_order'))['top'] or 0) + 1,
        uploaded_by=request.user,
    )
    video.local_name = f'{FOLDER}/{video.token}.{extension}'
    path = _local_path(video)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    video.save()
    return JsonResponse({'status': 'ok', 'id': video.pk, 'chunk_bytes': CHUNK_BYTES})


@dashboard_staff_required
def upload_chunk(request, pk):
    if request.method != 'POST':
        return _error('Invalid request method.', 405)
    piece = request.FILES.get('chunk')
    try:
        offset = int(request.POST.get('offset', ''))
    except ValueError:
        return _error('A piece of the video could not be read. Try the upload again.')
    with transaction.atomic():
        video = TutorialVideo.objects.select_for_update().filter(pk=pk).first()
        if video is None or video.status != TutorialVideo.STATUS_UPLOADING:
            return _error('This upload is no longer open. Start it again.', 404)
        if offset != video.received:
            # A retried piece the server already has, or one that skipped
            # ahead: the page carries on from where the server really is.
            return _error('Resuming the upload.', 409, received=video.received)
        if piece is None or piece.size == 0 or piece.size > CHUNK_BYTES or video.received + piece.size > video.size:
            return _error('A piece of the video was the wrong size. Try the upload again.')
        with open(_local_path(video), 'ab') as handle:
            for block in piece.chunks():
                handle.write(block)
        video.received += piece.size
        video.save(update_fields=['received', 'updated_at'])
    return JsonResponse({'status': 'ok', 'received': video.received})


@dashboard_staff_required
def upload_finish(request, pk):
    if request.method != 'POST':
        return _error('Invalid request method.', 405)
    video = get_object_or_404(TutorialVideo, pk=pk)
    if video.status != TutorialVideo.STATUS_UPLOADING:
        return _error('This upload is already finished.')
    if video.received != video.size or not _on_server(video):
        return _error('Part of the video is missing. Upload it again.')
    poster = (request.POST.get('poster') or '').strip()
    if poster and (len(poster) > MAX_POSTER_CHARS or not _POSTER_RE.match(poster)):
        poster = ''
    try:
        duration = max(0, min(int(float(request.POST.get('duration') or 0)), 6 * 3600))
    except ValueError:
        duration = 0
    video.poster = poster
    video.duration_seconds = duration
    video.status = TutorialVideo.STATUS_READY
    video.save(update_fields=['poster', 'duration_seconds', 'status', 'updated_at'])
    dropbox_images.enqueue_tutorial_video(video.pk)
    return JsonResponse({'status': 'ok'})


@dashboard_staff_required
def edit(request, pk):
    if request.method == 'POST':
        video = get_object_or_404(TutorialVideo, pk=pk)
        title = (request.POST.get('title') or '').strip()
        if not title:
            messages.error(request, 'The title can\'t be empty.')
        else:
            video.title = title[:120]
            video.description = (request.POST.get('description') or '').strip()[:300]
            video.is_active = request.POST.get('is_active') == 'on'
            video.save(update_fields=['title', 'description', 'is_active', 'updated_at'])
            messages.success(request, f'Saved “{video.title}”.')
    return redirect('dashboard_tutorials')


@dashboard_staff_required
def move(request, pk, direction):
    if request.method == 'POST':
        videos = list(TutorialVideo.objects.all())
        index = next((i for i, v in enumerate(videos) if v.pk == pk), None)
        other = None if index is None else index + (-1 if direction == 'up' else 1)
        if index is not None and other is not None and 0 <= other < len(videos):
            videos[index], videos[other] = videos[other], videos[index]
            for position, video in enumerate(videos, start=1):
                if video.sort_order != position:
                    TutorialVideo.objects.filter(pk=video.pk).update(sort_order=position)
    return redirect('dashboard_tutorials')


@dashboard_staff_required
def delete(request, pk):
    if request.method == 'POST':
        video = get_object_or_404(TutorialVideo, pk=pk)
        _delete_files(video)
        video.delete()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok'})
        messages.success(request, f'Deleted “{video.title}”.')
    return redirect('dashboard_tutorials')
