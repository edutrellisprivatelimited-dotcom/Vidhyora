import posixpath
import re

from django.conf import settings
from django.contrib import admin
from django.http import Http404
from django.urls import path, re_path, include
from django.contrib.sitemaps.views import sitemap
from django.views.generic import TemplateView, RedirectView
from django.views.static import serve

from myapp.login_guard import AdminLoginForm
from myapp.sitemaps import StaticViewSitemap
from myapp.views import custom_404

admin.site.login_form = AdminLoginForm

sitemaps = {
    'static': StaticViewSitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),

    # Private shortcut to the Django admin login — not linked anywhere on the
    # site, just a memorable URL for direct access.
    path('welcomernt/', RedirectView.as_view(url='/admin/', permanent=False), name='welcomernt_admin'),

    # Main App
    path('', include('myapp.urls')),

    # Sitemap
    path(
        'sitemap.xml',
        sitemap,
        {'sitemaps': sitemaps},
        name='django.contrib.sitemaps.views.sitemap'
    ),

    # Robots
    path(
        'robots.txt',
        TemplateView.as_view(
            template_name='robots.txt',
            content_type='text/plain'
        ),
    ),
]

# Only the media folders that hold public pictures/videos are served, with or
# without DEBUG. Everything else under media/ (call recordings in ai_calls/,
# YouTube downloads, …) is private and only reachable through views that check
# who is asking (e.g. /AI/api/calls/<id>/audio/).
PUBLIC_MEDIA_FOLDERS = (
    'avatars', 'pwa', 'branding', 'products', 'categories', 'about', 'ai_generated', 'ai_videos',
)


def public_media(request, path):
    # Checked again after resolving "..", so avatars/../ai_calls/… can't
    # climb from a public folder into a private one.
    path = posixpath.normpath(path).lstrip('/')
    if '\\' in path or path.split('/', 1)[0] not in PUBLIC_MEDIA_FOLDERS:
        raise Http404
    return serve(request, path, document_root=settings.MEDIA_ROOT)


urlpatterns += [
    re_path(
        r'^%s(?P<path>(?:%s)/.+)$' % (re.escape(settings.MEDIA_URL.lstrip('/')), '|'.join(PUBLIC_MEDIA_FOLDERS)),
        public_media,
    ),
]

# Catch-all: renders the custom 404 template for any unmatched URL. Django only
# consults handler404 when DEBUG=False, so with DEBUG=True it would otherwise
# show the technical debug page instead — this pattern bypasses that by
# matching (and rendering the 404 template) directly instead of raising.
urlpatterns += [re_path(r'^.*$', custom_404)]

handler404 = 'myapp.views.custom_404'
