"""The one parser for Postgres connection URLs, shared by Django code and the MCP server.

Parsing goes through libpq (psycopg's ``conninfo_to_dict``), so a builder that
pulls individual fields out of a URL decodes them exactly as a raw-URL
``psycopg.connect`` would. Must stay importable without Django setup.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432

_INVALID = "Invalid Postgres connection URL"

# The one committed copy of the AWS RDS global CA bundle, shared with Cube (PR #819);
# the Python image gets it through the Dockerfile's ``COPY . .``.
DEFAULT_DB_SSL_ROOT_CERT = Path(__file__).resolve().parents[2] / "cube_config/rds-global-bundle.pem"
DB_SSL_ROOT_CERT_ENV = "SCOUT_DB_SSL_ROOT_CERT"
# Dev and CI databases: loopback, the compose service, and Unix sockets (see is_local_db_host).
LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "platform-db"})


class DatabaseTLSConfigError(RuntimeError):
    """A remote database would be reached without a usable CA bundle to verify it."""


def parse_pg_url(url: str) -> dict[str, Any]:
    """Parse a ``postgresql://`` URL (or key=value conninfo) as libpq does."""
    try:
        return conninfo_to_dict(url)
    except ProgrammingError:
        # libpq's message quotes the offending text, which can be the password.
        raise ValueError(_INVALID) from None


def pg_connection_identity(url: str) -> dict[str, Any]:
    """Host, port, dbname, user, password and TLS settings from ``url``.

    Other query options (``options``, ``connect_timeout``, ``application_name``)
    are dropped: callers of this set their own per-connection options. Host and
    port default to ``localhost:5432`` rather than libpq's Unix-socket default.
    A URL listing several ports is rejected. A remote host always gets
    ``sslmode=verify-full`` and ``sslrootcert`` (``enforce_db_tls``); a local one keeps
    the URL's ``sslmode`` when set. ``sslcert``/``sslkey`` are dropped.
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
    return enforce_db_tls(params)


def is_local_db_host(host: str | None) -> bool:
    """Whether every host in a libpq ``host`` value is a dev/CI database or a Unix socket."""
    hosts = [h.strip() for h in str(host or "").split(",")]
    return all(not h or h.startswith("/") or h.lower() in LOCAL_DB_HOSTS for h in hosts)


def _is_local_target(host: Any, hostaddr: Any, service: Any) -> bool:
    # libpq dials hostaddr instead of resolving host, and a service file or the
    # PG* env vars can supply either, so any of them can point "localhost" remote.
    host = host or os.environ.get("PGHOST")
    hostaddr = hostaddr or os.environ.get("PGHOSTADDR")
    if service or os.environ.get("PGSERVICE"):
        return False
    addrs = [a.strip() for a in str(hostaddr or "").split(",")]
    return is_local_db_host(host) and all(not a or a in {"127.0.0.1", "::1"} for a in addrs)


def db_ssl_root_cert() -> str:
    """Path of the CA bundle remote connections verify against; raises if it is unusable."""
    return _validated_root_cert(
        os.environ.get(DB_SSL_ROOT_CERT_ENV) or str(DEFAULT_DB_SSL_ROOT_CERT)
    )


@cache
def _validated_root_cert(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DatabaseTLSConfigError(
            f"Database CA bundle {path} is unreadable ({exc.strerror}); set "
            f"{DB_SSL_ROOT_CERT_ENV} to the RDS CA bundle path."
        ) from None
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise DatabaseTLSConfigError(f"Database CA bundle {path} contains no certificates")
    return path


def enforce_db_tls(params: dict[str, Any]) -> dict[str, Any]:
    """``params`` with ``sslmode=verify-full`` and the RDS CA forced for a remote host.

    The single enforcement point for every Python Postgres connection. A remote
    host's own ``sslmode``/``sslrootcert`` (from URL query params or OPTIONS) is
    overwritten, never honoured, so ``?sslmode=disable`` cannot downgrade it.
    Local hosts are returned unchanged.
    """
    if _is_local_target(params.get("host"), params.get("hostaddr"), params.get("service")):
        return params
    return {**params, "sslmode": "verify-full", "sslrootcert": db_ssl_root_cert()}


def enforce_db_tls_conninfo(conninfo: str) -> str:
    """A conninfo string (URL or key=value) with ``enforce_db_tls`` applied."""
    params = parse_pg_url(conninfo)
    secured = enforce_db_tls(params)
    if secured is params:
        return conninfo
    return make_conninfo(conninfo, sslmode=secured["sslmode"], sslrootcert=secured["sslrootcert"])


def enforce_django_db_tls(db: dict[str, Any]) -> dict[str, Any]:
    """A Django ``DATABASES`` entry whose OPTIONS carry ``enforce_db_tls`` for a remote HOST."""
    options = db.get("OPTIONS", {})
    if _is_local_target(db.get("HOST"), options.get("hostaddr"), options.get("service")):
        return db
    options = {**options, "sslmode": "verify-full", "sslrootcert": db_ssl_root_cert()}
    return {**db, "OPTIONS": options}


def build_pg_url(*, host: str, port: int | str, dbname: str, user: str, password: str = "") -> str:
    """A ``postgresql://`` URL with percent-encoded credentials and database name.

    ``host`` is a bare name, a Unix-socket directory or an unbracketed IPv6
    literal, the forms Django's ``DATABASES["default"]["HOST"]`` holds.
    """
    credentials = quote(user, safe="")
    if password:
        credentials += f":{quote(password, safe='')}"
    if host.startswith("/"):
        host = quote(host, safe="")
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"postgresql://{credentials}@{host}:{port}/{quote(dbname, safe='')}"
