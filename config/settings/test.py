"""
Django test settings for Scout data agent platform.
"""

import hashlib

from .base import *
from .base import _build_caches

DEBUG = False

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# PostgreSQL (not sqlite) to match production and catch DB-specific issues.
# Local dev parses credentials from DATABASE_URL; CI sets DATABASE_* explicitly.
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

# Keyed by checkout so concurrent runs in separate worktrees don't drop each other's
# test database mid-run, while --reuse-db still finds its own. Two concurrent runs in
# the same checkout still need distinct TEST_DATABASE_NAMEs.
_checkout_key = hashlib.sha256(str(BASE_DIR).encode()).hexdigest()[:8]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("TEST_DATABASE_NAME", default=f"scout_test_{_checkout_key}"),
        "USER": env("DATABASE_USER", default=_defaults["USER"]),
        "PASSWORD": env("DATABASE_PASSWORD", default=_defaults["PASSWORD"]),
        "HOST": env("DATABASE_HOST", default=_defaults["HOST"]),
        "PORT": env("DATABASE_PORT", default=_defaults["PORT"]),
    }
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Never the developer's Redis from .env: concurrent runs shared rate-limit and
# cache keys through it and failed each other's cache tests, and every run wrote
# into the dev cache.
REDIS_URL = ""
CACHES = _build_caches(REDIS_URL)

# Test-only value; must be a valid Fernet key
DB_CREDENTIAL_KEY = "uHcVl3o7sAzBTV0ECblIGcB4imVnoutulGMF-dNsUoM="

# The shipped rule; the any-of fallback exists only for the production rollout.
WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = True

# Tests exercise the enforced gate; fixtures provision genuine fresh proofs.
UPSTREAM_ACCESS_FRESHNESS_ENFORCED = True
