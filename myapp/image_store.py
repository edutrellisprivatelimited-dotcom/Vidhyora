"""AI-generated images: compressed to WebP, kept in Dropbox, shown by the site.

    save_generated()      a new image: WebP on the server (a fast cache) and an
                          upload to "/<AI name>/AI Images/<email>/<YYYY-MM>/"
    /AI/images/<token>.webp
                          shows it, from the server copy or else from Dropbox;
                          ?download=1 gives a named file to save
    read_url()            the bytes behind any image link the app stored
    upload_pending()      re-queues uploads that did not finish
    adopt_legacy()        moves images saved before this (/media/ai_generated/
                          links) across, finding lost ones in the old
                          "/vidhyora/<email>/" Dropbox copies

The browser only ever sees this site's own address, never a Dropbox link, so
nothing depends on Dropbox share links (public, and limited in traffic).
"""
import io
import logging
import mimetypes
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from myapp import dropbox_images, dropbox_layout
from myapp.models import AIMessage, AIUserImage, StoredImage

logger = logging.getLogger(__name__)

WEBP_QUALITY = 80
_URL_RE = re.compile(r'^/AI/images/([A-Za-z0-9_-]{8,40})\.(webp|png|jpe?g|gif)$')
LEGACY_FOLDER = 'ai_generated/'


# ---- compressing ------------------------------------------------------------

def compress(content):
    """(bytes, extension): the image as WebP, which is usually a fraction of
    the PNG size. Anything Pillow can't read is kept as it is."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            keeps_alpha = image.mode in ('RGBA', 'LA', 'PA') or (image.mode == 'P' and 'transparency' in image.info)
            image = image.convert('RGBA' if keeps_alpha else 'RGB')
            out = io.BytesIO()
            image.save(out, 'WEBP', quality=WEBP_QUALITY, method=4)
            return out.getvalue(), 'webp'
    except Exception:
        logger.warning('Could not convert an image to WebP; keeping the original', exc_info=True)
        kind = _sniff(content)
        return content, kind or 'png'


def _sniff(content):
    if content.startswith(b'\x89PNG'):
        return 'png'
    if content.startswith(b'\xff\xd8'):
        return 'jpg'
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return 'webp'
    if content[:3] == b'GIF':
        return 'gif'
    return ''


def _dimensions(content):
    from PIL import Image
    try:
        with Image.open(io.BytesIO(content)) as image:
            return image.size
    except Exception:
        return 0, 0


# ---- saving -------------------------------------------------------------------

def url_for(image):
    return reverse('ai_image', args=[f'{image.token}.{Path(image.local_name).suffix.lstrip(".") or "webp"}'])


def save_generated(content, user=None, email='', brief=''):
    """Store one AI-generated image and queue its upload. Returns the
    StoredImage; url_for() gives the link to save in the chat."""
    data, extension = compress(content)
    width, height = _dimensions(data)
    image = StoredImage(user=user if getattr(user, 'is_authenticated', False) else None,
                        owner_email=(email or '')[:254], size=len(data), width=width, height=height,
                        brief=brief or '')
    image.local_name = default_storage.save(
        f'{LEGACY_FOLDER}{timezone.now():%Y/%m/%d}/{image.token}.{extension}', ContentFile(data),
    )
    image.save()
    dropbox_images.enqueue_stored_image(image.pk)
    return image


def upload_stored(pk):
    """Runs on the Dropbox worker thread: upload one image from its server copy."""
    image = StoredImage.objects.filter(pk=pk, uploaded_at__isnull=True).first()
    if image is None:
        return None
    try:
        with default_storage.open(image.local_name, 'rb') as stored:
            content = stored.read()
    except Exception:
        logger.warning('Image %s has no server copy to upload', image.token)
        return None
    when = timezone.localtime(image.created_at)
    filename = f'{when:%Y-%m-%d_%H-%M-%S}_{image.token[:10]}{Path(image.local_name).suffix}'
    path = dropbox_images.upload_bytes(content, dropbox_layout.image_path(image.owner_email, when, filename))
    if path:
        StoredImage.objects.filter(pk=pk).update(dropbox_path=path, uploaded_at=timezone.now())
        logger.info('Saved AI image to Dropbox: %s', path)
    return path


def upload_pending(limit=100):
    """Queue images whose upload did not finish (Dropbox was down, or the
    server restarted mid-way). Returns how many were queued."""
    waiting = StoredImage.objects.filter(
        uploaded_at__isnull=True, created_at__lt=timezone.now() - timedelta(minutes=2),
    ).values_list('pk', flat=True)[:limit]
    return sum(dropbox_images.enqueue_stored_image(pk) for pk in waiting)


# ---- reading back -----------------------------------------------------------------

def stored_image_for_url(url):
    match = _URL_RE.match(urlsplit(url or '').path)
    return StoredImage.objects.filter(token=match.group(1)).first() if match else None


def _storage_name(url):
    """The media-storage name behind one of our /media/ links, or ''."""
    if not url or url.startswith('data:'):
        return ''
    path = unquote(urlsplit(url).path or '')
    media = urlsplit(settings.MEDIA_URL or '/media/').path or '/media/'
    if not media.endswith('/'):
        media += '/'
    if not path.startswith(media):
        return ''
    name = path[len(media):].lstrip('/')
    parts = Path(name)
    if not name or '\\' in name or parts.is_absolute() or '..' in parts.parts:
        return ''
    return name


def _content_type(name):
    return mimetypes.guess_type(name)[0] or 'image/webp'


def read(image):
    """(bytes, content type) of a StoredImage, or None. A server copy that has
    gone (e.g. after a redeploy) is fetched from Dropbox and cached again."""
    try:
        with default_storage.open(image.local_name, 'rb') as stored:
            return stored.read(), _content_type(image.local_name)
    except Exception:
        pass
    content = dropbox_images.download(image.dropbox_path)
    if content is None:
        return None
    try:
        saved = default_storage.save(image.local_name, ContentFile(content))
        if saved != image.local_name:
            StoredImage.objects.filter(pk=image.pk).update(local_name=saved)
    except Exception:
        logger.warning('Could not cache image %s on the server again', image.token, exc_info=True)
    return content, _content_type(image.local_name)


def read_url(url):
    """(bytes, content type) behind an image link the app stored, or None."""
    image = stored_image_for_url(url)
    if image is not None:
        return read(image)
    name = _storage_name(url)
    if not name:
        return None
    try:
        with default_storage.open(name, 'rb') as stored:
            return stored.read(), _content_type(name)
    except Exception:
        return None


def is_available(url):
    """Whether the picture behind a stored link can still be shown."""
    image = stored_image_for_url(url)
    if image is not None:
        return bool(image.dropbox_path) or default_storage.exists(image.local_name)
    name = _storage_name(url)
    return not name or default_storage.exists(name)


def serve(request, name):
    """/AI/images/<token>.webp: the picture itself, or with ?download=1 a
    file to save, named after the AI and the date."""
    token = name.rsplit('.', 1)[0]
    image = StoredImage.objects.filter(token=token).first()
    found = read(image) if image else None
    if not found:
        return HttpResponse('This image is no longer available.', status=404, content_type='text/plain')
    content, content_type = found
    response = HttpResponse(content, content_type=content_type)
    # The token is unguessable and a picture never changes once made.
    response['Cache-Control'] = 'private, max-age=31536000, immutable'
    response['X-Content-Type-Options'] = 'nosniff'
    if request.GET.get('download'):
        extension = Path(image.local_name).suffix or '.webp'
        stamp = timezone.localtime(image.created_at).strftime('%Y-%m-%d_%H-%M')
        filename = f'{dropbox_layout.brand_name()}-AI-image-{stamp}{extension}'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ---- images saved before this ---------------------------------------------------

def _legacy_urls(limit):
    prefix = (settings.MEDIA_URL or '/media/').rstrip('/') + '/' + LEGACY_FOLDER
    urls = list(AIUserImage.objects.filter(url__startswith=prefix).values_list('url', flat=True).distinct()[:limit])
    if len(urls) < limit:
        urls += [u for u in AIMessage.objects.filter(image_data__startswith=prefix)
                 .values_list('image_data', flat=True).distinct()[:limit] if u not in urls][:limit - len(urls)]
    return urls


def _owner_of(url):
    row = AIUserImage.objects.filter(url=url).select_related('user').first()
    user = row.user if row else None
    if user is None:
        message = AIMessage.objects.filter(image_data=url).select_related('conversation__user').first()
        user = message.conversation.user if message and message.conversation_id else None
    return user, (user.email if user else '')


def _from_old_dropbox_copy(name, email, listings):
    """The copy the earlier archive put in "/vidhyora/<email>/", if any. Its
    file names end with the server file's name (without the extension)."""
    folder = f'{dropbox_layout.LEGACY_IMAGE_ROOT}/{dropbox_layout.folder_for(email)}'
    if folder not in listings:
        try:
            listings[folder] = dropbox_images.list_names(folder)
        except Exception:
            logger.warning('Could not list %s on Dropbox', folder, exc_info=True)
            listings[folder] = []
    stem = Path(name).stem[:64]
    match = next((entry for entry in listings[folder] if Path(entry).stem.endswith(stem)), None)
    return dropbox_images.download(f'{folder}/{match}') if match else None


def adopt_legacy(limit=40):
    """Move up to `limit` images saved as /media/ai_generated/ links into
    the new storage and point every chat and gallery row at the new link.
    An image found nowhere is left alone and not tried again for a day.
    Returns (moved, missing)."""
    moved = missing = 0
    listings = {}
    for url in _legacy_urls(limit):
        skip_key = f'image-adopt-missing:{url}'
        if cache.get(skip_key):
            continue
        name = _storage_name(url)
        user, email = _owner_of(url)
        content = None
        try:
            with default_storage.open(name, 'rb') as stored:
                content = stored.read()
        except Exception:
            content = _from_old_dropbox_copy(name, email, listings)
        if not content:
            cache.set(skip_key, True, 24 * 60 * 60)
            missing += 1
            continue
        image = save_generated(content, user, email)
        new_url = url_for(image)
        AIUserImage.objects.filter(url=url).update(url=new_url)
        AIMessage.objects.filter(image_data=url).update(image_data=new_url)
        try:
            default_storage.delete(name)
        except Exception:
            pass
        moved += 1
    if moved or missing:
        logger.info('Moved %d older AI images to the new storage (%d not found anywhere)', moved, missing)
    return moved, missing


def summary():
    """Counts for the Backup & Restore page."""
    prefix = (settings.MEDIA_URL or '/media/').rstrip('/') + '/' + LEGACY_FOLDER
    return {
        'in_dropbox': StoredImage.objects.filter(uploaded_at__isnull=False).count(),
        'waiting': StoredImage.objects.filter(uploaded_at__isnull=True).count(),
        'older': AIUserImage.objects.filter(url__startswith=prefix).count(),
    }
