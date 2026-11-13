from types import SimpleNamespace
from django.utils import translation
from django.utils.cache import patch_vary_headers
from .models import UserProfile


class ProfileLanguageMiddleware:
    """Explicit profile preference, otherwise the current browser language."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        preference = ''
        if request.user.is_authenticated:
            profile = UserProfile.objects.filter(user=request.user).values('language', 'role').first()
            if profile:
                preference = profile['language']
                request.user._probe_role = profile['role']
        # A stale language cookie must not override the browser in automatic mode.
        browser = SimpleNamespace(META=request.META, COOKIES={})
        language = preference or translation.get_language_from_request(browser)
        request.language_preference = preference
        request.LANGUAGE_CODE = language
        with translation.override(language):
            response = self.get_response(request)
        response.setdefault('Content-Language', language)
        patch_vary_headers(response, ('Accept-Language', 'Cookie'))
        return response
