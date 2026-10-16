"""Image and video models added on API Settings, video generation in chat,
counters starting from zero after a new key, and Tavily's limit codes."""
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from PIL import Image

from myapp import ai_chat, image_generation, media_models, model_controls, video_generation, web_search
from myapp.models import AIMessage, AIModelControl, AIVideoJob, CustomAIModel, ProviderAPICredential, StoreProfile

SETTINGS_URL = '/store/dashboard/api-settings/'


def _png():
    buffer = io.BytesIO()
    Image.new('RGB', (16, 16), (200, 30, 30)).save(buffer, format='PNG')
    return buffer.getvalue()


MP4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64


def _response(status, payload=None, text='', headers=None):
    return SimpleNamespace(status_code=status, json=lambda: payload, text=text or json.dumps(payload or {}),
                           headers=headers or {})


def _media_row(kind, active=True, **extra):
    row = CustomAIModel.objects.create(
        kind=kind, model_id=extra.pop('model_id', f'acme/{kind}-model'), display_name=f'Acme {kind}',
        endpoint_url=extra.pop('endpoint_url', f'https://ai.api.nvidia.com/v1/genai/acme/{kind}-model'),
        request_style=extra.pop('request_style', 'plain'), is_active=active, **extra,
    )
    ProviderAPICredential.objects.create(setting_name=row.key_setting, value='nvapi-media')
    return row


class MediaModelHelpersTests(TestCase):
    def test_the_kind_is_read_from_the_model_name(self):
        self.assertEqual(media_models.guess_kind('black-forest-labs/flux-3-image-klein'), 'image')
        self.assertEqual(media_models.guess_kind('nvidia/cosmos3-nano'), 'video')
        self.assertEqual(media_models.guess_kind('nvidia/cosmos3-nano-reasoner'), 'chat')
        self.assertEqual(media_models.guess_kind('meta/llama-3.3-70b-instruct'), 'chat')

    def test_page_names_with_underscores_are_tried_with_dots_too(self):
        urls = media_models.candidate_urls('black-forest-labs/flux_2-klein-4b', 'image')
        self.assertEqual(urls[1], 'https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b')
        self.assertTrue(media_models.candidate_urls('nvidia/cosmos3-nano', 'video')[0].startswith(media_models.COSMOS_URL))

    def test_a_video_comes_back_as_base64_or_a_link(self):
        self.assertEqual(media_models.extract_video({'b64_video': base64.b64encode(MP4).decode()}), (MP4, 'mp4'))
        with patch('myapp.media_models.requests.get', return_value=SimpleNamespace(content=MP4)) as get:
            self.assertEqual(media_models.extract_video({'video_url': 'https://cdn.example/v.mp4'}), (MP4, 'mp4'))
        self.assertNotIn('headers', get.call_args.kwargs)  # the API key never goes to a download link
        with self.assertRaises(media_models.MediaError):
            media_models.extract_video({'b64_video': base64.b64encode(b'not a video').decode()})

    @patch('myapp.media_models.time.sleep')
    @patch('myapp.media_models.requests.get')
    @patch('myapp.media_models.requests.post')
    def test_a_slow_job_is_polled_until_it_is_ready(self, post, get, _sleep):
        post.return_value = _response(202, {}, headers={'NVCF-REQID': 'req-1'})
        get.side_effect = [_response(202, {}, headers={'NVCF-REQID': 'req-1'}),
                           _response(200, {'b64_video': base64.b64encode(MP4).decode()})]
        content, extension = media_models.request_media('video', 'https://x/v', 'cosmos', 'nvapi-k', 'a cat')
        self.assertEqual((content, extension), (MP4, 'mp4'))
        self.assertEqual(get.call_args.args[0], media_models.STATUS_URL + 'req-1')
        self.assertEqual(post.call_args.kwargs['json']['model_mode'], 'text2video')

    @patch('myapp.media_models.requests.post')
    def test_checks_find_the_endpoint_and_the_request_shape_that_works(self, post):
        png = base64.b64encode(_png()).decode()

        def answer(url, json=None, headers=None, timeout=None):
            if not headers:  # the keyless probe for the address
                return _response(401 if '/genai/' in url and 'flux.2' in url else 404, text='404 page not found')
            if 'width' in json:
                return _response(422, {'detail': 'width not allowed'})
            return _response(200, {'artifacts': [{'base64': png, 'finishReason': 'SUCCESS'}]})
        post.side_effect = answer
        ok, report, found = media_models.run_checks('image', 'black-forest-labs/flux_2-klein-4b', 'nvapi-k')
        self.assertTrue(ok, report)
        self.assertEqual(found, {'endpoint_url': 'https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b',
                                 'request_style': 'plain'})

    @patch('myapp.media_models.requests.post', return_value=_response(404, text='404 page not found'))
    def test_a_model_without_a_hosted_endpoint_is_explained(self, _post):
        ok, report, _found = media_models.run_checks('video', 'nvidia/cosmos3-nano', 'nvapi-k')
        self.assertFalse(ok)
        self.assertIn('no hosted endpoint', report[-1]['detail'])

    def test_video_requests_are_told_apart_from_talk_about_videos(self):
        for text in ('make a video of a cat playing piano', 'ek video banao sunset ka', 'generate a short clip of rain'):
            self.assertTrue(ai_chat.is_video_generation_request(text), text)
        for text in ('can you make videos?', 'make notes from this video', 'how to make a video in canva',
                     'make a thumbnail for my youtube video', 'make an image of a cat'):
            self.assertFalse(ai_chat.is_video_generation_request(text), text)


class AddMediaModelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        staff = User.objects.create_user('media-staff', password='pw', is_staff=True)
        StoreProfile.objects.get_or_create(user=staff)
        self.client.force_login(staff)

    def _add(self, model_id, kind='auto'):
        found = {'endpoint_url': f'https://ai.api.nvidia.com/v1/genai/{model_id}', 'request_style': 'sized'}
        with patch('myapp.media_models.run_checks', return_value=(True, [], found)) as checks:
            response = self.client.post(SETTINGS_URL, {'action': 'add_model', 'api_type': 'nvidia', 'kind': kind,
                                                       'model_id': model_id, 'api_key': 'nvapi-new'})
        return response, checks

    def test_an_image_model_is_added_and_takes_over_from_the_previous_one(self):
        response, checks = self._add('black-forest-labs/flux-3-image-klein')
        self.assertEqual(checks.call_args.args[0], 'image')
        self.assertContains(response, 'now makes every picture')
        first = CustomAIModel.objects.get()
        self.assertEqual((first.kind, first.is_active, first.request_style), ('image', True, 'sized'))
        self.assertNotIn(first.model_key, ai_chat.MODELS)  # not a chat-picker entry
        self.assertEqual(media_models.active('image'), first)

        self._add('black-forest-labs/flux.1-schnell', kind='image')
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertEqual(media_models.active('image').model_id, 'black-forest-labs/flux.1-schnell')

    def test_media_models_from_other_providers_are_refused(self):
        response = self.client.post(SETTINGS_URL, {'action': 'add_model', 'api_type': 'openai', 'kind': 'video',
                                                   'base_url': 'https://api.example.com/v1',
                                                   'model_id': 'acme/video', 'api_key': 'sk-x'})
        self.assertContains(response, 'only be added from NVIDIA')
        self.assertFalse(CustomAIModel.objects.exists())

    @patch('myapp.ai_chat._client_for_key')
    def test_a_new_key_starts_the_numbers_from_zero(self, client_for_key):
        client_for_key.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='OK'))])
        AIModelControl.objects.create(model_key=ai_chat.TERRA_MODEL_KEY, request_count=50, success_count=40,
                                      error_count=10, last_error='old failure')
        response = self.client.post(SETTINGS_URL, {'panel': 'terra', 'action': 'save', 'enabled': 'on',
                                                   'key__NVIDIA_TERRA_API_KEY': 'nvapi-fresh'})
        self.assertContains(response, 'its numbers start from zero')
        row = AIModelControl.objects.get(model_key=ai_chat.TERRA_MODEL_KEY)
        self.assertEqual((row.request_count, row.success_count, row.error_count, row.last_error), (0, 0, 0, ''))
        self.assertTrue(row.last_test_ok)  # the new key's own test is kept


class ImageFromAddedModelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @patch('myapp.media_models.requests.post')
    def test_the_switched_on_image_model_makes_the_picture(self, post):
        row = _media_row('image', request_style='sized')
        post.return_value = _response(200, {'artifacts': [{'base64': base64.b64encode(_png()).decode()}]})
        image = image_generation.generate_image('a red apple')
        self.assertEqual(image.extension, 'png')
        self.assertEqual(post.call_args.args[0], row.endpoint_url)
        self.assertEqual(post.call_args.kwargs['json']['width'], 1024)
        self.assertEqual(AIModelControl.objects.get(model_key=row.model_key).success_count, 1)

    @override_settings(NVIDIA_FLUX_API_KEY='test-flux-key')
    @patch('myapp.media_models.requests.post')
    def test_flux_makes_the_picture_when_the_added_model_fails(self, post):
        row = _media_row('image')
        png = base64.b64encode(_png()).decode()
        post.side_effect = [_response(503, text='down'), _response(200, {'artifacts': [{'base64': png}]})]
        image = image_generation.generate_image('a red apple')
        self.assertEqual(image.extension, 'png')
        self.assertIn('flux', post.call_args.args[0])
        self.assertEqual(AIModelControl.objects.get(model_key=row.model_key).error_count, 1)


class VideoChatTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user('video-staff@example.com', password='pw', is_staff=True)
        self.client.force_login(self.user)
        self.row = _media_row('video', endpoint_url='https://ai.api.nvidia.com/v1/cosmos/acme/video-model',
                              request_style='cosmos')

    def _send(self, text):
        return self.client.post('/AI/api/send/', data=json.dumps({'message': text}), content_type='application/json')

    @patch('myapp.video_generation.start')
    def test_a_video_request_starts_a_job_and_the_page_can_follow_it(self, start):
        response = self._send('make a video of a cat playing piano')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['X-Request-Category'], 'video_generation')
        job = AIVideoJob.objects.get()
        start.assert_called_once_with(job.pk)
        self.assertEqual(response['X-Video-Job'], str(job.token))
        self.assertEqual((job.model_key, job.prompt), (self.row.model_key, 'make a video of a cat playing piano'))
        status = self.client.get(f'/AI/api/video/{job.token}/').json()
        self.assertEqual(status['status'], 'working')

        history = self.client.get(f'/AI/api/conversations/{job.message.conversation_id}/').json()['messages']
        self.assertEqual(history[-1]['video_job'], str(job.token))

        video_generation.finish(job, url='/media/ai_videos/clip.mp4')
        self.assertEqual(self.client.get(f'/AI/api/video/{job.token}/').json()['video_url'], '/media/ai_videos/clip.mp4')
        history = self.client.get(f'/AI/api/conversations/{job.message.conversation_id}/').json()['messages']
        self.assertEqual(history[-1]['video_url'], '/media/ai_videos/clip.mp4')
        self.assertNotIn('video_job', history[-1])

    @patch('myapp.video_generation.default_storage')
    @patch('myapp.media_models.requests.post')
    def test_the_background_job_saves_the_video_on_the_message(self, post, storage):
        post.return_value = _response(200, {'b64_video': base64.b64encode(MP4).decode()})
        storage.save.return_value = 'ai_videos/x.mp4'
        storage.url.return_value = '/media/ai_videos/x.mp4'
        with patch('myapp.video_generation.start'):
            self._send('make a video of waves on a beach')
        job = AIVideoJob.objects.get()
        with patch('myapp.video_generation.close_old_connections'):
            video_generation._run(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.video_url), ('ready', '/media/ai_videos/x.mp4'))
        self.assertEqual(job.message.video_url, '/media/ai_videos/x.mp4')
        self.assertEqual(post.call_args.kwargs['json']['num_frames'], 121)

    @patch('myapp.media_models.requests.post', return_value=_response(429, text='slow down'))
    def test_a_failed_video_says_so_in_plain_words(self, _post):
        with patch('myapp.video_generation.start'):
            self._send('make a video of a rocket launch')
        job = AIVideoJob.objects.get()
        with patch('myapp.video_generation.close_old_connections'):
            video_generation._run(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIn('busy', job.message.content)

    def test_without_a_video_model_it_is_an_ordinary_chat_turn(self):
        self.row.is_active = False
        self.row.save()
        with patch('myapp.ai_chat._stream_chat_impl', return_value=iter(['Sorry'])):
            response = self._send('make a video of a cat')
        self.assertNotIn('X-Video-Job', response)
        self.assertFalse(AIVideoJob.objects.exists())

    def test_the_chat_models_are_told_video_is_available(self):
        self.assertIn('video generation IS available', ai_chat.video_available_note())
        model_controls.set_enabled(self.row.model_key, False)
        self.assertEqual(ai_chat.video_available_note(), '')


class WebSourcesTests(TestCase):
    TAVILY = {'results': [
        {'title': 'Rain expected in Delhi', 'url': 'https://www.example-news.com/delhi-rain', 'content': 'Heavy rain today.',
         'favicon': 'https://www.example-news.com/favicon.ico', 'published_date': '2026-10-16'},
        {'title': 'Not a web link', 'url': 'ftp://files.example.com/x', 'content': 'x'},
    ]}

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(TAVILY_API_KEY='tvly-x')
    @patch('myapp.web_search.requests.post')
    def test_results_come_with_sources_for_the_page(self, post):
        post.return_value = SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.TAVILY)
        context, sources = web_search.build_context_with_sources('delhi weather today')
        self.assertTrue(post.call_args.kwargs['json']['include_favicon'])
        self.assertIn('example-news.com', context)
        self.assertEqual(sources, [{'title': 'Rain expected in Delhi', 'url': 'https://www.example-news.com/delhi-rain',
                                    'site': 'example-news.com', 'favicon': 'https://www.example-news.com/favicon.ico',
                                    'published': '2026-10-16'}])

    @patch('myapp.web_search.build_context_with_sources')
    def test_a_searched_reply_sends_and_saves_its_sources(self, build):
        sources = [{'title': 'Rain in Delhi — हिंदी', 'url': 'https://example-news.com/a', 'site': 'example-news.com',
                    'favicon': '', 'published': ''}]
        build.return_value = ('LIVE WEB RESULTS ...', sources)
        user = User.objects.create_user('sources-staff@example.com', password='pw', is_staff=True)
        self.client.force_login(user)
        with patch('myapp.ai_chat._stream_chat_impl', return_value=iter(['It will rain.'])):
            response = self.client.post('/AI/api/send/', data=json.dumps({'message': 'delhi weather today', 'web_search': True}),
                                        content_type='application/json')
            b''.join(response.streaming_content)
        self.assertEqual(json.loads(base64.b64decode(response['X-Web-Sources'])), sources)
        reply = AIMessage.objects.get(role=AIMessage.ROLE_ASSISTANT)
        self.assertEqual(reply.web_sources, sources)
        history = self.client.get(f'/AI/api/conversations/{reply.conversation_id}/').json()['messages']
        self.assertEqual(history[-1]['web_sources'], sources)

    def test_a_sourced_reply_can_be_saved_as_pdf_or_word(self):
        from pypdf import PdfReader
        from myapp.models import AIConversation
        user = User.objects.create_user('export-staff@example.com', password='pw', is_staff=True)
        self.client.force_login(user)
        conversation = AIConversation.objects.create(user=user, title='ISRO news')
        reply = AIMessage.objects.create(
            conversation=conversation, role=AIMessage.ROLE_ASSISTANT,
            content='ISRO launched a new satellite this week.',
            web_sources=[{'title': 'ISRO launch report', 'url': 'https://example-news.com/isro', 'site': 'example-news.com'}],
        )

        pdf = self.client.get(f'/AI/api/messages/{reply.pk}/export/pdf/')
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertIn('ISRO-news', pdf['Content-Disposition'])
        text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
        self.assertIn('ISRO launched a new satellite this week.', text)
        self.assertIn('ISRO launch report', text)
        self.assertIn('example-news.com/isro', text)

        docx = self.client.get(f'/AI/api/messages/{reply.pk}/export/docx/')
        self.assertEqual(docx.status_code, 200)
        self.assertTrue(docx.content.startswith(b'PK'))

        self.assertEqual(self.client.get(f'/AI/api/messages/{reply.pk}/export/rtf/').status_code, 400)

        # A user turn (not an answer) and someone else's reply are both refused.
        user_turn = AIMessage.objects.create(conversation=conversation, role=AIMessage.ROLE_USER, content='Any ISRO news?')
        self.assertEqual(self.client.get(f'/AI/api/messages/{user_turn.pk}/export/pdf/').status_code, 404)
        other = User.objects.create_user('export-other@example.com', password='pw')
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/AI/api/messages/{reply.pk}/export/pdf/').status_code, 404)


class TavilyLimitTests(TestCase):
    @override_settings(TAVILY_API_KEY='tvly-x')
    @patch('myapp.web_search.requests.post')
    def test_a_used_up_plan_is_explained(self, post):
        post.return_value = SimpleNamespace(status_code=432, ok=False)
        ok, message = web_search.test_connection()
        self.assertFalse(ok)
        self.assertIn('used up', message)
