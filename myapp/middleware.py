import logging
import traceback

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import render

from .devices import register_device, touch_device

logger = logging.getLogger(__name__)


class FriendlyErrorMiddleware:
    """DEBUG is on in production, and with DEBUG on Django answers a missing
    page or a crash with its developer page: the error, the code location,
    every URL of the site, and on a crash parts of the settings. Visitors get
    the site's own error page instead (a JSON error for the chat's API calls);
    the full error still goes to the server log. With DEBUG off Django already
    shows 404.html / 500.html, so this steps aside.

    process_exception covers errors raised by a view. __call__ is the safety
    net for the rarer error raised inside another middleware, recognised by
    the robots meta tag Django's developer pages carry, so this must stay the
    first entry in MIDDLEWARE."""

    DEBUG_PAGE_MARKER = b'<meta name="robots" content="NONE,NOARCHIVE">'

    def __init__(self, get_response):
        if not settings.DEBUG:
            raise MiddlewareNotUsed
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (response.status_code >= 400 and not response.streaming
                and response.get('Content-Type', '').startswith('text/html')
                and self.DEBUG_PAGE_MARKER in response.content):
            return self._error_response(request, response.status_code)
        return response

    def process_exception(self, request, exception):
        if isinstance(exception, Http404):
            return self._error_response(request, 404)
        logger.error('Error on %s %s', request.method, request.path, exc_info=exception)
        return self._error_response(request, 500, exception)

    @staticmethod
    def _error_response(request, status, exception=None):
        wants_page = 'text/html' in request.headers.get('Accept', '')
        if '/api/' in request.path and not wants_page:
            detail = 'Not found.' if status == 404 else 'Something went wrong. Please try again.'
            return JsonResponse({'status': 'error', 'detail': detail}, status=status)
        context = {}
        user = getattr(request, 'user', None)
        if exception is not None and user is not None and user.is_superuser:
            # Only the site owner sees what broke, so a problem can be fixed
            # without reading the server log. Visitors never get this.
            context['error_detail'] = ''.join(traceback.format_exception(exception))[-4000:]
        template = '404.html' if status == 404 else '500.html'
        if status == 403:
            # Usually a form sent from a page left open too long (CSRF).
            template = 'error.html'
            context.update(error_code='403', error_title="That didn't go through",
                           error_message='This page may have been open for too long. Reload it and try again.',
                           error_retry=True)
        try:
            return render(request, template, context, status=status)
        except Exception:
            # The page itself could not be built (say the database is down):
            # a bare message still beats Django's developer page.
            logger.exception('Could not render the error page')
            return HttpResponse('Something went wrong. Please try again in a moment.',
                                content_type='text/plain; charset=utf-8', status=status)


class AutoBackupMiddleware:
    """Starts the 24-hour automatic Dropbox backup timer (see
    dropbox_backup.start_scheduler) when the web server loads, then takes
    itself out of the request path. Running it here rather than in
    AppConfig.ready() keeps it out of migrate, collectstatic and other
    management commands, which never load the middleware."""

    def __init__(self, get_response):
        from myapp import dropbox_backup
        dropbox_backup.start_scheduler()
        raise MiddlewareNotUsed


class SiteDisabledMiddleware:
    """The dashboard Customize page's "Disable site" checkbox (see
    SiteCustomization.site_disabled) — every visitor gets a bare 404 instead
    of the real site. The dashboard and Django admin stay reachable
    (by path, not by login state) so staff can always get back in and
    switch it off again, even logged out, without touching the database
    directly. Static/media stay reachable too so the dashboard's own login
    page still renders correctly while the rest of the site is down."""

    EXEMPT_PREFIXES = ('/store/dashboard/', '/admin/', '/static/', '/media/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.EXEMPT_PREFIXES):
            return self.get_response(request)
        from myapp.models import SiteCustomization
        try:
            disabled = SiteCustomization.get_solo().site_disabled
        except Exception:
            disabled = False
        if disabled:
            return render(request, 'site_disabled.html', status=404)
        return self.get_response(request)


class CustomModelsMiddleware:
    """Keeps models added on the dashboard (API Settings → Add a new model)
    in ai_chat.MODELS for the pages and APIs that read it. Usually just a
    cache read — see myapp.custom_models.sync."""

    PREFIXES = ('/AI/', '/api/', '/store/dashboard/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.PREFIXES):
            from myapp import ai_chat, custom_models
            custom_models.sync()
            ai_chat.apply_model_id_overrides()
        return self.get_response(request)


class CanonicalHostMiddleware:
    """Consolidate the bare domain onto the canonical www hostname."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.get_host().partition(':')[0].lower() == 'edutrellis.in':
            return HttpResponsePermanentRedirect(
                f'https://www.edutrellis.in{request.get_full_path()}'
            )
        return self.get_response(request)


class HideAdminFromNonStaffMiddleware:
    """Django's built-in admin login view tells an authenticated non-staff
    user "You are authenticated as X, but are not authorized to access this
    page" — which both confirms /admin/ exists and echoes their email back
    to them. Anyone logged in but not staff/superuser (e.g. an
    AI-premium-only account) should see the same 404 as any unknown URL
    instead, same as an anonymous visitor poking at random paths."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/admin/') and request.user.is_authenticated and not (request.user.is_staff or request.user.is_superuser):
            from myapp.views import custom_404
            return custom_404(request)
        return self.get_response(request)


class SingleDeviceSessionMiddleware:
    """Keeps each logged-in browser listed as one of its account's devices
    (see myapp.devices). A device logged out from elsewhere has lost its
    session, so it never gets here as a logged-in user."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            if not request.session.session_key:
                request.session.save()
                register_device(request.user, request.session.session_key, request)
            elif not touch_device(request.user, request.session.session_key):
                # Logged in before devices were listed, or its session key
                # changed since (e.g. after a password change).
                register_device(request.user, request.session.session_key, request)

        return self.get_response(request)


class PublicAssetCacheMiddleware:
    """Give version-stable static/media files a useful browser cache.

    This middleware sits before WhiteNoise so it also wraps files served
    directly by WhiteNoise rather than only normal Django view responses.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code == 200:
            if request.path.startswith('/static/'):
                response['Cache-Control'] = 'public, max-age=86400, stale-while-revalidate=604800'
            elif request.path.startswith((
                '/media/products/', '/media/categories/', '/media/about/', '/media/pwa/',
            )):
                response['Cache-Control'] = 'public, max-age=604800, stale-while-revalidate=2592000'
        return response
