"""Health reports the monitoring pipeline, independently of monitored outages."""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from .health import DISPATCH_HEARTBEAT_KEY, HEALTH_GRACE_SECONDS
from .models import Target
from .tasks import dispatch_checks


class HealthTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        cache.delete(DISPATCH_HEARTBEAT_KEY)
        self.addCleanup(cache.delete, DISPATCH_HEARTBEAT_KEY)

    def heartbeat(self, age=0):
        cache.set(DISPATCH_HEARTBEAT_KEY, (self.now - timedelta(seconds=age)).timestamp(), 180)

    def test_anonymous_health_without_probes_after_dispatch(self):
        dispatch_checks()
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
        self.assertIn('version', response.json())
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.head('/health/').status_code, 200)
        self.assertEqual(self.client.post('/health/').status_code, 405)

    def test_missing_or_stale_heartbeat_is_unavailable(self):
        self.assertEqual(self.client.get('/health/').status_code, 503)
        self.heartbeat(age=HEALTH_GRACE_SECONDS + 1)
        self.assertEqual(self.client.get('/health/').status_code, 503)

    def test_overdue_enabled_probe_but_not_outage_or_paused_probe(self):
        self.heartbeat()
        target = Target.objects.create(
            name='Unavailable service', url='https://example.org', consecutive_failures=3,
            next_check_at=self.now + timedelta(seconds=60),
        )
        self.assertEqual(self.client.get('/health/').status_code, 200)
        Target.objects.filter(pk=target.pk).update(
            next_check_at=self.now - timedelta(seconds=HEALTH_GRACE_SECONDS + 1),
        )
        self.assertEqual(self.client.get('/health/').status_code, 503)
        Target.objects.filter(pk=target.pk).update(enabled=False)
        self.assertEqual(self.client.get('/health/').status_code, 200)

    def test_dependency_failures_return_only_static_status_and_version(self):
        self.heartbeat()
        for dependency in ('monitor.health.Target.objects.filter', 'monitor.health.cache.get'):
            with self.subTest(dependency=dependency), patch(dependency, side_effect=OperationalError('secret')):
                response = self.client.get('/health/')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(set(response.json()), {'status', 'version'})
                self.assertNotIn('secret', response.content.decode())

    def test_request_neither_refreshes_heartbeat_nor_dispatches_work(self):
        with patch('monitor.health.cache.set') as save, patch('monitor.tasks.check_target.apply_async') as send:
            self.client.get('/health/')
        save.assert_not_called()
        send.assert_not_called()

    def test_failed_dispatch_does_not_refresh_heartbeat(self):
        Target.objects.create(name='Due', url='https://example.org', next_check_at=self.now - timedelta(seconds=1))
        with patch('monitor.tasks.check_target.apply_async', side_effect=RuntimeError('broker unavailable')):
            with self.assertRaises(RuntimeError):
                dispatch_checks()
        self.assertIsNone(cache.get(DISPATCH_HEARTBEAT_KEY))

    def test_cache_failure_does_not_prevent_dispatch(self):
        target = Target.objects.create(name='Due', url='https://example.org', next_check_at=self.now - timedelta(seconds=1))
        with patch('monitor.tasks.cache.set', side_effect=RuntimeError('secret')), \
                patch('monitor.tasks.check_target.apply_async') as send, self.assertLogs('monitor.tasks', level='WARNING') as logs:
            dispatch_checks()
        send.assert_called_once_with(args=[target.pk], expires=30, retry=False)
        self.assertNotIn('secret', ''.join(logs.output))
