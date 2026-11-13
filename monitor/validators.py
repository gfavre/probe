from django.utils.translation import gettext_lazy as _
import re
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError


def validate_target_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError(_('Indiquez une URL HTTP(S) sans identifiants, paramètres ni fragment.'))


def validate_headers(value):
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k)
        or not isinstance(v, str) or '\r' in v or '\n' in v for k, v in value.items()
    ):
        raise ValidationError(_('Saisissez un objet JSON avec des noms et valeurs d’en-têtes textuels.'))


def parse_status_codes(value):
    codes = set()
    try:
        for part in value.split(','):
            bounds = part.strip().split('-')
            low, high = int(bounds[0]), int(bounds[-1])
            if len(bounds) > 2 or not 100 <= low <= high <= 599:
                raise ValueError
            codes.update(range(low, high + 1))
    except (ValueError, AttributeError):
        raise ValidationError(_('Exemple de codes acceptés: 200-299,301,302.')) from None
    return codes


def validate_status_codes(value):
    parse_status_codes(value)
