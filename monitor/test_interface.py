"""Shared page credits and accessible navigation across authorization levels."""
from collections import Counter
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from .errors import server_error
from .models import Target, UserProfile


class PageElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class InterfaceTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('administrator', is_staff=True, is_superuser=True)
        self.target = Target.objects.create(name='A long service name', url='https://example.org')

    def assert_credit(self, response, status=200):
        self.assertContains(response, 'href="https://beyondthewall.ch"', status_code=status, count=1)
        self.assertContains(response, '© beyond the wall', status_code=status, count=1)

    def test_publisher_credit_is_shared_by_public_internal_admin_and_error_shells(self):
        self.assert_credit(self.client.get('/login/'))
        self.assert_credit(self.client.get('/login/local/'))
        self.assert_credit(self.client.get('/unknown-test-page/'), status=404)
        self.assert_credit(server_error(RequestFactory().get('/broken/')), status=500)
        self.client.force_login(self.admin)
        for page in ('/', '/platform/', '/profile/', '/incidents/', '/invitations/',
                     f'/probes/{self.target.pk}/', '/admin/'):
            with self.subTest(page=page):
                self.assert_credit(self.client.get(page))

    def test_single_navigation_drawer_keeps_viewer_actions_without_admin_links_or_duplicate_ids(self):
        viewer = get_user_model().objects.create_user('viewer')
        UserProfile.objects.create(user=viewer, role=UserProfile.Role.VIEWER)
        self.client.force_login(viewer)
        response = self.client.get('/')
        self.assertContains(response, 'aria-controls="site-navigation"')
        self.assertContains(response, 'action="/logout/"')
        self.assertContains(response, 'action="/profile/language/"')
        self.assertNotContains(response, 'href="/platform/"')
        elements = PageElements(response.content.decode()).elements
        identifiers = Counter(attrs['id'] for _, attrs in elements if 'id' in attrs)
        self.assertTrue(all(count == 1 for count in identifiers.values()))
        self.assertEqual(identifiers['site-navigation'], 1)

    def test_stacked_history_retains_table_roles_and_field_labels(self):
        self.client.force_login(self.admin)
        response = self.client.get(f'/probes/{self.target.pk}/')
        elements = PageElements(response.content.decode()).elements
        headers = [attrs for tag, attrs in elements if tag == 'th']
        self.assertTrue(headers)
        self.assertTrue(all(attrs.get('scope') == 'col' for attrs in headers))
        cells = [attrs for tag, attrs in elements if tag == 'td' and 'colspan' not in attrs]
        self.assertTrue(cells)
        self.assertTrue(all(attrs.get('role') == 'cell' and attrs.get('data-label') for attrs in cells))

    def test_monitoring_pages_show_availability_without_secondary_coverage_percentage(self):
        self.client.force_login(self.admin)
        for page in ('/', f'/probes/{self.target.pk}/'):
            response = self.client.get(page)
            self.assertNotContains(response, 'observés')
            self.assertNotContains(response, 'Couverture')
