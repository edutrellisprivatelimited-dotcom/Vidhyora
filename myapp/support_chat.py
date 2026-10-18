"""/chats — a support chat with its own accounts, separate from the main login.

A /chats account (ChatUser) has its own password hash and is remembered in
the session under SESSION_KEY only; it never becomes a django.contrib.auth
user, so it cannot open the AI page or the dashboard, and a main-site login
does not sign anyone in here. Users write; staff reply from the dashboard's
Chats page. A user message is "pending" until a staff member opens the chat.
"""
import hashlib
import json
import re
import secrets
from datetime import timedelta

from django.contrib import messages as flash
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from myapp.india_locations import INDIA_LOCATIONS
from myapp.models import ChatMessage, ChatPasswordReset, ChatUser, SupportProfile

SESSION_KEY = 'support_chat_user_id'
MIN_PASSWORD = 8
MAX_MESSAGE = 4000
RESET_HOURS = 2
ATTEMPT_LIMIT = 10          # logins / sign-ups / reset requests per 15 min, per IP
ATTEMPT_WINDOW = 15 * 60
MESSAGE_LIMIT = 30          # messages per minute, per account
ONLINE_SECONDS = 20         # seen within this long = "online" (both sides poll every 4 s)
STAFF_SEEN_KEY = 'support-chat-staff-last-seen'
_PHONE_RE = re.compile(r'^(?:\+?91[\s-]?)?([6-9]\d{9})$')


def _json_body(request):
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _fail(error, status=400, field=''):
    return JsonResponse({'ok': False, 'error': error, 'field': field}, status=status)


def _over_limit(request, action):
    from myapp.views import _client_ip
    key = f'chats-{action}-{_client_ip(request) or "unknown"}'
    count = cache.get(key, 0)
    if count >= ATTEMPT_LIMIT:
        return True
    cache.set(key, count + 1, ATTEMPT_WINDOW)
    return False


GUEST_COOKIE = 'chats_guest'
GUEST_COOKIE_SALT = 'support-chat-guest'
GUEST_COOKIE_AGE = 365 * 24 * 3600


def _ip(request):
    from myapp.views import _client_ip
    ip = (_client_ip(request) or '')[:64]
    return '' if ip == 'unknown' else ip


def _cookie_guest(request):
    try:
        guest_id = int(request.get_signed_cookie(GUEST_COOKIE, salt=GUEST_COOKIE_SALT, max_age=GUEST_COOKIE_AGE))
    except (KeyError, ValueError, TypeError, Exception):
        return None
    return ChatUser.objects.filter(pk=guest_id, is_guest=True, is_active=True).first()


def current_user(request):
    user_id = request.session.get(SESSION_KEY)
    user = ChatUser.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    if not user:
        # A guest who left without signing up picks the same chat back up.
        user = _cookie_guest(request)
        if user:
            request.session[SESSION_KEY] = user.pk
    return user


def _remember_guest(response, user):
    response.set_signed_cookie(GUEST_COOKIE, str(user.pk), salt=GUEST_COOKIE_SALT, max_age=GUEST_COOKIE_AGE,
                               httponly=True, samesite='Lax')
    return response


def _sign_in(request, user):
    request.session.cycle_key()
    request.session[SESSION_KEY] = user.pk


def _presence(last_seen):
    """{'online': bool, 'last_seen': iso or None} from a last-activity time."""
    if not last_seen:
        return {'online': False, 'last_seen': None}
    return {
        'online': (timezone.now() - last_seen).total_seconds() <= ONLINE_SECONDS,
        'last_seen': timezone.localtime(last_seen).isoformat(),
    }


def touch_staff():
    """A staff member has the chats open — shows support as online."""
    cache.set(STAFF_SEEN_KEY, timezone.now(), None)


def _read_upto(user, sender):
    """Highest id of this sender's messages the other side has read."""
    flag = 'read_by_admin' if sender == ChatMessage.SENDER_USER else 'read_by_user'
    return user.messages.filter(sender=sender, **{flag: True}).order_by('-pk').values_list('pk', flat=True).first() or 0


def _user_status(user):
    return {'read_upto': _read_upto(user, ChatMessage.SENDER_USER), 'support': _presence(cache.get(STAFF_SEEN_KEY))}


def _message_json(message):
    data = {
        'id': message.pk, 'from': message.sender, 'kind': message.kind, 'body': message.body,
        'at': timezone.localtime(message.created_at).isoformat(),
    }
    if message.attachment:
        data['url'] = reverse('chats_attachment', args=[message.pk])
        data['name'] = message.attachment_name
    if message.kind == ChatMessage.KIND_LOCATION:
        data.update(_location_json(message))
    return data


def _location_json(message):
    return {
        'id': message.pk, 'lat': message.latitude, 'lng': message.longitude,
        'live_until': timezone.localtime(message.live_until).isoformat() if message.live_until else None,
        'live': bool(message.live_until and message.live_until > timezone.now()),
        'updated': timezone.localtime(message.location_updated_at or message.created_at).isoformat(),
    }


def _live_locations(user):
    """Live locations still running (or ended in the last minute), so open
    chats can move their pins without reloading."""
    recent = timezone.now() - timedelta(minutes=1)
    rows = user.messages.filter(kind=ChatMessage.KIND_LOCATION, live_until__gte=recent)
    return [_location_json(m) for m in rows]


# ── attachments ──────────────────────────────────────────────────────────────
MAX_UPLOAD_MB = 25
_UPLOAD_KINDS = {
    '.jpg': ChatMessage.KIND_IMAGE, '.jpeg': ChatMessage.KIND_IMAGE, '.png': ChatMessage.KIND_IMAGE,
    '.webp': ChatMessage.KIND_IMAGE, '.gif': ChatMessage.KIND_IMAGE,
    '.mp4': ChatMessage.KIND_VIDEO, '.webm': ChatMessage.KIND_VIDEO, '.mov': ChatMessage.KIND_VIDEO,
    '.pdf': ChatMessage.KIND_FILE, '.doc': ChatMessage.KIND_FILE, '.docx': ChatMessage.KIND_FILE,
    '.xls': ChatMessage.KIND_FILE, '.xlsx': ChatMessage.KIND_FILE, '.ppt': ChatMessage.KIND_FILE,
    '.pptx': ChatMessage.KIND_FILE, '.txt': ChatMessage.KIND_FILE, '.csv': ChatMessage.KIND_FILE,
    '.zip': ChatMessage.KIND_FILE, '.mp3': ChatMessage.KIND_FILE, '.m4a': ChatMessage.KIND_FILE,
}
_INLINE_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
    '.gif': 'image/gif', '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
}


def _message_from_upload(user, sender, request):
    """A photo/video/file message from a multipart request, or an error string."""
    import os
    upload = request.FILES.get('file')
    if not upload:
        return None, 'Choose a file to send.'
    ext = os.path.splitext(upload.name)[1].lower()
    kind = _UPLOAD_KINDS.get(ext)
    if not kind:
        return None, 'That file type is not supported. Send a photo, video, PDF, Office file, text, ZIP or audio file.'
    if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
        return None, f'Files can be up to {MAX_UPLOAD_MB} MB.'
    caption = request.POST.get('body', '').strip()[:MAX_MESSAGE]
    message = ChatMessage(user=user, sender=sender, kind=kind, body=caption,
                          attachment_name=os.path.basename(upload.name)[:200])
    upload.name = secrets.token_hex(12) + ext   # never trust the uploaded name on disk
    message.attachment = upload
    message.save()
    return message, ''


def attachment(request, pk):
    """Streams a chat attachment to the chat's own user or to staff only."""
    import mimetypes
    import os
    from django.http import FileResponse, Http404
    message = get_object_or_404(ChatMessage, pk=pk)
    user = current_user(request)
    staff = request.user.is_authenticated and request.user.is_staff
    if not message.attachment or not (staff or (user and user.pk == message.user_id)):
        raise Http404
    ext = os.path.splitext(message.attachment.name)[1].lower()
    try:
        handle = message.attachment.open('rb')
    except FileNotFoundError:
        raise Http404
    # Only staff get a real download; users can view photos and videos but not save them.
    inline = ext in _INLINE_TYPES and not (staff and request.GET.get('download'))
    response = FileResponse(
        handle, as_attachment=not inline, filename=message.attachment_name or f'file{ext}',
        content_type=_INLINE_TYPES.get(ext) or mimetypes.guess_type(message.attachment_name)[0] or 'application/octet-stream',
    )
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, max-age=3600' if staff else 'private, no-store'
    response['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"
    return response


# ── locations ────────────────────────────────────────────────────────────────
LIVE_MINUTES = (0, 15, 60, 480)


def _coords(data):
    try:
        lat, lng = float(data.get('lat')), float(data.get('lng'))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


@require_POST
def api_location(request):
    """Share a location pin, start a live location, move it, or stop it."""
    user = current_user(request)
    if not user:
        return _fail('Please log in again.', 401)
    data = _json_body(request)
    now = timezone.now()
    if data.get('id'):
        message = user.messages.filter(pk=data['id'], kind=ChatMessage.KIND_LOCATION,
                                       sender=ChatMessage.SENDER_USER).first()
        if not message:
            return _fail('That location is no longer shared.', 404)
        if data.get('stop'):
            message.live_until = now
            message.save(update_fields=['live_until'])
            return JsonResponse({'ok': True, 'location': _location_json(message)})
        coords = _coords(data)
        if not coords or not message.live_until or message.live_until <= now:
            return _fail('Live location has ended.')
        message.latitude, message.longitude = coords
        message.location_updated_at = now
        message.save(update_fields=['latitude', 'longitude', 'location_updated_at'])
        return JsonResponse({'ok': True, 'location': _location_json(message)})
    coords = _coords(data)
    if not coords:
        return _fail('Could not read your location.')
    try:
        minutes = int(data.get('live_minutes') or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes not in LIVE_MINUTES:
        minutes = 0
    message = ChatMessage.objects.create(
        user=user, sender=ChatMessage.SENDER_USER, kind=ChatMessage.KIND_LOCATION,
        body='Live location' if minutes else 'Location', latitude=coords[0], longitude=coords[1],
        live_until=now + timedelta(minutes=minutes) if minutes else None, location_updated_at=now,
    )
    return JsonResponse({'ok': True, 'message': _message_json(message)})


@require_POST
def api_upload(request):
    user = current_user(request)
    if not user:
        return _fail('Please log in again.', 401)
    key = f'chats-send-{user.pk}'
    sent = cache.get(key, 0)
    if sent >= MESSAGE_LIMIT:
        return _fail('You are sending messages too fast. Please wait a minute.', 429)
    cache.set(key, sent + 1, 60)
    message, error = _message_from_upload(user, ChatMessage.SENDER_USER, request)
    if error:
        return _fail(error)
    return JsonResponse({'ok': True, 'message': _message_json(message), **_user_status(user)})


# ── calls ────────────────────────────────────────────────────────────────────
# Voice/video calls go browser to browser (WebRTC). The server only passes the
# connection details between the two sides, through the cache: one call at a
# time per chat, under keys that start with call_key(user_id).
CALL_RING_SECONDS = 45
CALL_STALE_SECONDS = 4 * 60 * 60
ICE_SERVERS = [{'urls': ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302']}]


def call_key(user_id):
    return f'chatcall-{user_id}'


def _call_state(user):
    state = cache.get(call_key(user.pk))
    if state and state['status'] == 'ringing' and timezone.now().timestamp() - state['started'] > CALL_RING_SECONDS:
        state = _end_call(user, state, 'missed')
    return state


def _end_call(user, state, reason):
    if state['status'] == 'ended':
        return state
    state.update(status='ended', reason=reason, ended=timezone.now().timestamp())
    cache.set(call_key(user.pk), state, 120)
    label = 'Video call' if state['kind'] == 'video' else 'Voice call'
    if state.get('answered'):
        seconds = int(state['ended'] - state['answered'])
        body = f'{label} · {seconds // 60}:{seconds % 60:02d}'
    elif reason in ('missed', 'cancelled'):
        body = f'Missed {label.lower()}'
    else:
        body = f'{label} declined'
    ChatMessage.objects.create(
        user=user, sender=state['caller'], kind=ChatMessage.KIND_CALL, body=body,
        read_by_admin=not (state['caller'] == ChatMessage.SENDER_USER and reason == 'missed'),
    )
    return state


def _public_call(state, side):
    """What one side needs: the call plus the other side's ICE candidates."""
    if not state:
        return None
    other = 'admin' if side == 'user' else 'user'
    return {
        'id': state['id'], 'kind': state['kind'], 'caller': state['caller'], 'status': state['status'],
        'reason': state.get('reason', ''),
        'offer': state.get('offer') if side != state['caller'] else None,
        'answer': state.get('answer') if side == state['caller'] else None,
        'ice': cache.get(f"{call_key(state['user'])}-ice-{other}-{state['id']}", []),
        'answered': state.get('answered'),
    }


def call_action(user, side, data):
    """Start / answer / add ICE / end a call. Returns (state, error)."""
    action = data.get('action')
    state = _call_state(user)
    now = timezone.now().timestamp()
    if action == 'start':
        if state and state['status'] in ('ringing', 'active'):
            return None, 'A call is already in progress.'
        offer = data.get('offer')
        if not isinstance(offer, dict) or offer.get('type') != 'offer':
            return None, 'Could not start the call.'
        state = {'id': secrets.token_hex(6), 'user': user.pk, 'caller': side, 'status': 'ringing',
                 'kind': 'video' if data.get('kind') == 'video' else 'audio', 'offer': offer, 'started': now}
        cache.set(call_key(user.pk), state, CALL_STALE_SECONDS)
        return state, ''
    if not state or state.get('id') != data.get('id'):
        return None, 'This call has ended.'
    if action == 'answer':
        answer = data.get('answer')
        if state['status'] != 'ringing' or side == state['caller'] or not isinstance(answer, dict):
            return None, 'This call can no longer be answered.'
        state.update(status='active', answer=answer, answered=now)
        cache.set(call_key(user.pk), state, CALL_STALE_SECONDS)
        return state, ''
    if action == 'ice':
        key = f"{call_key(user.pk)}-ice-{side}-{state['id']}"
        candidates = cache.get(key, [])
        if isinstance(data.get('candidate'), dict) and len(candidates) < 100:
            candidates.append(data['candidate'])
            cache.set(key, candidates, CALL_STALE_SECONDS)
        return state, ''
    if action in ('end', 'decline'):
        if state['status'] == 'ringing':
            reason = 'cancelled' if side == state['caller'] else 'declined'
        else:
            reason = 'ended'
        return _end_call(user, state, reason), ''
    return None, 'Unknown call action.'


def api_call(request):
    user = current_user(request)
    if not user:
        return _fail('Please log in again.', 401)
    if request.method == 'POST':
        state, error = call_action(user, 'user', _json_body(request))
        if error:
            return _fail(error, 409)
        return JsonResponse({'ok': True, 'call': _public_call(state, 'user'), 'ice_servers': ICE_SERVERS})
    return JsonResponse({'ok': True, 'call': _public_call(_call_state(user), 'user'), 'ice_servers': ICE_SERVERS})


# ── pages ────────────────────────────────────────────────────────────────────

def chats_page(request):
    user = current_user(request)
    stamp = ''
    support = SupportProfile.load()
    return render(request, 'chats.html', {
        'chat_user': user,
        'support': support,
        'support_name_json': json.dumps(support.display_name).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'),
        'locations': INDIA_LOCATIONS,
        # Tiled faintly over the chat so any capture shows whose chat it is.
        'watermark': json.dumps(stamp).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'),
    })


def reset_page(request, token):
    reset = ChatPasswordReset.objects.filter(
        token_hash=hashlib.sha256(token.encode()).hexdigest(), used=False,
        created_at__gte=timezone.now() - timedelta(hours=RESET_HOURS),
    ).select_related('user').first()
    error = ''
    if reset and request.method == 'POST':
        password = request.POST.get('password', '')
        if len(password) < MIN_PASSWORD:
            error = f'Use at least {MIN_PASSWORD} characters.'
        elif password != request.POST.get('confirm', ''):
            error = 'The two passwords do not match.'
        else:
            reset.user.password = make_password(password)
            reset.user.save(update_fields=['password'])
            ChatPasswordReset.objects.filter(user=reset.user).update(used=True)
            _sign_in(request, reset.user)
            return redirect('chats_page')
    return render(request, 'chats_reset.html', {'valid': bool(reset), 'error': error})


# ── account API ──────────────────────────────────────────────────────────────

GUEST_LIMIT = 30            # guest chats started per 15 min, per IP


@require_POST
def api_guest(request):
    """Starts an anonymous chat for this browser (called by the page's script,
    so bots that don't run JavaScript never create one)."""
    user = current_user(request)
    if user:
        return JsonResponse({'ok': True})
    key = f'chats-guest-{_ip(request) or "unknown"}'
    count = cache.get(key, 0)
    if count >= GUEST_LIMIT:
        return _fail('Too many new chats from this network. Please wait a few minutes.', 429)
    cache.set(key, count + 1, ATTEMPT_WINDOW)
    tag = secrets.token_hex(6)
    user = ChatUser.objects.create(
        name=f'Guest {tag[:4].upper()}', email=f'guest-{tag}@guest.invalid', phone='', state='', city='',
        password='!', is_guest=True, ip=_ip(request), last_ip=_ip(request),
    )
    _sign_in(request, user)
    return _remember_guest(JsonResponse({'ok': True}), user)


@require_POST
def api_signup(request):
    if _over_limit(request, 'auth'):
        return _fail('Too many attempts. Please wait 15 minutes and try again.', 429)
    data = _json_body(request)
    name = str(data.get('name', '')).strip()[:120]
    email = str(data.get('email', '')).strip().lower()[:254]
    phone = str(data.get('phone', '')).strip()
    state = str(data.get('state', '')).strip()
    city = str(data.get('city', '')).strip()
    password = str(data.get('password', ''))
    if len(name) < 2:
        return _fail('Please enter your full name.', field='name')
    try:
        validate_email(email)
    except ValidationError:
        return _fail('Please enter a valid email address.', field='email')
    phone_match = _PHONE_RE.match(re.sub(r'[\s-]', '', phone))
    if not phone_match:
        return _fail('Please enter a valid 10-digit Indian mobile number.', field='phone')
    if state not in INDIA_LOCATIONS:
        return _fail('Please choose your state.', field='state')
    if city not in INDIA_LOCATIONS[state]:
        return _fail('Please choose your city.', field='city')
    if len(password) < MIN_PASSWORD:
        return _fail(f'Choose a password of at least {MIN_PASSWORD} characters.', field='password')
    if ChatUser.objects.filter(email__iexact=email).exists():
        return _fail('An account with this email already exists. Log in instead, or use "Forgot password".', field='email')
    fields = dict(name=name, email=email, phone=phone_match.group(1), state=state, city=city,
                  password=make_password(password))
    guest = current_user(request)
    if guest and guest.is_guest:
        # Keep the chat they already started.
        for field, value in fields.items():
            setattr(guest, field, value)
        guest.is_guest = False
        guest.created_at = timezone.now()
        guest.save()
        user = guest
    else:
        user = ChatUser.objects.create(**fields, ip=_ip(request), last_ip=_ip(request))
    _sign_in(request, user)
    return JsonResponse({'ok': True})


@require_POST
def api_login(request):
    if _over_limit(request, 'auth'):
        return _fail('Too many attempts. Please wait 15 minutes and try again.', 429)
    data = _json_body(request)
    email = str(data.get('email', '')).strip().lower()
    user = ChatUser.objects.filter(email__iexact=email).first()
    if not user or not check_password(str(data.get('password', '')), user.password):
        return _fail('Wrong email or password.', 400)
    if not user.is_active:
        return _fail('This account has been turned off. Please contact us if you think this is a mistake.', 403)
    guest = current_user(request)
    if guest and guest.is_guest and guest.pk != user.pk:
        # Bring the guest chat into the account, then drop the guest.
        guest.messages.update(user=user)
        guest.delete()
    _sign_in(request, user)
    return JsonResponse({'ok': True})


@require_POST
def api_logout(request):
    request.session.pop(SESSION_KEY, None)
    response = JsonResponse({'ok': True})
    response.delete_cookie(GUEST_COOKIE)
    return response


@require_POST
def api_forgot(request):
    """Reset links are switched off: people email support instead."""
    email = SupportProfile.load().support_email
    return _fail(f'Please email {email} to get back into your account.' if email
                 else 'Please contact support to get back into your account.', 410)


# ── messages API ─────────────────────────────────────────────────────────────

def api_messages(request):
    user = current_user(request)
    if not user:
        return _fail('Please log in again.', 401)
    if request.method == 'POST':
        data = _json_body(request)
        body = str(data.get('body', '')).strip()[:MAX_MESSAGE]
        if not body:
            return _fail('Type a message first.')
        # The page retries unsent messages; the same client id never makes a second copy.
        cid = re.sub(r'[^A-Za-z0-9_-]', '', str(data.get('cid', '')))[:40]
        cid_key = f'chats-cid-{user.pk}-{cid}'
        if cid:
            earlier = ChatMessage.objects.filter(pk=cache.get(cid_key) or 0, user=user).first()
            if earlier:
                return JsonResponse({'ok': True, 'message': _message_json(earlier), **_user_status(user)})
        key = f'chats-send-{user.pk}'
        sent = cache.get(key, 0)
        if sent >= MESSAGE_LIMIT:
            return _fail('You are sending messages too fast. Please wait a minute.', 429)
        cache.set(key, sent + 1, 60)
        message = ChatMessage.objects.create(user=user, sender=ChatMessage.SENDER_USER, body=body)
        if cid:
            cache.set(cid_key, message.pk, 24 * 3600)
        return JsonResponse({'ok': True, 'message': _message_json(message), **_user_status(user)})
    try:
        after = int(request.GET.get('after') or 0)
    except ValueError:
        after = 0
    seen = {'last_seen_at': timezone.now()}
    ip = _ip(request)
    if ip and ip != user.last_ip:
        seen.update(last_ip=ip, **({} if user.ip else {'ip': ip}))
    ChatUser.objects.filter(pk=user.pk).update(**seen)
    rows = list(user.messages.filter(pk__gt=after).order_by('pk')[:200])
    # The page only polls while it is on screen, so loading a reply = seeing it.
    user.messages.filter(sender=ChatMessage.SENDER_ADMIN, read_by_user=False).update(read_by_user=True)
    return JsonResponse({
        'ok': True, 'messages': [_message_json(m) for m in rows], **_user_status(user),
        'locations': _live_locations(user), 'call': _public_call(_call_state(user), 'user'),
    })


# ── dashboard ────────────────────────────────────────────────────────────────

def pending_chat_count():
    """Chats with at least one user message no staff member has opened yet."""
    return ChatUser.objects.filter(
        messages__sender=ChatMessage.SENDER_USER, messages__read_by_admin=False,
    ).distinct().count()


def dashboard_context(request):
    """Context processor: the pending-chats badge on every dashboard page."""
    if not request.path.startswith('/store/dashboard/'):
        return {}
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and user.is_staff):
        return {}
    try:
        return {'pending_chats': pending_chat_count(), 'chat_signup_total': ChatUser.objects.filter(is_guest=False).count(),
                'chat_online_total': _online_users().count(),
                'chat_ip_total': ChatUser.objects.exclude(ip='').values('ip').distinct().count()}
    except Exception:
        return {}


CHAT_SORTS = {
    'recent': ('-pending', '-last_at', '-created_at'),
    'unread': ('-pending', '-last_at'),
    'oldest': ('last_at', 'created_at'),
    'most': ('-total', '-last_at'),
    'name': ('name',),
    'newest': ('-created_at',),
}
CHAT_FILTER_LABELS = {
    'reply': {'waiting': 'Last message from them', 'replied': 'You replied last', 'never': 'Never replied'},
    'who': {'accounts': 'Accounts', 'guests': 'Guests'},
    'account': {'verified': 'Verified', 'unverified': 'Not verified', 'active': 'Active', 'deactivated': 'Deactivated'},
    'has': {'image': 'Has photos', 'video': 'Has videos', 'file': 'Has documents', 'location': 'Shared location',
            'call': 'Had calls', 'media': 'Any photo / video / file'},
    'when': {'today': 'Active today', '7': 'Active in last 7 days', '30': 'Active in last 30 days', 'custom': 'Custom dates'},
}


def _chat_filters(request):
    """The filters chosen on the Conversations page, cleaned up."""
    get = request.GET

    def pick(key, allowed):
        value = get.get(key, '')
        return value if value in allowed else ''
    return {
        'q': get.get('q', '').strip()[:100],
        'ip': get.get('ip', '').strip()[:64],
        'tab': pick('tab', ('waiting', 'online', 'accounts', 'guests')),
        'reply': pick('reply', CHAT_FILTER_LABELS['reply']),
        'who': pick('who', CHAT_FILTER_LABELS['who']),
        'account': pick('account', CHAT_FILTER_LABELS['account']),
        'has': pick('has', CHAT_FILTER_LABELS['has']),
        'when': pick('when', CHAT_FILTER_LABELS['when']),
        'from': get.get('from', '')[:10], 'to': get.get('to', '')[:10],
        'state': pick('state', INDIA_LOCATIONS),
        'shared_ip': '1' if get.get('shared_ip') == '1' else '',
        'sort': pick('sort', CHAT_SORTS) or 'recent',
    }


def _apply_tab(users, tab):
    if tab == 'waiting':
        return users.filter(pending__gt=0)
    if tab == 'online':
        return users.filter(last_seen_at__gte=timezone.now() - timedelta(seconds=ONLINE_SECONDS), is_active=True)
    if tab == 'accounts':
        return users.filter(is_guest=False)
    if tab == 'guests':
        return users.filter(is_guest=True)
    return users


def _date_range(f):
    from datetime import date
    today = timezone.localdate()
    if f['when'] == 'today':
        return today, None
    if f['when'] in ('7', '30'):
        return today - timedelta(days=int(f['when']) - 1), None
    if f['when'] == 'custom':
        try:
            return (date.fromisoformat(f['from']) if f['from'] else None,
                    date.fromisoformat(f['to']) if f['to'] else None)
        except ValueError:
            pass
    return None, None


def dashboard_chats(request):
    from django.db.models import OuterRef, Subquery
    touch_staff()
    f = _chat_filters(request)
    last_sender = ChatMessage.objects.filter(user=OuterRef('pk')).order_by('-pk').values('sender')[:1]
    base = ChatUser.objects.annotate(
        pending=Count('messages', filter=Q(messages__sender=ChatMessage.SENDER_USER, messages__read_by_admin=False)),
        last_at=Max('messages__created_at'),
        total=Count('messages'),
        replies=Count('messages', filter=Q(messages__sender=ChatMessage.SENDER_ADMIN)),
        last_sender=Subquery(last_sender),
    ).exclude(is_guest=True, total=0)
    tab_counts = {tab: _apply_tab(base, tab).count() for tab in ('waiting', 'online', 'accounts', 'guests')}
    tab_counts['all'] = base.count()

    users = _apply_tab(base, f['tab'])
    q = f['q']
    if q:
        matching = ChatMessage.objects.filter(body__icontains=q).values('user_id')
        users = users.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q) | Q(city__icontains=q)
                             | Q(state__icontains=q) | Q(ip=q) | Q(last_ip=q) | Q(pk__in=matching))
    if f['ip']:
        users = users.filter(Q(ip=f['ip']) | Q(last_ip=f['ip']))
    if f['reply'] == 'waiting':
        users = users.filter(last_sender=ChatMessage.SENDER_USER)
    elif f['reply'] == 'replied':
        users = users.filter(last_sender=ChatMessage.SENDER_ADMIN)
    elif f['reply'] == 'never':
        users = users.filter(total__gt=0, replies=0)
    if f['who']:
        users = users.filter(is_guest=f['who'] == 'guests')
    if f['account'] in ('verified', 'unverified'):
        users = users.filter(is_verified=f['account'] == 'verified', is_guest=False)
    elif f['account']:
        users = users.filter(is_active=f['account'] == 'active')
    if f['has']:
        kinds = ['image', 'video', 'file'] if f['has'] == 'media' else [f['has']]
        users = users.filter(pk__in=ChatMessage.objects.filter(kind__in=kinds).values('user_id'))
    start, end = _date_range(f)
    if start:
        users = users.filter(Q(last_at__date__gte=start) | Q(last_at__isnull=True, created_at__date__gte=start))
    if end:
        users = users.filter(Q(last_at__date__lte=end) | Q(last_at__isnull=True, created_at__date__lte=end))
    if f['state']:
        users = users.filter(state=f['state'])
    per_ip = _chats_per_ip()
    if f['shared_ip']:
        users = users.filter(ip__in=[ip for ip, n in per_ip.items() if n > 1])
    users = list(users.order_by(*CHAT_SORTS[f['sort']])[:300])
    for user in users:
        user.same_ip = per_ip.get(user.ip, 0) if user.ip else 0
        user.last_message = user.messages.order_by('-pk').first()
        user.presence = _presence(user.last_seen_at)
        call = cache.get(call_key(user.pk))
        user.ringing = bool(call and call['status'] == 'ringing' and call['caller'] == 'user')

    chips = []
    for key in ('reply', 'who', 'account', 'has', 'when'):
        if f[key]:
            text = CHAT_FILTER_LABELS[key][f[key]]
            if key == 'when' and f[key] == 'custom':
                text = ((f['from'] or '…') + ' to ' + (f['to'] or '…')) if f['from'] or f['to'] else 'Pick dates'
            chips.append((key, text))
    if f['state']:
        chips.append(('state', f['state']))
    if f['shared_ip']:
        chips.append(('shared_ip', 'Shared IPs only'))
    if f['ip']:
        chips.append(('ip', 'IP ' + f['ip']))
    more_open = any(f[k] for k in ('reply', 'who', 'account', 'has', 'when', 'state', 'shared_ip')) or f['sort'] != 'recent'
    return render(request, 'dashboard/chats.html', {
        'active': 'chats', 'chat_users': users, 'q': q, 'ip_filter': f['ip'], 'f': f, 'chips': chips,
        'tab_counts': tab_counts, 'more_open': more_open, 'states': sorted(INDIA_LOCATIONS),
        'filtered': bool(chips or q or f['tab']),
    })


def dashboard_chat_thread(request, pk):
    user = get_object_or_404(ChatUser, pk=pk)
    if request.method == 'POST':
        body = request.POST.get('body', '').strip()[:MAX_MESSAGE]
        touch_staff()
        wants_json = request.headers.get('x-requested-with') == 'XMLHttpRequest'
        if request.FILES.get('file'):
            message, error = _message_from_upload(user, ChatMessage.SENDER_ADMIN, request)
            if error:
                return _fail(error)
            user.messages.filter(sender=ChatMessage.SENDER_USER, read_by_admin=False).update(read_by_admin=True)
            return JsonResponse({'ok': True, 'message': _message_json(message)})
        if not body:
            if wants_json:
                return _fail('Type a reply first.')
            flash.error(request, 'Type a reply first.')
            return redirect('dashboard_chat_thread', pk=user.pk)
        message = ChatMessage.objects.create(user=user, sender=ChatMessage.SENDER_ADMIN, body=body)
        # Replying answers everything sent so far.
        user.messages.filter(sender=ChatMessage.SENDER_USER, read_by_admin=False).update(read_by_admin=True)
        if wants_json:
            return JsonResponse({'ok': True, 'message': _message_json(message)})
        return redirect('dashboard_chat_thread', pk=user.pk)
    touch_staff()
    user.messages.filter(sender=ChatMessage.SENDER_USER, read_by_admin=False).update(read_by_admin=True)
    return render(request, 'dashboard/chat_thread.html', {
        'active': 'chats', 'chat_user': user, 'same_ip': _chats_per_ip().get(user.ip, 0) if user.ip else 0, 'thread': [_message_json(m) for m in user.messages.order_by('pk')],
        'presence': _presence(user.last_seen_at), 'seen_upto': _read_upto(user, ChatMessage.SENDER_ADMIN),
        'last_id': user.messages.order_by('-pk').values_list('pk', flat=True).first() or 0,
    })


def dashboard_chat_poll(request, pk):
    """New messages in an open chat, so the admin sees replies arrive."""
    user = get_object_or_404(ChatUser, pk=pk)
    try:
        after = int(request.GET.get('after') or 0)
    except ValueError:
        after = 0
    touch_staff()
    rows = list(user.messages.filter(pk__gt=after).order_by('pk')[:200])
    user.messages.filter(pk__in=[m.pk for m in rows], sender=ChatMessage.SENDER_USER).update(read_by_admin=True)
    return JsonResponse({
        'ok': True, 'messages': [_message_json(m) for m in rows],
        'seen_upto': _read_upto(user, ChatMessage.SENDER_ADMIN), 'user': _presence(user.last_seen_at),
        'locations': _live_locations(user), 'call': _public_call(_call_state(user), 'admin'),
    })


def dashboard_chat_call(request, pk):
    user = get_object_or_404(ChatUser, pk=pk)
    touch_staff()
    if request.method == 'POST':
        state, error = call_action(user, 'admin', _json_body(request))
        if error:
            return _fail(error, 409)
        return JsonResponse({'ok': True, 'call': _public_call(state, 'admin'), 'ice_servers': ICE_SERVERS})
    return JsonResponse({'ok': True, 'call': _public_call(_call_state(user), 'admin'), 'ice_servers': ICE_SERVERS})


def dashboard_chat_signups(request):
    q = request.GET.get('q', '').strip()
    users = ChatUser.objects.filter(is_guest=False).annotate(total=Count('messages'), last_at=Max('messages__created_at'))
    if q:
        users = users.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q)
                             | Q(city__icontains=q) | Q(state__icontains=q))
    users = list(users.order_by('-created_at'))
    for user in users:
        user.presence = _presence(user.last_seen_at)
    today = timezone.localdate()
    everyone = ChatUser.objects.filter(is_guest=False)
    online_since = timezone.now() - timedelta(seconds=ONLINE_SECONDS)
    stats = {
        'total': everyone.count(),
        'today': everyone.filter(created_at__date=today).count(),
        'week': everyone.filter(created_at__date__gte=today - timedelta(days=6)).count(),
        'online': everyone.filter(last_seen_at__gte=online_since, is_active=True).count(),
        'inactive': everyone.filter(is_active=False).count(),
    }
    return render(request, 'dashboard/chat_signups.html', {
        'active': 'chat_signups', 'chat_users': users, 'q': q, 'stats': stats, 'locations': INDIA_LOCATIONS,
    })


# ── support profile ──────────────────────────────────────────────────────────

PHOTO_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif'}
MAX_PHOTO_MB = 5


def support_photo(request):
    """The support team's picture, shown to everyone on /chats."""
    import os
    from django.http import FileResponse, Http404
    profile = SupportProfile.load()
    ext = os.path.splitext(profile.photo.name or '')[1].lower()
    if not profile.photo or ext not in PHOTO_TYPES:
        raise Http404
    try:
        handle = profile.photo.open('rb')
    except FileNotFoundError:
        raise Http404
    response = FileResponse(handle, content_type=PHOTO_TYPES[ext])
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'public, max-age=300'
    return response


def _save_chat_profile(request, profile):
    import os
    name = request.POST.get('display_name', '').strip()[:60]
    if len(name) < 2:
        return 'Please enter a name of at least 2 letters.'
    photo = request.FILES.get('photo')
    if photo:
        ext = os.path.splitext(photo.name)[1].lower()
        if ext not in PHOTO_TYPES:
            return 'Please choose a JPG, PNG, WEBP or GIF picture.'
        if photo.size > MAX_PHOTO_MB * 1024 * 1024:
            return f'The picture must be smaller than {MAX_PHOTO_MB} MB.'
        try:
            from PIL import Image
            Image.open(photo).verify()
            photo.seek(0)
        except Exception:
            return 'That file does not look like a picture. Please try another one.'
    profile.display_name = name
    profile.about = request.POST.get('about', '').strip()[:120]
    profile.welcome = request.POST.get('welcome', '').strip()[:300]
    email = request.POST.get('support_email', '').strip().lower()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            return 'Please enter a valid support email address.'
    profile.support_email = email
    if (photo or request.POST.get('remove_photo')) and profile.photo:
        try:
            profile.photo.delete(save=False)
        except OSError:  # still being sent to someone; just forget it
            profile.photo = None
    if photo:
        profile.photo.save(secrets.token_hex(8) + ext, photo, save=False)
    profile.save()
    return ''


def dashboard_chat_profile(request):
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.password_validation import validate_password
    profile = SupportProfile.load()
    staff = request.user
    if request.method == 'POST':
        section = request.POST.get('section')
        if section == 'chat':
            error = _save_chat_profile(request, profile)
            if error:
                flash.error(request, error)
            else:
                flash.success(request, 'Chat profile saved. Users see it right away.')
        elif section == 'account':
            email = request.POST.get('email', '').strip().lower()
            try:
                validate_email(email)
            except ValidationError:
                flash.error(request, 'Please enter a valid email address.')
                return redirect('dashboard_chat_profile')
            staff.first_name = request.POST.get('first_name', '').strip()[:150]
            staff.last_name = request.POST.get('last_name', '').strip()[:150]
            staff.email = email
            staff.save(update_fields=['first_name', 'last_name', 'email'])
            flash.success(request, 'Your details are saved.')
        elif section == 'password':
            new = request.POST.get('new', '')
            if not staff.check_password(request.POST.get('current', '')):
                flash.error(request, 'Your current password is not correct.')
            elif new != request.POST.get('confirm', ''):
                flash.error(request, 'The two new passwords do not match.')
            else:
                try:
                    validate_password(new, staff)
                except ValidationError as exc:
                    flash.error(request, ' '.join(exc.messages))
                    return redirect('dashboard_chat_profile')
                staff.set_password(new)
                staff.save(update_fields=['password'])
                update_session_auth_hash(request, staff)
                flash.success(request, 'Password changed. You stay logged in here.')
        return redirect('dashboard_chat_profile')
    return render(request, 'dashboard/chat_profile.html', {'active': 'chat_profile', 'profile': profile})


# ── user's own profile ───────────────────────────────────────────────────────

def user_photo(request, pk):
    """A chat user's picture, for that user and staff only."""
    import os
    from django.http import FileResponse, Http404
    owner = get_object_or_404(ChatUser, pk=pk)
    me = current_user(request)
    staff = request.user.is_authenticated and request.user.is_staff
    ext = os.path.splitext(owner.photo.name or '')[1].lower()
    if not owner.photo or ext not in PHOTO_TYPES or not (staff or (me and me.pk == owner.pk)):
        raise Http404
    try:
        handle = owner.photo.open('rb')
    except FileNotFoundError:
        raise Http404
    response = FileResponse(handle, content_type=PHOTO_TYPES[ext])
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, max-age=300'
    return response


def _check_photo(photo):
    import os
    ext = os.path.splitext(photo.name)[1].lower()
    if ext not in PHOTO_TYPES:
        return '', 'Please choose a JPG, PNG, WEBP or GIF picture.'
    if photo.size > MAX_PHOTO_MB * 1024 * 1024:
        return '', f'The picture must be smaller than {MAX_PHOTO_MB} MB.'
    try:
        from PIL import Image
        Image.open(photo).verify()
        photo.seek(0)
    except Exception:
        return '', 'That file does not look like a picture. Please try another one.'
    return ext, ''


@require_POST
def api_profile(request):
    user = current_user(request)
    if not user:
        return _fail('Please log in again.', 401)
    if user.is_guest:
        return _fail('Please sign up first to set up your profile.', 403)
    data = request.POST
    name = data.get('name', '').strip()[:120]
    phone = _PHONE_RE.match(re.sub(r'[\s-]', '', data.get('phone', '').strip()))
    state, city = data.get('state', '').strip(), data.get('city', '').strip()
    if len(name) < 2:
        return _fail('Please enter your full name.', field='name')
    if not phone:
        return _fail('Please enter a valid 10-digit Indian mobile number.', field='phone')
    if state not in INDIA_LOCATIONS:
        return _fail('Please choose your state.', field='state')
    if city not in INDIA_LOCATIONS[state]:
        return _fail('Please choose your city.', field='city')
    new = data.get('new_password', '')
    if new:
        if not check_password(data.get('current_password', ''), user.password):
            return _fail('Your current password is not correct.', field='current_password')
        if len(new) < MIN_PASSWORD:
            return _fail(f'Choose a new password of at least {MIN_PASSWORD} characters.', field='new_password')
        user.password = make_password(new)
    photo = request.FILES.get('photo')
    ext = ''
    if photo:
        ext, error = _check_photo(photo)
        if error:
            return _fail(error, field='photo')
    if (photo or data.get('remove_photo')) and user.photo:
        try:
            user.photo.delete(save=False)
        except OSError:
            user.photo = None
    if photo:
        user.photo.save(secrets.token_hex(8) + ext, photo, save=False)
    user.name, user.phone, user.state, user.city = name, phone.group(1), state, city
    user.save()
    if new:
        _sign_in(request, user)
    return JsonResponse({'ok': True, 'name': user.name,
                         'photo': reverse('chats_user_photo', args=[user.pk]) + f'?v={secrets.token_hex(3)}' if user.photo else ''})


# ── managing chat accounts ───────────────────────────────────────────────────

def _online_users():
    since = timezone.now() - timedelta(seconds=ONLINE_SECONDS)
    return ChatUser.objects.filter(is_active=True, last_seen_at__gte=since)


def _delete_files(user):
    for message in user.messages.exclude(attachment=''):
        try:
            message.attachment.delete(save=False)
        except OSError:
            pass
    if user.photo:
        try:
            user.photo.delete(save=False)
        except OSError:
            pass


@require_POST
def dashboard_chat_user_action(request, pk):
    user = get_object_or_404(ChatUser, pk=pk)
    action = request.POST.get('action')
    back = request.POST.get('next') or reverse('dashboard_chat_signups')
    if not back.startswith('/store/dashboard/'):
        back = reverse('dashboard_chat_signups')
    if action == 'toggle_active':
        user.is_active = not user.is_active
        user.save(update_fields=['is_active'])
        call = _call_state(user)
        if not user.is_active and call:
            _end_call(user, call, 'ended')
        flash.success(request, f'{user.name} can use the chat again.' if user.is_active
                      else f'{user.name} is deactivated and logged out of the chat.')
    elif action == 'toggle_verified':
        user.is_verified = not user.is_verified
        user.save(update_fields=['is_verified'])
        flash.success(request, f'{user.name} is marked as {"verified" if user.is_verified else "not verified"}.')
    elif action == 'clear_chat':
        for message in user.messages.exclude(attachment=''):
            try:
                message.attachment.delete(save=False)
            except OSError:
                pass
        count = user.messages.count()
        user.messages.all().delete()
        flash.success(request, f'Deleted {count} message{"s" if count != 1 else ""} with {user.name}.')
    elif action == 'delete':
        name = user.name
        _delete_files(user)
        user.delete()
        flash.success(request, f'{name}\'s account and chat were deleted.')
        return redirect('dashboard_chat_signups')
    elif action == 'edit':
        error = _apply_edit(request, user)
        if error:
            flash.error(request, error)
        else:
            flash.success(request, f'{user.name}\'s details are saved.')
    return redirect(back)


def _apply_edit(request, user):
    data = request.POST
    name = data.get('name', '').strip()[:120]
    email = data.get('email', '').strip().lower()[:254]
    phone = _PHONE_RE.match(re.sub(r'[\s-]', '', data.get('phone', '').strip()))
    state, city = data.get('state', '').strip(), data.get('city', '').strip()
    if len(name) < 2:
        return 'Please enter a full name.'
    try:
        validate_email(email)
    except ValidationError:
        return 'Please enter a valid email address.'
    if ChatUser.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
        return 'Another account already uses that email.'
    if not phone:
        return 'Please enter a valid 10-digit Indian mobile number.'
    if state not in INDIA_LOCATIONS or city not in INDIA_LOCATIONS[state]:
        return 'Please choose a state and a city from the lists.'
    new = data.get('new_password', '')
    if new and len(new) < MIN_PASSWORD:
        return f'The new password needs at least {MIN_PASSWORD} characters.'
    user.name, user.email, user.phone, user.state, user.city = name, email, phone.group(1), state, city
    if new:
        user.password = make_password(new)
    user.save()
    return ''


def dashboard_chat_status(request):
    """Who is on /chats right now, refreshed live by the page."""
    users = list(ChatUser.objects.filter(last_seen_at__isnull=False).order_by('-last_seen_at')[:100])
    rows = []
    for user in users:
        presence = _presence(user.last_seen_at)
        call = cache.get(call_key(user.pk))
        rows.append({
            'id': user.pk, 'name': user.name, 'email': '' if user.is_guest else user.email,
            'place': 'Not signed up' if user.is_guest else f'{user.city}, {user.state}',
            'online': presence['online'] and user.is_active, 'last_seen': presence['last_seen'],
            'active': user.is_active, 'guest': user.is_guest, 'in_call': bool(call and call['status'] in ('ringing', 'active')),
            'chat': reverse('dashboard_chat_thread', args=[user.pk]),
            'photo': reverse('chats_user_photo', args=[user.pk]) if user.photo else '',
        })
    online = sum(1 for r in rows if r['online'])
    if request.GET.get('format') == 'json':
        return JsonResponse({'ok': True, 'online': online, 'users': rows,
                             'support_online': _presence(cache.get(STAFF_SEEN_KEY))['online']})
    return render(request, 'dashboard/chat_status.html', {
        'active': 'chat_status', 'rows': rows, 'online': online, 'total': ChatUser.objects.count(),
    })


# ── IP addresses ─────────────────────────────────────────────────────────────

def _chats_per_ip():
    """{ip: number of chats started from it}"""
    return dict(ChatUser.objects.exclude(ip='').values('ip').annotate(n=Count('id')).values_list('ip', 'n'))


def dashboard_chat_ips(request):
    q = request.GET.get('q', '').strip()
    users = ChatUser.objects.exclude(ip='').annotate(total=Count('messages'), last_at=Max('messages__created_at'))
    if q:
        users = users.filter(Q(ip__icontains=q) | Q(last_ip__icontains=q) | Q(name__icontains=q) | Q(email__icontains=q))
    groups = {}
    for user in users.order_by('-created_at'):
        user.presence = _presence(user.last_seen_at)
        group = groups.setdefault(user.ip, {'ip': user.ip, 'chats': [], 'guests': 0, 'accounts': 0,
                                            'messages': 0, 'online': 0, 'last': None})
        group['chats'].append(user)
        group['guests' if user.is_guest else 'accounts'] += 1
        group['messages'] += user.total
        group['online'] += 1 if user.presence['online'] and user.is_active else 0
        moment = user.last_seen_at or user.created_at
        if not group['last'] or moment > group['last']:
            group['last'] = moment
    rows = sorted(groups.values(), key=lambda g: (-len(g['chats']), -(g['last'].timestamp() if g['last'] else 0)))
    stats = {
        'ips': len(rows), 'chats': sum(len(g['chats']) for g in rows),
        'shared': sum(1 for g in rows if len(g['chats']) > 1),
        'no_ip': ChatUser.objects.filter(ip='').count(),
    }
    return render(request, 'dashboard/chat_ips.html', {'active': 'chat_ips', 'rows': rows, 'stats': stats, 'q': q})
