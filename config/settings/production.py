"""
Django production settings for Scout data agent platform.
"""

import environ

env = environ.Env()

from .base import *

DEBUG = False

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True

# When Scout is embedded in a cross-origin host (EMBED_ALLOWED_ORIGINS is set),
# the session + CSRF cookies must use SameSite=None so they're sent on iframe
# requests. Safe because Secure=True is already enforced above.
if EMBED_ALLOWED_ORIGINS:
    SESSION_COOKIE_SAMESITE = "None"
    CSRF_COOKIE_SAMESITE = "None"

SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Amazon SES. region_name is required: the container has no AWS_REGION, so boto3
# raises NoRegionError without it (SCOUT-DJANGO-22).
EMAIL_BACKEND = env("EMAIL_BACKEND", default="anymail.backends.amazon_ses.EmailBackend")
_ses_client_params = {
    "region_name": env("AWS_SES_REGION", default="us-east-1"),
}
# The Procrastinate worker sends invite email but runs on the scout_shared Docker
# bridge, where IMDS hop limit 1 (arch #329) blocks it from reaching the instance
# role. When these dedicated send-only keys (IAM user scout-ses-worker,
# ses:SendEmail/SendRawEmail only) are present it authenticates with them instead;
# unset (API/MCP containers, local dev) -> boto3 falls back to the instance role /
# default credential chain. Keeping the keys off the instance role means an SSRF on
# the API can neither reach IMDS (hop 1) nor read these keys (worker-only).
_ses_access_key = env("AWS_SES_ACCESS_KEY_ID", default=None)
_ses_secret_key = env("AWS_SES_SECRET_ACCESS_KEY", default=None)
if _ses_access_key and _ses_secret_key:
    _ses_client_params["aws_access_key_id"] = _ses_access_key
    _ses_client_params["aws_secret_access_key"] = _ses_secret_key
ANYMAIL = {
    "AMAZON_SES_CLIENT_PARAMS": _ses_client_params,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "mcp_server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # The agent tool-call audit trail (arch #257, finding 08#8). This is the
        # ONLY user/thread-attributed record of agent tool calls, emitted at INFO
        # from apps.chat.stream. Without an explicit logger it falls through to
        # root (WARNING) and is suppressed entirely in production — exactly the
        # blind spot the 2026-06-10 forensic review could not see past.
        "scout.agent.audit": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # The MCP-side audit trail. mcp_server.audit propagates to its parent
        # ``mcp_server`` logger (INFO, console) above, but pin it explicitly so a
        # future change to the parent's level can't silently mute the audit.
        "mcp_server.audit": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
