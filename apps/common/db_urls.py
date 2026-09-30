"""The one parser for Postgres connection URLs, shared by Django code and the MCP server.

Parsing goes through libpq (psycopg's ``conninfo_to_dict``), so a builder that
pulls individual fields out of a URL decodes them exactly as a raw-URL
``psycopg.connect`` would. Must stay importable without Django setup.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432

_INVALID = "Invalid Postgres connection URL"


def parse_pg_url(url: str) -> dict[str, Any]:
    """Parse a ``postgresql://`` URL (or key=value conninfo) as libpq does."""
    try:
        return conninfo_to_dict(url)
    except ProgrammingError:
        # libpq's message quotes the offending text, which can be the password.
        raise ValueError(_INVALID) from None


def pg_connection_identity(url: str) -> dict[str, Any]:
    """Host, port, dbname, user, password and (when set) sslmode from ``url``.

    Other query options (``options``, ``connect_timeout``, ``application_name``)
    are dropped: callers of this set their own per-connection options. Host and
    port default to ``localhost:5432`` rather than libpq's Unix-socket default.
    A multi-host URL is rejected. ``sslrootcert``/``sslcert``/``sslkey`` are
    dropped too, so a ``verify-*`` sslmode needs its files at libpq's default paths.
    """
    parsed = parse_pg_url(url)
    try:
        port = int(parsed.get("port") or DEFAULT_PORT)
    except ValueError:
        raise ValueError(_INVALID) from None
    params: dict[str, Any] = {
        "host": parsed.get("host") or DEFAULT_HOST,
        "port": port,
        "dbname": parsed.get("dbname") or "",
        "user": parsed.get("user") or "",
        "password": parsed.get("password") or "",
    }
    if parsed.get("sslmode"):
        params["sslmode"] = parsed["sslmode"]
    return params


def build_pg_url(*, host: str, port: int | str, dbname: str, user: str, password: str = "") -> str:
    """A ``postgresql://`` URL with percent-encoded credentials and database name.

    ``host`` may be a Unix-socket directory or an IPv6 literal, as Django's
    ``DATABASES["default"]["HOST"]`` allows.
    """
    credentials = quote(user, safe="")
    if password:
        credentials += f":{quote(password, safe='')}"
    if host.startswith("/"):
        host = quote(host, safe="")
    elif ":" in host:
        host = f"[{host}]"
    return f"postgresql://{credentials}@{host}:{port}/{quote(dbname, safe='')}"
