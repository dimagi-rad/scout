"""
Django base settings for Scout data agent platform.

Settings common to all environments. Environment-specific settings
override these in development.py, production.py, and test.py.
"""

import os
from pathlib import Path

import environ
import sentry_sdk

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)

env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))


# No default — raises ImproperlyConfigured if unset (overridden in development.py)
SECRET_KEY = env("DJANGO_SECRET_KEY")

DEBUG = env("DJANGO_DEBUG", default=True)

# Settings modules that carry the production security posture. connectlabs
# inherits from production.py (see config/settings/connectlabs.py) so it must be
# labeled "production" for telemetry even though its module name is not
# ".production". Keep this list in sync with any new prod-posture module.
PRODUCTION_SETTINGS_MODULES = (
    "config.settings.production",
    "config.settings.connectlabs",
)


def resolve_deploy_environment(settings_module: str) -> str:
    """Map a DJANGO_SETTINGS_MODULE name to a deploy-environment label.

    Any module that *is* or *inherits from* production (see
    PRODUCTION_SETTINGS_MODULES) resolves to "production"; everything else is
    "development". Matching the full module name — not just an ".production"
    suffix — is what lets connectlabs (prod posture, non-".production" name) be
    correctly tagged as production for Sentry / Task Badger (issue #248, 08#5).
    """
    return "production" if settings_module in PRODUCTION_SETTINGS_MODULES else "development"


# Default deployment environment label for Sentry / Task Badger. Derived from the
# settings module (set before settings load) rather than DEBUG: base.py defaults
# DEBUG to True and production.py only flips it after this file is imported, so a
# DEBUG-based default would freeze to "development" even under production settings.
# An explicit SENTRY_ENVIRONMENT / TASKBADGER_ENVIRONMENT env var still wins.
DEPLOY_ENVIRONMENT = resolve_deploy_environment(os.environ.get("DJANGO_SETTINGS_MODULE", ""))

ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "rest_framework",
    "procrastinate.contrib.django",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    "apps.users.providers.commcare",
    "apps.users.providers.commcare_connect",
    "apps.users.providers.ocs",
    "apps.users",
    "apps.workspaces",
    "apps.knowledge",
    "apps.agents",
    "apps.artifacts",
    "apps.recipes",
    "apps.chat",
    "apps.transformations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "config.middleware.embed.EmbedFrameOptionsMiddleware",
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


DATABASES = {
    "default": env.db("DATABASE_URL", default="postgresql://localhost/scout"),
}

# Separate from the application DB to allow future migration to Snowflake etc.
MANAGED_DATABASE_URL = env("MANAGED_DATABASE_URL", default="")


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


LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "users.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]


SITE_ID = 1  # required for django.contrib.sites

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = "optional"
ACCOUNT_DEFAULT_HTTP_PROTOCOL = env("ACCOUNT_DEFAULT_HTTP_PROTOCOL", default="http")

# Require POST (not GET) to initiate OAuth, so /accounts/<provider>/login/ can't
# be triggered by a forged GET (login CSRF). allauth renders a short CSRF-token
# "Continue with <provider>" interstitial on GET that POSTs to the same URL.
# (arch #258, finding 14#2.)
SOCIALACCOUNT_LOGIN_ON_GET = False
SOCIALACCOUNT_AUTO_SIGNUP = True
# Don't require email for OAuth signups (Connect doesn't provide one)
SOCIALACCOUNT_EMAIL_REQUIRED = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
# Trust Dimagi-operated providers to have verified the email. Required so
# allauth's _lookup_by_email gate fires for these providers.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_VERIFICATION = "none"  # provider already verified
SOCIALACCOUNT_STORE_TOKENS = True  # tokens reused for data materialization
SOCIALACCOUNT_ADAPTER = "apps.users.adapters.EncryptingSocialAccountAdapter"

LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Email backend (arch #258, finding 14#0).
# Django's default is the SMTP backend pointed at localhost:25; a container with
# no local MTA would silently fail (or 500) on any send. Scout's HTML
# password-reset / email-verification surface is no longer mounted (13#9), so no
# email-dependent flow is reachable by default and the safe default is the
# console backend. Override at deploy time with EMAIL_BACKEND (and the standard
# EMAIL_HOST/EMAIL_PORT/... vars) to stand up real transactional email — see the
# DEFAULT_FROM_EMAIL note below.
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_TIMEOUT = 5
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Scout <noreply@scout.dimagi.com>")
SERVER_EMAIL = env("SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env("EMAIL_SUBJECT_PREFIX", default="[Scout] ")

# Public base URL of the Scout SPA. Emails are sent from the Procrastinate worker
# (no request context), so absolute deep-links can't use request.build_absolute_uri;
# they build off this instead. Defaults to the Vite dev server.
SCOUT_BASE_URL = env("SCOUT_BASE_URL", default="http://localhost:5173")

# Client IDs/secrets configured via Django admin at /admin/socialaccount/socialapp/
SOCIALACCOUNT_PROVIDERS = {
    "commcare_connect": {
        "OAUTH_PKCE_ENABLED": True,
        "VERIFIED_EMAIL": True,
    },
    "commcare": {
        "OAUTH_PKCE_ENABLED": True,
        "VERIFIED_EMAIL": True,
    },
    "ocs": {
        "OAUTH_PKCE_ENABLED": True,
        "VERIFIED_EMAIL": True,
    },
}

# OAuth email-domain restriction: provider id -> allowed domains (lowercase, exact).
# Absent/empty list = unrestricted; a non-empty list rejects out-of-list domains
# AND rejects no-email logins (a missing email can't satisfy a configured
# restriction — arch #258, finding 07#2). Unrestricted providers (e.g. the
# deliberately-open Connect/OCS) still allow no-email logins.
SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS: dict[str, list[str]] = env.json(
    "SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS",
    default={
        "commcare": ["dimagi.com"],
    },
)


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "120/minute",
    },
}


# Encryption key for project database credentials
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DB_CREDENTIAL_KEY = env("DB_CREDENTIAL_KEY", default="")


ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
DEFAULT_LLM_MODEL = env("DEFAULT_LLM_MODEL", default="claude-opus-4-8")

# Hard ceiling on the materialization-resume agent.ainvoke. The agent's
# recursion_limit is 50; 120s is generous for any sane follow-up response.
# Beyond this, the user sees a synthetic "took too long" message instead of
# a forever-spinner. Override per-test to exercise the timeout path.
AGENT_RESUME_TIMEOUT_S = env.int("AGENT_RESUME_TIMEOUT_S", default=120)

# Langfuse observability (optional)
LANGFUSE_SECRET_KEY = env("LANGFUSE_SECRET_KEY", default="")
LANGFUSE_PUBLIC_KEY = env("LANGFUSE_PUBLIC_KEY", default="")
LANGFUSE_BASE_URL = env("LANGFUSE_BASE_URL", default="")

# Sentry error monitoring (optional — leave SENTRY_DSN blank to disable)
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env("SENTRY_ENVIRONMENT", default=DEPLOY_ENVIRONMENT),
        release=env("SENTRY_RELEASE", default="") or None,
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        send_default_pii=env.bool("SENTRY_SEND_DEFAULT_PII", default=False),
    )

# Task Badger background-task tracking (optional — leave TASKBADGER_API_KEY blank to disable)
TASKBADGER_API_KEY = env("TASKBADGER_API_KEY", default="")
TASKBADGER_ENVIRONMENT = env("TASKBADGER_ENVIRONMENT", default=DEPLOY_ENVIRONMENT)

# MCP server URL (Scout data access layer)
MCP_SERVER_URL = env("MCP_SERVER_URL", default="http://localhost:8100/mcp")

# Shared secret authenticating callers of the internal MCP HTTP server (arch
# #253, finding 01#6). Sent by the API/worker in the X-Scout-MCP-Secret header
# and verified by mcp_server.auth.SharedSecretMiddleware. Empty in local dev
# (loopback only) disables the check; production deploy configs set it.
MCP_SHARED_SECRET = env("MCP_SHARED_SECRET", default="")

# CommCare Connect API
CONNECT_API_URL = env("CONNECT_API_URL", default="https://connect.dimagi.com")
OCS_URL = env("OCS_URL", default="https://www.openchatstudio.com")


# The default cache backs chat rate limiting, DRF throttles, and per-user
# onboarding/tenant-resolution caches. Per-process LocMemCache under
# ``uvicorn --workers N`` multiplies the rate-limit budget by N and resets it on
# every deploy (arch #254, finding 06#2), so use shared Redis when REDIS_URL is
# set; otherwise fall back to LocMemCache so dev/test/CI need no extra services.


def _build_caches(redis_url: str) -> dict:
    if redis_url:
        return {
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": redis_url,
            }
        }
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }


# REDIS_URL points at the shared ElastiCache cluster (infra/scout-stack.yml).
# Threaded into the production deploy as a secret env (config/deploy.yml).
REDIS_URL = env("REDIS_URL", default="")
CACHES = _build_caches(REDIS_URL)


MAX_CONNECTIONS_PER_PROJECT = env.int("MAX_CONNECTIONS_PER_PROJECT", default=5)
MAX_QUERIES_PER_MINUTE = env.int("MAX_QUERIES_PER_MINUTE", default=60)


CSRF_COOKIE_NAME = "csrftoken_scout"
CSRF_COOKIE_HTTPONLY = False  # SPA must read the CSRF cookie via JavaScript
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=["http://localhost:5173"])
SESSION_COOKIE_NAME = "sessionid_scout"

EMBED_ALLOWED_ORIGINS = env.list("EMBED_ALLOWED_ORIGINS", default=[])


SCHEMA_TTL_HOURS = 24  # schemas inactive longer than this are expired

# Agent recursion limit for the post-materialization resume path. A healthy
# resume is 2-5 tool calls; 20 leaves headroom for follow-up exploration
# without giving a panic-looping agent runway for ~25 cycles. The user-facing
# chat path keeps its own (higher) limit.
AGENT_RESUME_RECURSION_LIMIT = env.int("AGENT_RESUME_RECURSION_LIMIT", default=20)
