"""Bounded dashboard reads; PostgreSQL reduces observations to three windows per probe."""
from hashlib import sha256

from django.core.cache import cache
from django.db import connection
from django.db.models import OuterRef, Subquery

from .models import Check, Target

DASHBOARD_DAYS = (1, 7, 30)
STATISTICS_CACHE_SECONDS = 15
STATISTICS_CACHE_PREFIX = 'dashboard-statistics:v2'

# All resolutions and their persisted cutoffs are read in ONE statement snapshot.
# In particular, a concurrent purge cannot mix old cutoffs with already deleted checks.
# The anchor and LEAD preserve the same clipped observation intervals as HistoryReader.
WINDOW_SQL = """
WITH requested AS (
    SELECT t.id AS target_id, t.created_at, h.raw_before, h.hourly_before, h.hourly_until,
           %(now)s::timestamptz AS ends_at
    FROM monitor_target t
    LEFT JOIN monitor_historystate h ON h.target_id = t.id
    WHERE t.id = ANY(%(ids)s)
), rollup_bounds AS (
    SELECT r.*,
           CASE WHEN r.hourly_until IS NOT NULL
                THEN LEAST(r.hourly_until, date_trunc('hour', r.ends_at, 'UTC')) END AS proposed_tail,
           GREATEST(r.raw_before, date_trunc('hour', r.created_at, 'UTC'),
                    date_trunc('hour', r.ends_at - %(max_days)s * interval '24 hours', 'UTC')) AS verify_from
    FROM requested r
), certified AS (
    SELECT r.*,
           CASE WHEN r.proposed_tail >= r.verify_from AND
               (SELECT COUNT(*) FROM monitor_hourlyprobestats
                WHERE target_id = r.target_id AND started_at >= r.verify_from AND started_at < r.proposed_tail)
               = date_part('epoch', r.proposed_tail - r.verify_from) / 3600
           THEN GREATEST(r.raw_before, r.proposed_tail) ELSE r.raw_before END AS tail_from
    FROM rollup_bounds r
), starts AS (
    SELECT r.*, d.days,
           GREATEST(r.created_at,
               CASE
                   WHEN r.ends_at - d.days * interval '24 hours' < r.hourly_before
                       THEN date_trunc('day', r.ends_at - d.days * interval '24 hours', 'UTC')
                   WHEN r.ends_at - d.days * interval '24 hours' < r.raw_before
                       THEN date_trunc('hour', r.ends_at - d.days * interval '24 hours', 'UTC')
                   ELSE r.ends_at - d.days * interval '24 hours'
               END) AS starts_at
    FROM certified r CROSS JOIN unnest(%(days)s::integer[]) AS d(days)
), windows AS (
    SELECT s.*, GREATEST(s.starts_at, s.tail_from) AS raw_starts_at
    FROM starts s
), ranges AS (
    SELECT target_id, days, raw_starts_at AS starts_at, ends_at FROM windows
    UNION ALL
    -- An unaligned left edge cannot be represented by a complete hourly bucket.
    -- The probe's partial creation hour is already included in its hourly aggregate.
    SELECT target_id, days, starts_at,
           LEAST(date_trunc('hour', starts_at, 'UTC') + interval '1 hour', tail_from)
    FROM windows
    WHERE starts_at < tail_from AND starts_at > created_at
      AND starts_at != date_trunc('hour', starts_at, 'UTC')
), segments AS (
    SELECT b.target_id, b.days, b.starts_at, s.checked_at, s.healthy, s.ends_at
    FROM ranges b CROSS JOIN LATERAL (
        SELECT c.checked_at, c.healthy,
               LEAST(b.ends_at, c.checked_at + c.coverage_seconds * interval '1 second',
                     LEAD(c.checked_at) OVER (ORDER BY c.checked_at, c.id)) AS ends_at
        FROM (
            SELECT id, checked_at, healthy, coverage_seconds
            FROM monitor_check
            WHERE target_id = b.target_id AND checked_at >= b.starts_at AND checked_at < b.ends_at
            UNION ALL
            (SELECT id, checked_at, healthy, coverage_seconds
             FROM monitor_check WHERE target_id = b.target_id AND checked_at < b.starts_at
             ORDER BY checked_at DESC, id DESC LIMIT 1)
        ) c
    ) s
    WHERE b.starts_at < b.ends_at
), raw_totals AS (
    SELECT s.target_id, s.days,
           COALESCE(SUM(GREATEST(0, date_part('epoch',
               s.ends_at - GREATEST(s.checked_at, s.starts_at))))
               FILTER (WHERE s.healthy), 0)::double precision AS up_seconds,
           COALESCE(SUM(GREATEST(0, date_part('epoch',
               s.ends_at - GREATEST(s.checked_at, s.starts_at))))
               FILTER (WHERE NOT s.healthy), 0)::double precision AS down_seconds
    FROM segments s GROUP BY s.target_id, s.days
)
SELECT w.target_id, w.days, w.starts_at, w.ends_at,
       COALESCE(r.up_seconds, 0) + COALESCE(h.up_seconds, 0) + COALESCE(d.up_seconds, 0) AS up_seconds,
       COALESCE(r.down_seconds, 0) + COALESCE(h.down_seconds, 0) + COALESCE(d.down_seconds, 0) AS down_seconds
FROM windows w LEFT JOIN raw_totals r USING (target_id, days)
LEFT JOIN LATERAL (
    SELECT SUM(up_seconds) AS up_seconds, SUM(down_seconds) AS down_seconds
    FROM monitor_hourlyprobestats
    WHERE target_id = w.target_id
      AND started_at >= date_trunc('hour', GREATEST(w.starts_at, w.hourly_before), 'UTC')
      AND started_at < LEAST(w.ends_at, COALESCE(w.tail_from, w.starts_at))
      AND GREATEST(started_at, w.created_at) >= GREATEST(w.starts_at, w.hourly_before)
      AND started_at + interval '1 hour' <= LEAST(w.ends_at, COALESCE(w.tail_from, w.starts_at))
) h ON true
LEFT JOIN LATERAL (
    SELECT SUM(up_seconds) AS up_seconds, SUM(down_seconds) AS down_seconds
    FROM monitor_dailyprobestats
    WHERE target_id = w.target_id
      AND started_at >= date_trunc('day', w.starts_at, 'UTC')
      AND started_at < LEAST(w.ends_at, COALESCE(w.hourly_before, w.starts_at))
      AND GREATEST(started_at, w.created_at) >= w.starts_at
      AND started_at + interval '24 hours' <= LEAST(w.ends_at, COALESCE(w.hourly_before, w.starts_at))
) d ON true
ORDER BY w.target_id, w.days
"""


def dashboard_targets():
    """Read configurations and latest observations in two queries, irrespective of probe count."""
    latest = Check.objects.filter(target_id=OuterRef('pk')).order_by('-checked_at', '-pk').values('pk')[:1]
    targets = list(Target.objects.annotate(latest_check_id=Subquery(latest)))
    latest_ids = [target.latest_check_id for target in targets if target.latest_check_id is not None]
    checks = Check.objects.only(
        'target_id', 'checked_at', 'healthy', 'coverage_seconds', 'duration_ms',
    ).in_bulk(latest_ids)
    return targets, {check.target_id: check for check in checks.values()}


def calculate_windows(target_ids, now):
    """Return numeric totals and presentation values without materializing Check objects."""
    result = {pk: [] for pk in target_ids}
    if not target_ids:
        return result
    with connection.cursor() as cursor:
        cursor.execute(WINDOW_SQL, {
            'ids': list(target_ids), 'now': now, 'days': list(DASHBOARD_DAYS), 'max_days': max(DASHBOARD_DAYS),
        })
        for pk, days, start, end, up, down in cursor.fetchall():
            known = up + down
            total = max(0, (end - start).total_seconds())
            result[pk].append({
                'days': days, 'up_seconds': up, 'down_seconds': down,
                'uptime': f'{100 * up / known:.2f}%' if known else '—',
                'coverage': f'{100 * known / total:.1f}%' if total else '—',
            })
    return result


def statistics_cache_key(target_ids):
    digest = sha256(','.join(map(str, sorted(target_ids))).encode()).hexdigest()
    return f'{STATISTICS_CACHE_PREFIX}:{digest}'


def dashboard_windows(targets, now):
    """Cache only statistics; current state, configuration and permissions are always read live."""
    ids = [target.pk for target in targets]
    if not ids:
        return {}
    key = statistics_cache_key(ids)
    cache_available = True
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
        cache_available = False
    if cached is not None and 0 <= (now - cached['at']).total_seconds() < STATISTICS_CACHE_SECONDS:
        return cached['windows']
    windows = calculate_windows(ids, now)
    if not cache_available:
        return windows
    try:
        cache.set(key, {'at': now, 'windows': windows}, timeout=STATISTICS_CACHE_SECONDS)
    except Exception:
        # Cache failure must neither hide live probe state nor break a dashboard read.
        pass
    return windows
