"""Dashboard query budgets, compaction equivalence and bounded statistics caching."""
from datetime import datetime, timedelta, timezone as dt_timezone
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from random import Random
from time import monotonic, sleep
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from .dashboard import STATISTICS_CACHE_SECONDS, WINDOW_SQL, calculate_windows, dashboard_targets, dashboard_windows
from .history import DAY, maintain_target
from .metrics import HistoryReader
from .models import Check, HistoryState, Target, UserProfile


class DashboardTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 12, 10, 25, tzinfo=dt_timezone.utc)
        cache.clear()
        self.addCleanup(cache.clear)

    def target(self, index=0, age=40, enabled=True):
        target = Target.objects.create(name=f'Probe {index}', url=f'https://service-{index}.example', enabled=enabled)
        Target.objects.filter(pk=target.pk).update(created_at=self.now - DAY * age)
        target.refresh_from_db()
        return target

    def check(self, target, when, healthy=True, coverage=180):
        return Check.objects.create(target=target, checked_at=when, healthy=healthy,
                                    detail='Observation', duration_ms=123, coverage_seconds=coverage)

    def assert_reference(self, targets):
        with self.assertNumQueries(1):
            actual = calculate_windows([target.pk for target in targets], self.now)
        for target in targets:
            reader = HistoryReader(target, self.now)
            for days, window in zip((1, 7, 30), actual[target.pk], strict=True):
                expected = reader.stats(reader.period_start(days), self.now)
                with self.subTest(target=target.pk, days=days):
                    for key in ('uptime', 'coverage'):
                        self.assertEqual(window[key], expected[key])
                    self.assertAlmostEqual(window['down_seconds'], expected['down_seconds'], places=6)

    def test_raw_windows_match_reference_with_gaps_failures_and_interval_changes(self):
        target = self.target()
        random = Random(41)
        when = self.now - DAY * 31
        rows = []
        while when < self.now:
            rows.append(Check(target=target, checked_at=when, healthy=random.random() > 0.2,
                              detail='Observation', duration_ms=12, coverage_seconds=random.choice((60, 180, 300))))
            when += timedelta(seconds=random.choice((60, 180, 300, 3600)))
        Check.objects.bulk_create(rows)
        # A future observation must not truncate the currently observed interval.
        self.check(target, self.now + timedelta(seconds=30), healthy=False)
        self.assert_reference([target])

    def test_window_anchors_ties_and_partial_last_observation(self):
        target = self.target()
        for days in (1, 7, 30):
            edge = self.now - DAY * days
            self.check(target, edge - timedelta(seconds=30), coverage=180)
            self.check(target, edge + timedelta(seconds=30), healthy=False, coverage=60)
            self.check(target, edge + timedelta(seconds=30), coverage=300)
        self.check(target, self.now - timedelta(seconds=15), healthy=False, coverage=60)
        self.assert_reference([target])

    def test_empty_recent_and_paused_probes_match_reference(self):
        targets = [self.target(0), self.target(1, age=0), self.target(2, age=2, enabled=False)]
        self.check(targets[2], self.now - DAY, healthy=False)
        self.assert_reference(targets)
        self.assertEqual(calculate_windows([targets[0].pk], self.now)[targets[0].pk][0]['uptime'], '—')

    @override_settings(CHECK_RETENTION_DAYS=2, HOURLY_RETENTION_DAYS=10)
    def test_compacted_daily_hourly_raw_ranges_and_partial_creation_day_match_reference(self):
        target = self.target(age=32)
        for age in (31, 29, 15, 9, 3, 1):
            when = self.now - DAY * age
            self.check(target, when, healthy=False, coverage=60)
            self.check(target, when + timedelta(seconds=45), coverage=300)
        raw_cutoff = (self.now - DAY * 2).replace(minute=0, second=0)
        self.check(target, raw_cutoff - timedelta(seconds=30), coverage=180)
        self.check(target, raw_cutoff + timedelta(seconds=60), healthy=False, coverage=60)
        self.check(target, self.now - timedelta(seconds=15))
        self.assert_reference([target])
        for _ in range(100):
            if not maintain_target(target.pk, now=self.now):
                break
        else:
            self.fail('Maintenance did not finish')
        state = HistoryState.objects.get(target=target)
        self.assertIsNotNone(state.raw_before)
        self.assertIsNotNone(state.hourly_before)
        self.assert_reference([target])
        # Already compacted failures remain diagnostic rows, not additional coverage.
        target.checks.filter(checked_at__lt=state.raw_before - timedelta(minutes=5)).update(healthy=False)
        self.assert_reference([target])

    def aggregate(self, target):
        for _ in range(100):
            if not maintain_target(target.pk, now=self.now, purge=False, hours_only=True):
                return
        self.fail('Progressive rollup did not finish')

    def test_progressive_hours_preserve_exact_sliding_edges_and_creation_hour(self):
        targets = [self.target(), self.target(1, age=2), self.target(2, age=40, enabled=False)]
        for target in targets:
            for days in (1, 7, 30):
                edge = self.now - DAY * days
                if edge >= target.created_at:
                    self.check(target, edge - timedelta(seconds=30), coverage=300)
                    self.check(target, edge + timedelta(seconds=30), healthy=False, coverage=60)
            self.check(target, target.created_at, healthy=False, coverage=60)
            self.check(target, self.now - timedelta(hours=1, minutes=11), healthy=False)
            self.check(target, self.now - timedelta(minutes=1))
        before = calculate_windows([target.pk for target in targets], self.now)
        for target in targets:
            self.aggregate(target)
        self.assertEqual(calculate_windows([target.pk for target in targets], self.now), before)
        self.assert_reference(targets)

    def test_missing_recent_hour_falls_back_to_raw_observations(self):
        from .models import HourlyProbeStats
        target = self.target(age=2)
        self.check(target, self.now - timedelta(hours=10), healthy=False)
        self.check(target, self.now - timedelta(minutes=1))
        expected = calculate_windows([target.pk], self.now)
        self.aggregate(target)
        deleted, _ = HourlyProbeStats.objects.filter(
            target=target, started_at=(self.now - timedelta(hours=10)).replace(minute=0, second=0),
        ).delete()
        self.assertEqual(deleted, 1)
        self.assertEqual(calculate_windows([target.pk], self.now), expected)

    def test_progressive_hours_use_utc_across_swiss_clock_change(self):
        self.now = datetime(2026, 3, 29, 2, 12, tzinfo=dt_timezone.utc)
        target = self.target(age=2)
        self.check(target, self.now - DAY - timedelta(seconds=30), healthy=False, coverage=300)
        self.check(target, self.now - timedelta(hours=2, minutes=13), coverage=300)
        self.check(target, self.now - timedelta(seconds=30))
        self.aggregate(target)
        self.assert_reference([target])

    def test_progressive_hours_preserve_fractional_observation_timestamps(self):
        target = self.target(age=2)
        random = Random(19)
        when = target.created_at
        rows = []
        while when < self.now:
            rows.append(Check(target=target, checked_at=when, healthy=random.random() > 0.2,
                              detail='Observation', duration_ms=12, coverage_seconds=random.choice((60, 180, 300))))
            when += timedelta(seconds=random.choice((60, 180, 300)), microseconds=random.randrange(1000000))
        Check.objects.bulk_create(rows)
        self.aggregate(target)
        self.assert_reference([target])

    def test_latest_observations_are_batched_including_missing_and_tied_checks(self):
        targets = [self.target(i) for i in range(50)]
        for target in targets[:-1]:
            self.check(target, self.now - timedelta(seconds=10))
        latest = self.check(targets[0], self.now - timedelta(seconds=10), healthy=False)
        with self.assertNumQueries(2):
            loaded, checks = dashboard_targets()
        self.assertEqual(len(loaded), 50)
        self.assertEqual(checks[targets[0].pk].pk, latest.pk)
        self.assertNotIn(targets[-1].pk, checks)

    def test_cache_hit_expiry_and_changed_probe_set(self):
        target = self.target()
        with self.assertNumQueries(1):
            first = dashboard_windows([target], self.now)
        with self.assertNumQueries(0):
            self.assertEqual(dashboard_windows([target], self.now + timedelta(seconds=14)), first)
        with self.assertNumQueries(1):
            dashboard_windows([target], self.now + timedelta(seconds=STATISTICS_CACHE_SECONDS))
        with self.assertNumQueries(1):
            dashboard_windows([target], self.now - timedelta(seconds=1))
        another = self.target(1)
        with self.assertNumQueries(1):
            self.assertEqual(set(dashboard_windows([target, another], self.now)), {target.pk, another.pk})

    def test_cache_failure_falls_back_to_database(self):
        target = self.target()
        with patch('monitor.dashboard.cache.get', side_effect=RuntimeError('unavailable')), \
                patch('monitor.dashboard.cache.set') as save:
            with self.assertNumQueries(1):
                windows = dashboard_windows([target], self.now)
        self.assertEqual(windows[target.pk][0]['uptime'], '—')
        save.assert_not_called()

    def test_cache_write_failure_does_not_break_statistics(self):
        target = self.target()
        with patch('monitor.dashboard.cache.set', side_effect=RuntimeError('unavailable')):
            self.assertEqual(dashboard_windows([target], self.now)[target.pk][0]['uptime'], '—')

    def test_empty_dashboard_needs_no_statistics_or_observation_query(self):
        with self.assertNumQueries(1):
            targets, checks = dashboard_targets()
        with self.assertNumQueries(0):
            self.assertEqual(dashboard_windows(targets, self.now), {})
        self.assertEqual(checks, {})

    def test_cached_statistics_never_cache_status_configuration_or_permissions(self):
        target = self.target()
        self.check(target, self.now - timedelta(seconds=5), healthy=False)
        user = get_user_model().objects.create_user('viewer')
        self.client.force_login(user)
        with patch('monitor.views.timezone.now', return_value=self.now):
            response = self.client.get('/')
        self.assertEqual(response.context['rows'][0]['tone'], 'down')
        self.assertFalse(response.context['can_edit_probes'])
        self.check(target, self.now + timedelta(seconds=1))
        Target.objects.filter(pk=target.pk).update(name='Renamed')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.Role.EDITOR})
        with patch('monitor.views.timezone.now', return_value=self.now + timedelta(seconds=2)), \
                patch('monitor.dashboard.calculate_windows', side_effect=AssertionError('Expected cached windows')):
            response = self.client.get('/')
        self.assertEqual(response.context['rows'][0]['tone'], 'up')
        self.assertEqual(response.context['rows'][0]['target'].name, 'Renamed')
        self.assertTrue(response.context['can_edit_probes'])
        Target.objects.filter(pk=target.pk).update(enabled=False)
        with patch('monitor.views.timezone.now', return_value=self.now + timedelta(seconds=3)):
            response = self.client.get('/')
        self.assertEqual(response.context['rows'][0]['tone'], 'paused')

    def test_full_page_query_budget_does_not_grow_with_probe_count(self):
        user = get_user_model().objects.create_user('viewer')
        self.client.force_login(user)
        counts = []
        for count in (1, 50):
            for index in range(Target.objects.count(), count):
                self.check(self.target(index), self.now - timedelta(seconds=15))
            cache.clear()
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get('/')
                self.assertEqual(response.status_code, 200)
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[1])
        self.assertLessEqual(counts[1], 15)


class DashboardCompactionConcurrencyTests(TransactionTestCase):
    def test_purge_during_statistics_read_uses_one_statement_snapshot(self):
        now = datetime(2026, 11, 16, 12, tzinfo=dt_timezone.utc)
        target = Target.objects.create(name='Concurrent', url='https://concurrent.example')
        Target.objects.filter(pk=target.pk).update(created_at=now - DAY * 32)
        target.refresh_from_db()
        old = Check.objects.create(target=target, checked_at=now - DAY * 20, healthy=True,
                                   detail='Old', duration_ms=10, coverage_seconds=180)
        Check.objects.create(target=target, checked_at=now - DAY * 10, healthy=True,
                             detail='Boundary anchor', duration_ms=10, coverage_seconds=180)
        Check.objects.create(target=target, checked_at=now - DAY, healthy=False,
                             detail='Recent', duration_ms=10, coverage_seconds=60)
        for _ in range(100):
            if not maintain_target(target.pk, now=now, purge=False):
                break
        else:
            self.fail('Maintenance preparation did not finish')
        expected = calculate_windows([target.pk], now)
        backend = Queue()
        # Pause inside the statistics statement, after its MVCC snapshot is established.
        gated_sql = WINDOW_SQL.replace(
            'WITH requested AS (',
            'WITH gate AS MATERIALIZED (SELECT pg_advisory_xact_lock(734219, 1)), requested AS (',
        ).replace('FROM monitor_target t\n', 'FROM monitor_target t CROSS JOIN gate\n')

        def read():
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    backend.put(cursor.fetchone()[0])
                return calculate_windows([target.pk], now)
            finally:
                connections.close_all()

        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_lock(734219, 1)')
        with ThreadPoolExecutor(max_workers=1) as pool, patch('monitor.dashboard.WINDOW_SQL', gated_sql):
            future = pool.submit(read)
            try:
                pid = backend.get(timeout=5)
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid = %s "
                                       "AND locktype = 'advisory' AND NOT granted)", [pid])
                        if cursor.fetchone()[0]:
                            break
                    sleep(0.01)
                else:
                    self.fail('Statistics reader did not reach the snapshot gate')
                maintain_target(target.pk, now=now)
                self.assertFalse(Check.objects.filter(pk=old.pk).exists())
            finally:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_unlock(734219, 1)')
            self.assertEqual(future.result(timeout=5), expected)
