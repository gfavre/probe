from datetime import datetime, timedelta, timezone as dt_timezone
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from celery.schedules import crontab
from django.conf import settings
from django.core.management import call_command
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from .history import DAY, HOUR, maintain_target
from .metrics import HistoryReader, availability
from .models import Check, DailyProbeStats, HistoryState, HourlyProbeStats, Incident, Target
from .notifications import incident_notification
from .tasks import record_result
from .probes import ProbeResult


class HistoryTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 14, 12, 10, tzinfo=dt_timezone.utc)
        self.start = self.now.replace(hour=0, minute=0) - DAY * 40
        self.target = Target.objects.create(name='Service', url='https://example.com')
        Target.objects.filter(pk=self.target.pk).update(created_at=self.start)
        self.target.refresh_from_db()

    def check(self, when, healthy=True, coverage=180, duration=100):
        return Check.objects.create(target=self.target, checked_at=when, healthy=healthy,
                                    detail='HTTP 200' if healthy else 'HTTP 503', status_code=200 if healthy else 503,
                                    duration_ms=duration, coverage_seconds=coverage)

    def run_maintenance(self, purge=True):
        for _ in range(200):
            if not maintain_target(self.target.pk, now=self.now, purge=purge):
                return
        self.fail('Maintenance did not finish')

    def test_preserves_availability_unknown_time_and_response_weighting(self):
        for age in (39, 31, 29, 8, 6, 1):
            when = self.now.replace(hour=10, minute=0) - DAY * age
            self.check(when, duration=100)
            self.check(when + timedelta(minutes=3), False, coverage=60)
            self.check(when + timedelta(minutes=4), duration=300)
        before = list(self.target.checks.order_by('checked_at'))
        expected = availability(before, self.start, self.now)
        self.run_maintenance()
        reader = HistoryReader(self.target, self.now)
        actual = reader.stats(self.start, self.now)
        self.assertEqual({k: actual[k] for k in expected}, expected)
        values = reader.values(self.start, self.now)
        self.assertEqual(values['success_count'], 12)
        self.assertEqual(values['failure_count'], 6)
        self.assertEqual(values['response_sum_ms'] / values['success_count'], 200)
        self.assertLess(float(actual['coverage'].rstrip('%')), 1)
        # One pre-cutoff anchor is retained for intervals crossing the raw boundary.
        self.assertEqual(self.target.checks.filter(healthy=True, checked_at__lt=self.now - DAY * 7).count(), 1)
        recent_failure = self.now.replace(hour=10, minute=3) - DAY * 29
        self.assertTrue(self.target.checks.filter(healthy=False, checked_at=recent_failure).exists())
        self.assertFalse(self.target.checks.filter(healthy=False, checked_at__lt=self.now - DAY * 30).exists())

    def make_recent_probe(self, age=2):
        self.start = self.now - DAY * age
        Target.objects.filter(pk=self.target.pk).update(created_at=self.start)
        self.target.refresh_from_db()

    def test_next_night_accepts_existing_cutoffs_older_than_probe_creation(self):
        self.make_recent_probe()
        self.check(self.start + timedelta(minutes=1))
        self.check(self.now - timedelta(minutes=1))
        self.run_maintenance()
        state = HistoryState.objects.get(target=self.target)
        self.assertLess(state.raw_before, self.target.created_at)
        HistoryState.objects.filter(pk=state.pk).update(last_error='ValueError')
        self.now += DAY
        self.run_maintenance()
        state.refresh_from_db()
        self.assertEqual(state.last_error, '')
        self.assertEqual(state.last_success_at, self.now)
        self.assertEqual(state.deleted_checks, 0)
        self.assertEqual(self.target.checks.count(), 2)

    def test_young_probe_without_checks_stays_unknown_across_nightly_runs(self):
        self.make_recent_probe(age=1)
        self.run_maintenance()
        self.now += DAY
        self.run_maintenance()
        stats = HistoryReader(self.target, self.now).stats(self.start, self.now)
        self.assertEqual(stats['uptime'], '—')
        self.assertEqual(stats['coverage'], '0.0%')
        self.assertFalse(self.target.checks.exists())

    def test_young_probe_can_age_into_raw_retention_without_losing_statistics(self):
        self.make_recent_probe()
        for offset in (1, 4, 7):
            self.check(self.start + timedelta(minutes=offset))
        self.run_maintenance()
        self.now += DAY * 8
        self.check(self.now - timedelta(minutes=1))
        expected = availability(list(self.target.checks.order_by('checked_at')), self.start, self.now)
        self.run_maintenance()
        stats = HistoryReader(self.target, self.now).stats(self.start, self.now)
        self.assertEqual(stats['uptime'], expected['uptime'])
        self.assertEqual(stats['coverage'], expected['coverage'])
        self.assertEqual(HistoryState.objects.get(target=self.target).deleted_checks, 2)

    def test_crossing_midnight_and_hour_uses_split_durations_but_single_count(self):
        boundary = self.start + DAY
        self.check(boundary - timedelta(seconds=30), False, coverage=60)
        self.check(boundary + timedelta(seconds=30), coverage=180)
        self.run_maintenance(purge=False)
        days = list(DailyProbeStats.objects.filter(
            target=self.target, started_at__lt=boundary + DAY,
        ).order_by('started_at'))
        self.assertEqual([row.down_seconds for row in days], [30, 30])
        self.assertEqual(sum(row.failure_count for row in days), 1)
        self.assertEqual(sum(row.success_count for row in days), 1)

    def test_idempotent_and_no_purge_preserves_sources_and_reader(self):
        self.check(self.start)
        self.check(self.now - DAY)
        self.run_maintenance(purge=False)
        self.assertEqual(self.target.checks.count(), 2)
        self.assertIsNone(HistoryState.objects.get(target=self.target).raw_before)
        original = list(HourlyProbeStats.objects.values_list('started_at', 'up_seconds', 'success_count'))
        self.run_maintenance(purge=False)
        repeated = list(HourlyProbeStats.objects.values_list('started_at', 'up_seconds', 'success_count'))
        self.assertEqual(original, repeated)
        self.run_maintenance()
        reader = HistoryReader(self.target, self.now)
        baseline = reader.stats(self.start, self.now)
        self.run_maintenance()
        self.assertEqual(HistoryReader(self.target, self.now).stats(self.start, self.now), baseline)

    def test_retains_anchor_and_latest_for_a_paused_probe(self):
        self.target.enabled = False
        self.target.save()
        old = self.check(self.start)
        latest = self.check(self.start + DAY)
        self.run_maintenance()
        self.assertFalse(Check.objects.filter(pk=old.pk).exists())
        self.assertTrue(Check.objects.filter(pk=latest.pk).exists())
        self.assertEqual(HistoryReader(self.target, self.now).values(self.start, self.now)['success_count'], 2)

    def test_anchor_covers_raw_boundary_without_double_counting(self):
        cutoff = (self.now - DAY * 7).replace(minute=0)
        self.check(cutoff - timedelta(seconds=30), coverage=180)
        self.check(cutoff + timedelta(seconds=60), False, coverage=60)
        before = availability(list(self.target.checks.order_by('checked_at')), self.start, self.now)
        self.run_maintenance()
        stats = HistoryReader(self.target, self.now).stats(self.start, self.now)
        self.assertEqual(stats['uptime'], before['uptime'])
        self.assertEqual(stats['coverage'], before['coverage'])
        self.assertEqual(HistoryReader(self.target, self.now).values(self.start, self.now)['success_count'], 1)

    def test_failure_rolls_back_bucket_and_never_purges(self):
        check = self.check(self.start)
        with patch('monitor.history.validate_stats', side_effect=ValueError('broken')):
            with self.assertRaises(ValueError):
                maintain_target(self.target.pk, now=self.now)
        self.assertTrue(Check.objects.filter(pk=check.pk).exists())
        self.assertFalse(HourlyProbeStats.objects.exists())
        self.run_maintenance()
        self.assertIsNotNone(HistoryState.objects.get(target=self.target).last_success_at)

    def test_incident_snapshot_survives_source_deletion_and_service_rename(self):
        self.check(self.start, False)
        self.check(self.start + timedelta(minutes=1), False)
        incident = Incident.objects.create(target=self.target, started_at=self.start,
                                           confirmed_at=self.start + timedelta(minutes=1))
        self.check(self.start + DAY)
        self.check(self.now - DAY)
        self.run_maintenance()
        incident.refresh_from_db()
        self.assertEqual(incident.latest_failure['status_code'], 503)
        self.assertFalse(self.target.checks.filter(healthy=False).exists())
        self.target.name = 'Renamed'
        self.target.url = 'https://changed.example.com'
        incident.ended_at = self.now
        incident.recovery_result = {'detail': 'HTTP 200', 'status_code': 200, 'duration_ms': 123}
        subject, body, html = incident_notification(self.target, incident, recovered=True)
        self.assertIn('Service', subject)
        self.assertIn('HTTP 503', body)
        self.assertIn('example.com', body)
        self.assertIn('123 ms', body)
        self.assertNotIn('Renamed', html)

    def test_prunes_yearly_statistics_but_retains_incidents(self):
        DailyProbeStats.objects.create(target=self.target, started_at=self.now - DAY * 367)
        self.run_maintenance()
        self.assertFalse(DailyProbeStats.objects.filter(started_at__lt=self.now - DAY * 366).exists())
        cutoff = (self.now - DAY * 30).replace(hour=0, minute=0)
        self.assertFalse(HourlyProbeStats.objects.filter(started_at__lt=cutoff).exists())

    def test_dry_run_writes_nothing(self):
        check = self.check(self.start)
        call_command('maintain_history', target=self.target.pk, dry_run=True, stdout=StringIO())
        self.assertFalse(HistoryState.objects.exists())
        self.assertFalse(HourlyProbeStats.objects.exists())
        self.assertTrue(Check.objects.filter(pk=check.pk).exists())

    def test_nightly_schedule_uses_swiss_time(self):
        schedule = settings.CELERY_BEAT_SCHEDULE['maintain-history']['schedule']
        self.assertIsInstance(schedule, crontab)
        self.assertEqual(schedule.hour, {2})
        self.assertEqual(schedule.minute, {0})
        self.assertEqual(settings.CELERY_TIMEZONE, 'Europe/Zurich')
        # Local 02:00 changes UTC offset between winter and summer.
        self.assertEqual(datetime(2026, 1, 10, 2, tzinfo=ZoneInfo(settings.CELERY_TIMEZONE)).utcoffset(), HOUR)
        self.assertEqual(datetime(2026, 7, 10, 2, tzinfo=ZoneInfo(settings.CELERY_TIMEZONE)).utcoffset(), HOUR * 2)

    def test_partial_creation_day_is_included_after_daily_compaction(self):
        created = self.start + timedelta(hours=12, minutes=37)
        Target.objects.filter(pk=self.target.pk).update(created_at=created)
        self.target.refresh_from_db()
        self.check(created)
        self.check(created + timedelta(minutes=3), False, coverage=60)
        self.check(self.now - DAY)
        checks = list(self.target.checks.order_by('checked_at'))
        expected = availability(checks, created, self.now)
        self.run_maintenance()
        stats = HistoryReader(self.target, self.now).stats(created, self.now)
        self.assertEqual(stats['uptime'], expected['uptime'])
        self.assertEqual(stats['coverage'], expected['coverage'])

    def test_missing_aggregate_prevents_source_purge(self):
        check = self.check(self.start)
        self.check(self.now - DAY)
        self.run_maintenance(purge=False)
        HourlyProbeStats.objects.filter(target=self.target, started_at=self.start).delete()
        with self.assertRaisesMessage(ValueError, 'Missing hourly aggregates'):
            maintain_target(self.target.pk, now=self.now)
        self.assertTrue(Check.objects.filter(pk=check.pk).exists())
        self.assertIsNone(HistoryState.objects.get(target=self.target).raw_before)
        self.assertEqual(HistoryState.objects.get(target=self.target).last_error, 'MISSING_HOURLY')

    def test_spring_clock_change_has_a_three_oclock_catch_up_only_that_night(self):
        from .tasks import maintain_history_dst
        for day, expected in ((29, True), (30, False)):
            local = datetime(2026, 3, day, 3, tzinfo=ZoneInfo('Europe/Zurich'))
            with (patch('monitor.tasks.timezone.localtime', return_value=local),
                  patch('monitor.tasks.maintain_history') as task):
                maintain_history_dst()
            self.assertEqual(task.called, expected)

    def test_bounded_task_continues_between_other_work(self):
        from .tasks import maintain_target_history
        with (patch('monitor.tasks.maintain_target', return_value=True),
              patch.object(maintain_target_history, 'apply_async') as queued):
            maintain_target_history(self.target.pk)
        queued.assert_called_once_with(args=[self.target.pk], countdown=1, retry=False)
        with (patch('monitor.tasks.maintain_target', return_value=False),
              patch.object(maintain_target_history, 'apply_async') as queued):
            maintain_target_history(self.target.pk)
        queued.assert_not_called()

    def test_new_incident_recovery_keeps_results_without_any_source_checks(self):
        start = self.now - timedelta(minutes=3)
        record_result(self.target, start, ProbeResult(False, 'HTTP 502', 30, 502))
        record_result(self.target, start + timedelta(minutes=1), ProbeResult(False, 'HTTP 503', 40, 503))
        self.target.checks.all().delete()
        record_result(self.target, self.now, ProbeResult(True, 'HTTP 200', 50, 200))
        incident = self.target.incidents.get()
        self.assertEqual(incident.initial_result['status_code'], 502)
        self.assertEqual(incident.confirmation_result['status_code'], 503)
        self.assertEqual(incident.recovery_result['duration_ms'], 50)
        subject, body, html = incident_notification(self.target, incident, recovered=True)
        self.assertIn('HTTP 503', body)
        self.assertIn('50 ms', body)

    def test_reader_reloads_when_purge_commits_during_a_read(self):
        from .history import source_checks
        self.check(self.start)
        self.check(self.now - DAY)
        expected = availability(list(self.target.checks.order_by('checked_at')), self.start, self.now)
        self.run_maintenance(purge=False)
        calls = 0

        def overlapping_read(target, start, end):
            nonlocal calls
            rows = source_checks(target, start, end)
            calls += 1
            if calls == 1:
                self.run_maintenance()
            return rows

        with patch('monitor.history.source_checks', side_effect=overlapping_read):
            reader = HistoryReader(self.target, self.now)
        self.assertEqual(calls, 2)
        stats = reader.stats(self.start, self.now)
        self.assertEqual(stats['uptime'], expected['uptime'])
        self.assertEqual(stats['coverage'], expected['coverage'])


class MaintenanceConcurrencyTests(TransactionTestCase):
    def test_another_connection_cannot_compact_the_same_probe(self):
        target = Target.objects.create(name='Concurrent', url='https://concurrent.example.com')

        def competing_maintenance(hours_only):
            try:
                return maintain_target(target.pk, hours_only=hours_only)
            finally:
                connections.close_all()

        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_lock(734218, %s)', [target.pk])
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                for hours_only in (False, True):
                    with self.subTest(hours_only=hours_only):
                        self.assertFalse(pool.submit(competing_maintenance, hours_only).result(timeout=5))
        finally:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(734218, %s)', [target.pk])
        self.assertFalse(HistoryState.objects.filter(target=target).exists())
