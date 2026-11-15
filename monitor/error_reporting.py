"""Optional error-only reporting; omit request data, credentials and runtime values."""
import os

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from .exceptions import HistoryIntegrityError

# Keep code locations for diagnosis, never frame locals or captured HTTP/SQL breadcrumbs.
FRAME_FIELDS = ('filename', 'module', 'function', 'lineno', 'in_app')
EVENT_FIELDS = ('event_id', 'timestamp', 'platform', 'level', 'release', 'environment')


def private_error_event(event, hint):
    """Allowlist outgoing fields instead of guessing every possible secret key."""
    result = {key: event[key] for key in EVENT_FIELDS if key in event}
    values = []
    exception = (hint.get('exc_info') or (None, None, None))[1]
    for error in event.get('exception', {}).get('values', []):
        frames = [{key: frame[key] for key in FRAME_FIELDS if key in frame}
                  for frame in error.get('stacktrace', {}).get('frames', [])]
        values.append({
            'type': error.get('type', 'Error'), 'module': error.get('module'),
            'value': (exception.code.value
                      if isinstance(exception, HistoryIntegrityError) and error.get('type') == 'HistoryIntegrityError'
                      else 'Unhandled error; runtime values omitted to protect credentials.'),
            'stacktrace': {'frames': frames},
            'mechanism': {'type': 'generic', 'handled': False},
        })
    if values:
        result['exception'] = {'values': values}
    else:
        result['message'] = 'Application error; runtime values omitted to protect credentials.'
    return result


def configure_error_reporting(dsn):
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv('SENTRY_ENVIRONMENT', 'production'),
        release=os.getenv('SENTRY_RELEASE') or os.getenv('APP_VERSION') or None,
        integrations=[DjangoIntegration(transaction_style='function_name'),
                      CeleryIntegration(monitor_beat_tasks=False),
                      LoggingIntegration(level=None, event_level=None)],
        auto_enabling_integrations=False,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size='never',
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        auto_session_tracking=False,
        enable_logs=False,
        before_send=private_error_event,
    )
