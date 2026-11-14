"""Security behaviour at browser, proxy and authentication boundaries."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from .security import AuthenticationThrottleMiddleware, client_network

LOCAL_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                           'LOCATION': 'security-tests'}}


@override_settings(TRUST_PROXY_HEADERS=False)
class ProxyAddressTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_untrusted_forwarded_headers_cannot_rotate_the_bucket(self):
        request = self.factory.post('/login/local/', REMOTE_ADDR='192.0.2.1',
                                    HTTP_X_FORWARDED_FOR='198.51.100.12')
        self.assertEqual(client_network(request), '192.0.2.1/32')

    @override_settings(TRUST_PROXY_HEADERS=True)
    def test_trusted_proxy_uses_its_last_appended_address(self):
        request = self.factory.post('/login/local/', REMOTE_ADDR='192.0.2.1',
                                    HTTP_X_FORWARDED_FOR='203.0.113.99, 198.51.100.12')
        self.assertEqual(client_network(request), '198.51.100.12/32')

    def test_ipv6_addresses_share_a_64_bit_network_bucket(self):
        first = self.factory.post('/login/local/', REMOTE_ADDR='2001:db8::1')
        second = self.factory.post('/login/local/', REMOTE_ADDR='2001:db8::2')
        self.assertEqual(client_network(first), client_network(second))


@override_settings(CACHES=LOCAL_CACHE, TRUST_PROXY_HEADERS=False)
class AuthenticationThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('reader', password='Reader-password-2026!')

    def test_local_and_admin_login_share_the_account_budget(self):
        for index in range(5):
            path = '/login/local/' if index % 2 else '/admin/login/'
            self.assertEqual(self.client.post(path, {'username': 'reader', 'password': 'wrong'}).status_code, 200)
        response = self.client.post('/login/local/', {'username': 'reader', 'password': 'Reader-password-2026!'})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)
        self.assertNotIn('_auth_user_id', self.client.session)
        with patch('monitor.security.time.time', return_value=9_000_000_000):
            response = self.client.post('/login/local/', {'username': 'reader', 'password': 'Reader-password-2026!'})
        self.assertEqual(response.status_code, 302)

    def test_rotating_usernames_is_still_limited_by_network(self):
        middleware = AuthenticationThrottleMiddleware(lambda request: HttpResponse('OK'))
        with patch('monitor.security.render', side_effect=lambda *args, **kwargs: HttpResponse(status=kwargs['status'])):
            for index in range(20):
                request = RequestFactory().post('/login/local/', {'username': f'user-{index}'})
                self.assertEqual(middleware(request).status_code, 200)
            request = RequestFactory().post('/admin/login/', {'username': 'another-user'})
            self.assertEqual(middleware(request).status_code, 429)

    @patch('monitor.security.cache.add', side_effect=ConnectionError('do-not-expose-credentials'))
    def test_cache_outage_fails_closed_without_exposing_connection_details(self, add):
        response = self.client.post('/login/local/', {'username': 'reader', 'password': 'Reader-password-2026!'})
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, 'do-not-expose-credentials', status_code=503)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(self.client.get('/login/').status_code, 200)

    @patch('monitor.security.cache.add', side_effect=ConnectionError)
    def test_limiter_errors_follow_the_browser_language(self, add):
        response = self.client.post('/login/local/', {'username': 'reader', 'password': 'wrong'},
                                    HTTP_ACCEPT_LANGUAGE='en')
        self.assertContains(response, 'Sign-in is temporarily unavailable.', status_code=503)


class BrowserSecurityTests(TestCase):
    def test_headers_and_no_store_are_present_on_public_and_private_pages(self):
        for path in ('/login/', '/login/local/'):
            response = self.client.get(path)
            self.assertEqual(response['X-Frame-Options'], 'DENY')
            self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response['Referrer-Policy'], 'same-origin')
            self.assertIn('no-store', response['Cache-Control'])
            self.assertIn("frame-ancestors 'none'", response['Content-Security-Policy'])
            self.assertNotIn("script-src 'self' 'unsafe-inline'", response['Content-Security-Policy'])
            self.assertIn('camera=()', response['Permissions-Policy'])

    def test_inline_scripts_have_a_unique_nonce_matching_the_policy(self):
        user = get_user_model().objects.create_superuser('administrator', 'admin@example.org', 'Admin-password-2026!')
        self.client.force_login(user)
        first = self.client.get('/styleguide/')
        nonce = first.wsgi_request.csp_nonce
        self.assertContains(first, f'<script nonce="{nonce}">')
        self.assertIn(f"'nonce-{nonce}'", first['Content-Security-Policy'])
        second = self.client.get('/styleguide/')
        self.assertNotEqual(nonce, second.wsgi_request.csp_nonce)

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_HSTS_SECONDS=3600,
                       SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'))
    def test_https_redirect_and_hsts_behind_tls_proxy(self):
        response = self.client.get('/login/', HTTP_HOST='localhost')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], 'https://localhost/login/')
        response = self.client.get('/login/', HTTP_HOST='localhost', HTTP_X_FORWARDED_PROTO='https')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Strict-Transport-Security'], 'max-age=3600')
        self.assertIn('upgrade-insecure-requests', response['Content-Security-Policy'])

    def test_csrf_remains_required_for_local_login(self):
        response = Client(enforce_csrf_checks=True).post('/login/local/', {'username': 'reader', 'password': 'wrong'})
        self.assertEqual(response.status_code, 403)

    def test_https_form_accepts_valid_csrf_with_same_origin_referrer(self):
        client = Client(enforce_csrf_checks=True)
        page = client.get('/login/local/', secure=True)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        token = page.cookies['csrftoken'].value
        response = client.post('/login/local/', {'csrfmiddlewaretoken': token}, secure=True,
                               HTTP_REFERER='https://testserver/login/local/')
        # Empty credentials redisplay the form after passing CSRF verification.
        self.assertEqual(response.status_code, 200)

    def test_https_form_rejects_foreign_null_and_missing_origins_with_valid_token(self):
        client = Client(enforce_csrf_checks=True)
        token = client.get('/login/local/', secure=True).cookies['csrftoken'].value
        for headers in ({'HTTP_ORIGIN': 'https://attacker.example'}, {'HTTP_ORIGIN': 'null'}, {}):
            with self.subTest(headers=headers):
                response = client.post('/login/local/', {'csrfmiddlewaretoken': token}, secure=True, **headers)
                self.assertEqual(response.status_code, 403)
