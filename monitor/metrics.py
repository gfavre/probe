from datetime import timedelta


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} s'
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes} min {seconds} s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours} h {minutes} min'


def availability(checks, start, end):
    known = up = 0.0
    for index, check in enumerate(checks):
        following = checks[index + 1].checked_at if index + 1 < len(checks) else end
        left = max(start, check.checked_at)
        right = min(end, following, check.checked_at + timedelta(seconds=check.coverage_seconds))
        seconds = max(0.0, (right - left).total_seconds())
        known += seconds
        if check.healthy:
            up += seconds
    total = max(0, (end - start).total_seconds())
    return {'uptime': f'{100 * up / known:.2f}%' if known else '—', 'coverage': f'{100 * known / total:.1f}%' if total else '—'}


def period_stats(checks, incidents, start, end):
    stats = availability(checks, start, end)
    durations = [max(0, (min(i.ended_at or end, end) - max(i.started_at, start)).total_seconds())
                 for i in incidents if i.started_at < end and (i.ended_at is None or i.ended_at > start)]
    stats.update(incidents=len(durations), downtime=duration(sum(durations)),
                 longest=duration(max(durations, default=0)), average=duration(sum(durations) / len(durations) if durations else 0))
    return stats


class HistoryReader:
    """Read disjoint raw/hourly/daily ranges using committed compaction boundaries."""

    def __init__(self, target, now):
        from .history import DAY, source_checks
        from .models import DailyProbeStats, HistoryState, HourlyProbeStats
        self.target, self.now = target, now
        beginning = now - DAY * 366
        for _ in range(5):
            state = HistoryState.objects.filter(target=target).values_list('raw_before', 'hourly_before').first()
            self.raw_before, self.hourly_before = state or (None, None)
            self.checks = source_checks(target, max(beginning, self.raw_before or beginning), now)
            self.hours = list(HourlyProbeStats.objects.filter(target=target, started_at__gte=beginning))
            self.days = list(DailyProbeStats.objects.filter(target=target, started_at__gte=beginning))
            current = HistoryState.objects.filter(target=target).values_list('raw_before', 'hourly_before').first()
            if current == state:
                break
        else:
            raise RuntimeError('History boundaries kept changing while reading')

    def period_start(self, days):
        from .history import day_start, hour_start
        start = self.now - timedelta(days=days)
        if self.hourly_before and start < self.hourly_before:
            start = day_start(start)
        elif self.raw_before and start < self.raw_before:
            start = hour_start(start)
        return max(start, self.target.created_at)

    def values(self, start, end):
        from .history import DAY, HOUR, combine, observation_stats
        raw_start = max(start, self.raw_before or start)
        values = observation_stats(self.checks, raw_start, end, self.target.created_at)
        for rows, size, lower, upper in (
            (self.days, DAY, start, min(end, self.hourly_before or start)),
            (self.hours, HOUR, max(start, self.hourly_before or start), min(end, self.raw_before or start)),
        ):
            # Callers use complete archived buckets; never invent a partial daily/hourly share.
            selected = [row for row in rows
                        if max(row.started_at, self.target.created_at) >= lower and row.started_at + size <= upper]
            archived = combine(selected)
            for field in ('up_seconds', 'down_seconds', 'success_count', 'failure_count', 'response_sum_ms'):
                values[field] += archived[field]
        values['observable_seconds'] = max(0, (end - max(start, self.target.created_at)).total_seconds())
        return values

    def stats(self, start, end):
        values = self.values(start, end)
        up, down = values['up_seconds'], values['down_seconds']
        known, total = up + down, values['observable_seconds']
        return {'uptime': f'{100 * up / known:.2f}%' if known else '—',
                'coverage': f'{100 * known / total:.1f}%' if total else '—',
                'down_seconds': down}

    def response_points(self, start, end):
        from types import SimpleNamespace
        points = []
        if self.raw_before:
            for row in self.hours:
                if start <= row.started_at < min(end, self.raw_before) and row.success_count:
                    points.append(SimpleNamespace(checked_at=row.started_at,
                                                  duration_ms=row.response_sum_ms / row.success_count,
                                                  maximum_ms=row.response_max_ms,
                                                  coverage_seconds=max(1, row.up_seconds + row.down_seconds),
                                                  healthy=True, aggregated=True))
        points.extend(SimpleNamespace(checked_at=c.checked_at, duration_ms=c.duration_ms,
                                      maximum_ms=c.duration_ms, coverage_seconds=c.coverage_seconds,
                                      healthy=c.healthy, aggregated=False)
                      for c in self.checks if max(start, self.raw_before or start) <= c.checked_at < end)
        return sorted(points, key=lambda point: point.checked_at)
