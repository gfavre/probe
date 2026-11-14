import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = False
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
CSRF_TRUSTED_ORIGINS = list(filter(None, os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',')))
INSTALLED_APPS = [
    'monitor',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_celery_beat',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.postgresql',
    'HOST': os.getenv('POSTGRES_HOST', 'db'),
    'PORT': os.getenv('POSTGRES_PORT', '5432'),
    'NAME': os.getenv('POSTGRES_DB', 'uptime'),
    'USER': os.getenv('POSTGRES_USER', 'uptime'),
    'PASSWORD': os.environ['POSTGRES_PASSWORD'],
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LANGUAGE_CODE = 'fr-ch'
TIME_ZONE = 'Europe/Zurich'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Docker builds a content-hashed manifest; local runserver keeps source URLs.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        if os.getenv('STATIC_MANIFEST', 'false') == 'true'
        else 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}
LOGIN_URL = '/admin/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = os.getenv('HTTPS', 'false') == 'true'
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 12 * 60 * 60
SECURE_SSL_REDIRECT = SESSION_COOKIE_SECURE
SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '3600')) if SESSION_COOKIE_SECURE else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
# HTTPS form posts need a same-origin referrer for Django's CSRF checks.
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
# Enable only when the reverse proxy overwrites these headers and cannot be bypassed.
TRUST_PROXY_HEADERS = os.getenv('TRUST_PROXY_HEADERS', 'false') == 'true'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if TRUST_PROXY_HEADERS else None
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200
DATA_UPLOAD_MAX_NUMBER_FILES = 0
HTTP_TIMEOUT = float(os.getenv('HTTP_TIMEOUT', '10'))
if not 0 < HTTP_TIMEOUT <= 15:
    raise ValueError('HTTP_TIMEOUT must be greater than 0 and at most 15 seconds.')
POSTMARK_SERVER_TOKEN = os.getenv('POSTMARK_SERVER_TOKEN', '')
EMAIL_FROM = os.getenv('EMAIL_FROM', '')
EMAIL_TO = os.getenv('EMAIL_TO', '')
POSTMARK_MESSAGE_STREAM = os.getenv('POSTMARK_MESSAGE_STREAM', 'outbound')
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://redis:6379/0')
CACHES = {'default': {
    'BACKEND': 'django.core.cache.backends.redis.RedisCache' if SESSION_COOKIE_SECURE
    else 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': os.getenv('CACHE_URL', CELERY_BROKER_URL) if SESSION_COOKIE_SECURE else 'local-security',
    'KEY_PREFIX': 'probe-security',
}}
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_PUBLISH_RETRY = False
CELERY_TASK_ACKS_LATE = False
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = 40
CELERY_TASK_TIME_LIMIT = 45
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_BEAT_SCHEDULE = {
    'dispatch-checks': {
        'task': 'monitor.tasks.dispatch_checks',
        'schedule': 15.0,
        'options': {'expires': 15},
    },
}

# Google login by invitation, alongside Django local accounts.
INSTALLED_APPS += ['allauth', 'allauth.account', 'allauth.socialaccount', 'allauth.socialaccount.providers.google']
MIDDLEWARE.insert(MIDDLEWARE.index('django.contrib.auth.middleware.AuthenticationMiddleware') + 1, 'allauth.account.middleware.AccountMiddleware')
AUTHENTICATION_BACKENDS = ['django.contrib.auth.backends.ModelBackend', 'allauth.account.auth_backends.AuthenticationBackend']
OWNER_EMAIL = os.getenv('OWNER_EMAIL', '').strip().lower()
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
SOCIALACCOUNT_PROVIDERS = {'google': {
    'APP': {'client_id': GOOGLE_CLIENT_ID, 'secret': GOOGLE_CLIENT_SECRET, 'key': ''},
    'SCOPE': ['profile', 'email'],
    'AUTH_PARAMS': {'prompt': 'select_account'},
    'OAUTH_PKCE_ENABLED': True,
}}
ACCOUNT_ADAPTER = 'monitor.auth.ClosedAccountAdapter'
SOCIALACCOUNT_ADAPTER = 'monitor.auth.OwnerSocialAdapter'
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*']
ACCOUNT_EMAIL_VERIFICATION = 'none'
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_STORE_TOKENS = False
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
ACCOUNT_LOGOUT_REDIRECT_URL = '/login/'
SLACK_WEBHOOK_URL = os.getenv('SLACK_WEBHOOK_URL', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
TEMPLATES[0]['OPTIONS']['context_processors'].append('monitor.context_processors.auth_options')
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://status.beyondthewall.ch')
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGES = [('fr', 'Français'), ('en', 'English')]
LOCALE_PATHS = [BASE_DIR / 'locale']
USE_I18N = True
MIDDLEWARE.insert(MIDDLEWARE.index('django.contrib.auth.middleware.AuthenticationMiddleware') + 1, 'monitor.middleware.ProfileLanguageMiddleware')
PLATFORM_ENCRYPTION_KEY = os.getenv('PLATFORM_ENCRYPTION_KEY', '')
MIDDLEWARE.insert(1, 'monitor.security.SecurityHeadersMiddleware')
MIDDLEWARE.insert(MIDDLEWARE.index('monitor.middleware.ProfileLanguageMiddleware') + 1,
                  'monitor.security.AuthenticationThrottleMiddleware')
