"""Account → Themes: five free themes besides Classic, for every account."""
import re

from django.contrib.auth.models import User
from django.test import TestCase

from myapp.models import StoreProfile

SKINS = ('aurora', 'paper', 'sakura', 'terminal', 'ocean')


class ThemesPageTests(TestCase):
    def setUp(self):
        # An ordinary free account: themes are not a premium feature.
        self.user = User.objects.create_user('plain@example.com', 'plain@example.com', 'pw')
        StoreProfile.objects.get_or_create(user=self.user, defaults={'ai_onboarded': True})
        self.client.force_login(self.user)
        self.body = self.client.get('/').content.decode()

    def test_the_menu_and_the_account_window_offer_themes(self):
        self.assertIn('data-account-panel="themes"', self.body)
        self.assertIn('data-account-tab="themes"', self.body)
        self.assertIn('data-account-content="themes"', self.body)

    def test_classic_and_five_themes_each_have_a_card_and_their_own_look(self):
        cards = re.findall(r'data-skin-choice="([a-z]+)"', self.body)
        self.assertEqual(cards, ['classic', *SKINS])
        for skin in SKINS:
            # Colours, fonts, a moving backdrop, bubbles and an entrance animation.
            self.assertIn(f':root[data-skin="{skin}"]{{--red:', self.body)
            self.assertIn(f':root[data-skin="{skin}"]{{--font-head:', self.body)
            self.assertIn(f':root[data-skin="{skin}"] .chat-shell::before{{', self.body)
            self.assertIn(f':root[data-skin="{skin}"] .msg-row.assistant .msg-bubble{{', self.body)
            self.assertRegex(self.body, rf':root\[data-skin="{skin}"\] \.msg-row(\.assistant|\.user)?\{{animation:skin-')
            self.assertIn(f'.skin-preview[data-p="{skin}"]{{', self.body)

    def test_a_saved_theme_is_applied_before_the_page_paints(self):
        head = self.body.split('</script>', 1)[0]
        self.assertIn("var AI_SKINS = { aurora: 'dark', paper: 'light', sakura: 'light', terminal: 'dark', ocean: 'dark' };", head)
        self.assertIn("localStorage.getItem('ai_skin')", head)

    def test_a_font_picked_under_customize_still_wins_over_a_theme(self):
        theme_fonts = self.body.index(':root[data-skin="aurora"]{--font-head:')
        picker_fonts = self.body.index(':root[data-font="inter"]{')
        self.assertLess(theme_fonts, picker_fonts)
        # ...while the theme's colours come after the light/dark ones.
        self.assertLess(self.body.index(':root[data-theme="dark"]{'), self.body.index(':root[data-skin="aurora"]{--red:'))
