"""Progressive rollups share retention invariants without purging source observations."""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase

from .history import DAY, HOUR, completed_hour, maintain_target
from .models import Check, DailyProbeStats, HistoryState, HourlyProbeStats, Incident, Target
from .tasks import aggregate_hours, aggregate_target_hours


class HourlyRollupTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 12, 10, tzinfo=dt_timezone.utc)
        self.target = Target.objects.create(name='Probe', url='https://example.com')
        Target.objects.filter(pk=self.target.pk).update(created_at=self.now - DAY * 3)
        self.target.refresh_from_db()

    def check(self, when, healthy=True, coverage=180):
        return Check.objects.create(target=self.target, checked_at=when, healthy=healthy,
                                    detail='Observation', duration_ms=120, coverage_seconds=coverage)

    def aggregate(self):
        for _ in range(10):
            if not maintain_target(self.target.pk, now=self.now, purge=False, hours_only=True):
                return
        self.fail('Rollup did not finish')

    def test_bounded_idempotent_hours_only_work_never_purges_or_snapshots(self):
        self.check(self.target.created_at, healthy=False)
        self.check(self.now - timedelta(minutes=1))
        incident = Incident.objects.create(target=self.target, started_at=self.target.created_at,
                                           confirmed_at=self.target.created_at + timedelta(minutes=1))
        with patch('monitor.tasks.probe') as probe, patch('monitor.tasks.send_notification') as send:
            self.assertTrue(maintain_target(self.target.pk, now=self.now, hours_only=True))
            self.assertLessEqual(HourlyProbeStats.objects.filter(target=self.target).count(), 24)
            self.aggregate()
        probe.assert_not_called()
        send.assert_not_called()
        self.assertEqual(Check.objects.filter(target=self.target).count(), 2)
        self.assertFalse(DailyProbeStats.objects.exists())
        incident.refresh_from_db()
        self.assertEqual(incident.service_name, '')
        state = HistoryState.objects.get(target=self.target)
        self.assertEqual(state.hourly_until, completed_hour(self.now))
        self.assertIsNone(state.raw_before)
        self.assertIsNone(state.daily_until)
        self.assertIsNone(state.last_success_at)
        original = list(HourlyProbeStats.objects.values_list('started_at', 'up_seconds', 'down_seconds'))
        self.assertFalse(maintain_target(self.target.pk, now=self.now, hours_only=True))
        self.assertEqual(
            list(HourlyProbeStats.objects.values_list('started_at', 'up_seconds', 'down_seconds')), original,
        )

    def test_waits_five_minutes_before_finalizing_an_hour(self):
        self.check(self.now.replace(hour=11, minute=59), coverage=300)
        self.now = self.now.replace(minute=4)
        self.aggregate()
        state = HistoryState.objects.get(target=self.target)
        self.assertEqual(state.hourly_until, self.now.replace(hour=11, minute=0))
        self.now = self.now.replace(minute=5)
        self.aggregate()
        state.refresh_from_db()
        self.assertEqual(state.hourly_until, self.now.replace(minute=0))
        last = HourlyProbeStats.objects.get(target=self.target, started_at=self.now.replace(hour=11, minute=0))
        self.assertEqual(last.up_seconds, 60)
        self.assertFalse(HourlyProbeStats.objects.filter(
            target=self.target, started_at__gte=state.hourly_until,
        ).exists())

    def test_failed_validation_keeps_watermark_and_all_source_observations(self):
        check = self.check(self.target.created_at)
        with patch('monitor.history.validate_stats', side_effect=ValueError('invalid bucket')):
            with self.assertRaises(ValueError):
                maintain_target(self.target.pk, now=self.now, hours_only=True)
        state = HistoryState.objects.get(target=self.target)
        self.assertIsNone(state.hourly_until)
        self.assertEqual(state.last_error, 'ValueError')
        self.assertFalse(HourlyProbeStats.objects.exists())
        self.assertTrue(Check.objects.filter(pk=check.pk).exists())

    def test_dispatches_only_due_probes_including_paused_probes(self):
        complete = Target.objects.create(name='Complete', url='https://complete.example')
        paused = Target.objects.create(name='Paused', url='https://paused.example', enabled=False)
        HistoryState.objects.create(target=complete, hourly_until=completed_hour(self.now))
        HistoryState.objects.create(target=paused, hourly_until=completed_hour(self.now) - HOUR)
        with patch('monitor.tasks.timezone.now', return_value=self.now), \
                patch.object(aggregate_target_hours, 'apply_async') as queued:
            aggregate_hours()
        self.assertEqual({call.kwargs['args'][0] for call in queued.call_args_list}, {self.target.pk, paused.pk})
        for call in queued.call_args_list:
            self.assertEqual(call.kwargs['expires'], 300)
            self.assertFalse(call.kwargs['retry'])

    def test_continuations_yield_without_retrying_observations(self):
        with patch('monitor.tasks.maintain_target', return_value=True) as maintain, \
                patch.object(aggregate_target_hours, 'apply_async') as queued:
            aggregate_target_hours(self.target.pk)
        maintain.assert_called_once_with(self.target.pk, purge=False, hours_only=True)
        queued.assert_called_once_with(args=[self.target.pk], countdown=1, expires=300, retry=False)
        with patch('monitor.tasks.maintain_target', return_value=False), \
                patch.object(aggregate_target_hours, 'apply_async') as queued:
            aggregate_target_hours(self.target.pk)
        queued.assert_not_called()

    def test_periodic_refresh_keeps_nightly_retention_schedule(self):
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['aggregate-hours']['schedule'], 300)
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['aggregate-hours']['options']['expires'], 300)
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['maintain-history']['schedule'].hour, {2})
