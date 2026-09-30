"""The one parser for Postgres connection URLs, shared by Django code and the MCP server.

Parsing goes through libpq (psycopg's ``conninfo_to_dict``), so a builder that
hands psycopg or dbt individual fields sees exactly what a raw-URL
``psycopg.connect`` would: percent-decoded credentials and the same query
options. Must stay importable without Django setup.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432


def parse_pg_url(url: str) -> dict[str, Any]:
    """Parse a ``postgresql://`` URL (or key=value conninfo) as libpq does."""
    try:
        return conninfo_to_dict(url)
    except ProgrammingError:
        # libpq's message quotes the offending text, which can be the password.
        raise ValueError("Invalid Postgres connection URL") from None


def pg_connection_identity(url: str) -> dict[str, Any]:
    """Host, port, dbname, user, password and (when set) sslmode from ``url``.

    Other query options (``options``, ``connect_timeout``, ``application_name``)
    are dropped: callers of this set their own per-connection options. Host and
    port default to ``localhost:5432`` rather than libpq's Unix-socket default.
    """
    parsed = parse_pg_url(url)
    params: dict[str, Any] = {
        "host": parsed.get("host") or DEFAULT_HOST,
        "port": int(parsed.get("port") or DEFAULT_PORT),
        "dbname": parsed.get("dbname") or "",
        "user": parsed.get("user") or "",
        "password": parsed.get("password") or "",
    }
    if parsed.get("sslmode"):
        params["sslmode"] = parsed["sslmode"]
    return params


def build_pg_url(*, host: str, port: int | str, dbname: str, user: str, password: str = "") -> str:
    """A ``postgresql://`` URL with percent-encoded credentials and database name."""
    credentials = quote(user, safe="")
    if password:
        credentials += f":{quote(password, safe='')}"
    return f"postgresql://{credentials}@{host}:{port}/{quote(dbname, safe='')}"
