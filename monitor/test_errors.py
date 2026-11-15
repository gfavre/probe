"""OAuth cancellation, resilient branded errors and privacy-preserving error reporting."""
import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import sentry_sdk
from django.core.cache import cache
from django.db import DatabaseError
from django.http import Http404
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import path, reverse
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.transport import Transport

from .error_reporting import configure_error_reporting, private_error_event
from .errors import page_not_found, server_error
from .models import PlatformSettings


def broken_view(request):
    private_runtime_value = 'fake-private-runtime-value'
    raise RuntimeError(private_runtime_value)


def missing_view(request):
    raise Http404('fake-private-not-found-details')


urlpatterns = [path('broken/', broken_view), path('missing/', missing_view)]
handler500 = 'monitor.errors.server_error'
handler404 = 'monitor.errors.page_not_found'


@override_settings(GOOGLE_CLIENT_ID='test-client', GOOGLE_CLIENT_SECRET='test-secret',
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class GoogleCancellationTests(TestCase):
    def setUp(self):
        cache.clear()

    def start_login(self):
        response = self.client.post(reverse('google_login'))
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response.url).query)['state'][0]

    @patch('allauth.socialaccount.providers.oauth2.client.OAuth2Client.get_access_token')
    def test_google_access_denied_returns_to_login_without_exchanging_tokens(self, exchange):
        state = self.start_login()
        response = self.client.get(reverse('google_callback'), {'error': 'access_denied', 'state': state}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Connexion Google annulée')
        self.assertEqual(response.redirect_chain[-1][0], reverse('login'))
        self.assertNotIn('_auth_user_id', self.client.session)
        exchange.assert_not_called()

    def test_missing_or_invalid_state_stays_an_authentication_failure(self):
        response = self.client.get(reverse('google_callback'), {'error': 'access_denied', 'state': 'invalid'})
        self.assertEqual(response.status_code, 401)
        self.assertContains(response, 'Connexion Google impossible', status_code=401)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_unknown_provider_error_is_safe_and_does_not_leak_callback_details(self):
        state = self.start_login()
        response = self.client.get(reverse('google_callback'), {
            'error': 'server_error', 'error_description': 'fake-private-provider-detail', 'state': state,
        })
        self.assertEqual(response.status_code, 401)
        self.assertNotContains(response, 'fake-private-provider-detail', status_code=401)
        self.assertContains(response, 'Revenir à la connexion', status_code=401)


@override_settings(ROOT_URLCONF=__name__, DEBUG=False)
class ErrorPageTests(TestCase):
    def setUp(self):
        self.client = Client(raise_request_exception=False)
        # Error templates link to the real application's dashboard; reuse it in this test URLconf.
        from config.urls import urlpatterns as application_urls
        self.urls = [*urlpatterns, *application_urls]
        self.url_override = override_settings(ROOT_URLCONF=type('ErrorURLs', (), {
            'urlpatterns': self.urls, 'handler500': server_error, 'handler404': page_not_found,
        }))
        self.url_override.enable()
        self.addCleanup(self.url_override.disable)

    def test_actual_unhandled_exception_uses_branded_page_and_reference(self):
        PlatformSettings.objects.create(pk=1, product_name='Twisted probe')
        response = self.client.get('/broken/')
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, 'Twisted probe', status_code=500)
        self.assertContains(response, 'Nous sommes désolés', status_code=500)
        self.assertContains(response, 'Référence du problème:', status_code=500)
        self.assertNotContains(response, 'fake-private-runtime-value', status_code=500)
        self.assertNotContains(response, 'signalement automatique', status_code=500)
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_404_has_same_shell_but_neither_error_reference_nor_reporting_claim(self):
        response = self.client.get('/missing/')
        self.assertContains(response, 'Page introuvable', status_code=404)
        self.assertContains(response, 'welcome-page', status_code=404)
        self.assertNotContains(response, 'fake-private-not-found-details', status_code=404)
        self.assertNotContains(response, 'Référence du problème:', status_code=404)
        self.assertNotContains(response, 'signalement automatique', status_code=404)
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_error_page_survives_unavailable_branding_and_skips_secret_context_processor(self):
        with (patch('monitor.branding.logo_options', side_effect=DatabaseError('private-db-details')),
              patch('monitor.context_processors.platform_values', side_effect=AssertionError('must not run'))):
            response = server_error(RequestFactory().get('/broken/'))
        self.assertContains(response, 'beyond the probe', status_code=500)
        self.assertNotContains(response, 'private-db-details', status_code=500)

    def test_capture_reference_is_shown_only_when_sentry_created_an_event(self):
        with (patch('monitor.errors.sentry_sdk.get_client') as client,
              patch('monitor.errors.sentry_sdk.last_event_id', return_value='a' * 32)):
            client.return_value.is_active.return_value = True
            response = server_error(RequestFactory().get('/broken/'))
        self.assertContains(response, 'signalement automatique', status_code=500)
        self.assertContains(response, 'a' * 32, status_code=500)

    def test_previous_event_is_not_claimed_as_a_new_report(self):
        request = RequestFactory().get('/broken/')
        request.error_event_before = 'a' * 32
        with (patch('monitor.errors.sentry_sdk.get_client') as client,
              patch('monitor.errors.sentry_sdk.last_event_id', return_value='a' * 32)):
            client.return_value.is_active.return_value = True
            response = server_error(request)
        self.assertNotContains(response, 'signalement automatique', status_code=500)
        self.assertNotContains(response, 'a' * 32, status_code=500)

    def test_sentry_captures_real_django_exception_without_private_runtime_data(self):
        envelopes = []

        class MemoryTransport(Transport):
            def capture_envelope(self, envelope):
                envelopes.append(envelope)

        with sentry_sdk.init(
            dsn='https://public@example.com/1', transport=MemoryTransport, default_integrations=False,
            integrations=[DjangoIntegration()], before_send=private_error_event,
            include_local_variables=False, send_default_pii=False, traces_sample_rate=0,
        ):
            response = self.client.get('/broken/?code=fake-private-oauth-code')
        self.assertEqual(response.status_code, 500)
        events = [item.get_event() for envelope in envelopes for item in envelope.items if item.type == 'event']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['exception']['values'][0]['type'], 'RuntimeError')
        self.assertNotIn('fake-private', json.dumps(events))


class ErrorReportingTests(SimpleTestCase):
    def test_static_history_error_code_survives_redaction_without_exception_runtime_values(self):
        from .exceptions import HistoryErrorCode, HistoryIntegrityError
        error = HistoryIntegrityError(HistoryErrorCode.MISSING_HOURLY)
        error.args = ('private-credential-value',)
        event = {'exception': {'values': [{'type': 'HistoryIntegrityError', 'value': str(error)}]}}
        safe = private_error_event(event, {'exc_info': (type(error), error, None)})
        self.assertEqual(safe['exception']['values'][0]['value'], HistoryErrorCode.MISSING_HOURLY.value)
        self.assertNotIn('private-credential-value', json.dumps(safe))

    def test_disabled_reporting_does_not_initialize_sdk(self):
        with patch('monitor.error_reporting.sentry_sdk.init') as initialize:
            configure_error_reporting('')
        initialize.assert_not_called()

    def test_configuration_is_error_only_and_omits_private_data(self):
        with patch('monitor.error_reporting.sentry_sdk.init') as initialize:
            configure_error_reporting('https://public@example.com/1')
        values = initialize.call_args.kwargs
        self.assertFalse(values['send_default_pii'])
        self.assertFalse(values['include_local_variables'])
        self.assertEqual(values['max_request_body_size'], 'never')
        self.assertEqual(values['traces_sample_rate'], 0)
        self.assertEqual(values['profiles_sample_rate'], 0)
        self.assertIs(values['before_send'], private_error_event)

    def test_outgoing_event_allowlist_drops_oauth_tokens_forms_headers_logs_and_locals(self):
        event = {
            'event_id': 'a' * 32, 'level': 'error', 'environment': 'test',
            'request': {'url': 'https://example.org/callback/?code=secret', 'headers': {'Authorization': 'secret'}},
            'user': {'email': 'secret'}, 'breadcrumbs': [{'message': 'secret'}], 'extra': {'token': 'secret'},
            'exception': {'values': [{'type': 'RuntimeError', 'value': 'secret', 'stacktrace': {'frames': [
                {'filename': 'monitor/auth.py', 'lineno': 12, 'vars': {'password': 'secret'}, 'context_line': 'secret'},
            ]}}]},
        }
        safe = private_error_event(event, {})
        self.assertNotIn('secret', json.dumps(safe))
        self.assertEqual(safe['exception']['values'][0]['stacktrace']['frames'][0]['filename'], 'monitor/auth.py')
