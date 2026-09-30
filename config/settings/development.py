"""
Django development settings for Scout data agent platform.
"""

from apps.common.db_urls import build_pg_url

from .base import *

DEBUG = True

# Keep local dev from exhausting the stock Postgres max_connections=100.
# This pool is per Python process, and reloaders/workers multiply it quickly.
LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE = env.int("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE", default=0)
LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE = env.int("LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE", default=4)

# The MCP server rejects all requests without a shared secret (#51). A fixed
# local value lets the API, worker and MCP server agree without any setup; it
# guards nothing beyond loopback and is never read by production settings.
MCP_SHARED_SECRET = MCP_SHARED_SECRET or "scout-local-dev-mcp-secret"

# Use console email backend for development
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Default the managed DB to the app DB in dev (schema isolation still applies)
if not MANAGED_DATABASE_URL:
    _db = DATABASES["default"]
    MANAGED_DATABASE_URL = build_pg_url(
        host=_db.get("HOST", "localhost"),
        port=_db.get("PORT", 5432),
        dbname=_db.get("NAME", "scout"),
        user=_db.get("USER", "postgres"),
        password=_db.get("PASSWORD", ""),
    )

# Allow local Connect Labs to embed Scout
EMBED_ALLOWED_ORIGINS = ["http://localhost:8001", "http://localhost:8010", "http://localhost:3000"]
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:5173",  # Vite dev
    "http://localhost:8001",  # Connect Labs dev
    "http://localhost:8010",  # Connect Labs dev (alt port)
    "http://localhost:3000",  # Connect Labs frontend dev
]

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
