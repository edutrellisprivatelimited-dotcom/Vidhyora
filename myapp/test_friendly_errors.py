"""Visitors never see Django's developer error pages, even though DEBUG is on."""
import uuid

from django.core.exceptions import MiddlewareNotUsed
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import path

from myapp.middleware import FriendlyErrorMiddleware


def _crash(request):
    raise RuntimeError('secret detail that must not reach the visitor')


def _missing(request):
    raise Http404('No Thing matches the given query.')


urlpatterns = [
    path('crash/', _crash),
    path('AI/api/crash/', _crash),
    path('missing/', _missing),
]

BROWSER = {'HTTP_ACCEPT': 'text/html,application/xhtml+xml,*/*;q=0.8'}
DEVELOPER_PAGE_TEXT = ('Traceback', 'URLconf', 'Raised by', 'secret detail', 'matches the given query')


@override_settings(DEBUG=True, ROOT_URLCONF='myapp.test_friendly_errors')
class FriendlyErrorPageTests(TestCase):
    def assertNoDeveloperDetails(self, response):
        body = response.content.decode()
        for text in DEVELOPER_PAGE_TEXT:
            self.assertNotIn(text, body)

    def test_a_crash_shows_the_try_again_page(self):
        with self.assertLogs('myapp.middleware', 'ERROR') as logs:
            response = self.client.get('/crash/', **BROWSER)
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, 'Something went wrong', status_code=500)
        self.assertContains(response, 'Try again', status_code=500)
        self.assertNoDeveloperDetails(response)
        self.assertIn('secret detail', '\n'.join(logs.output))  # still in the server log

    def test_a_crash_in_an_api_call_answers_with_json(self):
        with self.assertLogs('myapp.middleware', 'ERROR'):
            response = self.client.get('/AI/api/crash/', HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {'status': 'error', 'detail': 'Something went wrong. Please try again.'})

    def test_a_missing_item_shows_the_not_found_page(self):
        response = self.client.get('/missing/', **BROWSER)
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'Page Not Found', status_code=404)
        self.assertNoDeveloperDetails(response)

    def test_a_developer_page_made_outside_a_view_is_replaced_too(self):
        # This test URL list has no catch-all, so Django builds its own
        # "page not found" developer page before any view runs.
        response = self.client.get('/no-such-page/', **BROWSER)
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'Page Not Found', status_code=404)
        self.assertNoDeveloperDetails(response)

    def test_it_steps_aside_when_debug_is_off(self):
        with override_settings(DEBUG=False), self.assertRaises(MiddlewareNotUsed):
            FriendlyErrorMiddleware(lambda request: None)


@override_settings(DEBUG=True)
class SharedFileLinkTests(TestCase):
    def test_a_file_link_opened_elsewhere_explains_why(self):
        response = self.client.get(f'/AI/api/files/{uuid.uuid4()}/download/', **BROWSER)
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'File not available here', status_code=404)
        self.assertContains(response, 'account that created it', status_code=404)
        self.assertNotContains(response, 'AIGeneratedFile', status_code=404)
