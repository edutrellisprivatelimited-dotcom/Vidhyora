"""Editing a picture by reading it and drawing it again, and sending several
images (and images with files) in one message."""
import base64
import io
import json
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from PIL import Image

from myapp import ai_chat, image_edit, image_generation, image_sheet, image_store
from myapp.models import AIMessage, StoredImage, StoreProfile


def data_uri(size=(300, 400), color=(30, 90, 200), fmt='PNG'):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, fmt)
    mime = 'jpeg' if fmt == 'JPEG' else fmt.lower()
    return f'data:image/{mime};base64,' + base64.b64encode(out.getvalue()).decode()


def picture_bytes(size=(896, 1152), color=(40, 60, 120)):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, 'PNG')
    return out.getvalue()


POSTER_WORDS = 'ChatGPT\nALL PLANS\nSmarter, Faster, Better\nImage generation\nVoice features\nUPGRADE TO ALL PLANS NOW'
POSTER_DESCRIPTION = 'A tall purple and blue promotional poster with the ChatGPT logo at the top and a white feature list.'


class ImageSheetTests(TestCase):
    def test_several_pictures_become_one_numbered_picture(self):
        sheet = image_sheet.combine([data_uri(color=(255, 0, 0)), data_uri(color=(0, 255, 0)), data_uri()])
        self.assertTrue(sheet.startswith('data:image/jpeg;base64,'))
        with Image.open(io.BytesIO(base64.b64decode(sheet.split(',', 1)[1]))) as opened:
            # Two columns, two rows.
            self.assertEqual(opened.size, (2 * image_sheet.CELL + 3 * image_sheet.GAP,) * 2)
        self.assertLessEqual(len(sheet), image_sheet.MAX_DATA_URI_CHARS)

    def test_an_unreadable_picture_gives_nothing(self):
        self.assertEqual(image_sheet.combine([data_uri(), 'data:image/png;base64,bm90IGFuIGltYWdl']), '')

    def test_the_note_says_how_many(self):
        self.assertIn('3 images', image_sheet.note(3))


class RedrawTests(TestCase):
    def setUp(self):
        self.source = data_uri(size=(600, 800))

    def _redraw(self, plan, *, description=POSTER_DESCRIPTION, words=POSTER_WORDS, earlier_brief=''):
        generated = image_generation.GeneratedImage(picture_bytes(), 'png')
        with patch.object(ai_chat, 'describe_image_for_edit', return_value=description) as describe, \
                patch.object(image_edit.image_ocr, 'extract_data_uri', return_value=words) as ocr, \
                patch.object(ai_chat, 'complete_json', return_value=plan) as complete, \
                patch.object(image_generation, 'generate_image', return_value=generated) as generate:
            result = image_edit.redraw(self.source, 'update name gpt to claude and give image', earlier_brief=earlier_brief)
        return result, describe, ocr, complete, generate

    def test_a_poster_full_of_words_is_lettered_with_the_change(self):
        plan = {
            'prompt': 'A purple poster for "Claude ALL PLANS"',
            'brief': 'A tall purple poster. Title: Claude ALL PLANS. Lines: Image generation, Voice features.',
            'change': 'Changed the name ChatGPT to Claude.',
            'poster': {
                'title': 'Claude ALL PLANS', 'tagline': 'Smarter, Faster, Better',
                'features': ['Image generation', 'Voice features', 'Free pizza forever'],
                'cta': 'UPGRADE TO ALL PLANS NOW', 'scene': 'blurry purple and blue glowing waves',
            },
        }
        with patch.object(image_edit.poster, 'compose', return_value=b'lettered') as compose:
            result, describe, ocr, complete, generate = self._redraw(plan)
        self.assertEqual(result.image, image_generation.GeneratedImage(b'lettered', 'jpg'))
        self.assertEqual(result.change, 'Changed the name ChatGPT to Claude.')
        self.assertIn('Claude ALL PLANS', result.brief)
        spec = compose.call_args.args[1]
        self.assertEqual(spec.title, 'Claude ALL PLANS')
        # A line that was neither on the picture nor in the request is dropped.
        self.assertEqual(spec.features, ['Image generation', 'Voice features'])
        # Only the background is drawn by the image model, in the poster's shape,
        # and a "blurry" copy never makes blurry artwork.
        self.assertIn('No text', generate.call_args.args[0])
        self.assertNotIn('blurry', generate.call_args.args[0])
        self.assertEqual(generate.call_args.kwargs['size'], (896, 1152))
        # The vision model got a smaller JPEG copy, with more time than a chat turn.
        self.assertTrue(describe.call_args.args[0].startswith('data:image/jpeg;base64,'))
        self.assertEqual(describe.call_args.kwargs['timeout'], image_edit.DESCRIBE_TIMEOUT)
        sent = complete.call_args.args[1]
        self.assertIn('update name gpt to claude', sent)
        self.assertIn('UPGRADE TO ALL PLANS NOW', sent)

    def test_a_photo_is_drawn_whole_from_the_written_prompt(self):
        plan = {'prompt': 'A golden retriever on a red sofa, warm light', 'brief': 'A dog on a red sofa.',
                'change': 'Made the sofa red.', 'poster': None}
        result, _, _, _, generate = self._redraw(plan, description='A dog on a blue sofa.', words='')
        self.assertEqual(generate.call_args.args[0], 'A golden retriever on a red sofa, warm light')
        self.assertEqual(generate.call_args.kwargs['size'], (896, 1152))
        self.assertEqual(result.change, 'Made the sofa red.')

    def test_without_a_plan_the_request_and_description_are_drawn(self):
        result, _, _, _, generate = self._redraw(None, description='A dog on a blue sofa.', words='')
        self.assertEqual(generate.call_args.args[0], 'update name gpt to claude and give image. A dog on a blue sofa.')
        self.assertEqual(result.change, '')
        self.assertIn('A dog on a blue sofa', result.brief)

    def test_the_earlier_brief_goes_to_the_writer(self):
        plan = {'prompt': 'x', 'brief': 'new brief', 'change': 'Done.', 'poster': None}
        _, _, _, complete, _ = self._redraw(plan, earlier_brief='Title: Claude ALL PLANS')
        self.assertIn('Brief the current picture was drawn from:\nTitle: Claude ALL PLANS', complete.call_args.args[1])

    def test_a_picture_that_cannot_be_read_is_reported(self):
        with self.assertRaises(image_generation.ImageGenerationError) as raised:
            self._redraw({}, description=None, words='')
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("couldn't read that picture", str(raised.exception))

    def test_ocr_already_done_by_the_page_is_not_repeated(self):
        generated = image_generation.GeneratedImage(picture_bytes(), 'png')
        with patch.object(ai_chat, 'describe_image_for_edit', return_value='A sign.'), \
                patch.object(image_edit.image_ocr, 'extract_data_uri') as ocr, \
                patch.object(ai_chat, 'complete_json', return_value=None), \
                patch.object(image_generation, 'generate_image', return_value=generated):
            image_edit.redraw(self.source, 'make it red', words='OPEN')
        ocr.assert_not_called()


class DescribeTests(TestCase):
    def test_the_description_is_streamed_and_cut_at_the_deadline(self):
        def pieces():
            yield 'A purple poster '
            yield 'with a logo.'
            raise AssertionError('read past the deadline')

        ticks = iter([0.0, 1.0, 99.0])
        with patch.object(ai_chat, '_get_client'), \
                patch.object(ai_chat, '_stream_content', return_value=pieces()) as stream, \
                patch.object(ai_chat.time, 'perf_counter', side_effect=lambda: next(ticks)):
            text = ai_chat.describe_image_for_edit(data_uri(), timeout=20, deadline=40)
        self.assertEqual(text, 'A purple poster with a logo.')
        sent = stream.call_args.args[1]
        self.assertTrue(sent['stream'])
        self.assertEqual(sent['timeout'], 20)

    def test_a_failure_gives_none(self):
        with patch.object(ai_chat, '_get_client', side_effect=ValueError('NVIDIA_API_KEY is not configured.')):
            self.assertIsNone(ai_chat.describe_image_for_edit(data_uri()))


class EditThroughTheChatTests(TestCase):
    """The user's report: "update name gpt to claude and give image" on an
    attached poster ended with "Uploaded-image editing needs an upload-capable
    image-editing server." """

    def setUp(self):
        cache.clear()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        self.enterContext(override_settings(MEDIA_ROOT=media, FLUX_EDIT_API_URL=''))
        self.user = User.objects.create_user('edit@example.com', 'edit@example.com', 'pw', is_staff=True)
        StoreProfile.objects.get_or_create(user=self.user, defaults={'ai_onboarded': True})
        self.client.force_login(self.user)

    def _send(self, body):
        return self.client.post('/AI/api/send/', data=json.dumps(body), content_type='application/json')

    def test_an_attached_picture_is_redrawn_and_the_next_edit_builds_on_it(self):
        first = image_edit.Redrawn(image_generation.GeneratedImage(picture_bytes(), 'png'),
                                   brief='Poster. Title: Claude ALL PLANS.', change='Changed the name ChatGPT to Claude.')
        with patch('myapp.views.image_ocr.extract_data_uri', return_value=POSTER_WORDS), \
                patch.object(image_edit, 'redraw', return_value=first) as redraw:
            response = self._send({'message': 'update name gpt to claude and give image',
                                   'image': data_uri(), 'model': ai_chat.SOL_MODEL_KEY})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['X-Request-Category'], 'image_edit')
        reply = response.content.decode()
        self.assertIn('Changed the name ChatGPT to Claude.', reply)
        self.assertIn('small details may differ', reply)
        self.assertEqual(redraw.call_args.kwargs['words'], POSTER_WORDS)
        stored = StoredImage.objects.get()
        self.assertEqual(stored.brief, 'Poster. Title: Claude ALL PLANS.')
        self.assertEqual(response['X-Generated-Image-Url'], image_store.url_for(stored))

        # "Again edit the same image": the picture just made is the one changed,
        # starting from the brief it was saved with.
        second = image_edit.Redrawn(image_generation.GeneratedImage(picture_bytes(color=(200, 0, 0)), 'png'),
                                    brief='Poster in red.', change='Made the background red.')
        with patch.object(image_edit, 'redraw', return_value=second) as redraw:
            response = self._send({'message': 'change the background colour to red',
                                   'conversation_id': int(response['X-Conversation-Id']),
                                   'model': ai_chat.SOL_MODEL_KEY})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('Made the background red.', response.content.decode())
        self.assertTrue(redraw.call_args.args[0].startswith('data:image/'))
        self.assertIsNone(redraw.call_args.kwargs['words'])
        self.assertEqual(redraw.call_args.kwargs['earlier_brief'], 'Poster. Title: Claude ALL PLANS.')

    def test_an_unreadable_picture_says_so_instead_of_blaming_the_server(self):
        with patch('myapp.views.image_ocr.extract_data_uri', return_value=''), \
                patch.object(ai_chat, 'describe_image_for_edit', return_value=None):
            response = self._send({'message': 'change the name to Claude', 'image': data_uri(),
                                   'model': ai_chat.SOL_MODEL_KEY})
        self.assertEqual(response.status_code, 502)
        detail = response.json()['detail']
        self.assertIn("couldn't read that picture", detail)
        self.assertNotIn('upload-capable', detail)


class SeveralAttachmentsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('many@example.com', 'many@example.com', 'pw', is_staff=True)
        StoreProfile.objects.get_or_create(user=self.user, defaults={'ai_onboarded': True})
        self.client.force_login(self.user)

    def _send(self, body):
        with patch('myapp.views.image_ocr.extract_data_uri', return_value='SALE 50%'), \
                patch('myapp.views.ai_chat.stream_chat', return_value=iter(['They differ in colour.'])) as stream:
            response = self.client.post('/AI/api/send/', data=json.dumps(body), content_type='application/json')
            if response.status_code == 200:
                b''.join(response.streaming_content)
        return response, stream

    def test_several_images_go_to_the_model_as_one_numbered_picture(self):
        response, stream = self._send({'message': 'What is different between these?',
                                       'images': [data_uri(color=(255, 0, 0)), data_uri(color=(0, 0, 255))],
                                       'image': data_uri(color=(255, 0, 0)), 'model': ai_chat.SOL_MODEL_KEY})
        self.assertEqual(response.status_code, 200)
        saved = AIMessage.objects.get(role=AIMessage.ROLE_USER)
        self.assertTrue(saved.image_data.startswith('data:image/jpeg;base64,'))
        self.assertIn('2 images were attached', saved.document_text)
        content = stream.call_args.args[0][-1]['content']
        self.assertEqual([part['type'] for part in content], ['text', 'image_url'])
        self.assertEqual(content[1]['image_url']['url'], saved.image_data)
        self.assertIn('2 images were attached', content[0]['text'])

    def test_more_than_four_images_are_refused(self):
        response, stream = self._send({'message': 'compare', 'images': [data_uri()] * 5})
        self.assertEqual(response.status_code, 400)
        self.assertIn('up to 4 images', response.json()['detail'])
        stream.assert_not_called()

    def test_files_and_an_image_are_read_together(self):
        response, stream = self._send({
            'message': 'Does the poster match the price list?', 'image': data_uri(),
            'document_text': 'Price list: Basic 99, Pro 199', 'document_name': 'prices.pdf',
            'document_mode': 'multi', 'model': ai_chat.SOL_MODEL_KEY,
        })
        self.assertEqual(response.status_code, 200)
        saved = AIMessage.objects.get(role=AIMessage.ROLE_USER)
        self.assertEqual(saved.document_name, 'prices.pdf')
        self.assertIn('Price list: Basic 99', saved.document_text)
        self.assertIn('Text read from the attached image:\nSALE 50%', saved.document_text)
        content = stream.call_args.args[0][-1]['content']
        self.assertIn('[Attached document: prices.pdf]', content[0]['text'])
        self.assertIn('Does the poster match the price list?', content[0]['text'])
        self.assertEqual(content[1]['type'], 'image_url')


class ComposerPageTests(TestCase):
    def test_the_message_box_sits_above_the_tools_with_no_scrollbar(self):
        body = self.client.get('/').content.decode()
        rule = body.split('#chatInput{')[1].split('}')[0]
        self.assertIn('order:-1', rule)
        self.assertIn('flex:1 1 100%', rule)
        self.assertIn('scrollbar-width:none', rule)
        self.assertIn('#chatInput::-webkit-scrollbar{display:none', body)
        wrap = body.split('.input-wrap{')[1].split('}')[0]
        self.assertIn('flex-wrap:wrap', wrap)
        self.assertIn('var IMAGE_MAX_FILES = 4;', body)
        self.assertNotIn('Only one image can be sent at a time', body)
