"""Small database-backed brand assets, bounded uploads and passive image validation."""
import hashlib
import math
import re
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree

from django.conf import settings
from django.core.exceptions import RequestDataTooBig, ValidationError
from django.core.files.uploadhandler import MemoryFileUploadHandler
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.templatetags.static import static
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import BrandingLogo, PlatformSettings
from .platform import SETTINGS_PK

MAX_LOGO_BYTES = 256 * 1024
MAX_LOGO_DIMENSION = 4096
MAX_LOGO_PIXELS = 2_000_000
SVG_NAMESPACE = 'http://www.w3.org/2000/svg'
BLOCKED_SVG_ELEMENTS = {
    'script', 'foreignObject', 'image', 'feImage', 'a', 'iframe', 'object', 'embed',
    'animate', 'animateMotion', 'animateTransform', 'set', 'discard', 'audio', 'video',
}
LOCAL_URL = re.compile(r'''url\(\s*['"]?#[\w.-]+['"]?\s*\)''', re.IGNORECASE)


class LogoUploadHandler(MemoryFileUploadHandler):
    """Reject oversized multipart bodies before buffering; only platform accepts files."""

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        if content_length > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
            raise RequestDataTooBig('Upload body exceeds the configured limit.')
        return super().handle_raw_input(input_data, META, content_length, boundary, encoding)

    def new_file(self, *args, **kwargs):
        if self.request.path != reverse('platform_settings'):
            raise RequestDataTooBig('File uploads are only accepted in platform settings.')
        return super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        if start + len(raw_data) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
            raise RequestDataTooBig('Upload exceeds the configured limit.')
        return super().receive_data_chunk(raw_data, start)


@dataclass(frozen=True)
class ValidatedLogo:
    data: bytes
    content_type: str
    width: int
    height: int


def validate_dimensions(width, height):
    if not (math.isfinite(width) and math.isfinite(height)
            and 1 <= width <= MAX_LOGO_DIMENSION and 1 <= height <= MAX_LOGO_DIMENSION
            and width * height <= MAX_LOGO_PIXELS):
        raise ValidationError(_("Logo trop grand. Maximum 4'096 px par côté et 2 millions de pixels."))


def validate_svg(data):
    try:
        text = data.decode('utf-8-sig')
        if '<!doctype' in text.lower() or '<!entity' in text.lower():
            raise ValueError
        root = ElementTree.fromstring(text)
        if root.tag != f'{{{SVG_NAMESPACE}}}svg':
            raise ValueError
        nodes = list(root.iter())
        if len(nodes) > 5000:
            raise ValueError
        for node in nodes:
            if not node.tag.startswith(f'{{{SVG_NAMESPACE}}}'):
                raise ValueError
            tag = node.tag.split('}', 1)[-1]
            if tag in BLOCKED_SVG_ELEMENTS or tag.startswith('animate'):
                raise ValueError
            values = [node.text or ''] if tag == 'style' else []
            for key, value in node.attrib.items():
                name = key.rsplit('}', 1)[-1].lower()
                if name.startswith('on') or (name == 'href' and not re.fullmatch(r'#[\w.-]+', value)):
                    raise ValueError
                values.append(value)
            for value in values:
                remaining = LOCAL_URL.sub('', value).lower()
                if any(token in remaining for token in ('url(', 'javascript:', 'expression(', '@', '\\', '/*')):
                    raise ValueError
        if root.get('viewBox'):
            viewbox = [float(value) for value in re.split(r'[\s,]+', root.get('viewBox').strip())]
            if len(viewbox) != 4 or not all(math.isfinite(value) for value in viewbox):
                raise ValueError
            width, height = viewbox[2:]
        else:
            width, height = [float(re.sub(r'px$', '', root.get(key, ''))) for key in ('width', 'height')]
        validate_dimensions(width, height)
        # Serialization drops processing instructions and comments; never insert SVG markup in HTML.
        normalized = ElementTree.tostring(root, encoding='utf-8')
        return ValidatedLogo(normalized, 'image/svg+xml', math.ceil(width), math.ceil(height))
    except (UnicodeError, ElementTree.ParseError, ValueError, RecursionError):
        message = _('SVG invalide ou actif. Utilisez un SVG autonome sans script ni ressource externe.')
        raise ValidationError(message) from None


def validate_logo(upload):
    if upload.size > MAX_LOGO_BYTES:
        raise ValidationError(_('Le logo doit peser au maximum 256 Ko.'))
    data = upload.read(MAX_LOGO_BYTES + 1)
    if not data or len(data) > MAX_LOGO_BYTES:
        raise ValidationError(_('Le logo doit peser au maximum 256 Ko.'))
    if data.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'<'):
        return validate_svg(data)
    try:
        with Image.open(BytesIO(data), formats=['PNG', 'JPEG', 'WEBP']) as image:
            validate_dimensions(*image.size)
            if getattr(image, 'n_frames', 1) != 1:
                raise ValidationError(_('Utilisez un logo fixe, sans animation.'))
            image.load()
            oriented = ImageOps.exif_transpose(image)
            mode = 'RGBA' if 'A' in oriented.getbands() or 'transparency' in image.info else 'RGB'
            clean = oriented.convert(mode)
            clean.info.clear()
            output = BytesIO()
            clean.save(output, format='PNG')
            normalized = output.getvalue()
            if len(normalized) > MAX_LOGO_BYTES:
                raise ValidationError(_('Le logo doit peser au maximum 256 Ko après conversion en PNG.'))
            return ValidatedLogo(normalized, 'image/png', clean.width, clean.height)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ValidationError(_('Image invalide. Choisissez un fichier SVG, PNG, JPEG ou WebP.')) from None


def save_logo(logo):
    digest = hashlib.sha256(logo.data).hexdigest()
    asset, _created = BrandingLogo.objects.get_or_create(digest=digest, defaults={
        'data': logo.data, 'content_type': logo.content_type, 'width': logo.width, 'height': logo.height,
    })
    return asset


def logo_options(*, email=False):
    record = PlatformSettings.objects.filter(pk=SETTINGS_PK).values(
        'logo_id', 'logo__width', 'logo__height', 'logo__content_type',
    ).first()
    if record and record['logo_id']:
        if email and record['logo__content_type'] != 'image/png':
            return {}
        return {
            'platform_logo_url': reverse('platform_logo', args=[record['logo_id']]),
            'platform_logo_width': record['logo__width'], 'platform_logo_height': record['logo__height'],
            'platform_logo_custom': True,
        }
    return {} if email else {
        'platform_logo_url': static('monitor/brand/logo.svg'),
        'platform_logo_width': 1068, 'platform_logo_height': 240, 'platform_logo_custom': False,
    }


@require_safe
def platform_logo(request, digest):
    asset = get_object_or_404(BrandingLogo, digest=digest)
    response = HttpResponse(bytes(asset.data), content_type=asset.content_type)
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["Content-Disposition"] = "inline"
    response["ETag"] = '"' + asset.digest + '"'
    response.branding_asset = True
    return response
