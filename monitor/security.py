"""Browser hardening and bounded authentication attempts without extra dependencies."""
import ipaddress
import logging
import secrets
import time
import sentry_sdk

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)
AUTH_WINDOW_SECONDS = 300
AUTH_IP_LIMIT = 20
AUTH_ACCOUNT_IP_LIMIT = 5


def client_network(request):
    """Trust only the last address appended by an explicitly trusted proxy."""
    address = request.META.get('REMOTE_ADDR', '')
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        address = forwarded.rsplit(',', 1)[-1].strip() or address
    try:
        ip = ipaddress.ip_address(address)
        return str(ipaddress.ip_network(f'{ip}/{64 if ip.version == 6 else 32}', strict=False))
    except ValueError:
        return 'unknown'


def authentication_limited(request):
    if request.method != 'POST':
        return False
    return request.path in ('/login/local/', '/admin/login/', '/accounts/google/login/', '/profile/password/') or (
        request.path.startswith('/invitations/accept/')
    )


class AuthenticationThrottleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not authentication_limited(request):
            return self.get_response(request)
        window = int(time.time()) // AUTH_WINDOW_SECONDS
        network = client_network(request)
        username = request.POST.get('username', '').strip().casefold()
        buckets = [(network, AUTH_IP_LIMIT)]
        if username:
            buckets.append((f'{network}:{username}', AUTH_ACCOUNT_IP_LIMIT))
        try:
            for identity, limit in buckets:
                digest = salted_hmac('authentication-throttle', identity).hexdigest()
                key = f'auth:{window}:{digest}'
                if cache.add(key, 1, timeout=AUTH_WINDOW_SECONDS * 2):
                    count = 1
                else:
                    count = cache.incr(key)
                if count > limit:
                    response = render(request, 'monitor/security_error.html', {
                        'error': _('Trop de tentatives. Réessayez dans quelques minutes.'),
                    }, status=429)
                    response['Retry-After'] = str(AUTH_WINDOW_SECONDS - int(time.time()) % AUTH_WINDOW_SECONDS)
                    return response
        except Exception:
            # Do not bypass the limiter or expose connection credentials during an outage.
            logger.warning('Authentication rate limiter unavailable')
            response = render(request, 'monitor/security_error.html', {
                'error': _('La connexion est temporairement indisponible. Réessayez plus tard.'),
            }, status=503)
            response['Retry-After'] = '60'
            return response
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(24)
        request.error_event_before = sentry_sdk.last_event_id()
        response = self.get_response(request)
        policy = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{request.csp_nonce}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self' https://accounts.google.com"
        )
        if settings.SECURE_SSL_REDIRECT:
            policy += '; upgrade-insecure-requests'
        if getattr(response, 'branding_asset', False):
            # Uploaded assets never inherit the script permissions of application pages.
            policy = "sandbox; default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        response['Content-Security-Policy'] = policy
        response['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=(), usb=()'
        if not request.path.startswith(settings.STATIC_URL) and not getattr(response, 'branding_asset', False):
            response['Cache-Control'] = 'private, no-store'
        return response
