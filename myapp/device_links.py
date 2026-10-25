"""Connected devices on the AI page: see every device logged in to your
account, log any of them out, and add one with a one-time login link (the
rules are in myapp.devices).

    /AI/api/devices/                 GET: this account's devices
    /AI/api/devices/<id>/remove/     POST: log that device out
    /AI/api/devices/link/            POST: make a login link
    /device/<token>/                 the login page the link opens; POST logs in
"""
import re
from datetime import timedelta

from django.contrib.auth import login
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from myapp import devices
from myapp.models import ActiveUserSession, DeviceLoginLink
from myapp.views import _merge_session_ai_chats_into_user

LINK_MINUTES = 10

CLOSED_MESSAGES = {
    'missing': 'This login link is not valid. Make a new one from Connected devices on your other device.',
    'used': 'This login link was already used. Make a new one from Connected devices on your other device.',
    'expired': f'This login link has expired (links work for {LINK_MINUTES} minutes). Make a new one from Connected devices on your other device.',
    'full': 'Your account is already logged in on as many devices as it allows. Log one out from Connected devices first.',
}


def _error(detail, status=400):
    return JsonResponse({'status': 'error', 'detail': detail}, status=status)


def _limit_message(user):
    return (f"You're already logged in on {devices.device_limit(user)} devices. "
            'Log one out below to add another.')


# Checked in order: Edge and Opera say "Chrome" too, and Chrome says "Safari".
_BROWSERS = (('Edg', 'Edge'), ('OPR', 'Opera'), ('SamsungBrowser', 'Samsung Internet'),
             ('Firefox', 'Firefox'), ('FxiOS', 'Firefox'), ('CriOS', 'Chrome'),
             ('Chrome', 'Chrome'), ('Safari', 'Safari'))
_SYSTEMS = (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'), ('Windows', 'Windows'),
            ('Mac OS X', 'Mac'), ('Macintosh', 'Mac'), ('CrOS', 'Chromebook'), ('Linux', 'Linux'))


def describe(user_agent):
    """('Chrome on Windows', 'desktop'|'phone'|'tablet') from a User-Agent."""
    user_agent = user_agent or ''
    browser = next((name for mark, name in _BROWSERS if mark in user_agent), 'Browser')
    system = next((name for mark, name in _SYSTEMS if mark in user_agent), '')
    if system == 'iPad' or (system == 'Android' and 'Mobile' not in user_agent):
        kind = 'tablet'
    elif system in ('iPhone', 'Android') or re.search(r'\bMobile\b', user_agent):
        kind = 'phone'
    else:
        kind = 'desktop'
    return (f'{browser} on {system}' if system else browser), kind


def _device_payload(device, current_key):
    name, kind = describe(device.user_agent)
    return {
        'id': device.pk,
        'name': name,
        'kind': kind,
        'ip': device.ip,
        'added_at': device.created_at.isoformat(),
        'last_active': device.updated_at.isoformat(),
        'current': device.session_key == current_key,
        'by_link': device.added_by_link,
    }


def _signed_in(request):
    return request.user.is_authenticated


def api_devices(request):
    if not _signed_in(request):
        return _error('Log in to see your devices.', 401)
    if request.method != 'GET':
        return _error('Invalid request method.', 405)
    rows = list(devices.live_devices(request.user).order_by('-updated_at'))
    current_key = request.session.session_key
    # The device asking first.
    rows.sort(key=lambda d: d.session_key != current_key)
    limit = devices.device_limit(request.user)
    return JsonResponse({
        'status': 'ok',
        'devices': [_device_payload(d, current_key) for d in rows],
        'limit': limit,
        'can_add': len(rows) < limit,
        'link_minutes': LINK_MINUTES,
    })


def api_device_remove(request, pk):
    if not _signed_in(request):
        return _error('Log in to manage your devices.', 401)
    if request.method != 'POST':
        return _error('Invalid request method.', 405)
    if ActiveUserSession.objects.filter(user=request.user, pk=pk, session_key=request.session.session_key).exists():
        return _error('This is the device you are using. Use Log out in the menu to sign out here.')
    if not devices.remove_device(request.user, pk):
        return _error('That device is already logged out.', 404)
    return JsonResponse({'status': 'ok'})


def api_device_link(request):
    """A fresh one-time login link; any older unused link stops working."""
    if not _signed_in(request):
        return _error('Log in to add a device.', 401)
    if request.method != 'POST':
        return _error('Invalid request method.', 405)
    if devices.live_devices(request.user).count() >= devices.device_limit(request.user):
        return _error(_limit_message(request.user))
    DeviceLoginLink.objects.filter(user=request.user, used_at__isnull=True).delete()
    link = DeviceLoginLink.objects.create(
        user=request.user, expires_at=timezone.now() + timedelta(minutes=LINK_MINUTES),
    )
    return JsonResponse({
        'status': 'ok',
        'url': request.build_absolute_uri(reverse('device_login', args=[link.token])),
        'expires_at': link.expires_at.isoformat(),
        'minutes': LINK_MINUTES,
    })


# ---- the page a login link opens ---------------------------------------------

def _link_state(link):
    if link is None:
        return 'missing'
    if link.used_at:
        return 'used'
    if link.expires_at <= timezone.now():
        return 'expired'
    if devices.live_devices(link.user).count() >= devices.device_limit(link.user):
        return 'full'
    return 'open'


def device_login(request, token):
    link = DeviceLoginLink.objects.select_related('user').filter(token=token).first()
    state = _link_state(link)
    already_in = state == 'open' and request.user.is_authenticated and request.user.pk == link.user_id
    if request.method == 'POST' and state == 'open' and not already_in:
        with transaction.atomic():
            # Only one device can use a link, even if two open it at once.
            taken = DeviceLoginLink.objects.filter(pk=link.pk, used_at__isnull=True).update(used_at=timezone.now())
        if not taken:
            state = 'used'
        else:
            if not request.session.session_key:
                request.session.create()
            guest_session_key = request.session.session_key
            request.device_login_link = True  # read by myapp.signals
            login(request, link.user, backend='django.contrib.auth.backends.ModelBackend')
            _merge_session_ai_chats_into_user(link.user, guest_session_key)
            return render(request, 'device_login.html', {'state': 'done', 'email': link.user.email or link.user.username})
    return render(request, 'device_login.html', {
        'state': 'already' if already_in else state,
        'email': (link.user.email or link.user.username) if link else '',
        'message': CLOSED_MESSAGES.get(state, ''),
        'expires_at': link.expires_at if link else None,
    }, status=200 if state == 'open' else 410)
