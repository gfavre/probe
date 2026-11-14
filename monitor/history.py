"""Bounded history compaction. UTC buckets preserve observed and unknown time."""
from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Min, Q
from django.utils import timezone

from .models import Check, DailyProbeStats, HistoryState, HourlyProbeStats, Target

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
DELETE_BATCH = 5000
SUM_FIELDS = ('up_seconds', 'down_seconds', 'observable_seconds', 'success_count',
              'failure_count', 'response_sum_ms')


def hour_start(value):
    return value.astimezone(dt_timezone.utc).replace(minute=0, second=0, microsecond=0)


def day_start(value):
    return hour_start(value).replace(hour=0)


def result_snapshot(result):
    return {key: getattr(result, key) for key in ('detail', 'status_code', 'duration_ms')}


def snapshot_incident(incident):
    """Backfill old incidents before deleting any source observations."""
    end = incident.ended_at or timezone.now()
    failures = incident.target.checks.filter(
        healthy=False, checked_at__gte=incident.started_at, checked_at__lte=end,
    ).order_by('checked_at')
    first = failures.first()
    confirmed = failures.filter(checked_at__gte=incident.confirmed_at).first()
    latest = failures.last()
    if not incident.service_name:
        incident.service_name = incident.target.name
        incident.service_url = incident.target.url
    for field, check in (('initial_result', first), ('confirmation_result', confirmed), ('latest_failure', latest)):
        if not getattr(incident, field) and check:
            setattr(incident, field, result_snapshot(check))
    if incident.ended_at and not incident.recovery_result:
        check = incident.target.checks.filter(healthy=True, checked_at=incident.ended_at).first()
        if check:
            incident.recovery_result = result_snapshot(check)
    incident.save(update_fields=['service_name', 'service_url', 'initial_result', 'confirmation_result',
                                 'latest_failure', 'recovery_result'])


def empty_stats():
    return {**dict.fromkeys(SUM_FIELDS, 0), 'response_min_ms': None, 'response_max_ms': None}


def combine(rows):
    values = empty_stats()
    for row in rows:
        for field in SUM_FIELDS:
            values[field] += getattr(row, field)
        low, high = row.response_min_ms, row.response_max_ms
        if low is not None:
            previous = values['response_min_ms']
            values['response_min_ms'] = min(previous, low) if previous is not None else low
        if high is not None:
            values['response_max_ms'] = max(values['response_max_ms'] or 0, high)
    return values


def source_checks(target, start, end):
    # Keep the preceding observation: its coverage can cross the left boundary.
    previous = target.checks.filter(checked_at__lt=start).order_by('-checked_at', '-pk').first()
    rows = list(target.checks.filter(checked_at__gte=start, checked_at__lt=end).order_by('checked_at', 'pk'))
    return ([previous] if previous else []) + rows


def observation_stats(checks, start, end, created_at):
    values = empty_stats()
    values['observable_seconds'] = max(0, (end - max(start, created_at)).total_seconds())
    for index, check in enumerate(checks):
        following = checks[index + 1].checked_at if index + 1 < len(checks) else end
        left = max(start, created_at, check.checked_at)
        right = min(end, following, check.checked_at + timedelta(seconds=check.coverage_seconds))
        values['up_seconds' if check.healthy else 'down_seconds'] += max(0, (right - left).total_seconds())
        if start <= check.checked_at < end:
            values['success_count' if check.healthy else 'failure_count'] += 1
            if check.healthy:
                values['response_sum_ms'] += check.duration_ms
                low = values['response_min_ms']
                values['response_min_ms'] = min(low, check.duration_ms) if low is not None else check.duration_ms
                values['response_max_ms'] = max(values['response_max_ms'] or 0, check.duration_ms)
    return values


def validate_stats(values):
    known = values['up_seconds'] + values['down_seconds']
    if known < 0 or known > values['observable_seconds'] + 0.001:
        raise ValueError('Invalid observation coverage')


def maintenance_status(now):
    """Small read-only operational summary, exposed only to platform administrators."""
    overdue = now - DAY * 2
    problematic = Target.objects.filter(created_at__lt=overdue).filter(
        Q(history_state__isnull=True) | Q(history_state__last_success_at__isnull=True)
        | Q(history_state__last_success_at__lt=overdue)
    ).count()
    failed = HistoryState.objects.exclude(last_error='').count()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_total_relation_size('monitor_check'), "
                       "pg_total_relation_size('monitor_hourlyprobestats') + "
                       "pg_total_relation_size('monitor_dailyprobestats')")
        check_bytes, aggregate_bytes = cursor.fetchone()
    return {
        'last_success': HistoryState.objects.aggregate(value=Min('last_success_at'))['value'],
        'warning': problematic > 0 or failed > 0,
        'checks': Check.objects.count(), 'hours': HourlyProbeStats.objects.count(),
        'days': DailyProbeStats.objects.count(), 'check_bytes': check_bytes, 'aggregate_bytes': aggregate_bytes,
        'raw_days': settings.CHECK_RETENTION_DAYS, 'failure_days': settings.FAILURE_RETENTION_DAYS,
        'hourly_days': settings.HOURLY_RETENTION_DAYS, 'daily_days': settings.DAILY_RETENTION_DAYS,
    }


def maintain_target(pk, *, now=None, purge=True):
    """Process at most one UTC day or one deletion batch. Return whether more work remains.

    A session advisory lock prevents overlapping manual/Celery maintenance. Each
    transaction also locks the probe, like the writer and notification sender.
    No source purge precedes validated, committed aggregates and incident snapshots.
    """
    now = now or timezone.now()
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(734218, %s)', [pk])
        if not cursor.fetchone()[0]:
            return False
    try:
        if not Target.objects.filter(pk=pk).exists():
            return False
        HistoryState.objects.get_or_create(target_id=pk)
        HistoryState.objects.filter(target_id=pk).update(last_started_at=now)
        return _maintain_target(pk, now, purge)
    except Exception as exc:
        HistoryState.objects.filter(target_id=pk).update(last_error=type(exc).__name__)
        raise
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(734218, %s)', [pk])


def _maintain_target(pk, now, purge):
    with transaction.atomic():
        target = Target.objects.select_for_update().filter(pk=pk).first()
        if target is None:
            return False
        state, _ = HistoryState.objects.get_or_create(target=target)
        state.last_started_at = now
        # The latest bucket must be closed beyond the maximum recorded check interval.
        closed_hour = hour_start(now - timedelta(minutes=5))
        closed_day = day_start(closed_hour)
        first = target.checks.order_by('checked_at').first()
        origin = hour_start(min(target.created_at, first.checked_at)) if first else hour_start(target.created_at)
        start = state.hourly_until or origin
        if start < closed_hour:
            end = min(day_start(start) + DAY, closed_hour)
            checks = source_checks(target, start, end)
            rows = []
            bucket = start
            while bucket < end:
                values = observation_stats(checks, bucket, bucket + HOUR, target.created_at)
                validate_stats(values)
                rows.append(HourlyProbeStats(target=target, started_at=bucket, **values))
                bucket += HOUR
            direct = observation_stats(checks, start, end, target.created_at)
            rolled = combine(rows)
            if any(abs(direct[field] - rolled[field]) > 0.001 for field in SUM_FIELDS):
                raise ValueError('Hourly rollup differs from source observations')
            HourlyProbeStats.objects.bulk_create(rows, update_conflicts=True, unique_fields=['target', 'started_at'],
                                                 update_fields=[*SUM_FIELDS, 'response_min_ms', 'response_max_ms',
                                                                'calculated_at', 'calculation_version'])
            state.hourly_until = end
            state.save()
            return True
        start = state.daily_until or day_start(origin)
        if start < closed_day:
            end = start + DAY
            hours = list(HourlyProbeStats.objects.filter(target=target, started_at__gte=start, started_at__lt=end))
            expected = int((end - max(start, origin)).total_seconds() / 3600)
            if len(hours) != expected:
                raise ValueError('Incomplete hourly source')
            values = combine(hours)
            validate_stats(values)
            DailyProbeStats.objects.update_or_create(target=target, started_at=start, defaults=values)
            state.daily_until = end
            state.save()
            return True
        # Backfill old incident snapshots in bounded batches, before removing any checks.
        incidents = list(target.incidents.filter(service_name='')[:100])
        if incidents:
            for incident in incidents:
                snapshot_incident(incident)
            state.save()
            return True
        if purge:
            raw_cutoff = min(hour_start(now - DAY * settings.CHECK_RETENTION_DAYS), state.hourly_until or origin)
            hourly_cutoff = min(day_start(now - DAY * settings.HOURLY_RETENTION_DAYS), state.daily_until or origin)
            daily_cutoff = day_start(now - DAY * settings.DAILY_RETENTION_DAYS)
            raw_cutoff = max(state.raw_before or raw_cutoff, raw_cutoff)
            hourly_cutoff = max(state.hourly_before or hourly_cutoff, hourly_cutoff)
            required_start = max(state.raw_before or origin, state.hourly_before or origin)
            expected = max(0, int((raw_cutoff - required_start).total_seconds() / 3600))
            available = HourlyProbeStats.objects.filter(
                target=target, started_at__gte=required_start, started_at__lt=raw_cutoff,
            ).count()
            if available != expected:
                raise ValueError('Missing hourly aggregates; purge refused')
            required_start = max(day_start(origin), state.hourly_before or day_start(origin))
            expected = max(0, (hourly_cutoff - required_start).days)
            if DailyProbeStats.objects.filter(
                target=target, started_at__gte=required_start, started_at__lt=hourly_cutoff,
            ).count() != expected:
                raise ValueError('Missing daily aggregates; purge refused')
            # Retain the left-boundary anchor and latest observation, including paused probes.
            anchor = target.checks.filter(checked_at__lt=raw_cutoff).order_by('-checked_at', '-pk').first()
            latest = target.checks.order_by('-checked_at', '-pk').first()
            protected = [check.pk for check in (anchor, latest) if check]
            eligible = target.checks.filter(checked_at__lt=raw_cutoff).exclude(pk__in=protected)
            eligible = eligible.filter(healthy=True) | eligible.filter(
                checked_at__lt=now - DAY * settings.FAILURE_RETENTION_DAYS,
            )
            ids = list(eligible.order_by('pk').values_list('pk', flat=True)[:DELETE_BATCH])
            # Source selection changes atomically with deletion. Leftover failures are diagnostic only.
            state.raw_before = max(state.raw_before or raw_cutoff, raw_cutoff)
            state.hourly_before = max(state.hourly_before or hourly_cutoff, hourly_cutoff)
            if ids:
                deleted, _ = Check.objects.filter(pk__in=ids).delete()
                state.deleted_checks += deleted
            HourlyProbeStats.objects.filter(target=target, started_at__lt=hourly_cutoff).delete()
            DailyProbeStats.objects.filter(target=target, started_at__lt=daily_cutoff).delete()
            state.save()
            if len(ids) == DELETE_BATCH:
                return True
        state.last_success_at = now
        state.last_error = ''
        state.save()
        return False
