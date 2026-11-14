"""Confirmation timing, structured diagnostics and incident email rendering."""
import socket
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from .models import Check, Incident, Target
from .notifications import incident_notification, readable_duration, send_notification
from .probes import probe


@override_settings(PUBLIC_URL='https://status.example.org')
class IncidentEmailTests(TestCase):
    def setUp(self):
        self.start = timezone.now() - timedelta(seconds=72)
        self.target = Target.objects.create(name='<script>Service</script>', url='https://service.example.org')
        self.incident = Incident.objects.create(target=self.target, started_at=self.start,
                                                confirmed_at=self.start + timedelta(seconds=60))
        for offset in (0, 60):
            Check.objects.create(target=self.target, checked_at=self.start + timedelta(seconds=offset),
                                 healthy=False, detail='HTTP 503', duration_ms=1741,
                                 coverage_seconds=60, status_code=503)

    def test_outage_email_has_readable_times_failure_details_and_escaped_html(self):
        with patch('monitor.notifications.timezone.now', return_value=self.start + timedelta(seconds=72)):
            subject, body, html = incident_notification(self.target, self.incident)
        self.assertIn('INDISPONIBLE', subject)
        for value in ('1 min 12 s', 'HTTP 503', 'Erreur serveur', self.target.url, 'Premier échec', 'Panne confirmée'):
            self.assertIn(value, body)
        self.assertNotIn('<script>Service</script>', html)
        self.assertIn('&lt;script&gt;Service&lt;/script&gt;', html)
        self.assertIn(f'https://status.example.org/probes/{self.target.pk}/', html)

    def test_recovery_keeps_failure_reason_and_adds_total_duration_and_success(self):
        self.incident.ended_at = self.start + timedelta(seconds=192)
        Check.objects.create(target=self.target, checked_at=self.incident.ended_at, healthy=True,
                             detail='HTTP 200', duration_ms=123, coverage_seconds=180, status_code=200)
        subject, body, html = incident_notification(self.target, self.incident, recovered=True)
        self.assertIn('RÉTABLI', subject)
        self.assertIn('3 min 12 s', subject)
        for value in ('HTTP 503', 'HTTP 200', '123 ms', 'Rétablissement', 'Durée totale observée'):
            self.assertIn(value, body)
        self.assertIn('SERVICE RÉTABLI', html)

    @override_settings(POSTMARK_SERVER_TOKEN='test-token', EMAIL_FROM='alerts@example.org', EMAIL_TO='ops@example.org')
    @patch('monitor.notifications.requests.post')
    def test_postmark_receives_both_html_and_plain_text(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {'ErrorCode': 0}
        post.return_value.__enter__.return_value = response
        subject, body, html = incident_notification(self.target, self.incident)
        send_notification(subject, body, html_body=html)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['TextBody'], body)
        self.assertEqual(payload['HtmlBody'], html)


class DiagnosticTests(SimpleTestCase):
    def test_readable_duration_omits_empty_units_and_includes_days(self):
        self.assertEqual(readable_duration(0), '0 s')
        self.assertEqual(readable_duration(60), '1 min')
        self.assertEqual(readable_duration(72), '1 min 12 s')
        self.assertEqual(readable_duration(90061), '1 j 1 h 1 min 1 s')

    def test_network_error_categories_do_not_expose_exception_content(self):
        dns = requests.ConnectionError('private-provider-detail')
        dns.__cause__ = socket.gaierror('private-dns-detail')
        for error, expected in (
            (dns, 'Erreur DNS'),
            (requests.exceptions.SSLError('private-provider-detail'), 'Erreur TLS'),
            (requests.ConnectionError('private-provider-detail'), 'Connexion impossible'),
            (requests.Timeout('private-provider-detail'), 'Délai de réponse dépassé'),
            (requests.TooManyRedirects('private-provider-detail'), 'Trop de redirections'),
        ):
            with self.subTest(expected=expected), patch('monitor.probes.requests.Session') as session:
                session.return_value.__enter__.return_value.request.side_effect = error
                result = probe(Target(name='Service', url='https://service.example.org'))
                self.assertFalse(result.healthy)
                self.assertEqual(result.detail, expected)
                self.assertNotIn('private', result.detail)


class ConfirmationMigrationTests(TransactionTestCase):
    def test_existing_single_failure_settings_gain_confirmation_and_custom_thresholds_survive(self):
        before = [('monitor', '0006_existing_access')]
        after = [('monitor', '0007_confirm_outage_before_alert')]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            legacy = executor.loader.project_state(before).apps.get_model('monitor', 'Target')
            legacy.objects.create(name='Default', url='https://default.example.org', failure_threshold=1)
            legacy.objects.create(name='Custom', url='https://custom.example.org', failure_threshold=4)
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            migrated = executor.loader.project_state(after).apps.get_model('monitor', 'Target')
            self.assertEqual(migrated.objects.get(name='Default').failure_threshold, 2)
            self.assertEqual(migrated.objects.get(name='Custom').failure_threshold, 4)
        finally:
            MigrationExecutor(connection).migrate(after)
