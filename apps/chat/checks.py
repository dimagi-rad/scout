"""Startup checks for the chat checkpointer's configuration."""

from django.conf import settings
from django.core.checks import Error, Warning

from apps.chat.checkpointer import get_database_url
from apps.common.db_urls import DEFAULT_PORT, parse_pg_url


def same_database(django_db: dict, conninfo: str) -> bool:
    """Whether ``conninfo`` names the database ``django_db`` connects to.

    Raises ``ValueError`` (with the URL scrubbed) when ``conninfo`` doesn't parse.
    """
    saver = parse_pg_url(conninfo)
    if str(django_db.get("NAME") or "") != str(saver.get("dbname") or ""):
        return False
    if str(django_db.get("PORT") or DEFAULT_PORT) != str(saver.get("port") or DEFAULT_PORT):
        return False
    # An empty Django HOST is libpq's default (socket or localhost), which a URL can
    # spell several ways, so only an explicit host is compared. This misses a hostless
    # Django config against a remote URL with the same dbname; accepted as unlikely.
    django_host = str(django_db.get("HOST") or "")
    return not django_host or django_host == str(saver.get("host") or "")


def check_checkpointer_shares_default_database(app_configs, **kwargs):
    """``thread_has_checkpoint`` reads the saver's tables through Django's connection, so
    the two must point at one database or the deleted-thread guard silently finds nothing.

    Emits chat.E001 (different databases) or chat.E002 (unresolvable URL) outside
    DEBUG, so ``migrate`` stops a misconfigured deploy; chat.W001/chat.W002 in local
    development. ``config.settings.test`` silences both errors: with a ``.env``
    DATABASE_URL it diverges on purpose, and its tests build the saver themselves.
    """
    level, prefix = (Warning, "W") if settings.DEBUG else (Error, "E")
    try:
        matches = same_database(settings.DATABASES.get("default", {}), get_database_url())
    except ValueError as exc:
        return [
            level(
                f"The chat checkpointer's database could not be resolved: {exc}",
                id=f"chat.{prefix}002",
            )
        ]
    if matches:
        return []
    return [
        level(
            "The chat checkpointer and Django's default database are different databases.",
            hint=(
                "Point DATABASE_URL and DATABASES['default'] at the same database; the "
                "checkpoint lookup that guards deleted thread ids reads through Django."
            ),
            id=f"chat.{prefix}001",
        )
    ]
