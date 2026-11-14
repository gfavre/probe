import base64
import hashlib
import json
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables
from .models import DEFAULT_PRODUCT_NAME, PlatformSettings

SETTINGS_PK = 1
SECRET_FIELDS = ('postmark_server_token', 'slack_webhook_url', 'telegram_bot_token', 'google_client_secret')
PUBLIC_FIELDS = ('product_name', 'email_from', 'email_to', 'telegram_chat_id', 'postmark_message_stream', 'google_client_id')


@sensitive_variables()
def cipher():
    key = getattr(settings, 'PLATFORM_ENCRYPTION_KEY', '')
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(('beyond-probe-platform:' + settings.SECRET_KEY).encode()).digest())
    return Fernet(key)


@sensitive_variables()
def decrypt_secrets(value):
    if not value:
        return {}
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError):
        raise ImproperlyConfigured('Unable to decrypt platform credentials.') from None


@sensitive_variables()
def encrypt_secrets(value):
    return cipher().encrypt(json.dumps(value).encode()).decode()


@sensitive_variables()
def platform_values():
    """Database overrides, with environment fallback for initial setup."""
    record = PlatformSettings.objects.filter(pk=SETTINGS_PK).first()
    result = {name: getattr(settings, name.upper(), '') for name in (*SECRET_FIELDS, *PUBLIC_FIELDS)}
    if record:
        result.update({name: getattr(record, name) for name in PUBLIC_FIELDS})
        result.update(decrypt_secrets(record.encrypted_secrets))
    result['product_name'] = result['product_name'] or DEFAULT_PRODUCT_NAME
    return result


def product_name():
    """Read the public display name without loading or decrypting credentials."""
    return (PlatformSettings.objects.filter(pk=SETTINGS_PK).values_list('product_name', flat=True).first()
            or DEFAULT_PRODUCT_NAME)
