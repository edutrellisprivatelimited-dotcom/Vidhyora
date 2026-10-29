"""The developer API's pictures, reply language and usage numbers (the
access rules and limits are in test_security)."""
import base64
import io
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from PIL import Image

from myapp import ai_chat
from myapp.models import AIAPIAccess, AIAPIKey, AIAPIRequest, AIUserImage, StoreProfile
from myapp.views import AI_IMAGE_DAILY_LIMIT


def png_bytes(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (64, 64), color).save(out, format='PNG')
    return out.getvalue()


def data_uri(content=None):
    return 'data:image/png;base64,' + base64.b64encode(content or png_bytes()).decode()


class DeveloperApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user('maker@example.com', email='maker@example.com', password='pw-123456', is_staff=True)
        StoreProfile.objects.create(user=self.user)
        AIAPIAccess.objects.create(user=self.user, model_keys='quick')
        self.raw_key = AIAPIKey.generate_for(self.user)
        enabled = patch('myapp.views.ai_chat.is_model_enabled', return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def post(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type='application/json',
                                HTTP_AUTHORIZATION=f'Bearer {self.raw_key}')


@patch('myapp.views.image_ocr.extract_data_uri', return_value='HELLO 42')
@patch('myapp.views.ai_chat.stream_chat', return_value=iter(['It says hello.']))
class ChatWithPictureTests(DeveloperApiTestCase):
    URL = '/api/v1/chat/'

    def test_a_picture_goes_to_the_vision_model_answering_as_the_chosen_one(self, stream, ocr):
        response = self.post(self.URL, {'model': 'quick', 'message': 'What does it say?', 'image': data_uri()})
        self.assertEqual(response.status_code, 200, response.content)
        messages = stream.call_args.args[0]
        self.assertEqual(stream.call_args.kwargs['model_key'], 'vision')
        self.assertEqual(stream.call_args.kwargs['identity_model_key'], 'quick')
        blocks = messages[-1]['content']
        self.assertIn('What does it say?', blocks[0]['text'])
        self.assertIn('HELLO 42', blocks[0]['text'])
        self.assertTrue(blocks[1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertTrue(AIAPIRequest.objects.get(user=self.user).with_image)

    def test_text_only_requests_keep_the_chosen_model(self, stream, ocr):
        self.post(self.URL, {'model': 'quick', 'message': 'Hi'})
        self.assertEqual(stream.call_args.kwargs['model_key'], 'quick')
        self.assertIsNone(stream.call_args.kwargs['identity_model_key'])
        ocr.assert_not_called()

    def test_a_web_address_is_not_accepted_as_a_picture(self, stream, ocr):
        response = self.post(self.URL, {'model': 'quick', 'message': 'Hi', 'image': 'http://169.254.169.254/latest'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_request')
        self.assertIn('Web addresses are not accepted', response.json()['error'])
        stream.assert_not_called()

    def test_broken_base64_is_refused(self, stream, ocr):
        response = self.post(self.URL, {'model': 'quick', 'message': 'Hi', 'image': 'data:image/png;base64,abc'})
        self.assertEqual(response.status_code, 400)

    def test_reply_language_is_passed_on(self, stream, ocr):
        self.post(self.URL, {'model': 'quick', 'message': 'Hi', 'language': 'hi'})
        self.assertEqual(stream.call_args.kwargs['language'], 'hi')

    def test_an_unknown_language_lists_the_codes(self, stream, ocr):
        response = self.post(self.URL, {'model': 'quick', 'message': 'Hi', 'language': 'klingon'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('ta', response.json()['error'])


class PictureEndpointTests(DeveloperApiTestCase):
    URL = '/api/v1/images/'

    def setUp(self):
        super().setUp()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)

    @patch('myapp.views.poster.make_poster', return_value=None)
    @patch('myapp.views.image_generation.generate_image')
    def test_a_prompt_makes_a_picture_in_my_images(self, generate, make_poster):
        generate.return_value = SimpleNamespace(content=png_bytes('blue'), extension='png')
        response = self.post(self.URL, {'prompt': 'A blue square'})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['image_url'].startswith('http://testserver/AI/images/'))
        image = self.client.get(body['image_url'].replace('http://testserver', ''))
        self.assertEqual(image.status_code, 200)
        self.assertTrue(AIUserImage.objects.filter(user=self.user, prompt='A blue square').exists())
        row = AIAPIRequest.objects.get(user=self.user)
        self.assertEqual((row.kind, row.ok), ('image', True))

    @patch('myapp.views.image_edit.redraw')
    def test_a_picture_with_a_prompt_is_redrawn(self, redraw):
        redraw.return_value = SimpleNamespace(
            image=SimpleNamespace(content=png_bytes('green'), extension='png'), brief='a green square', change='Made it green.',
        )
        response = self.post(self.URL, {'prompt': 'make it green', 'image': data_uri()})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['note'], 'Made it green.')
        self.assertEqual(redraw.call_args.args[1], 'make it green')

    def test_a_prompt_is_required(self):
        self.assertEqual(self.post(self.URL, {}).status_code, 400)

    @patch('myapp.views.image_generation.generate_image')
    def test_the_daily_picture_limit_counts_the_chat_page_too(self, generate):
        AIUserImage.objects.bulk_create([AIUserImage(user=self.user, url=f'/x/{n}') for n in range(AI_IMAGE_DAILY_LIMIT)])
        response = self.post(self.URL, {'prompt': 'One more'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['code'], 'daily_image_limit')
        generate.assert_not_called()

    @patch('myapp.views.poster.make_poster', return_value=None)
    @patch('myapp.views.image_generation.generate_image')
    def test_a_vendor_error_is_never_shown(self, generate, make_poster):
        from myapp.image_generation import ImageGenerationError
        generate.side_effect = ImageGenerationError('NVIDIA FLUX endpoint returned 500', status_code=503)
        response = self.post(self.URL, {'prompt': 'A cat'})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('NVIDIA', response.json()['error'])
        self.assertFalse(AIAPIRequest.objects.get(user=self.user).ok)


@patch('myapp.views.ai_chat.stream_chat', return_value=iter(['Hello']))
class UsageNumbersTests(DeveloperApiTestCase):
    def test_the_account_panel_shows_usage_models_and_limits(self, stream):
        self.post('/api/v1/chat/', {'model': 'quick', 'message': 'Hi'})
        self.client.force_login(self.user)
        api = self.client.get('/AI/api/account/').json()['api_access']
        self.assertEqual(api['usage']['today'], 1)
        self.assertEqual(api['usage']['total'], 1)
        self.assertEqual(api['usage']['last_7_days'][-1]['count'], 1)
        self.assertEqual(len(api['usage']['last_7_days']), 7)
        self.assertEqual(api['usage']['by_model'][0]['key'], 'quick')
        self.assertTrue(api['usage']['recent'][0]['ok'])
        self.assertEqual(api['models'][0]['key'], 'quick')
        self.assertEqual(api['models'][0]['label'], ai_chat.MODELS['quick']['label'])
        self.assertIn('per_minute', api['limits'])
        self.assertTrue(any(lang['code'] == 'hi' for lang in api['languages']))
        self.assertEqual(api['key_prefix'], self.raw_key[:11])
        self.assertNotIn(self.raw_key, json.dumps(api))

    def test_usage_survives_regenerating_the_key(self, stream):
        self.post('/api/v1/chat/', {'model': 'quick', 'message': 'Hi'})
        AIAPIKey.generate_for(self.user)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/AI/api/account/').json()['api_access']['usage']['total'], 1)
