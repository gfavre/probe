from django.utils.translation import gettext_lazy as _
from functools import wraps
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.base import AuthError
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from .models import Invitation


def can_manage(user):
    return user.is_authenticated and user.is_active


def operator_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not can_manage(request.user):
            return HttpResponseForbidden(_('Ce compte n’a pas accès à cette application.'))
        return view(request, *args, **kwargs)
    return wrapped


class ClosedAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False


class OwnerSocialAdapter(DefaultSocialAccountAdapter):
    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        # Do not expose provider exceptions, OAuth parameters or account details.
        if error == AuthError.CANCELLED:
            raise ImmediateHttpResponse(redirect('socialaccount_login_cancelled'))
        raise ImmediateHttpResponse(render(request, 'monitor/social_login_error.html', status=401))

    def get_app(self, request, provider, client_id=None):
        if provider == 'google':
            from allauth.socialaccount.models import SocialApp
            from .platform import platform_values
            values = platform_values()
            if values['google_client_id'] and values['google_client_secret']:
                return SocialApp(provider='google', client_id=values['google_client_id'], secret=values['google_client_secret'])
        return super().get_app(request, provider, client_id)

    def verified_email(self, sociallogin):
        email = sociallogin.user.email.strip().lower()
        if sociallogin.account.provider != 'google' or not any(a.verified and a.email.lower() == email for a in sociallogin.email_addresses):
            return None
        return email

    def allowed(self, sociallogin):
        email = self.verified_email(sociallogin)
        if not email:
            return False
        existing = get_user_model().objects.filter(email__iexact=email).first()
        if existing:
            if existing.is_active:
                return True
            invitation = Invitation.objects.filter(user=existing).first()
            return bool(invitation and invitation.pending)
        # Optional first-account bootstrap; subsequent users require an invitation.
        return bool(settings.OWNER_EMAIL and email == settings.OWNER_EMAIL and not get_user_model().objects.exists())

    def pre_social_login(self, request, sociallogin):
        if not self.allowed(sociallogin):
            raise ImmediateHttpResponse(render(request, 'monitor/access_denied.html', status=403))
        email = self.verified_email(sociallogin)
        existing = get_user_model().objects.filter(email__iexact=email).first()
        if existing and not existing.is_active:
            from .invitations import activate_invitation
            existing = activate_invitation(Invitation.objects.get(user=existing))
        if existing and not sociallogin.is_existing:
            sociallogin.connect(request, existing)

    def is_open_for_signup(self, request, sociallogin):
        return self.allowed(sociallogin)

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        user.is_staff = True
        user.is_superuser = True
        user.set_unusable_password()
        user.save()
        from .models import UserProfile
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.Role.ADMIN})
        return user
