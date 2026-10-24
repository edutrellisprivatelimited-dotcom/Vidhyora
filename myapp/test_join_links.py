"""Join by link: superusers make invite links; people join with email, password and a plan."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from myapp.models import JoinLink, JoinLinkSignup, StoreProfile


class JoinLinkTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser('owner@example.com', 'owner@example.com', 'owner-pass')
        StoreProfile.objects.get_or_create(user=self.owner)

    def post_json(self, url, body):
        return self.client.post(url, json.dumps(body), content_type='application/json')

    def make_link(self, **fields):
        return JoinLink.objects.create(created_by=self.owner, **fields)

    def join(self, link, email, password, plan='2m'):
        return self.post_json(f'/join/{link.token}/', {'email': email, 'password': password, 'plan': plan})


class ManagingLinksTests(JoinLinkTestCase):
    def test_a_superuser_makes_lists_and_removes_links(self):
        self.client.force_login(self.owner)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        made = self.post_json('/AI/api/join-links/', {'label': 'Class A', 'max_uses': '1', 'expires_on': tomorrow}).json()
        self.assertEqual(made['status'], 'ok')
        self.assertEqual(made['link']['max_uses'], 1)
        self.assertIn('/join/', made['link']['url'])

        links = self.client.get('/AI/api/join-links/').json()['links']
        self.assertEqual([link['label'] for link in links], ['Class A'])
        self.assertEqual(links[0]['status'], 'active')

        self.assertEqual(self.post_json(f"/AI/api/join-links/{made['link']['id']}/remove/", {}).status_code, 200)
        self.assertEqual(self.client.get('/AI/api/join-links/').json()['links'], [])
        self.assertIsNotNone(JoinLink.objects.get().removed_at)  # kept for the admin panel

    def test_a_past_expiry_date_or_a_bad_limit_is_refused(self):
        self.client.force_login(self.owner)
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertIn('past', self.post_json('/AI/api/join-links/', {'expires_on': yesterday}).json()['detail'])
        self.assertEqual(self.post_json('/AI/api/join-links/', {'max_uses': '0'}).status_code, 400)
        self.assertFalse(JoinLink.objects.exists())

    def test_only_superusers_can_manage_links(self):
        staff = User.objects.create_user('staff@example.com', 'staff@example.com', 'pw', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/AI/api/join-links/').status_code, 403)
        self.assertEqual(self.post_json('/AI/api/join-links/', {}).status_code, 403)

    def test_the_menu_item_shows_only_for_superusers(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get('/'), 'data-account-panel="joinlinks"')
        member = User.objects.create_user('member@example.com', 'member@example.com', 'pw')
        StoreProfile.objects.get_or_create(user=member)
        self.client.force_login(member)
        self.assertNotContains(self.client.get('/'), 'data-account-panel="joinlinks"')


class JoiningTests(JoinLinkTestCase):
    def test_a_new_person_gets_an_account_premium_and_is_logged_in(self):
        link = self.make_link()
        page = self.client.get(f'/join/{link.token}/')
        self.assertContains(page, 'Get your')
        self.assertContains(page, '2 Months')
        self.assertNotContains(page, '₹')  # durations only, never prices

        self.assertFalse(self.post_json(f'/join/{link.token}/check/', {'email': 'New@Example.com'}).json()['exists'])
        body = self.join(link, 'New@Example.com', 'secret1').json()
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['new_account'])
        self.assertEqual(body['redirect'], '/')

        user = User.objects.get(email='new@example.com')
        self.assertTrue(user.check_password('secret1'))
        until = user.store_profile.ai_subscription_until
        self.assertAlmostEqual((until - timezone.now()).days, 59, delta=1)
        signup = JoinLinkSignup.objects.get()
        self.assertEqual((signup.link, signup.user, signup.plan, signup.plan_days, signup.new_account), (link, user, '2m', 60, True))
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_an_existing_account_must_give_its_password_and_premium_is_added_on_top(self):
        member = User.objects.create_user('member@example.com', 'member@example.com', 'right-pass')
        left = timezone.now() + timedelta(days=10)
        StoreProfile.objects.create(user=member, ai_subscription_until=left)
        link = self.make_link()

        self.assertTrue(self.post_json(f'/join/{link.token}/check/', {'email': 'member@example.com'}).json()['exists'])
        wrong = self.join(link, 'member@example.com', 'wrong-pass', plan='31d')
        self.assertEqual(wrong.status_code, 400)
        self.assertIn('password', wrong.json()['detail'])
        self.assertFalse(JoinLinkSignup.objects.exists())

        body = self.join(link, 'member@example.com', 'right-pass', plan='31d').json()
        self.assertFalse(body['new_account'])
        member.store_profile.refresh_from_db()
        self.assertEqual(member.store_profile.ai_subscription_until, left + timedelta(days=31))

        again = self.join(link, 'member@example.com', 'right-pass', plan='31d')
        self.assertIn('already joined', again.json()['detail'])

    def test_a_one_person_link_closes_after_the_first_join(self):
        link = self.make_link(max_uses=1)
        self.assertEqual(self.join(link, 'first@example.com', 'secret1').status_code, 200)
        self.client.logout()
        second = self.join(link, 'second@example.com', 'secret1')
        self.assertEqual(second.status_code, 410)
        self.assertFalse(User.objects.filter(email='second@example.com').exists())
        self.assertContains(self.client.get(f'/join/{link.token}/'), "This link can't be used", status_code=404)

    def test_expired_removed_and_unknown_links_show_a_clear_message(self):
        expired = self.make_link(expires_at=timezone.now() - timedelta(minutes=1))
        removed = self.make_link(removed_at=timezone.now())
        self.assertContains(self.client.get(f'/join/{expired.token}/'), 'has expired', status_code=404)
        self.assertContains(self.client.get(f'/join/{removed.token}/'), 'no longer active', status_code=404)
        self.assertContains(self.client.get('/join/not-a-real-token/'), 'no longer active', status_code=404)
        self.assertEqual(self.join(expired, 'late@example.com', 'secret1').status_code, 410)

    def test_a_short_password_or_a_missing_plan_is_refused(self):
        link = self.make_link()
        self.assertIn('at least 6', self.join(link, 'new@example.com', '123').json()['detail'])
        self.assertEqual(self.join(link, 'new@example.com', 'secret1', plan='free').json()['detail'], 'Choose a plan.')
        self.assertFalse(User.objects.filter(email='new@example.com').exists())


class AdminPanelTests(JoinLinkTestCase):
    def test_the_admin_page_lists_links_and_who_joined_with_the_price(self):
        link = self.make_link(label='Batch 7')
        self.join(link, 'buyer@example.com', 'secret1', plan='6m')
        self.client.force_login(self.owner)
        page = self.client.get('/store/dashboard/join-links/')
        self.assertContains(page, 'Batch 7')
        self.assertContains(page, 'buyer@example.com')
        self.assertContains(page, '6 Months')
        self.assertContains(page, '₹320')
        self.assertContains(page, 'New account')

        self.client.post(f'/store/dashboard/join-links/{link.pk}/remove/')
        link.refresh_from_db()
        self.assertIsNotNone(link.removed_at)
        self.assertContains(self.client.get('/store/dashboard/join-links/'), 'buyer@example.com')
