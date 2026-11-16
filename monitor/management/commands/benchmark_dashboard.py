"""Reproducible synthetic dashboard measurements, restricted to benchmark databases."""
import json
import resource
import sys
from contextlib import nullcontext
from datetime import timedelta
from math import isclose
from statistics import median
from time import perf_counter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from monitor.history import DAY, hour_start, maintain_target
from monitor.dashboard import calculate_windows, statistics_cache_key
from monitor.metrics import HistoryReader
from monitor.models import HistoryState, Target


class Command(BaseCommand):
    help = 'Seed or measure a synthetic dashboard in an isolated benchmark_* PostgreSQL database only.'

    def add_arguments(self, parser):
        parser.add_argument('--mode', choices=('seed', 'rollup', 'reference', 'cold', 'cached'), required=True)
        parser.add_argument('--probes', type=int, default=50)
        parser.add_argument('--interval', type=int, choices=(60, 180), default=180)
        parser.add_argument('--iterations', type=int, default=3)

    def handle(self, *args, **options):
        if not connection.settings_dict['NAME'].startswith('benchmark_'):
            raise CommandError('Use an isolated database whose name starts with benchmark_.')
        if options['probes'] < 1 or options['iterations'] < 1:
            raise CommandError('Probe and iteration counts must be positive.')
        if options['mode'] == 'seed':
            self.seed(options['probes'], options['interval'])
            return
        if options['mode'] == 'rollup':
            now = timezone.now()
            ids = list(Target.objects.values_list('pk', flat=True))
            before = calculate_windows(ids, now)
            for pk in ids:
                while maintain_target(pk, now=now, purge=False, hours_only=True):
                    pass
            after = calculate_windows(ids, now)
            for pk in ids:
                for old, new in zip(before[pk], after[pk], strict=True):
                    if any(old[key] != new[key] for key in ('uptime', 'coverage')) or any(
                        not isclose(old[key], new[key], abs_tol=0.000001) for key in ('up_seconds', 'down_seconds')
                    ):
                        raise CommandError('Progressive rollup statistics differ from the raw reference.')
            self.stdout.write('Completed hourly rollups; verified all windows against the pre-rollup values. No purge.')
            return
        user = get_user_model().objects.get(username='benchmark')
        client = Client(HTTP_HOST='localhost')
        client.force_login(user)
        mode = options['mode']
        timings, sql_timings, query_counts = [], [], []
        cache_key = statistics_cache_key(Target.objects.values_list('pk', flat=True))
        cache.delete(cache_key)
        with (patch('monitor.views.dashboard_targets', side_effect=self.reference_targets)
              if mode == 'reference' else nullcontext()):
            with (patch('monitor.views.dashboard_windows', side_effect=self.reference_windows)
                  if mode == 'reference' else nullcontext()):
                if mode == 'cached':
                    client.get('/', secure=True)
                for _ in range(options['iterations']):
                    if mode != 'cached':
                        cache.delete(cache_key)
                    with CaptureQueriesContext(connection) as queries:
                        start = perf_counter()
                        response = client.get('/', secure=True)
                        timings.append((perf_counter() - start) * 1000)
                    if response.status_code != 200:
                        raise CommandError(f'Unexpected HTTP {response.status_code}')
                    query_counts.append(len(queries))
                    sql_timings.append(sum(float(query['time']) for query in queries) * 1000)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.stdout.write(json.dumps({
            'mode': mode, 'probes': Target.objects.count(), 'python': sys.version.split()[0],
            'queries': query_counts, 'median_ms': round(median(timings), 1),
            'max_ms': round(max(timings), 1), 'median_sql_ms': round(median(sql_timings), 1),
            'peak_rss_mib': round(rss / (1024 ** 2 if sys.platform == 'darwin' else 1024), 1),
        }))

    @staticmethod
    def reference_targets():
        return list(Target.objects.all()), None

    @staticmethod
    def reference_windows(targets, now):
        windows = {}
        for target in targets:
            reader = HistoryReader(target, now)
            windows[target.pk] = [reader.stats(reader.period_start(days), now) for days in (1, 7, 30)]
        return windows

    def seed(self, count, interval):
        if Target.objects.exists() or get_user_model().objects.exists():
            raise CommandError('Seeding requires an empty benchmark database; no existing data will be deleted.')
        now = timezone.now()
        raw_before = hour_start(now - DAY * 7)
        hourly_before = hour_start(now - DAY * 30).replace(hour=0)
        created = hourly_before - DAY * 335
        targets = Target.objects.bulk_create([
            Target(name=f'Probe {i:03}', url=f'https://benchmark-{i}.invalid', interval_minutes=interval // 60,
                   next_check_at=now + timedelta(seconds=interval), enabled=i % 10 != 0)
            for i in range(count)
        ])
        ids = [target.pk for target in targets]
        Target.objects.filter(pk__in=ids).update(created_at=created)
        HistoryState.objects.bulk_create([
            HistoryState(target=target, raw_before=raw_before, hourly_before=hourly_before,
                         hourly_until=raw_before) for target in targets
        ])
        with connection.cursor() as cursor:
            # A missing observation every 29 samples, and failures mixed with successes.
            cursor.execute("""
                INSERT INTO monitor_check
                    (target_id, checked_at, healthy, detail, duration_ms, coverage_seconds, status_code)
                SELECT t.id, s.at, s.n %% 97 > 1, 'Synthetic benchmark', 123, %(interval)s,
                       CASE WHEN s.n %% 97 > 1 THEN 200 ELSE 503 END
                FROM monitor_target t CROSS JOIN generate_series(
                    %(start)s::timestamptz - %(interval)s * interval '1 second',
                    %(now)s::timestamptz - interval '1 second',
                    %(interval)s * interval '1 second') WITH ORDINALITY s(at, n)
                WHERE t.id = ANY(%(ids)s) AND s.n %% 29 != 0
            """, {'interval': interval, 'start': raw_before, 'now': now, 'ids': ids})
            for table, start, end, step, seconds in (
                ('monitor_hourlyprobestats', hourly_before, raw_before, '1 hour', 3600),
                ('monitor_dailyprobestats', created, hourly_before, '24 hours', 86400),
            ):
                # Table names are fixed literals above, never command-line input.
                cursor.execute(f"""
                    INSERT INTO {table}
                        (target_id, started_at, up_seconds, down_seconds, observable_seconds,
                         success_count, failure_count, response_sum_ms, response_min_ms, response_max_ms,
                         calculated_at, calculation_version)
                    SELECT t.id, s.at, %(seconds)s * 0.92, %(seconds)s * 0.03, %(seconds)s,
                           92, 3, 11316, 123, 123, %(now)s, 1
                    FROM monitor_target t CROSS JOIN generate_series(
                        %(start)s::timestamptz, %(end)s::timestamptz - %(step)s::interval,
                        %(step)s::interval) s(at)
                    WHERE t.id = ANY(%(ids)s)
                """, {'start': start, 'end': end, 'step': step, 'seconds': seconds, 'now': now, 'ids': ids})
            cursor.execute('ANALYZE')
        get_user_model().objects.create_user('benchmark', password=None)
        self.stdout.write(f'Seeded {count} probes with seven raw days, hourly and daily history, gaps and failures.')
