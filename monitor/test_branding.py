"""Brand upload, access boundaries, passive assets and shared rendering."""
from io import BytesIO
from pathlib import Path
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image, PngImagePlugin

from .branding import MAX_LOGO_BYTES, logo_options, save_logo, validate_logo
from .models import BrandingLogo, Incident, PlatformSettings, Target, UserProfile
from .notifications import incident_notification
from .platform import decrypt_secrets, encrypt_secrets

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 100"><path d="M0 0h400v100H0z" fill="#fff"/></svg>'


def raster_upload(image_format='PNG', size=(400, 100)):
    output = BytesIO()
    Image.new('RGB', size, color='#6750d0').save(output, format=image_format)
    return SimpleUploadedFile('logo.bin', output.getvalue(), content_type='application/octet-stream')


class BrandingTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('admin', is_staff=True, is_superuser=True)
        self.settings = PlatformSettings.objects.create(
            pk=1, email_from='alerts@example.com', email_to='ops@example.com',
            encrypted_secrets=encrypt_secrets({'postmark_server_token': 'preserved-token'}),
        )
        self.data = {'postmark_message_stream': 'outbound', 'email_from': 'alerts@example.com',
                     'email_to': 'ops@example.com'}

    def upload(self, content=SVG):
        self.client.force_login(self.admin)
        return self.client.post(reverse('platform_settings'), {
            **self.data, 'logo': SimpleUploadedFile('logo.svg', content, content_type='image/svg+xml'),
        })

    def test_admin_upload_is_durable_shared_and_does_not_clear_credentials(self):
        response = self.upload()
        self.assertRedirects(response, reverse('platform_settings'))
        self.settings.refresh_from_db()
        logo = self.settings.logo
        self.assertEqual((logo.width, logo.height), (400, 100))
        self.assertEqual(logo.content_type, 'image/svg+xml')
        self.assertEqual(decrypt_secrets(self.settings.encrypted_secrets)['postmark_server_token'], 'preserved-token')
        url = reverse('platform_logo', args=[logo.pk])
        for page in ('/login/', '/', '/admin/', '/platform/'):
            with self.subTest(page=page):
                self.assertContains(self.client.get(page), url)
        self.client.logout()
        self.assertContains(self.client.get('/login/'), url)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertNotIn('script-src', response['Content-Security-Policy'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('immutable', response['Cache-Control'])
        self.assertEqual(response.content, bytes(logo.data))
        self.assertEqual(self.client.post(url).status_code, 405)

    def test_empty_upload_keeps_logo_and_reset_restores_default_without_breaking_old_urls(self):
        self.upload()
        self.settings.refresh_from_db()
        old = self.settings.logo_id
        self.client.post('/platform/', self.data)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.logo_id, old)
        self.client.post('/platform/', {**self.data, 'reset_logo': 'on'})
        self.settings.refresh_from_db()
        self.assertIsNone(self.settings.logo_id)
        self.assertFalse(logo_options()['platform_logo_custom'])
        self.assertEqual(self.client.get(reverse('platform_logo', args=[old])).status_code, 200)

    def test_identical_uploads_reuse_the_same_asset(self):
        self.upload()
        self.upload()
        self.assertEqual(BrandingLogo.objects.count(), 1)

    def test_reset_and_replacement_cannot_be_selected_together(self):
        self.client.force_login(self.admin)
        response = self.client.post('/platform/', {**self.data, 'reset_logo': 'on',
                                                 'logo': SimpleUploadedFile('logo.svg', SVG)})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form']['reset_logo'].errors)
        self.assertFalse(BrandingLogo.objects.exists())

    def test_viewers_and_editors_cannot_upload(self):
        for role in (UserProfile.Role.VIEWER, UserProfile.Role.EDITOR):
            user = get_user_model().objects.create_user(role)
            UserProfile.objects.create(user=user, role=role)
            self.client.force_login(user)
            response = self.client.post('/platform/', {**self.data, 'logo': SimpleUploadedFile('logo.svg', SVG)})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(BrandingLogo.objects.exists())

    def test_invalid_file_preserves_existing_logo(self):
        self.upload()
        self.settings.refresh_from_db()
        original = self.settings.logo_id
        response = self.upload(b'<html>wrong</html>')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form']['logo'].errors)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.logo_id, original)

    def test_file_content_not_extension_determines_raster_format(self):
        for image_format in ('PNG', 'JPEG', 'WEBP'):
            with self.subTest(image_format=image_format):
                logo = validate_logo(raster_upload(image_format))
                self.assertEqual(logo.content_type, 'image/png')
                self.assertTrue(logo.data.startswith(b'\x89PNG\r\n\x1a\n'))
                self.assertEqual((logo.width, logo.height), (400, 100))

    def test_raster_reencoding_removes_embedded_metadata(self):
        output = BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('Comment', 'private-logo-metadata')
        exif = Image.Exif()
        exif[315] = 'private-logo-author'
        Image.new('RGB', (20, 20)).save(output, format='PNG', pnginfo=metadata, exif=exif,
                                       icc_profile=b'private-profile-data')
        logo = validate_logo(SimpleUploadedFile('logo.png', output.getvalue()))
        with Image.open(BytesIO(logo.data)) as image:
            self.assertEqual(image.info, {})
        self.assertNotIn(b'private-', logo.data)

    def test_multipart_upload_requires_valid_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        response = client.get('/platform/')
        token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', response.content).group(1).decode()
        response = client.post('/platform/', {**self.data, 'logo': SimpleUploadedFile('logo.svg', SVG)})
        self.assertEqual(response.status_code, 403)
        response = client.post('/platform/', {**self.data, 'csrfmiddlewaretoken': token,
                                              'logo': SimpleUploadedFile('logo.svg', SVG)})
        self.assertEqual(response.status_code, 302)

    def test_static_svg_preserves_local_gradients_and_existing_default(self):
        original = Path(settings.BASE_DIR, 'monitor/static/monitor/brand/logo.svg').read_bytes()
        logo = validate_logo(SimpleUploadedFile('logo.svg', original))
        self.assertEqual((logo.width, logo.height), (1068, 240))
        self.assertIn(b'url(#neon)', logo.data)

    def test_active_svg_entities_external_resources_and_disguised_formats_are_rejected(self):
        invalid = (
            SVG.replace(b'<path', b'<script>alert(1)</script><path'),
            SVG.replace(b'<path', b'<foreignObject/><path'),
            SVG.replace(b'<path', b'<image href="https://example.com/track"/><path'),
            SVG.replace(b'<path', b'<use href="https://example.com/logo.svg#x"/><path'),
            SVG.replace(b'<path', b'<animate attributeName="fill"/><path'),
            SVG.replace(b'<path', b'<path onload="alert(1)"/><path'),
            SVG.replace(b'<path', b'<style>@import "https://example.com/font";</style><path'),
            b'<!DOCTYPE svg [<!ENTITY bad "hello">]>' + SVG,
            b'<html><script>alert(1)</script></html>', b'not an image',
        )
        for content in invalid:
            with self.subTest(content=content[:60]), self.assertRaises(ValidationError):
                validate_logo(SimpleUploadedFile('logo.png', content))

    def test_size_dimensions_and_animation_are_bounded(self):
        for upload in (SimpleUploadedFile('logo.svg', b'x' * (MAX_LOGO_BYTES + 1)),
                       raster_upload(size=(4097, 1)), raster_upload(size=(1600, 1600))):
            with self.assertRaises(ValidationError):
                validate_logo(upload)
        output = BytesIO()
        images = [Image.new('RGB', (10, 10), color) for color in ('red', 'blue')]
        images[0].save(output, format='PNG', save_all=True, append_images=images[1:])
        with self.assertRaises(ValidationError):
            validate_logo(SimpleUploadedFile('animated.png', output.getvalue()))

    def test_multipart_request_limits_and_other_routes_reject_uploads(self):
        self.client.force_login(self.admin)
        response = self.client.post('/platform/', {**self.data,
                                                 'logo': SimpleUploadedFile('big.png', b'x' * (1024 * 1024))})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/platform/', {**self.data, 'logo': SimpleUploadedFile('a.svg', SVG),
                                                 'other': SimpleUploadedFile('b.svg', SVG)})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/probes/new/', {'logo': SimpleUploadedFile('a.svg', SVG)})
        self.assertEqual(response.status_code, 400)

    def test_bitmap_logo_is_in_email_but_svg_keeps_compatible_text_fallback(self):
        target = Target.objects.create(name='Service', url='https://example.org')
        incident = Incident.objects.create(target=target, started_at=timezone.now(), confirmed_at=timezone.now())
        self.settings.logo = save_logo(validate_logo(raster_upload()))
        self.settings.save()
        _, _, html = incident_notification(target, incident)
        self.assertIn(settings.PUBLIC_URL.rstrip('/') + reverse('platform_logo', args=[self.settings.logo.pk]), html)
        self.settings.logo = save_logo(validate_logo(SimpleUploadedFile('logo.svg', SVG)))
        self.settings.save()
        _, _, html = incident_notification(target, incident)
        self.assertNotIn('/branding/logo/', html)
        self.assertIn('BEYOND THE PROBE', html)
