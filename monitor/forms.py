from django.utils.translation import gettext_lazy as _
import re
from django import forms
from django.contrib.auth import get_user_model
from .models import Target, UserProfile


class ProbeURLField(forms.URLField):
    def to_python(self, value):
        value = super().to_python(value)
        # A bare hostname with a port can otherwise be mistaken for a URI scheme.
        if value and not re.match(r'^[a-z][a-z0-9+.-]*://', value, re.IGNORECASE):
            value = 'https://' + value
        return value


class TargetForm(forms.ModelForm):
    url = ProbeURLField(
        label=_('URL HTTP(S)'), max_length=2000, assume_scheme='https',
        widget=forms.TextInput(attrs={
            'inputmode': 'url', 'autocomplete': 'url', 'autocapitalize': 'none',
            'autocorrect': 'off', 'spellcheck': 'false', 'placeholder': 'example.org/health',
        }),
    )

    class Meta:
        model = Target
        fields = [
            'name', 'url', 'notes', 'enabled', 'interval_minutes', 'failure_threshold',
            'notification_delay_minutes', 'timeout_seconds', 'ip_version', 'verify_tls',
            'follow_redirects', 'cache_buster', 'method', 'auth_type', 'auth_username',
            'auth_password', 'auth_token', 'body_type', 'body', 'request_headers',
            'expected_status_codes', 'keyword', 'keyword_ignore_case', 'response_headers',
            'notification_channel', 'email_to', 'telegram_chat_id',
        ]
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
            'body': forms.Textarea(attrs={'rows': 5}),
            'request_headers': forms.Textarea(attrs={'rows': 4, 'placeholder': '{"Accept": "application/json"}'}),
            'response_headers': forms.Textarea(attrs={'rows': 4, 'placeholder': '{}'}),
            'auth_password': forms.PasswordInput(render_value=False),
            'auth_token': forms.PasswordInput(render_value=False),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for name in ('auth_password', 'auth_token'):
                self.fields[name].widget.attrs['autocomplete'] = 'new-password'
            self.fields[name].help_text = _('Laisser vide pour conserver la valeur enregistrée.')

    def clean_auth_password(self):
        return self.cleaned_data['auth_password'] or (self.instance.auth_password if self.instance.pk else '')

    def clean_auth_token(self):
        return self.cleaned_data['auth_token'] or (self.instance.auth_token if self.instance.pk else '')

    def clean_request_headers(self):
        return self.cleaned_data['request_headers'] or {}

    def clean_response_headers(self):
        return self.cleaned_data['response_headers'] or {}


class InvitationForm(forms.Form):
    role = forms.ChoiceField(label=_("Droits"), choices=UserProfile.Role.choices, initial=UserProfile.Role.VIEWER)
    email = forms.EmailField(label=_('Adresse email'), widget=forms.EmailInput(attrs={'placeholder': 'nom@example.org'}))


class AcceptInvitationForm(forms.Form):
    password = forms.CharField(label=_('Mot de passe'), widget=forms.PasswordInput)
    password_confirm = forms.CharField(label=_('Confirmer le mot de passe'), widget=forms.PasswordInput)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        from django.contrib.auth.password_validation import validate_password
        cleaned = super().clean()
        if cleaned.get('password') != cleaned.get('password_confirm'):
            self.add_error('password_confirm', _('Les mots de passe ne correspondent pas.'))
        if cleaned.get('password'):
            try:
                validate_password(cleaned['password'], user=self.user)
            except forms.ValidationError as exc:
                self.add_error('password', exc)
        return cleaned


class ProfileForm(forms.ModelForm):
    language = forms.ChoiceField(
        label=_('Langue de l’interface'), required=False,
        help_text=_('Sans préférence enregistrée, l’interface suit la langue du navigateur.'),
    )

    class Meta:
        model = get_user_model()
        fields = ['first_name', 'last_name', 'username']
        labels = {'first_name': _('Prénom'), 'last_name': _('Nom'), 'username': _('Identifiant local')}

    def __init__(self, *args, **kwargs):
        from .models import UserProfile
        super().__init__(*args, **kwargs)
        self.fields['language'].choices = [('', '—'), *UserProfile.Language.choices]
        if not self.is_bound:
            self.initial['language'] = UserProfile.objects.filter(user=self.instance).values_list('language', flat=True).first() or ''


class PlatformSettingsForm(forms.Form):
    logo = forms.FileField(
        label=_('Nouveau logo'), required=False,
        help_text=_('SVG, PNG, JPEG ou WebP · 256 Ko maximum. PNG/JPEG/WebP sont aussi utilisés dans les emails.'),
        widget=forms.FileInput(attrs={'accept': '.svg,.png,.jpg,.jpeg,.webp'}),
    )
    reset_logo = forms.BooleanField(label=_('Revenir au logo par défaut'), required=False)
    postmark_server_token = forms.CharField(label=_('Clé serveur Postmark'), required=False, widget=forms.PasswordInput)
    email_from = forms.EmailField(label=_('Expéditeur email'), required=False)
    email_to = forms.EmailField(label=_('Destinataire email par défaut'), required=False)
    postmark_message_stream = forms.CharField(label=_('Message Stream Postmark'), initial='outbound')
    slack_webhook_url = forms.URLField(label=_('Webhook Slack'), required=False, widget=forms.PasswordInput)
    telegram_bot_token = forms.CharField(label=_('Token du bot Telegram'), required=False, widget=forms.PasswordInput)
    telegram_chat_id = forms.CharField(label=_('Conversation Telegram par défaut'), required=False)
    google_client_id = forms.CharField(label=_('Client ID Google'), required=False)
    google_client_secret = forms.CharField(label=_('Client secret Google'), required=False, widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        from .platform import SECRET_FIELDS, platform_values
        values = platform_values()
        super().__init__(*args, **kwargs)
        self.configured = {name: bool(values[name]) for name in SECRET_FIELDS}
        for name in SECRET_FIELDS:
            self.fields[name].help_text = _('Configuré. Laisser vide pour conserver le secret.') if values[name] else _('Non configuré.')
            self.fields['clear_' + name] = forms.BooleanField(label=_('Effacer ce secret'), required=False)
        for name in self.fields:
            if name in values and name not in SECRET_FIELDS:
                self.initial[name] = values[name]

    def clean_slack_webhook_url(self):
        value = self.cleaned_data['slack_webhook_url']
        if value and not value.startswith('https://hooks.slack.com/services/'):
            raise forms.ValidationError(_('Utilisez un webhook entrant Slack en HTTPS.'))
        return value

    def clean_logo(self):
        from .branding import validate_logo
        upload = self.cleaned_data['logo']
        return validate_logo(upload) if upload else None

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('logo') and cleaned.get('reset_logo'):
            self.add_error('reset_logo', _('Choisissez un nouveau logo ou le retour au logo par défaut.'))
        return cleaned

    def save(self):
        from django.db import transaction
        from .models import PlatformSettings
        from .platform import SETTINGS_PK, PUBLIC_FIELDS, SECRET_FIELDS, decrypt_secrets, encrypt_secrets
        from .branding import save_logo
        with transaction.atomic():
            record, _created = PlatformSettings.objects.select_for_update().get_or_create(pk=SETTINGS_PK)
            secrets = decrypt_secrets(record.encrypted_secrets)
            for name in PUBLIC_FIELDS:
                setattr(record, name, self.cleaned_data[name])
            for name in SECRET_FIELDS:
                if self.cleaned_data.get('clear_' + name):
                    secrets[name] = ''
                elif self.cleaned_data[name]:
                    secrets[name] = self.cleaned_data[name]
            record.encrypted_secrets = encrypt_secrets(secrets)
            if self.cleaned_data.get('reset_logo'):
                record.logo = None
            elif self.cleaned_data.get('logo'):
                record.logo = save_logo(self.cleaned_data['logo'])
            record.save()
        return record
