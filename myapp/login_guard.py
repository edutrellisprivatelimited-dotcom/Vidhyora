"""A brake on password guessing, for every place a password is checked: the
AI page's login, join links and Django's /admin/ login.

Failed attempts are counted per IP address and per account for WINDOW
seconds. Past either limit every attempt is refused, even with the right
password, until the window ends. A successful login clears the account's
count but not the IP's, so logging in to your own account between guesses
does not reset the brake.

Failures are counted from Django's user_login_failed signal, which every
authenticate() call sends; callers that reject a login without calling
authenticate() (an unknown email) call record_failure() themselves.
"""
import time

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.cache import cache
from django.dispatch import receiver

from myapp.devices import client_ip

WINDOW = 15 * 60
MAX_FAILURES_PER_IP = 20
MAX_FAILURES_PER_ACCOUNT = 8


def _account_key(username):
    username = str(username or '').strip().lower()
    return f'login_fail:acct:{username}' if username else ''


def _counters(request, username):
    counters = []
    ip = client_ip(request) if request is not None else ''
    if ip:
        counters.append((f'login_fail:ip:{ip}', MAX_FAILURES_PER_IP))
    account_key = _account_key(username)
    if account_key:
        counters.append((account_key, MAX_FAILURES_PER_ACCOUNT))
    return counters


def wait_seconds(request, username):
    """0 when a login attempt may go ahead, else how many seconds are left."""
    now = time.time()
    wait = 0
    for key, limit in _counters(request, username):
        window = cache.get(key)
        if window and window['count'] >= limit and now < window['reset_at']:
            wait = max(wait, window['reset_at'] - now)
    return int(-(-wait // 1))


def record_failure(request, username):
    now = time.time()
    for key, _limit in _counters(request, username):
        window = cache.get(key)
        if not window or now >= window['reset_at']:
            window = {'count': 0, 'reset_at': now + WINDOW}
        window['count'] += 1
        cache.set(key, window, WINDOW)


@receiver(user_login_failed)
def _count_failed_login(sender, credentials, request=None, **kwargs):
    record_failure(request, credentials.get('username'))


@receiver(user_logged_in)
def _clear_account_failures(sender, request, user, **kwargs):
    account_key = _account_key(user.get_username())
    if account_key:
        cache.delete(account_key)


class AdminLoginForm(AdminAuthenticationForm):
    """Django's admin login with the same brake (see edutrellis/urls.py)."""

    def clean(self):
        if wait_seconds(self.request, self.cleaned_data.get('username')):
            raise forms.ValidationError(
                'Too many wrong passwords. Please wait 15 minutes and try again.',
                code='too_many_attempts',
            )
        return super().clean()
