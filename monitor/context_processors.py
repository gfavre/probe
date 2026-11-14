from .permissions import can_edit_probes, is_platform_admin
from .platform import platform_values
from .branding import logo_options


def auth_options(request):
    values = platform_values()
    user = getattr(request, 'user', None)
    return {
        **logo_options(),
        'google_configured': bool(values['google_client_id'] and values['google_client_secret']),
        'can_edit_probes': can_edit_probes(user) if user else False,
        'is_platform_admin': is_platform_admin(user) if user else False,
    }
