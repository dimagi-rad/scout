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
