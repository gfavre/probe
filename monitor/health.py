"""Read-only health check for an independent external monitor."""
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import Target

DISPATCH_HEARTBEAT_KEY = 'monitor:dispatch-heartbeat'
HEALTH_GRACE_SECONDS = 180
HEALTH_OK = 'ok'
HEALTH_UNAVAILABLE = 'unavailable'


@never_cache
@require_safe
def health(request):
    cutoff = timezone.now() - timedelta(seconds=HEALTH_GRACE_SECONDS)
    try:
        # Always query PostgreSQL, including installations without active probes.
        overdue = Target.objects.filter(enabled=True, next_check_at__lt=cutoff).exists()
        heartbeat = cache.get(DISPATCH_HEARTBEAT_KEY)
        healthy = not overdue and heartbeat is not None and heartbeat >= cutoff.timestamp()
    except Exception:
        # Dependency failures must not expose credentials or render a database-backed page.
        healthy = False
    return JsonResponse(
        {'status': HEALTH_OK if healthy else HEALTH_UNAVAILABLE, 'version': settings.APP_VERSION},
        status=200 if healthy else 503,
    )
