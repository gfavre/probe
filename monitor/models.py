from django.utils.translation import gettext_lazy as _
import uuid
import json
from urllib.parse import parse_qsl
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from .validators import validate_target_url, validate_headers, validate_status_codes

DEFAULT_PRODUCT_NAME = 'beyond the probe'


class Target(models.Model):
    CONFIRMATION_DELAY_SECONDS = 60
    CHANNELS = [('email', _('Email')), ('slack', _('Slack')), ('telegram', _('Telegram'))]
    name = models.CharField(_('Nom'), max_length=200)
    url = models.URLField(_('URL HTTP(S)'), max_length=2000, unique=True, validators=[validate_target_url])
    notes = models.TextField(_('Notes'), blank=True)
    enabled = models.BooleanField(_('Surveillance active'), default=True)
    interval_minutes = models.PositiveSmallIntegerField(_('Intervalle en minutes'), default=3, validators=[MinValueValidator(1), MaxValueValidator(5)])
    failure_threshold = models.PositiveSmallIntegerField(
        _('Échecs consécutifs avant alerte'), default=2, validators=[MinValueValidator(2)],
        help_text=_('Au moins deux échecs avant de confirmer une panne.'),
    )
    notification_delay_minutes = models.PositiveSmallIntegerField(_('Délai avant alerte en minutes'), default=0, validators=[MaxValueValidator(60)])
    notification_channel = models.CharField(_('Canal de notification'), max_length=10, choices=CHANNELS, default='email')
    email_to = models.EmailField(_('Destinataire email'), blank=True, help_text=_('Vide: utiliser le destinataire configuré sur le serveur.'))
    telegram_chat_id = models.CharField(_('Conversation Telegram'), max_length=100, blank=True, help_text=_('Vide: utiliser la conversation configurée sur le serveur.'))
    timeout_seconds = models.PositiveSmallIntegerField(_('Timeout en secondes'), default=10, validators=[MinValueValidator(1), MaxValueValidator(15)])
    ip_version = models.CharField(_('Version IP'), max_length=3, default='any', choices=[('any', _('IPv4 ou IPv6')), ('4', _('IPv4')), ('6', _('IPv6'))])
    verify_tls = models.BooleanField(_('Vérifier le certificat TLS'), default=True)
    follow_redirects = models.BooleanField(_('Suivre les redirections'), default=True)
    cache_buster = models.BooleanField(_('Contourner le cache'), default=False)
    method = models.CharField(_('Méthode HTTP'), max_length=7, default='GET', choices=[(v, v) for v in ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']])
    auth_type = models.CharField(_('Authentification HTTP'), max_length=6, default='none', choices=[('none', _('Aucune')), ('basic', _('Basic')), ('bearer', _('Bearer'))])
    auth_username = models.CharField(_('Identifiant HTTP'), max_length=200, blank=True)
    auth_password = models.CharField(_('Mot de passe HTTP'), max_length=500, blank=True)
    auth_token = models.CharField(_('Jeton Bearer'), max_length=2000, blank=True)
    body_type = models.CharField(_('Format du corps'), max_length=4, default='raw', choices=[('raw', _('Texte brut')), ('json', _('JSON')), ('form', _('Formulaire'))])
    body = models.TextField(_('Corps de la requête'), blank=True)
    request_headers = models.JSONField(_('En-têtes de la requête'), default=dict, blank=True, validators=[validate_headers])
    expected_status_codes = models.CharField(_('Codes HTTP acceptés'), max_length=200, default='200-299', validators=[validate_status_codes])
    keyword = models.CharField(_('Texte attendu dans la réponse'), max_length=2000, blank=True)
    keyword_ignore_case = models.BooleanField(_('Ignorer la casse'), default=False)
    response_headers = models.JSONField(_('En-têtes attendus dans la réponse'), default=dict, blank=True, validators=[validate_headers])
    created_at = models.DateTimeField(auto_now_add=True)
    next_check_at = models.DateTimeField(default=timezone.now)
    consecutive_failures = models.PositiveIntegerField(default=0, editable=False)
    first_failure_at = models.DateTimeField(null=True, editable=False)
    tls_expires_at = models.DateTimeField(null=True, editable=False)
    tls_checked_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        ordering = ['name']
        verbose_name = _('Probe')
        verbose_name_plural = _('Probes')
        constraints = [
            models.CheckConstraint(condition=Q(interval_minutes__gte=1, interval_minutes__lte=5), name='interval_1_to_5'),
            models.CheckConstraint(condition=Q(failure_threshold__gte=2), name='failure_threshold_at_least_two'),
            models.CheckConstraint(condition=Q(timeout_seconds__gte=1, timeout_seconds__lte=15), name='timeout_1_to_15'),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.body and self.body_type == 'json':
            try:
                json.loads(self.body)
            except ValueError:
                errors['body'] = _('Le corps doit contenir un JSON valide.')
        if self.body and self.body_type == 'form':
            try:
                parse_qsl(self.body, strict_parsing=True)
            except ValueError:
                errors['body'] = _('Utilisez le format clé=valeur&autre=valeur.')
        if self.auth_type == 'basic' and not self.auth_username:
            errors['auth_username'] = _('Indiquez l’identifiant HTTP.')
        if self.auth_type == 'bearer' and not self.auth_token:
            errors['auth_token'] = _('Indiquez le jeton Bearer.')
        if self.method == 'HEAD' and self.keyword:
            errors['keyword'] = _('Un contrôle HEAD ne renvoie pas de corps. Choisissez GET pour chercher un texte.')
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class Check(models.Model):
    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name='checks')
    checked_at = models.DateTimeField()
    healthy = models.BooleanField()
    detail = models.CharField(max_length=200)
    duration_ms = models.PositiveIntegerField()
    coverage_seconds = models.PositiveIntegerField()
    status_code = models.PositiveSmallIntegerField(null=True)

    class Meta:
        indexes = [models.Index(fields=['target', '-checked_at'])]


class Incident(models.Model):
    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name='incidents')
    started_at = models.DateTimeField()
    confirmed_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    down_sent_at = models.DateTimeField(null=True, blank=True)
    recovery_sent_at = models.DateTimeField(null=True, blank=True)
    notification_channel = models.CharField(max_length=10, choices=Target.CHANNELS, default='email')
    notification_destination = models.CharField(max_length=500, blank=True)
    service_name = models.CharField(max_length=200, blank=True)
    service_url = models.CharField(max_length=2000, blank=True)
    initial_result = models.JSONField(default=dict, blank=True)
    confirmation_result = models.JSONField(default=dict, blank=True)
    latest_failure = models.JSONField(default=dict, blank=True)
    recovery_result = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['target'], condition=Q(ended_at__isnull=True), name='one_open_incident')]
        ordering = ['-started_at']


class ProbeStats(models.Model):
    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name='%(class)s_rows')
    started_at = models.DateTimeField()
    up_seconds = models.FloatField(default=0)
    down_seconds = models.FloatField(default=0)
    observable_seconds = models.FloatField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)
    response_sum_ms = models.PositiveBigIntegerField(default=0)
    response_min_ms = models.PositiveIntegerField(null=True)
    response_max_ms = models.PositiveIntegerField(null=True)
    calculated_at = models.DateTimeField(auto_now=True)
    calculation_version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        abstract = True
        constraints = [models.UniqueConstraint(fields=['target', 'started_at'], name='%(class)s_period')]


class HourlyProbeStats(ProbeStats):
    pass


class DailyProbeStats(ProbeStats):
    pass


class HistoryState(models.Model):
    target = models.OneToOneField(Target, on_delete=models.CASCADE, related_name='history_state')
    hourly_until = models.DateTimeField(null=True)
    daily_until = models.DateTimeField(null=True)
    raw_before = models.DateTimeField(null=True)
    hourly_before = models.DateTimeField(null=True)
    last_started_at = models.DateTimeField(null=True)
    last_success_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=100, blank=True)
    deleted_checks = models.PositiveBigIntegerField(default=0)


class Invitation(models.Model):
    email = models.EmailField(unique=True)
    user = models.OneToOneField('auth.User', on_delete=models.CASCADE, related_name='invitation')
    key = models.UUIDField(default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def pending(self):
        return self.accepted_at is None and self.revoked_at is None and self.expires_at > timezone.now()


class UserProfile(models.Model):
    class Role(models.TextChoices):
        VIEWER = 'viewer', _('Consultation')
        EDITOR = 'editor', _('Gestion des sondes')
        ADMIN = 'admin', _('Administration plateforme')

    class Language(models.TextChoices):
        FRENCH = 'fr', _('Français')
        ENGLISH = 'en', _('English')

    user = models.OneToOneField('auth.User', on_delete=models.CASCADE, related_name='profile')
    language = models.CharField(max_length=2, choices=Language.choices, blank=True, default='')
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.VIEWER)

    def __str__(self):
        return self.user.get_username()


class BrandingLogo(models.Model):
    digest = models.CharField(primary_key=True, max_length=64)
    data = models.BinaryField()
    content_type = models.CharField(max_length=30)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class PlatformSettings(models.Model):
    product_name = models.CharField(max_length=80, default=DEFAULT_PRODUCT_NAME)
    logo = models.ForeignKey(BrandingLogo, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='platform_settings')
    email_from = models.EmailField(blank=True)
    email_to = models.EmailField(blank=True)
    telegram_chat_id = models.CharField(max_length=100, blank=True)
    postmark_message_stream = models.CharField(max_length=100, default='outbound')
    google_client_id = models.CharField(max_length=500, blank=True)
    encrypted_secrets = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.product_name
