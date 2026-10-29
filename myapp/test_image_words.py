"""Logos and quoted words lettered exactly (myapp.lettering), and plain
backgrounds cut out without redrawing (myapp.cutout)."""
import base64
import io
import json
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image, ImageDraw

from myapp import ai_chat, cutout, image_edit, image_generation, lettering
from myapp.models import StoredImage, StoreProfile


def png(image):
    out = io.BytesIO()
    image.save(out, 'PNG')
    return out.getvalue()


def logo_on(colour=(5, 5, 8), size=(512, 512)):
    """A ring with a dark middle and a white block, on a plain background."""
    image = Image.new('RGB', size, colour)
    draw = ImageDraw.Draw(image)
    draw.ellipse((130, 130, 382, 382), outline=(30, 220, 120), width=14)
    draw.rectangle((210, 230, 300, 280), fill=(255, 255, 255))
    return image


def as_data_uri(content):
    return 'data:image/png;base64,' + base64.b64encode(content).decode()


class NameTests(SimpleTestCase):
    def test_names_are_taken_exactly_as_typed(self):
        cases = {
            'create similar images with brand name ClaudeChats simialr logo and create new image': 'ClaudeChats',
            'Create a logo for Sweet Crumbs bakery, pastel colours': 'Sweet Crumbs',
            'make a logo for my bakery called sweet crumbs with a cupcake': 'sweet crumbs',
            'Design a logo named NovaPay, a fintech app': 'NovaPay',
            'logo with brand name Sweet Crumbs and tagline "Fresh Cakes Daily"': 'Sweet Crumbs',
            'Create a logo "Nova" with tagline "Pay smarter"': 'Nova',
            'create a logo for a tech startup': '',
        }
        for prompt, name in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(lettering.find_name(prompt), name)

    def test_taglines(self):
        self.assertEqual(lettering.find_tagline('logo with brand name Sweet Crumbs and tagline "Fresh Cakes Daily"'), 'Fresh Cakes Daily')
        self.assertEqual(lettering.find_tagline('logo for Vidhyantra Services, tagline All Genuine Services'), 'All Genuine Services')

    def test_the_symbol_prompt_never_carries_the_words(self):
        prompt = lettering.symbol_prompt('Create a logo for ClaudeChats with tagline "Chat smarter"', 'ClaudeChats', 'Chat smarter')
        self.assertNotIn('ClaudeChats', prompt)
        self.assertNotIn('Chat smarter', prompt)
        self.assertIn('No text', prompt)

    def test_a_model_name_must_be_in_the_message(self):
        with patch.object(ai_chat, 'complete_json', return_value={'name': 'Totally Made Up'}):
            self.assertEqual(lettering._model_name('make my shop logo, it is the one we discussed'), '')
        with patch.object(ai_chat, 'complete_json', return_value={'name': 'chai point'}):
            self.assertEqual(lettering._model_name('logo for my tea stall, the name is going to be chai point'), 'chai point')


class LetteringTests(SimpleTestCase):
    def test_a_named_logo_is_drawn_without_letters_then_lettered(self):
        art = image_generation.GeneratedImage(png(logo_on((255, 255, 255))), 'png')
        with patch.object(image_generation, 'generate_image', return_value=art) as generate:
            result = lettering.make_logo('Create a logo for ClaudeChats, blue and purple')
        self.assertEqual(result.extension, 'png')
        sent = generate.call_args.args[0]
        self.assertNotIn('ClaudeChats', sent)
        self.assertEqual(generate.call_args.kwargs['size'], (1024, 1024))
        with Image.open(io.BytesIO(result.content)) as made:
            self.assertEqual(made.size, (1080, 1080))

    def test_not_a_logo_or_no_name_is_left_alone(self):
        with patch.object(image_generation, 'generate_image') as generate, \
                patch.object(lettering, '_model_name', return_value=''):
            self.assertIsNone(lettering.make_logo('a cat on the moon'))
            self.assertIsNone(lettering.make_logo('create a logo for a tech startup'))
        generate.assert_not_called()

    def test_quoted_words_on_any_picture(self):
        art = image_generation.GeneratedImage(png(Image.new('RGB', (896, 1152), (200, 120, 200))), 'png')
        with patch.object(image_generation, 'generate_image', return_value=art) as generate, \
                patch('myapp.poster.compose', return_value=b'lettered') as compose:
            result = lettering.make_text_picture('Create a birthday card image that says "Happy Birthday Riya" with balloons')
        self.assertEqual(result.content, b'lettered')
        self.assertEqual(compose.call_args.args[1].title, 'Happy Birthday Riya')
        self.assertNotIn('Happy Birthday Riya', generate.call_args.args[0])
        self.assertIsNone(lettering.make_text_picture("make Riya's birthday card with balloons"))


class CutoutTests(SimpleTestCase):
    def test_a_plain_background_becomes_transparent_and_nothing_else_changes(self):
        source = logo_on()
        result = cutout.remove_plain_background(as_data_uri(png(source)))
        with Image.open(io.BytesIO(result)) as made:
            made.load()
            self.assertEqual(made.mode, 'RGBA')
            alpha = made.getchannel('A')
            # Cropped to the logo: the ring's outer edge is at the border now.
            self.assertEqual(alpha.getpixel((0, 0)), 0)
            middle = (made.width // 2, made.height // 2 - 40)
            self.assertEqual(alpha.getpixel(middle), 255)     # the dark middle of the ring stays
            self.assertEqual(made.getpixel(middle)[:3], (5, 5, 8))

    def test_a_busy_background_is_left_to_the_redraw(self):
        noise = Image.effect_noise((300, 200), 90).convert('RGB')
        self.assertIsNone(cutout.remove_plain_background(png(noise)))

    def test_what_counts_as_asking(self):
        for text in ('remove background', 'remove bckground and give new image', 'make the background transparent',
                     'bg hatao', 'background remove karo', 'png without background'):
            self.assertTrue(cutout.wants_background_removed(text), text)
        for text in ('change the background to a beach', 'remove the text', 'add a blue background'):
            self.assertFalse(cutout.wants_background_removed(text), text)
        self.assertTrue(ai_chat.is_image_edit_instruction('make the background transparent'))


class ThroughTheChatTests(TestCase):
    def setUp(self):
        cache.clear()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        self.enterContext(override_settings(MEDIA_ROOT=media, FLUX_EDIT_API_URL=''))
        self.user = User.objects.create_user('pics@example.com', 'pics@example.com', 'pw', is_staff=True)
        StoreProfile.objects.get_or_create(user=self.user, defaults={'ai_onboarded': True})
        self.client.force_login(self.user)

    def _send(self, body):
        return self.client.post('/AI/api/send/', data=json.dumps(body), content_type='application/json')

    def test_remove_background_cuts_out_exactly_without_redrawing(self):
        with patch('myapp.views.image_ocr.extract_data_uri', return_value=''), \
                patch.object(image_edit, 'redraw') as redraw, \
                patch.object(image_generation, 'generate_image') as generate:
            response = self._send({'message': 'remove bckground and give new image',
                                   'image': as_data_uri(png(logo_on())), 'model': ai_chat.SOL_MODEL_KEY})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('Removed the background', response.content.decode())
        redraw.assert_not_called()
        generate.assert_not_called()
        self.assertTrue(StoredImage.objects.exists())

    def test_a_logo_request_gets_its_name_lettered(self):
        art = image_generation.GeneratedImage(png(logo_on((255, 255, 255))), 'png')
        with patch.object(image_generation, 'generate_image', return_value=art) as generate:
            response = self._send({'message': 'create a logo with brand name ClaudeChats, blue and yellow',
                                   'model': ai_chat.SOL_MODEL_KEY})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response['X-Generated-Image-Url'])
        self.assertNotIn('ClaudeChats', generate.call_args.args[0])
