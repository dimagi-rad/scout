"""
Django development settings for Scout data agent platform.
"""
from .base import *  # noqa: F401, F403

DEBUG = True

# Allow common development hosts
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", ".ngrok-free.app"]

# Use console email backend for development
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# For dev, default to the same database as the app (separate schema isolation still applies)
if not MANAGED_DATABASE_URL:  # noqa: F405
    MANAGED_DATABASE_URL = DATABASES["default"]["NAME"]  # noqa: F405

# Debug toolbar (optional, add to INSTALLED_APPS if needed)
# INSTALLED_APPS += ["debug_toolbar"]
# MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
# INTERNAL_IPS = ["127.0.0.1"]

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "allauth": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}
