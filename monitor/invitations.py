from django.utils.translation import gettext_lazy as _
import uuid
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from .models import Invitation, UserProfile
from .notifications import send_notification

SALT = 'monitor.invitation.v1'


def invitation_token(invitation):
    return signing.dumps({'id': invitation.pk, 'key': str(invitation.key)}, salt=SALT)


def invitation_url(invitation):
    return settings.PUBLIC_URL.rstrip('/') + reverse('accept_invitation', args=[invitation_token(invitation)])


def get_invitation(token):
    try:
        payload = signing.loads(token, salt=SALT, max_age=7 * 86400)
        invitation = Invitation.objects.select_related('user').get(pk=payload['id'], key=payload['key'])
    except (signing.BadSignature, Invitation.DoesNotExist, KeyError, ValueError, TypeError):
        return None
    return invitation if invitation.pending else None


@transaction.atomic
def create_invitation(email, role=None):
    if role is not None and role not in UserProfile.Role.values:
        raise ValidationError(_('Droits invalides.'))
    email = email.strip().lower()
    existing = get_user_model().objects.filter(email__iexact=email).first()
    invitation = Invitation.objects.select_for_update().filter(email=email).first()
    if existing and (not invitation or invitation.accepted_at or existing.is_active):
        raise ValidationError(_('Un compte existe déjà pour cette adresse.'))
    if invitation:
        invitation.key = uuid.uuid4()
        invitation.created_at = timezone.now()
        invitation.expires_at = timezone.now() + timedelta(days=7)
        invitation.revoked_at = None
        invitation.save()
    else:
        user = get_user_model()(username=f'invite_{uuid.uuid4().hex}', email=email, is_active=False)
        user.set_unusable_password()
        user.save()
        invitation = Invitation.objects.create(email=email, user=user, expires_at=timezone.now() + timedelta(days=7))
    profile, _created = UserProfile.objects.get_or_create(user=invitation.user)
    if role is not None:
        profile.role = role
        profile.save(update_fields=['role'])
    admin = profile.role == UserProfile.Role.ADMIN
    get_user_model().objects.filter(pk=invitation.user_id).update(is_staff=admin, is_superuser=admin)
    return invitation


def send_invitation(invitation):
    send_notification('Invitation à beyond the probe',
                      f'Vous avez été invité à accéder à beyond the probe.\n\n'
                      f'Activez votre compte avec ce lien valable 7 jours:\n{invitation_url(invitation)}\n\n'
                      'Vous pourrez utiliser un mot de passe local ou votre compte Google avec cette même adresse.',
                      'email', invitation.email)


@transaction.atomic
def activate_invitation(invitation, password=None):
    current = Invitation.objects.select_for_update().get(pk=invitation.pk)
    if current.key != invitation.key or not current.pending:
        raise ValidationError(_('Cette invitation n’est plus valable.'))
    user = current.user
    user.is_active = True
    if password is not None:
        user.set_password(password)
    user.save()
    current.accepted_at = timezone.now()
    current.save(update_fields=['accepted_at'])
    return user
