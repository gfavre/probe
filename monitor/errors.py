"""A database-independent fallback page for unhandled server errors."""
import logging
import uuid

import sentry_sdk
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from .models import DEFAULT_PRODUCT_NAME

logger = logging.getLogger(__name__)


def error_context():
    context = {
        'product_name': DEFAULT_PRODUCT_NAME, 'product_name_custom': False,
    }
    try:
        from .branding import logo_options
        from .platform import product_name
        context.update(logo_options())
        context['product_name'] = product_name()
        context['product_name_custom'] = context['product_name'] != DEFAULT_PRODUCT_NAME
    except Exception:
        # Broken database/credentials must not recursively break the error page.
        pass
    return context


def server_error(request):
    event_id = sentry_sdk.last_event_id() if sentry_sdk.get_client().is_active() else None
    reference = event_id or uuid.uuid4().hex
    logger.error('Unhandled server error; reference=%s', reference)
    context = {
        **error_context(), 'error_reference': reference, 'error_reported': bool(event_id),
        'error_code': 500, 'error_title': _('Une erreur s’est produite'),
    }
    # Deliberately omit request: application context processors read the database/secrets.
    return HttpResponse(render_to_string('monitor/server_error.html', context), status=500)


def page_not_found(request, exception):
    context = {**error_context(), 'error_code': 404, 'error_title': _('Page introuvable')}
    return HttpResponse(render_to_string('monitor/not_found.html', context), status=404)
