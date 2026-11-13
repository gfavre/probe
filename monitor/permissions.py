from functools import wraps
from django.contrib.auth.views import redirect_to_login
from django.conf import settings
from django.core.exceptions import PermissionDenied
from .models import UserProfile

ROLE_RANK = {UserProfile.Role.VIEWER: 0, UserProfile.Role.EDITOR: 1, UserProfile.Role.ADMIN: 2}


def access_role(user):
    if not user.is_authenticated or not user.is_active:
        return None
    if user.is_superuser:
        return UserProfile.Role.ADMIN
    if not hasattr(user, '_probe_role'):
        user._probe_role = UserProfile.objects.filter(user=user).values_list('role', flat=True).first() or UserProfile.Role.VIEWER
    return user._probe_role


def can_edit_probes(user):
    return ROLE_RANK.get(access_role(user), -1) >= ROLE_RANK[UserProfile.Role.EDITOR]


def is_platform_admin(user):
    return access_role(user) == UserProfile.Role.ADMIN


def require_role(minimum):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if ROLE_RANK.get(access_role(request.user), -1) < ROLE_RANK[minimum]:
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


probe_editor_required = require_role(UserProfile.Role.EDITOR)
platform_admin_required = require_role(UserProfile.Role.ADMIN)
