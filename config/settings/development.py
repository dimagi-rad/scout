"""
Django development settings for Scout data agent platform.
"""

from .base import *  # noqa: F401, F403

DEBUG = True

# Allow common development hosts
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    ".ngrok-free.app",
]

# Use console email backend for development
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# For dev, default to the same database as the app (separate schema isolation still applies)
if not MANAGED_DATABASE_URL:  # noqa: F405
    _db = DATABASES["default"]  # noqa: F405
    _user = _db.get("USER", "postgres")
    _password = _db.get("PASSWORD", "")
    _host = _db.get("HOST", "localhost")
    _port = _db.get("PORT", 5432)
    _name = _db.get("NAME", "scout")
    _cred = f"{_user}:{_password}@" if _password else f"{_user}@"
    MANAGED_DATABASE_URL = f"postgresql://{_cred}{_host}:{_port}/{_name}"  # noqa: F405

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
