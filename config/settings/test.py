"""
Django test settings for Scout data agent platform.
"""

from .base import *  # noqa: F401, F403

DEBUG = False

# Use faster password hasher in tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Use PostgreSQL test database to match production and catch DB-specific issues.
# Local dev: parse credentials from DATABASE_URL in .env.
# CI: sets DATABASE_USER/PASSWORD/HOST/PORT explicitly (no DATABASE_URL).
_db_url = env.str("DATABASE_URL", default="")
if _db_url:
    _parsed = env.db("DATABASE_URL")
    _defaults = {
        "USER": _parsed.get("USER", "postgres"),
        "PASSWORD": _parsed.get("PASSWORD", ""),
        "HOST": _parsed.get("HOST", "localhost"),
        "PORT": str(_parsed.get("PORT", 5432)),
    }
else:
    _defaults = {"USER": "postgres", "PASSWORD": "", "HOST": "localhost", "PORT": "5432"}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("TEST_DATABASE_NAME", default="scout_test"),
        "USER": env("DATABASE_USER", default=_defaults["USER"]),
        "PASSWORD": env("DATABASE_PASSWORD", default=_defaults["PASSWORD"]),
        "HOST": env("DATABASE_HOST", default=_defaults["HOST"]),
        "PORT": env("DATABASE_PORT", default=_defaults["PORT"]),
    }
}

# Disable email sending in tests
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Use a test encryption key (valid Fernet key)
DB_CREDENTIAL_KEY = "uHcVl3o7sAzBTV0ECblIGcB4imVnoutulGMF-dNsUoM="
