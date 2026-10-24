"""Join by link: superusers make invite links on the AI page; whoever opens
one creates an account (or signs in to theirs), picks a premium plan, and is
let straight into the AI.

    /join/<token>/                           the welcome page; POST joins
    /join/<token>/check/                     POST: is this email new?
    /AI/api/join-links/                      superuser: list (GET), make (POST)
    /AI/api/join-links/<id>/remove/          superuser: POST
    /store/dashboard/join-links/             Dashboard → Joined by Link
    /store/dashboard/join-links/<id>/remove/ POST
"""
import datetime
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from myapp.models import JoinLink, JoinLinkSignup, StoreProfile
from myapp.views import _merge_session_ai_chats_into_user, _parse_json_body, dashboard_staff_required

# key -> (label, days, price in ₹). The join page shows only the label; the
# price is for the admin panel, so staff know what each joiner owes.
PLANS = {
    '31d': ('31 Days', 31, 125),
    '2m': ('2 Months', 60, 199),
    '3m': ('3 Months', 90, 250),
    '6m': ('6 Months', 180, 320),
    '1y': ('1 Year', 365, 520),
}
MIN_PASSWORD_LENGTH = 6
MAX_USES_LIMIT = 10000

STATUS_LABELS = {'active': 'Active', 'expired': 'Expired', 'full': 'Limit reached', 'removed': 'Removed'}
CLOSED_MESSAGES = {
    'expired': 'This invite link has expired. Ask the person who sent it for a new one.',
    'full': 'This invite link has already been used by as many people as it allows. Ask the person who sent it for a new one.',
    'removed': 'This invite link is no longer active. Ask the person who sent it for a new one.',
}


def _error(detail, status=400):
    return JsonResponse({'status': 'error', 'detail': detail}, status=status)


# ---- superuser tools on the AI page ----------------------------------------

def _link_payload(request, link):
    used = link.used if hasattr(link, 'used') else link.signups.count()
    status = link.status(used)
    return {
        'id': link.pk,
        'url': request.build_absolute_uri(reverse('join_link', args=[link.token])),
        'label': link.label,
        'created_at': link.created_at.isoformat(),
        'expires_at': link.expires_at.isoformat() if link.expires_at else None,
        'max_uses': link.max_uses,
        'used': used,
        'status': status,
        'status_label': STATUS_LABELS[status],
    }


def _parse_expiry(value):
    """'YYYY-MM-DD' -> the end of that day, local time. '' -> None."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        raise ValidationError('Choose a valid expiry date.')
    if day < timezone.localdate():
        raise ValidationError('The expiry date cannot be in the past.')
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(23, 59, 59)))


def _parse_max_uses(value):
    value = str(value if value is not None else '').strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        raise ValidationError('Enter how many people can join as a number.')
    if not 1 <= number <= MAX_USES_LIMIT:
        raise ValidationError(f'The number of people must be between 1 and {MAX_USES_LIMIT}.')
    return number


def api_links(request):
    """GET: the links that are not removed. POST: make a new one."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return _error('Only the site owner can manage join links.', 403)
    if request.method == 'POST':
        data = _parse_json_body(request)
        try:
            expires_at = _parse_expiry(data.get('expires_on'))
            max_uses = _parse_max_uses(data.get('max_uses'))
        except ValidationError as exc:
            return _error(exc.messages[0])
        link = JoinLink.objects.create(
            label=str(data.get('label') or '').strip()[:80],
            expires_at=expires_at, max_uses=max_uses, created_by=request.user,
        )
        return JsonResponse({'status': 'ok', 'link': _link_payload(request, link)})
    if request.method != 'GET':
        return _error('Invalid request method.', 405)
    links = JoinLink.objects.filter(removed_at__isnull=True).annotate(used=Count('signups'))
    return JsonResponse({'status': 'ok', 'links': [_link_payload(request, link) for link in links]})


def api_link_remove(request, pk):
    if not (request.user.is_authenticated and request.user.is_superuser):
        return _error('Only the site owner can manage join links.', 403)
    if request.method != 'POST':
        return _error('Invalid request method.', 405)
    updated = JoinLink.objects.filter(pk=pk, removed_at__isnull=True).update(removed_at=timezone.now())
    if not updated:
        return _error('That link was already removed.', 404)
    return JsonResponse({'status': 'ok'})


# ---- the public join page ---------------------------------------------------

def _open_link(token):
    """(link, status). A token that matches nothing counts as removed."""
    link = JoinLink.objects.filter(token=token).first()
    if link is None:
        return None, 'removed'
    return link, link.status()


def _find_account(email):
    return User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).first()


def _clean_email(value):
    email = str(value or '').strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise ValidationError('Enter a valid email address, like name@gmail.com.')
    return email


def join_page(request, token):
    if request.method == 'POST':
        return _join(request, token)
    link, status = _open_link(token)
    return render(request, 'join.html', {
        'token': token,
        'link_open': status == 'active',
        'closed_message': CLOSED_MESSAGES.get(status, ''),
        'plans': [{'key': key, 'label': label, 'days': days} for key, (label, days, _price) in PLANS.items()],
        'min_password_length': MIN_PASSWORD_LENGTH,
    }, status=200 if status == 'active' else 404)


def join_check_email(request, token):
    """Step 1: tells the page whether to ask for a new password or the existing one."""
    if request.method != 'POST':
        return _error('Invalid request method.', 405)
    link, status = _open_link(token)
    if status != 'active':
        return _error(CLOSED_MESSAGES[status], 410)
    try:
        email = _clean_email(_parse_json_body(request).get('email'))
    except ValidationError as exc:
        return _error(exc.messages[0])
    account = _find_account(email)
    if account and link.signups.filter(user=account).exists():
        return _error('This account has already joined with this link. Log in on the home page instead.')
    return JsonResponse({'status': 'ok', 'exists': bool(account)})


def _join(request, token):
    data = _parse_json_body(request)
    try:
        email = _clean_email(data.get('email'))
    except ValidationError as exc:
        return _error(exc.messages[0])
    password = str(data.get('password') or '')
    plan = PLANS.get(str(data.get('plan') or ''))
    if plan is None:
        return _error('Choose a plan.')
    plan_label, plan_days, _price = plan

    with transaction.atomic():
        link, status = _open_link(token)
        if status != 'active':
            return _error(CLOSED_MESSAGES[status], 410)
        account = _find_account(email)
        if account:
            if link.signups.filter(user=account).exists():
                return _error('This account has already joined with this link. Log in on the home page instead.')
            user = authenticate(request, username=account.username, password=password)
            if user is None:
                return _error('That password is not right for this account. Try again.')
        else:
            if len(password) < MIN_PASSWORD_LENGTH:
                return _error(f'Your password needs at least {MIN_PASSWORD_LENGTH} characters.')
            User.objects.create_user(username=email, email=email, password=password)
            user = authenticate(request, username=email, password=password)

        profile, _ = StoreProfile.objects.get_or_create(user=user)
        now = timezone.now()
        if profile.is_ai_subscribed:
            base = profile.ai_subscription_until  # add the new plan on top of the time left
        else:
            base = now
            profile.ai_subscription_started_at = now
        profile.ai_subscription_until = base + timedelta(days=plan_days)
        profile.ai_free_messages_used = 0
        profile.save(update_fields=['ai_subscription_started_at', 'ai_subscription_until', 'ai_free_messages_used'])
        JoinLinkSignup.objects.create(
            link=link, user=user, email=user.email or email, plan=data['plan'], plan_days=plan_days,
            new_account=account is None, premium_until=profile.ai_subscription_until,
        )

    if not request.session.session_key:
        request.session.create()
    guest_session_key = request.session.session_key
    login(request, user)
    _merge_session_ai_chats_into_user(user, guest_session_key)
    return JsonResponse({
        'status': 'ok',
        'new_account': account is None,
        'plan_label': plan_label,
        'premium_until': f'{timezone.localtime(profile.ai_subscription_until):%d %b %Y}',
        'redirect': '/',
    })


# ---- Dashboard → Joined by Link ---------------------------------------------

@dashboard_staff_required
def dashboard_links(request):
    links = list(
        JoinLink.objects.select_related('created_by').annotate(used=Count('signups'))
        .prefetch_related('signups__user')
    )
    for link in links:
        link.status_key = link.status(link.used)
        link.status_label = STATUS_LABELS[link.status_key]
        link.url = request.build_absolute_uri(reverse('join_link', args=[link.token]))
        for signup in link.signups.all():
            label, _days, price = PLANS.get(signup.plan, (f'{signup.plan_days} days', signup.plan_days, None))
            signup.plan_label, signup.plan_price = label, price
    return render(request, 'dashboard/join_links.html', {
        'active': 'join_links',
        'links': links,
        'active_count': sum(1 for link in links if link.status_key == 'active'),
        'joined_count': sum(link.used for link in links),
        'new_account_count': JoinLinkSignup.objects.filter(new_account=True).count(),
    })


@dashboard_staff_required
def dashboard_link_remove(request, pk):
    if request.method == 'POST':
        link = get_object_or_404(JoinLink, pk=pk)
        if link.removed_at is None:
            link.removed_at = timezone.now()
            link.save(update_fields=['removed_at'])
            messages.success(request, 'Link removed. It no longer works, and the people who joined with it are listed below as before.')
    return redirect('dashboard_join_links')
