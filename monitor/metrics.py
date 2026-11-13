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
