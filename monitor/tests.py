import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from .auth import OwnerSocialAdapter
from .forms import TargetForm
from .metrics import availability
from .models import Target, Incident, Check
from .notifications import send_notification
from .probes import ProbeResult, probe
from .tasks import check_target, dispatch_checks


class MonitoringTests(TestCase):
    def setUp(self):
        self.target = Target.objects.create(name='Service', url='https://example.org', interval_minutes=1, tls_checked_at=timezone.now())
        self.now = timezone.now()

    def run_check(self, healthy):
        self.now += timedelta(seconds=60)
        Target.objects.filter(pk=self.target.pk).update(next_check_at=self.now - timedelta(seconds=1))
        with patch('monitor.tasks.timezone.now', return_value=self.now), patch('monitor.tasks.probe', return_value=ProbeResult(healthy, 'HTTP 200' if healthy else 'HTTP 503', 12, 200 if healthy else 503)):
            check_target(self.target.pk)

    @patch('monitor.tasks.send_notification')
    def test_one_notification_per_incident_then_recovery(self, send):
        self.run_check(False)
        send.assert_not_called()
        for _ in range(4):
            self.run_check(False)
        self.assertEqual(send.call_count, 1)
        self.run_check(True)
        self.run_check(True)
        self.assertEqual(send.call_count, 2)
        self.assertIsNotNone(Incident.objects.get().ended_at)
        self.run_check(False)
        self.assertEqual(send.call_count, 2)
        self.run_check(False)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(Incident.objects.count(), 2)
        self.assertEqual(Check.objects.count(), 9)
        self.assertEqual(Check.objects.first().status_code, 503)

    @patch('monitor.tasks.send_notification')
    def test_channel_is_snapshotted_for_recovery(self, send):
        Target.objects.filter(pk=self.target.pk).update(notification_channel='telegram', telegram_chat_id='123')
        self.run_check(False)
        self.run_check(False)
        Target.objects.filter(pk=self.target.pk).update(notification_channel='slack')
        self.run_check(True)
        self.assertEqual(send.call_args.args[2:], ('telegram', '123'))

    @patch('monitor.tasks.send_notification')
    def test_threshold_and_failed_delivery(self, send):
        self.target.failure_threshold = 2
        self.target.save()
        self.run_check(False)
        self.assertEqual(Incident.objects.count(), 0)
        send.side_effect = ValueError('offline')
        self.run_check(False)
        self.assertIsNone(Incident.objects.get().down_sent_at)
        send.side_effect = None
        self.run_check(True)
        self.assertEqual(send.call_count, 1)

    @patch('monitor.tasks.send_notification')
    def test_notification_delay_does_not_repeat_http(self, send):
        Target.objects.filter(pk=self.target.pk).update(notification_delay_minutes=2)
        self.run_check(False)
        send.assert_not_called()
        self.run_check(False)
        send.assert_not_called()
        self.run_check(False)
        send.assert_called_once()
        self.run_check(False)
        self.assertEqual(send.call_count, 1)

    @patch('monitor.tasks.send_notification')
    def test_failed_recovery_is_pending_until_next_check(self, send):
        self.run_check(False)
        self.run_check(False)
        send.side_effect = ValueError('offline')
        self.run_check(True)
        self.assertIsNone(Incident.objects.get().recovery_sent_at)
        send.side_effect = None
        self.run_check(True)
        self.assertIsNotNone(Incident.objects.get().recovery_sent_at)

    @patch('monitor.tasks.probe')
    def test_disabled_or_not_due_does_not_probe(self, request):
        Target.objects.filter(pk=self.target.pk).update(enabled=False)
        check_target(self.target.pk)
        Target.objects.filter(pk=self.target.pk).update(enabled=True, next_check_at=timezone.now() + timedelta(minutes=1))
        check_target(self.target.pk)
        request.assert_not_called()

    @patch('monitor.tasks.check_target.apply_async')
    def test_dispatch_has_expiry_and_no_retry(self, enqueue):
        dispatch_checks()
        enqueue.assert_called_once_with(args=[self.target.pk], expires=30, retry=False)
        self.assertEqual(check_target.max_retries, 0)

    @patch('monitor.tasks.send_notification')
    def test_confirmation_is_durable_due_after_sixty_seconds_without_early_alert(self, send):
        Target.objects.filter(pk=self.target.pk).update(interval_minutes=5)
        start = timezone.now()
        failure = ProbeResult(False, 'HTTP 503', 12, 503)
        with patch('monitor.tasks.probe', return_value=failure) as request:
            with patch('monitor.tasks.timezone.now', return_value=start):
                check_target(self.target.pk)
            self.target.refresh_from_db()
            self.assertEqual(self.target.next_check_at, start + timedelta(seconds=60))
            self.assertEqual(Check.objects.get().coverage_seconds, 60)
            self.assertFalse(Incident.objects.exists())
            send.assert_not_called()
            with patch('monitor.tasks.timezone.now', return_value=start + timedelta(seconds=59)):
                check_target(self.target.pk)
            self.assertEqual(request.call_count, 1)
            with patch('monitor.tasks.timezone.now', return_value=start + timedelta(seconds=60)):
                check_target(self.target.pk)
        incident = Incident.objects.get()
        self.assertEqual(incident.started_at, start)
        self.assertEqual(incident.confirmed_at, start + timedelta(seconds=60))
        self.assertEqual(request.call_count, 2)
        send.assert_called_once()
        self.target.refresh_from_db()
        self.assertEqual(self.target.next_check_at, start + timedelta(seconds=360))

    @patch('monitor.tasks.send_notification')
    def test_isolated_failure_counts_as_downtime_without_outage_or_recovery_alert(self, send):
        Target.objects.filter(pk=self.target.pk).update(interval_minutes=5)
        start = timezone.now()
        for offset, healthy in ((0, False), (60, True)):
            with patch('monitor.tasks.timezone.now', return_value=start + timedelta(seconds=offset)), \
                    patch('monitor.tasks.probe', return_value=ProbeResult(healthy, 'HTTP 200' if healthy else 'HTTP 503', 10)):
                check_target(self.target.pk)
        send.assert_not_called()
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(Check.objects.count(), 2)
        stats = availability(list(Check.objects.order_by('checked_at')), start, start + timedelta(seconds=300))
        self.assertEqual(stats, {'uptime': '80.00%', 'coverage': '100.0%'})
        self.target.refresh_from_db()
        self.assertEqual(self.target.consecutive_failures, 0)
        self.assertIsNone(self.target.first_failure_at)


class MetricsTests(SimpleTestCase):
    def test_gaps_are_unknown(self):
        now = timezone.now()
        checks = [SimpleNamespace(checked_at=now-timedelta(minutes=10), coverage_seconds=60, healthy=True), SimpleNamespace(checked_at=now-timedelta(minutes=1), coverage_seconds=60, healthy=False)]
        self.assertEqual(availability(checks, now-timedelta(minutes=10), now), {'uptime': '50.00%', 'coverage': '20.0%'})
        self.assertEqual(availability([], now-timedelta(days=1), now)['uptime'], '—')

    def test_no_double_counting(self):
        now = timezone.now()
        checks = [SimpleNamespace(checked_at=now-timedelta(minutes=2), coverage_seconds=300, healthy=True), SimpleNamespace(checked_at=now-timedelta(minutes=1), coverage_seconds=60, healthy=False)]
        self.assertEqual(availability(checks, now-timedelta(minutes=2), now)['uptime'], '50.00%')


@override_settings(OWNER_EMAIL='owner@example.org')
class InterfaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('owner', email='owner@example.org', is_staff=True, is_superuser=True)
        self.target = Target.objects.create(name='<script>Service</script>', url='https://example.org', auth_password='saved-secret')
        self.client.force_login(self.user)

    def form_data(self):
        return {'name': 'New', 'url': 'https://new.example.org', 'enabled': 'on', 'interval_minutes': 2,
                'failure_threshold': 2, 'notification_delay_minutes': 0, 'timeout_seconds': 10, 'ip_version': 'any',
                'verify_tls': 'on', 'follow_redirects': 'on', 'method': 'GET', 'auth_type': 'none',
                'body_type': 'raw', 'expected_status_codes': '200-299', 'notification_channel': 'telegram',
                'request_headers': '{}', 'response_headers': '{}'}

    def test_all_pages_render_and_escape(self):
        for url in ['/', '/incidents/', '/styleguide/', '/probes/new/', f'/probes/{self.target.pk}/', f'/probes/{self.target.pk}/failures/', f'/probes/{self.target.pk}/alerts/', f'/probes/{self.target.pk}/edit/', f'/probes/{self.target.pk}/delete/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, '<script>Service</script>')
                self.assertNotContains(response, 'saved-secret')
        self.assertContains(self.client.get('/'), '&lt;script&gt;')

    def test_new_accounts_are_read_only(self):
        other = get_user_model().objects.create_user('other', email='other@example.org')
        self.client.force_login(other)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get(f'/probes/{self.target.pk}/edit/').status_code, 403)
        self.assertEqual(self.client.post(f'/probes/{self.target.pk}/toggle/').status_code, 403)
        self.client.logout()
        self.assertContains(self.client.get('/'), 'Compte local')

    def test_create_and_update_without_replacing_secret(self):
        response = self.client.post('/probes/new/', self.form_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Target.objects.get(name='New').notification_channel, 'telegram')
        data = self.form_data()
        data['url'] = self.target.url
        self.client.post(f'/probes/{self.target.pk}/edit/', data)
        self.target.refresh_from_db()
        self.assertEqual(self.target.auth_password, 'saved-secret')

    def test_probe_urls_assume_https_and_preserve_explicit_http(self):
        for entered, expected in [
            ('new.example.org', 'https://new.example.org'),
            ('  new.example.org/health/  ', 'https://new.example.org/health/'),
            ('new.example.org:8443/health', 'https://new.example.org:8443/health'),
            ('127.0.0.1:8080/health', 'https://127.0.0.1:8080/health'),
            ('//new.example.org/health', 'https://new.example.org/health'),
            ('http://new.example.org/health', 'http://new.example.org/health'),
        ]:
            with self.subTest(entered=entered):
                data = self.form_data()
                data['url'] = entered
                form = TargetForm(data)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data['url'], expected)
        data = self.form_data()
        data['url'] = 'new.example.org/health'
        self.assertEqual(self.client.post('/probes/new/', data).status_code, 302)
        self.assertEqual(Target.objects.get(name='New').url, 'https://new.example.org/health')

    def test_probe_url_normalization_keeps_security_validation(self):
        for value in ('ftp://new.example.org', 'user:password@new.example.org',
                      'new.example.org/?token=secret', 'new.example.org/#fragment'):
            with self.subTest(value=value):
                data = self.form_data()
                data['url'] = value
                self.assertIn('url', TargetForm(data).errors)

    def test_validation_of_intervals_headers_and_json(self):
        for field, value in [('interval_minutes', 6), ('timeout_seconds', 0), ('request_headers', '{"X-Test":"a\\nb"}'), ('expected_status_codes', '600'), ('auth_type', 'bearer')]:
            data = self.form_data()
            data[field] = value
            form = TargetForm(data)
            self.assertFalse(form.is_valid(), (field, form.errors))
        data = self.form_data()
        data.update(body_type='json', body='{broken')
        self.assertIn('body', TargetForm(data).errors)

    @patch('monitor.views.send_notification')
    def test_outage_simulation_uses_saved_channel_without_recording_history(self, send):
        now = timezone.now()
        Check.objects.create(target=self.target, checked_at=now, healthy=False, detail='HTTP 503',
                             duration_ms=12, coverage_seconds=180, status_code=503)
        Incident.objects.create(target=self.target, started_at=now, confirmed_at=now, down_sent_at=now)
        for channel, destination in [('email', 'ops@example.org'), ('slack', ''), ('telegram', '987')]:
            with self.subTest(channel=channel):
                self.target.notification_channel = channel
                self.target.email_to = 'ops@example.org'
                self.target.telegram_chat_id = '987'
                self.target.save()
                target_before = Target.objects.filter(pk=self.target.pk).values().get()
                checks_before = list(Check.objects.values())
                incidents_before = list(Incident.objects.values())
                send.reset_mock()
                response = self.client.post(f'/probes/{self.target.pk}/simulate-outage/')
                self.assertRedirects(response, f'/probes/{self.target.pk}/edit/', fetch_redirect_response=False)
                send.assert_called_once()
                self.assertIn('SIMULATION', send.call_args.args[0])
                self.assertIn(self.target.url, send.call_args.args[1])
                self.assertEqual(send.call_args.args[2:], (channel, destination))
                self.assertEqual(Target.objects.filter(pk=self.target.pk).values().get(), target_before)
                self.assertEqual(list(Check.objects.values()), checks_before)
                self.assertEqual(list(Incident.objects.values()), incidents_before)

    @patch('monitor.views.send_notification', side_effect=ValueError('private-provider-detail'))
    def test_rejected_outage_simulation_does_not_record_or_expose_provider_details(self, send):
        response = self.client.post(f'/probes/{self.target.pk}/simulate-outage/', follow=True)
        self.assertContains(response, 'Simulation refusée.')
        self.assertNotContains(response, 'private-provider-detail')
        self.assertFalse(Check.objects.exists())
        self.assertFalse(Incident.objects.exists())

    @patch('monitor.views.send_notification')
    def test_outage_simulation_requires_editor_post_and_csrf(self, send):
        url = f'/probes/{self.target.pk}/simulate-outage/'
        self.assertEqual(self.client.get(url).status_code, 405)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(url).status_code, 403)
        viewer = get_user_model().objects.create_user('simulation-viewer')
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)
        send.assert_not_called()

    def test_mutations_require_post_and_csrf(self):
        for suffix in ['toggle', 'test', 'test-notification']:
            self.assertEqual(self.client.get(f'/probes/{self.target.pk}/{suffix}/').status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(f'/probes/{self.target.pk}/toggle/').status_code, 403)

    @patch('monitor.views.probe', return_value=ProbeResult(True, 'HTTP 200', 23, 200))
    def test_manual_check_does_not_create_incident_or_check(self, probe_mock):
        self.assertContains(self.client.post(f'/probes/{self.target.pk}/test/'), 'Requête conforme')
        self.assertEqual(Check.objects.count(), 0)
        self.assertEqual(Incident.objects.count(), 0)

    @patch('monitor.views.send_notification')
    def test_explicit_notification_test_uses_selected_channel(self, send):
        self.target.notification_channel = 'telegram'
        self.target.telegram_chat_id = '123'
        self.target.save()
        self.client.post(f'/probes/{self.target.pk}/test-notification/')
        self.assertEqual(send.call_args.args[2:], ('telegram', '123'))

    def test_failures_filter_by_code(self):
        for code in (503, 502):
            Check.objects.create(target=self.target, checked_at=timezone.now(), healthy=False, detail=f'HTTP {code}', duration_ms=12, coverage_seconds=60, status_code=code)
        response = self.client.get(f'/probes/{self.target.pk}/failures/?status=503')
        self.assertEqual(len(response.context['page']), 1)
        self.assertEqual(response.context['page'][0].status_code, 503)


@override_settings(OWNER_EMAIL='owner@example.org')
class GoogleAccessTests(TestCase):
    def social(self, email, verified=True, provider='google'):
        return SocialLogin(user=get_user_model()(email=email), account=SocialAccount(provider=provider, uid='123'), email_addresses=[EmailAddress(email=email, verified=verified, primary=True)])

    def test_only_verified_google_owner_is_allowed(self):
        adapter = OwnerSocialAdapter()
        self.assertTrue(adapter.allowed(self.social('owner@example.org')))
        for social in [self.social('other@example.org'), self.social('owner@example.org', False), self.social('owner@example.org', provider='other')]:
            with self.assertRaises(ImmediateHttpResponse) as error:
                adapter.pre_social_login(RequestFactory().get('/'), social)
            self.assertEqual(error.exception.response.status_code, 403)

    @override_settings(OWNER_EMAIL='')
    def test_missing_owner_fails_closed(self):
        self.assertFalse(OwnerSocialAdapter().allowed(self.social('owner@example.org')))

    def test_password_and_signup_endpoints_are_absent(self):
        for path in ['/accounts/signup/', '/accounts/password/reset/', '/accounts/password/change/']:
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertContains(self.client.get('/admin/login/'), 'name="username"')

    def test_inactive_owner_cannot_log_in(self):
        get_user_model().objects.create_user('inactive', email='owner@example.org', is_active=False)
        with self.assertRaises(ImmediateHttpResponse):
            OwnerSocialAdapter().pre_social_login(RequestFactory().get('/'), self.social('owner@example.org'))


@override_settings(POSTMARK_SERVER_TOKEN='secret', EMAIL_FROM='a@example.org', EMAIL_TO='b@example.org', POSTMARK_MESSAGE_STREAM='outbound', SLACK_WEBHOOK_URL='https://hooks.slack.com/services/TEST/TEST/secret', TELEGRAM_BOT_TOKEN='123:secret', TELEGRAM_CHAT_ID='123')
class NotificationTests(TestCase):
    @patch('monitor.notifications.requests.post')
    def test_all_three_contracts(self, post):
        response = Mock(status_code=200, text='ok')
        post.return_value.__enter__.return_value = response
        for channel, data in [('email', {'ErrorCode': 0}), ('slack', {}), ('telegram', {'ok': True})]:
            response.json.return_value = data
            send_notification('Panne', 'Service indisponible', channel)
            kwargs = post.call_args.kwargs
            self.assertFalse(kwargs['allow_redirects'])
            if channel == 'email':
                self.assertEqual(kwargs['headers']['X-Postmark-Server-Token'], 'secret')
                self.assertEqual(kwargs['json']['MessageStream'], 'outbound')
            elif channel == 'telegram':
                self.assertEqual(kwargs['json']['chat_id'], '123')
            else:
                self.assertFalse(kwargs['json']['mrkdwn'])

    @patch('monitor.notifications.requests.post')
    def test_refusals_and_errors_never_leak_tokens(self, post):
        import requests
        post.side_effect = requests.ConnectionError('https://secret-token')
        with self.assertRaisesMessage(ValueError, 'Service de notification inaccessible') as error:
            send_notification('Test', 'Test', 'telegram')
        self.assertNotIn('secret-token', str(error.exception))
        post.side_effect = None
        response = Mock(status_code=200)
        response.json.return_value = {'ErrorCode': 10, 'Message': 'secret'}
        post.return_value.__enter__.return_value = response
        with self.assertRaisesMessage(ValueError, 'Notification refusée'):
            send_notification('Panne', 'Service indisponible')


class ProbeHTTPTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.path, self.headers.get('Authorization'), self.headers.get('X-Probe')))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/ok')
                else:
                    self.send_response(503 if self.path == '/bad' else 200)
                    self.send_header('X-Health', 'ready')
                self.end_headers()
                self.wfile.write(b'Application READY')
            def do_POST(self):
                cls.requests.append((self.path, self.rfile.read(int(self.headers.get('Content-Length', '0')))))
                self.send_response(201)
                self.end_headers()
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        super().tearDownClass()

    def target(self, path='/ok', **kwargs):
        return Target(name='HTTP', url=f'http://127.0.0.1:{self.server.server_port}{path}', **kwargs)

    def test_keyword_headers_and_case(self):
        result = probe(self.target(keyword='ready', keyword_ignore_case=True, response_headers={'x-health': 'ready'}))
        self.assertTrue(result.healthy)
        self.assertFalse(probe(self.target(keyword='absent')).healthy)
        self.assertFalse(probe(self.target(response_headers={'X-Health': 'wrong'})).healthy)

    def test_redirect_and_failure_without_retry(self):
        self.assertTrue(probe(self.target('/redirect')).healthy)
        self.assertEqual(probe(self.target('/redirect', follow_redirects=False)).status_code, 302)
        before = len(self.requests)
        self.assertFalse(probe(self.target('/bad')).healthy)
        self.assertEqual(len(self.requests), before + 1)

    def test_auth_custom_headers_and_cache_buster(self):
        result = probe(self.target(auth_type='bearer', auth_token='token', request_headers={'X-Probe': 'yes'}, cache_buster=True))
        self.assertTrue(result.healthy)
        path, auth, header = self.requests[-1]
        self.assertIn('_uptime=', path)
        self.assertEqual((auth, header), ('Bearer token', 'yes'))

    def test_json_post_and_ipv4(self):
        result = probe(self.target('/post', method='POST', body_type='json', body='{"ok": true}', ip_version='4'))
        self.assertTrue(result.healthy)
        self.assertEqual(json.loads(self.requests[-1][1]), {'ok': True})


class ConcurrentChecksTests(TransactionTestCase):
    def test_duplicate_task_skips_locked_target(self):
        target = Target.objects.create(name='Concurrent', url='https://example.org', tls_checked_at=timezone.now())
        entered, release = Event(), Event()
        def slow_probe(target):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError('Concurrent test timed out')
            return ProbeResult(True, 'HTTP 200', 10, 200)
        def run():
            try:
                check_target(target.pk)
            finally:
                connections.close_all()
        with patch('monitor.tasks.probe', side_effect=slow_probe) as request:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(run)
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    pool.submit(run).result(timeout=5)
                finally:
                    release.set()
                first.result(timeout=5)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(Check.objects.count(), 1)


class InvitationTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_user('operator', email='operator@example.org', is_staff=True, is_superuser=True)
        self.client.force_login(self.operator)

    def invite(self, email='guest@example.org'):
        from .invitations import create_invitation
        return create_invitation(email)

    def test_invitation_page_is_available_to_admin(self):
        self.assertEqual(self.client.get('/invitations/').status_code, 200)

    @patch('monitor.invitations.send_notification')
    def test_invitation_precreates_inactive_account_and_emails_link(self, send):
        from .models import Invitation
        response = self.client.post('/invitations/', {'email': 'Guest@Example.org', 'role': 'viewer'})
        self.assertEqual(response.status_code, 302)
        invitation = Invitation.objects.get()
        self.assertEqual(invitation.user.email, 'guest@example.org')
        self.assertFalse(invitation.user.is_active)
        self.assertFalse(invitation.user.has_usable_password())
        self.assertEqual(send.call_args.args[2:], ('email', 'guest@example.org'))
        self.assertIn('/invitations/accept/', send.call_args.args[1])

    def test_accept_sets_password_and_preserves_viewer_role(self):
        from .invitations import invitation_token
        invitation = self.invite()
        self.client.logout()
        url = f'/invitations/accept/{invitation_token(invitation)}/'
        response = self.client.post(url, {'password': 'A-long-unique-password-2026!', 'password_confirm': 'A-long-unique-password-2026!'})
        self.assertRedirects(response, '/')
        invitation.refresh_from_db()
        invitation.user.refresh_from_db()
        self.assertTrue(invitation.user.is_active)
        self.assertFalse(invitation.user.is_superuser)
        self.assertTrue(invitation.user.check_password('A-long-unique-password-2026!'))
        self.assertIsNotNone(invitation.accepted_at)
        self.assertEqual(self.client.get('/probes/new/').status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_expired_revoked_and_reissued_tokens_fail(self):
        from .invitations import invitation_token, get_invitation, create_invitation
        invitation = self.invite()
        token = invitation_token(invitation)
        invitation.expires_at = timezone.now()-timedelta(seconds=1)
        invitation.save()
        self.assertIsNone(get_invitation(token))
        replacement = create_invitation(invitation.email)
        self.assertIsNone(get_invitation(token))
        new_token = invitation_token(replacement)
        self.client.post(f'/invitations/{invitation.pk}/revoke/')
        self.assertIsNone(get_invitation(new_token))
        self.assertIsNone(get_invitation(new_token+'tampered'))

    def test_google_accepts_only_verified_invited_address(self):
        invitation = self.invite()
        adapter = OwnerSocialAdapter()
        social = SocialLogin(user=get_user_model()(email=invitation.email), account=SocialAccount(provider='google', uid='invited'), email_addresses=[EmailAddress(email=invitation.email, verified=True)])
        self.assertTrue(adapter.allowed(social))
        with patch.object(social, 'connect') as connect:
            adapter.pre_social_login(RequestFactory().get('/'), social)
            connect.assert_called_once()
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)
        self.assertTrue(invitation.user.is_active)

    def test_weak_password_does_not_activate(self):
        from .invitations import invitation_token
        invitation = self.invite()
        response = self.client.post(f'/invitations/accept/{invitation_token(invitation)}/', {'password': '123', 'password_confirm': '123'})
        self.assertEqual(response.status_code, 200)
        invitation.user.refresh_from_db()
        self.assertFalse(invitation.user.is_active)

    def test_local_admin_login_still_works(self):
        local = get_user_model().objects.create_superuser('local', 'local@example.org', 'Local-long-password-2026!')
        self.client.logout()
        response = self.client.post('/admin/login/?next=/', {'username': local.username, 'password': 'Local-long-password-2026!', 'next': '/'})
        self.assertRedirects(response, '/')


class LanguageAndProfileTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('language-user', email='language@example.org', password='Existing-password-2026!')
        self.client.force_login(self.user)

    def test_browser_language_without_profile_does_not_write_preference(self):
        from .models import UserProfile
        self.client.cookies['django_language'] = 'fr'
        response = self.client.get('/', HTTP_ACCEPT_LANGUAGE='de-CH,en;q=0.8')
        self.assertContains(response, 'Your probes')
        self.assertEqual(response.headers['Content-Language'], 'en')
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())
        self.assertNotContains(response, '>Auto<')

    def test_selection_survives_new_session_and_overrides_browser(self):
        from .models import UserProfile
        response = self.client.post('/profile/language/', {'language': 'en', 'next': '/profile/'})
        self.assertRedirects(response, '/profile/', fetch_redirect_response=False)
        self.assertEqual(UserProfile.objects.get(user=self.user).language, 'en')
        another = Client()
        another.force_login(self.user)
        response = another.get('/', HTTP_ACCEPT_LANGUAGE='fr-CH')
        self.assertContains(response, 'Your probes')
        self.assertEqual(response.headers['Content-Language'], 'en')
        self.assertIn('Accept-Language', response.headers['Vary'])
        self.assertIn('Cookie', response.headers['Vary'])

    def test_invalid_preference_get_csrf_and_external_redirect(self):
        from .models import UserProfile
        self.assertEqual(self.client.get('/profile/language/').status_code, 405)
        for language in ['de', '', 'en<script>']:
            self.assertEqual(self.client.post('/profile/language/', {'language': language}).status_code, 400)
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post('/profile/language/', {'language': 'en'}).status_code, 403)
        response = self.client.post('/profile/language/', {'language': 'fr', 'next': 'https://external.example.org/'})
        self.assertEqual(response.headers['Location'], '/')

    def test_anonymous_uses_browser_and_cannot_save(self):
        self.client.logout()
        self.assertContains(self.client.get('/login/', HTTP_ACCEPT_LANGUAGE='en-US'), 'Local account')
        self.assertEqual(self.client.post('/profile/language/', {'language': 'en'}).status_code, 302)
        self.assertEqual(self.client.get('/profile/').status_code, 302)

    def test_profile_saves_name_language_and_username_but_not_email_or_role(self):
        from .models import UserProfile
        response = self.client.post('/profile/', {'first_name': 'Alex', 'last_name': 'Doe', 'username': 'new-local', 'language': 'en', 'email': 'other@example.org', 'role': 'admin', 'is_superuser': True})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.get_full_name(), 'Alex Doe')
        self.assertEqual(self.user.username, 'new-local')
        self.assertEqual(self.user.email, 'language@example.org')
        self.assertFalse(self.user.is_superuser)
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.language, 'en')
        self.assertEqual(profile.role, UserProfile.Role.VIEWER)

    def test_password_change_preserves_session(self):
        response = self.client.post('/profile/password/', {'old_password': 'Existing-password-2026!', 'new_password1': 'Replacement-password-2026!', 'new_password2': 'Replacement-password-2026!'})
        self.assertRedirects(response, '/profile/', fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Replacement-password-2026!'))
        self.assertEqual(self.client.get('/profile/').status_code, 200)

    def test_google_only_account_can_set_local_password(self):
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.post('/profile/password/', {'new_password1': 'New-local-password-2026!', 'new_password2': 'New-local-password-2026!'})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('New-local-password-2026!'))

    def test_english_forms_labels_and_readonly_views(self):
        from .models import UserProfile
        UserProfile.objects.create(user=self.user, language='en')
        self.assertContains(self.client.get('/profile/'), 'Your information')
        self.assertContains(self.client.get('/profile/password/'), 'Change password')
        editor = get_user_model().objects.create_user('editor-lang', email='editor-lang@example.org')
        UserProfile.objects.create(user=editor, language='en', role=UserProfile.Role.EDITOR)
        self.client.force_login(editor)
        response = self.client.get('/probes/new/')
        self.assertContains(response, 'Expected response')
        self.assertContains(response, 'Notification channel')
        self.assertContains(response, 'HTTP username')


class AccessAndPlatformTests(TestCase):
    def setUp(self):
        from .models import UserProfile
        self.viewer = get_user_model().objects.create_user('reader', email='reader@example.org', password='Reader-password-2026!')
        self.editor = get_user_model().objects.create_user('editor', email='editor@example.org')
        UserProfile.objects.create(user=self.editor, role=UserProfile.Role.EDITOR)
        self.admin = get_user_model().objects.create_superuser('platform-admin', 'admin@example.org', 'Admin-password-2026!')
        self.target = Target.objects.create(name='Protected', url='https://example.org')

    def test_viewer_reads_but_cannot_mutate_or_access_platform(self):
        self.client.force_login(self.viewer)
        for suffix in ['', 'failures/', 'alerts/']:
            self.assertEqual(self.client.get(f'/probes/{self.target.pk}/{suffix}').status_code, 200)
        for path in [f'/probes/{self.target.pk}/edit/', f'/probes/{self.target.pk}/delete/', '/probes/new/', '/platform/', '/invitations/', '/admin/']:
            self.assertIn(self.client.get(path).status_code, [403, 302])
        for suffix in ['toggle', 'test', 'test-notification']:
            self.assertEqual(self.client.post(f'/probes/{self.target.pk}/{suffix}/').status_code, 403)
        self.assertEqual(self.client.post('/platform/', {}).status_code, 403)
        self.assertEqual(self.client.post(f'/platform/users/{self.viewer.pk}/role/', {'role': 'admin'}).status_code, 403)
        self.assertNotContains(self.client.get('/'), '/probes/new/')
        self.assertNotContains(self.client.get('/'), '/platform/')

    def test_editor_can_edit_all_probes_but_not_platform_or_invitations(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get('/probes/new/').status_code, 200)
        self.assertEqual(self.client.get(f'/probes/{self.target.pk}/edit/').status_code, 200)
        self.assertEqual(self.client.post(f'/probes/{self.target.pk}/toggle/').status_code, 302)
        for path in ['/platform/', '/invitations/']:
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_reader_local_login_does_not_require_django_staff(self):
        response = self.client.post('/login/local/', {'username': 'reader', 'password': 'Reader-password-2026!'})
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/platform/').status_code, 403)

    def platform_data(self, **changes):
        data = {'postmark_server_token': '', 'email_from': 'alerts@example.org', 'email_to': 'operator@example.org', 'postmark_message_stream': 'outbound', 'slack_webhook_url': '', 'telegram_bot_token': '', 'telegram_chat_id': '', 'google_client_id': '', 'google_client_secret': ''}
        data.update(changes)
        return data

    def test_secrets_are_encrypted_masked_preserved_and_explicitly_cleared(self):
        from .models import PlatformSettings
        from .platform import platform_values
        self.client.force_login(self.admin)
        secret = 'secret-postmark-token-not-for-html'
        response = self.client.post('/platform/', self.platform_data(postmark_server_token=secret))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(secret, PlatformSettings.objects.get().encrypted_secrets)
        self.assertEqual(platform_values()['postmark_server_token'], secret)
        self.assertNotContains(self.client.get('/platform/'), secret)
        self.client.post('/platform/', self.platform_data(email_to='new@example.org'))
        self.assertEqual(platform_values()['postmark_server_token'], secret)
        self.client.post('/platform/', self.platform_data(clear_postmark_server_token='on'))
        self.assertEqual(platform_values()['postmark_server_token'], '')

    @patch('monitor.notifications.requests.post')
    def test_database_credentials_override_environment_for_actual_sender(self, post):
        from .forms import PlatformSettingsForm
        response = Mock(status_code=200)
        response.json.return_value = {'ErrorCode': 0}
        post.return_value.__enter__.return_value = response
        form = PlatformSettingsForm(self.platform_data(postmark_server_token='database-token'))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        send_notification('Test', 'Test')
        self.assertEqual(post.call_args.kwargs['headers']['X-Postmark-Server-Token'], 'database-token')
        self.assertEqual(post.call_args.kwargs['json']['To'], 'operator@example.org')

    def test_admin_assigns_roles_and_cannot_remove_last_admin(self):
        from .models import UserProfile
        self.client.force_login(self.admin)
        self.client.post(f'/platform/users/{self.viewer.pk}/role/', {'role': 'editor'})
        self.assertEqual(UserProfile.objects.get(user=self.viewer).role, UserProfile.Role.EDITOR)
        self.client.post(f'/platform/users/{self.admin.pk}/role/', {'role': 'viewer'})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)
        self.assertEqual(self.client.get('/platform/').status_code, 200)

    @patch('monitor.invitations.send_notification')
    def test_invitation_assigns_selected_role_and_no_unintended_admin(self, send):
        from .models import UserProfile, Invitation
        self.client.force_login(self.admin)
        self.client.post('/invitations/', {'email': 'new-reader@example.org', 'role': 'viewer'})
        invitation = Invitation.objects.get()
        self.assertEqual(invitation.user.profile.role, UserProfile.Role.VIEWER)
        self.assertFalse(invitation.user.is_superuser)
        self.assertFalse(invitation.user.is_staff)

    def test_admin_can_disable_and_reactivate_an_account_and_sessions_are_revoked(self):
        from django.test import Client
        from django.contrib.sessions.models import Session
        reader = Client()
        reader.force_login(self.viewer)
        session_key = reader.session.session_key
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(f'/platform/users/{self.viewer.pk}/toggle/').status_code, 405)
        self.client.post(f'/platform/users/{self.viewer.pk}/toggle/')
        self.viewer.refresh_from_db()
        self.assertFalse(self.viewer.is_active)
        self.assertFalse(Session.objects.filter(session_key=session_key).exists())
        self.assertEqual(reader.get('/profile/').status_code, 302)
        self.assertContains(self.client.get('/platform/'), 'reader')
        self.client.post(f'/platform/users/{self.viewer.pk}/toggle/')
        self.viewer.refresh_from_db()
        self.assertTrue(self.viewer.is_active)
        self.assertEqual(reader.get('/profile/').status_code, 302)

    def test_account_deletion_requires_post_and_preserves_probes(self):
        from .models import UserProfile
        self.client.force_login(self.admin)
        url = f'/platform/users/{self.viewer.pk}/delete/'
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(get_user_model().objects.filter(pk=self.viewer.pk).exists())
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(get_user_model().objects.filter(pk=self.viewer.pk).exists())
        self.assertFalse(UserProfile.objects.filter(user_id=self.viewer.pk).exists())
        self.assertTrue(Target.objects.filter(pk=self.target.pk).exists())

    def test_account_management_is_admin_only_and_self_removal_is_refused(self):
        for user in (self.viewer, self.editor):
            self.client.force_login(user)
            self.assertEqual(self.client.post(f'/platform/users/{self.viewer.pk}/toggle/').status_code, 403)
            self.assertEqual(self.client.post(f'/platform/users/{self.viewer.pk}/delete/').status_code, 403)
        self.client.force_login(self.admin)
        for action in ('toggle', 'delete'):
            self.client.post(f'/platform/users/{self.admin.pk}/{action}/')
            self.admin.refresh_from_db()
            self.assertTrue(self.admin.is_active)

    def test_pending_invitation_cannot_be_activated_by_toggle(self):
        from .invitations import create_invitation
        invitation = create_invitation('pending@example.org')
        self.client.force_login(self.admin)
        self.client.post(f'/platform/users/{invitation.user_id}/toggle/')
        invitation.user.refresh_from_db()
        self.assertFalse(invitation.user.is_active)
