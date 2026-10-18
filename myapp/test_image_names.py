import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from myapp import ai_chat, image_generation


class ImageReplyNameTests(TestCase):
    """An image turn is always presented under the model the user picked —
    never under the image model or provider that actually drew it."""

    def setUp(self):
        self.user = User.objects.create_user(username='names@example.com', email='names@example.com',
                                             password='test-password-123', is_staff=True)
        self.client.force_login(self.user)

    def _fail_image(self, model_key, message):
        error = image_generation.ImageGenerationError(message, status_code=502)
        with patch('myapp.views.poster.make_poster', return_value=None), \
                patch('myapp.views.image_generation.generate_image', side_effect=error):
            response = self.client.post('/AI/api/send/', content_type='application/json', data=json.dumps(
                {'message': 'generate an image of a cat sitting on a sofa', 'model': model_key}))
        return response

    def test_an_image_error_never_names_the_image_model_or_provider(self):
        for model_key in ('terra', 'sol', 'quick', 'ultra'):
            response = self._fail_image(model_key, 'FLUX.2 Klein (NVIDIA) rejected the NVIDIA_FLUX_API_KEY.')
            detail = response.json()['detail']
            self.assertEqual(response['X-Model-Key'], model_key)
            for hidden in ('FLUX', 'Klein', 'NVIDIA', 'API_KEY'):
                self.assertNotIn(hidden, detail, model_key)
            self.assertIn(ai_chat.MODELS[model_key]['label'], detail)

    def test_the_image_instruction_never_points_to_another_model(self):
        captured = {}

        def fake_stream(client, kwargs):
            captured.update(kwargs)
            yield 'Yes, I can generate images.'

        with patch.object(ai_chat, '_stream_content', side_effect=fake_stream), \
                patch.object(ai_chat, '_client_for_cfg', return_value=object()):
            ''.join(ai_chat._stream_chat_impl(
                [{'role': 'user', 'content': 'can you generate images?'}], 'quick'))
        reminder = captured['messages'][-2]['content']
        self.assertIn('Yes, I can generate images', reminder)
        self.assertNotIn('FLUX', reminder)
        self.assertIn('never tell the user to switch models', reminder)
