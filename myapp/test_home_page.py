from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from myapp.models import ContactSubmission

CONTACT = {
    'name': 'Rahul Sharma', 'email': 'rahul@example.com', 'phone': '+91 98765 43210',
    'service': 'seo', 'message': 'Need help ranking my school website.',
}


class HomePageTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_the_page_opens_without_a_login_form(self):
        response = self.client.get('/home/')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('id="contactForm"', body)
        for gone in ('loginForm', 'type="password"', 'Admin Login'):
            self.assertNotIn(gone, body)

    def test_a_contact_message_is_saved(self):
        response = self.client.post('/home/contact/', CONTACT)
        self.assertEqual(response.json(), {'ok': True})
        entry = ContactSubmission.objects.get()
        self.assertEqual((entry.source, entry.name, entry.email, entry.service),
                         ('contact', 'Rahul Sharma', 'rahul@example.com', 'seo'))
        self.assertEqual(entry.message, CONTACT['message'])

    def test_a_bad_contact_message_is_refused_with_a_reason(self):
        for change, words in (({'email': 'not-an-email'}, 'valid email'),
                              ({'message': ''}, 'name and message'),
                              ({'service': 'hacking'}, 'service')):
            response = self.client.post('/home/contact/', {**CONTACT, **change})
            self.assertEqual(response.status_code, 400)
            self.assertIn(words, response.json()['error'])
        self.assertFalse(ContactSubmission.objects.exists())

    def test_a_newsletter_email_is_saved_once(self):
        for email in ('priya@example.com', 'PRIYA@example.com'):
            self.assertEqual(self.client.post('/home/newsletter/', {'email': email}).json(), {'ok': True})
        self.assertEqual(ContactSubmission.objects.filter(source='newsletter').count(), 1)

    def test_one_connection_cannot_flood_the_forms(self):
        for _ in range(10):
            self.client.post('/home/newsletter/', {'email': 'a@example.com'})
        response = self.client.post('/home/contact/', CONTACT)
        self.assertEqual(response.status_code, 429)


class ContactFormDashboardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user(username='staff@example.com', email='staff@example.com',
                                              password='test-password-123', is_staff=True)

    def test_staff_see_every_entry_and_can_filter(self):
        ContactSubmission.objects.create(name='Rahul', email='rahul@example.com', service='seo', message='Hello there')
        ContactSubmission.objects.create(source='newsletter', email='priya@example.com')
        self.client.force_login(self.staff)
        body = self.client.get('/store/dashboard/contact-forms/').content.decode()
        self.assertIn('rahul@example.com', body)
        self.assertIn('Search Engine Optimization', body)
        self.assertIn('priya@example.com', body)
        body = self.client.get('/store/dashboard/contact-forms/?type=newsletter').content.decode()
        self.assertNotIn('rahul@example.com', body)
        self.assertIn('Contact Form Data', self.client.get('/store/dashboard/backup/').content.decode())

    def test_other_people_cannot_open_it(self):
        ContactSubmission.objects.create(name='Rahul', email='rahul@example.com', message='private')
        response = self.client.get('/store/dashboard/contact-forms/')
        self.assertNotIn(b'rahul@example.com', response.content)

    def test_staff_can_delete_an_entry(self):
        entry = ContactSubmission.objects.create(name='Spam', email='spam@example.com', message='buy now')
        self.client.force_login(self.staff)
        self.client.post(f'/store/dashboard/contact-forms/{entry.pk}/delete/', {'next': 'https://evil.example/'})
        self.assertFalse(ContactSubmission.objects.exists())
