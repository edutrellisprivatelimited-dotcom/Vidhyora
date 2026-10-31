"""Where everything this site keeps in Dropbox lives. One folder named after
the AI (the name on the dashboard's Customize page), unless a different
folder name is chosen on the Backup page:

    /<AI name>/Backups/                               database backups
    /<AI name>/AI Images/<email>/<YYYY-MM>/<file>.webp
    /<AI name>/Reports/<email>/<file>.webp            bug-report screenshots

Renaming the AI (or the folder) moves the whole folder to the new name
(sync_root). Before
this layout, backups sat in "/EduTrellis Store/backups" beside another
project's files, and images in "/vidhyora/<email>/"; sync_root brings this
site's backups across once.
"""
import logging
import re
import threading

logger = logging.getLogger(__name__)

DEFAULT_BRAND = 'Vidhyora'
BACKUPS = 'Backups'
IMAGES = 'AI Images'
REPORTS = 'Reports'
GUEST_FOLDER = 'guests'
LEGACY_BACKUP_FOLDER = '/EduTrellis Store/backups'
LEGACY_IMAGE_ROOT = '/vidhyora'

_UNSAFE_PATH_CHARS_RE = re.compile(r'[\\/:?*<>"|]+')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]+')
# This site's own backup names, old and new (the other project in the legacy
# folder names its files db_*.sqlite3 and customize_*.zip).
_OWN_BACKUP_RE = re.compile(r'^(?:[A-Za-z0-9-]+_(?:Auto-)?Backup_.+|(?:auto_)?backup_\d.*)\.zip$')

_sync_lock = threading.Lock()


def safe_name(text):
    """Letters, digits and dashes only, at most 40 characters ('' if none)."""
    return re.sub(r'[^A-Za-z0-9-]+', '-', text or '').strip('-')[:40].strip('-')


def brand_name():
    """The AI name from the Customize page, safe for a file or folder name."""
    from myapp.models import SiteCustomization
    try:
        brand = SiteCustomization.get_solo().ai_brand_name or ''
    except Exception:  # e.g. a restored database from before that setting existed
        brand = ''
    return safe_name(brand) or DEFAULT_BRAND


def custom_folder():
    """The folder name chosen on the Backup page, or '' to follow the AI name."""
    from myapp.models import DropboxSettings
    try:
        return safe_name(DropboxSettings.get_solo().dropbox_folder)
    except Exception:  # e.g. a database from before this setting existed
        return ''


def root():
    return f'/{custom_folder() or brand_name()}'


def backups_folder():
    return f'{root()}/{BACKUPS}'


def folder_for(email):
    """An account email as one safe Dropbox folder name (guests share one)."""
    name = _CONTROL_CHARS_RE.sub('', (email or '').strip().lower())
    name = _UNSAFE_PATH_CHARS_RE.sub('_', name)
    # Cut first, then strip: Dropbox refuses a folder name ending in a dot.
    return name[:120].strip(' .') or GUEST_FOLDER


def image_path(email, when, filename):
    return f'{root()}/{IMAGES}/{folder_for(email)}/{when:%Y-%m}/{filename}'


def report_path(email, filename):
    return f'{root()}/{REPORTS}/{folder_for(email)}/{filename}'


def is_own_backup(name):
    return bool(_OWN_BACKUP_RE.match(name or ''))


def _list_files(dbx, folder):
    import dropbox
    try:
        result = dbx.files_list_folder(folder)
    except Exception as exc:
        if 'not_found' in str(exc).lower():
            return []
        raise
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return [e for e in entries if isinstance(e, dropbox.files.FileMetadata)]


def _move_backups(dbx, source_folder, target_folder):
    """Move this site's backup files from one folder to another; the rest stay."""
    moved = 0
    for entry in _list_files(dbx, source_folder):
        if not is_own_backup(entry.name):
            continue
        try:
            dbx.files_move_v2(f'{source_folder}/{entry.name}', f'{target_folder}/{entry.name}', autorename=True)
            moved += 1
        except Exception:
            logger.warning('Could not move backup %s to %s', entry.name, target_folder, exc_info=True)
    return moved


def sync_root(dbx):
    """Make sure the Dropbox folder matches its current name, and return
    it. The first time, this site's backups come out of the shared legacy
    folder; after a rename, the old folder moves to the new name."""
    from django.conf import settings
    from django.db.models import Value
    from django.db.models.functions import Concat, Substr

    from myapp.models import DropboxSettings, StoredImage
    current = root()
    # Only the live server reorganises Dropbox (the same switch as the daily
    # backup): a local copy of the site, with its own database and AI name,
    # shares the Dropbox account and must not move the live site's files.
    if not getattr(settings, 'DROPBOX_AUTO_BACKUP_ENABLED', False):
        return current
    with _sync_lock:
        try:
            settings_obj = DropboxSettings.get_solo()
        except Exception:  # e.g. a database from before this setting existed
            return current
        previous = settings_obj.dropbox_root
        if previous == current:
            return current
        if not previous:
            try:
                moved = _move_backups(dbx, LEGACY_BACKUP_FOLDER, f'{current}/{BACKUPS}')
            except Exception:
                # Dropbox unreachable: try again next time, nothing is lost.
                logger.warning('Could not check %s for older backups', LEGACY_BACKUP_FOLDER, exc_info=True)
                return current
            if moved:
                logger.info('Moved %d backups from %s to %s', moved, LEGACY_BACKUP_FOLDER, current)
        else:
            try:
                if previous.lower() == LEGACY_IMAGE_ROOT:
                    # Dropbox ignores case, so "/Vidhyora" is also the old
                    # image archive; leave that where it is.
                    raise RuntimeError('the old image archive stays put')
                dbx.files_move_v2(previous, current)
                StoredImage.objects.filter(dropbox_path__startswith=previous + '/').update(
                    dropbox_path=Concat(Value(current), Substr('dropbox_path', len(previous) + 1)),
                )
                logger.info('Moved the Dropbox folder %s to %s after it was renamed', previous, current)
            except Exception as exc:
                if 'not_found' not in str(exc).lower():
                    # The new name is taken already (or the old folder is the
                    # image archive): bring the backups over and leave the
                    # images where their saved paths point.
                    logger.warning('Could not move %s to %s; moving the backups only', previous, current, exc_info=True)
                    _move_backups(dbx, f'{previous}/{BACKUPS}', f'{current}/{BACKUPS}')
        DropboxSettings.objects.filter(pk=settings_obj.pk).update(dropbox_root=current)
        return current
