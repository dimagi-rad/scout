"""
Django base settings for Scout data agent platform.

Settings common to all environments. Environment-specific settings
override these in development.py, production.py, and test.py.
"""
import environ
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Initialize environment variables
env = environ.Env(
    DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)

# Read .env file if it exists
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))


# SECURITY WARNING: keep the secret key used in production secret!
# No default - will raise ImproperlyConfigured if not set (overridden in development.py)
SECRET_KEY = env("DJANGO_SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Third-party apps
    "rest_framework",
    "django_celery_beat",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    # Custom OAuth providers (example implementation)
    "apps.users.providers.commcare",
    # Local apps
    "apps.users",
    "apps.projects",
    "apps.knowledge",
    "apps.agents",
    "apps.artifacts",
    "apps.recipes",
    "apps.chat",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# https://docs.djangoproject.com/en/5.0/ref/settings/#databases

DATABASES = {
    "default": env.db("DATABASE_URL", default="postgresql://localhost/scout"),
}


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.0/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Custom User model
AUTH_USER_MODEL = "users.User"


# Authentication backends
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]


# django-allauth settings
# Required for django.contrib.sites
SITE_ID = 1

# Account settings - use email as primary identifier
# django-allauth 65+ uses new syntax for these settings
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
# Keep these for compatibility with older allauth versions and documentation
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = "optional"

# Social account settings
# Auto-create Django user on first OAuth login
SOCIALACCOUNT_AUTO_SIGNUP = True
# Auto-connect social account to existing user with matching email
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
# Allow OAuth users to skip email verification since provider already verified
SOCIALACCOUNT_EMAIL_VERIFICATION = "none"
# Store OAuth tokens so we can use them for data materialization
SOCIALACCOUNT_STORE_TOKENS = True
SOCIALACCOUNT_ADAPTER = "apps.users.adapters.EncryptingSocialAccountAdapter"

# Redirect URLs after login/logout
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Provider-specific settings (credentials stored in DB via Django admin SocialApp model)
# Configure client IDs and secrets via Django admin at /admin/socialaccount/socialapp/
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    },
    "github": {
        "SCOPE": ["user:email"],
    },
}


# Django REST Framework settings
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}


# Encryption key for project database credentials
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DB_CREDENTIAL_KEY = env("DB_CREDENTIAL_KEY", default="")


# LLM settings
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
DEFAULT_LLM_MODEL = "claude-sonnet-4-5-20250929"

# MCP server URL (Scout data access layer)
MCP_SERVER_URL = env("MCP_SERVER_URL", default="http://localhost:8100/mcp")


# Cache configuration
# Use Redis if available, otherwise fall back to local memory cache
REDIS_URL = env("REDIS_URL", default="")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }


# Rate limiting
MAX_CONNECTIONS_PER_PROJECT = env.int("MAX_CONNECTIONS_PER_PROJECT", default=5)
MAX_QUERIES_PER_MINUTE = env.int("MAX_QUERIES_PER_MINUTE", default=60)


# SPA / CSRF settings
# Allow the SPA to read the CSRF cookie via JavaScript
CSRF_COOKIE_NAME = "csrftoken_scout"
CSRF_COOKIE_HTTPONLY = False
# Trust the Vite dev server origin for CSRF
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=["http://localhost:5173"])
SESSION_COOKIE_NAME = "sessionid_scout"


# Celery configuration
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL or "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL or "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes max per task
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # Don't prefetch tasks (better for long-running tasks)
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
