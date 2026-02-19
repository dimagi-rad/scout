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
# CI sets DATABASE_USER/PASSWORD/HOST/PORT explicitly; local dev falls back to defaults.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("TEST_DATABASE_NAME", default="scout_test"),
        "USER": env("DATABASE_USER", default="postgres"),
        "PASSWORD": env("DATABASE_PASSWORD", default=""),
        "HOST": env("DATABASE_HOST", default="localhost"),
        "PORT": env("DATABASE_PORT", default="5432"),
    }
}

# Disable email sending in tests
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Use a test encryption key (valid Fernet key)
DB_CREDENTIAL_KEY = "uHcVl3o7sAzBTV0ECblIGcB4imVnoutulGMF-dNsUoM="
