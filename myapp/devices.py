"""Which browsers (devices) may be logged in to an account at once.

* A normal account has one main device: logging in with the password on
  another device logs the old one out. "Connected devices" can add one more
  device with a one-time login link, so DEVICE_LIMIT devices in all.
* A superuser can be logged in on up to SUPERUSER_DEVICE_LIMIT devices; a
  login past that logs out the device used longest ago.

Logging a device out deletes its session, so that browser is signed out on
its next request. The HTTP side (list, remove, login links) is in
myapp.device_links.
"""
import ipaddress
from datetime import timedelta

from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone

from .models import ActiveUserSession

DEVICE_LIMIT = 2
SUPERUSER_DEVICE_LIMIT = 10
# "Last active" is written at most this often, not on every request.
TOUCH_EVERY = timedelta(minutes=5)


def device_limit(user):
    return SUPERUSER_DEVICE_LIMIT if user.is_superuser else DEVICE_LIMIT


def client_ip(request):
    """The visitor's real IP address, or '' when unknown.

    Railway's edge sets X-Real-IP to the address that connected to it and
    replaces any copy the visitor sent (still right with Cloudflare in front).
    X-Forwarded-For is never read: its first entry is whatever the visitor
    typed, so trusting it let anyone dodge rate limits and IP blocks.
    Without the edge (local runserver) it is the direct connection.
    """
    for value in (request.META.get('HTTP_X_REAL_IP', ''), request.META.get('REMOTE_ADDR', '')):
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError:
            continue
    return ''


def live_devices(user):
    """The account's devices, after dropping any whose session has ended
    without a log out (expired, or replaced by another account's login)."""
    rows = ActiveUserSession.objects.filter(user=user)
    live = set(Session.objects.filter(
        session_key__in=rows.values('session_key'), expire_date__gt=timezone.now(),
    ).values_list('session_key', flat=True))
    rows.exclude(session_key__in=live).delete()
    return ActiveUserSession.objects.filter(user=user)


def register_device(user, session_key, request=None, via_link=False):
    """Record that this session is logged in to the account, then log out
    whatever the rules above say must go."""
    if not user or not user.is_authenticated or not session_key:
        return
    details = {}
    if request is not None:
        details = {'user_agent': request.META.get('HTTP_USER_AGENT', '')[:300], 'ip': client_ip(request)[:64]}

    with transaction.atomic():
        devices = list(live_devices(user).select_for_update())
        current = next((d for d in devices if d.session_key == session_key), None)
        if current is None:
            current = ActiveUserSession.objects.create(
                user=user, session_key=session_key, added_by_link=via_link, **details,
            )
        else:
            for field, value in details.items():
                setattr(current, field, value)
            current.save()
        others = [d for d in devices if d.pk != current.pk]
        removed = []
        if not user.is_superuser and not current.added_by_link:
            # A password login takes over the main device slot.
            removed = [d for d in others if not d.added_by_link]
        kept = sorted((d for d in others if d not in removed), key=lambda d: (d.updated_at, d.pk))
        over = len(kept) + 1 - device_limit(user)
        if over > 0:
            removed += kept[:over]  # the ones used longest ago
        if removed:
            ActiveUserSession.objects.filter(pk__in=[d.pk for d in removed]).delete()

    if removed:
        Session.objects.filter(session_key__in=[d.session_key for d in removed]).delete()


def touch_device(user, session_key):
    """False when this session isn't recorded as one of the account's devices."""
    device = ActiveUserSession.objects.filter(user=user, session_key=session_key).only('pk', 'updated_at').first()
    if device is None:
        return False
    now = timezone.now()
    if device.updated_at < now - TOUCH_EVERY:
        ActiveUserSession.objects.filter(pk=device.pk).update(updated_at=now)
    return True


def forget_device(user, session_key):
    """On log out: the device no longer counts."""
    if user and user.is_authenticated and session_key:
        ActiveUserSession.objects.filter(user=user, session_key=session_key).delete()


def remove_device(user, pk):
    """Log one of the account's devices out. True when it was there."""
    device = ActiveUserSession.objects.filter(user=user, pk=pk).first()
    if device is None:
        return False
    device.delete()
    Session.objects.filter(session_key=device.session_key).delete()
    return True


def keep_only_this_device(user, old_key, new_key):
    """After a password change: other devices are signed out (their sessions
    no longer match the password), and this one keeps its place under the
    session key it was given."""
    others = list(ActiveUserSession.objects.filter(user=user).exclude(session_key__in=[old_key, new_key]))
    ActiveUserSession.objects.filter(pk__in=[d.pk for d in others]).delete()
    Session.objects.filter(session_key__in=[d.session_key for d in others]).delete()
    if old_key and old_key != new_key:
        ActiveUserSession.objects.filter(user=user, session_key=old_key).update(session_key=new_key)
