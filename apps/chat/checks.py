"""Startup checks for the chat checkpointer's configuration."""

from django.conf import settings
from django.core.checks import Error
from psycopg.conninfo import conninfo_to_dict

from apps.chat.checkpointer import get_database_url


def _target(dbname, host, port) -> tuple[str, str, str]:
    return str(dbname or ""), str(host or ""), str(port or "5432")


def same_database(django_db: dict, conninfo: str) -> bool:
    saver = conninfo_to_dict(conninfo)
    return _target(django_db.get("NAME"), django_db.get("HOST"), django_db.get("PORT")) == (
        _target(saver.get("dbname"), saver.get("host"), saver.get("port"))
    )


def check_checkpointer_shares_default_database(app_configs, **kwargs):
    """``thread_has_checkpoint`` reads the saver's tables through Django's connection, so
    the two must point at one database or the deleted-thread guard silently finds nothing.
    """
    if same_database(settings.DATABASES.get("default", {}), get_database_url()):
        return []
    return [
        Error(
            "The chat checkpointer and Django's default database are different databases.",
            hint=(
                "Point DATABASE_URL and DATABASES['default'] at the same database; the "
                "checkpoint lookup that guards deleted thread ids reads through Django."
            ),
            id="chat.E001",
        )
    ]
