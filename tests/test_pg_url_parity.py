"""Parity of every Python builder that turns a Postgres URL into connection settings.

Each builder is run over the same URLs and its output normalised to libpq keys
with string values, so a divergence between builders shows up as a row here.
"""

import environ
import pytest
import yaml
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import ConnectionHandler
from psycopg.conninfo import conninfo_to_dict

from apps.common.db_urls import (
    DB_SSL_ROOT_CERT_ENV,
    DEFAULT_DB_SSL_ROOT_CERT,
    build_pg_url,
    enforce_db_tls_conninfo,
    enforce_django_db_tls,
    parse_pg_url,
    pg_connection_identity,
)
from mcp_server.context import _parse_db_url
from mcp_server.services.dbt_runner import generate_profiles_yml

_LIBPQ_KEYS = {
    "host",
    "port",
    "dbname",
    "user",
    "password",
    "sslmode",
    "sslrootcert",
    "connect_timeout",
    "application_name",
    "options",
}


def _mcp(url, tmp_path):
    params = _parse_db_url(url, "t_schema")
    assert params.pop("options") == "-c search_path=t_schema,public -c statement_timeout=30000"
    return {key: str(value) for key, value in params.items()}


def _dbt(url, tmp_path):
    path = tmp_path / "profiles.yml"
    generate_profiles_yml(output_path=path, schema_name="t_schema", db_url=url)
    profile = yaml.safe_load(path.read_text())["data_explorer"]["outputs"]["tenant_schema"]
    return {key: str(value) for key, value in profile.items() if key in _LIBPQ_KEYS}


def _libpq(url, tmp_path):
    # What schema_manager.get_managed_db_connection and the checkpointer's
    # DATABASE_URL path hand to psycopg, parsed by libpq.
    return conninfo_to_dict(enforce_db_tls_conninfo(url))


def _django(url, tmp_path):
    # settings.DATABASES["default"] (env.db) as psycopg receives it, which is also
    # what data_operation._connection_params passes to psycopg.connect.
    db = enforce_django_db_tls(environ.Env.db_url_config(url))
    wrapper = ConnectionHandler({"default": db})["default"]
    try:
        params = wrapper.get_connection_params()
    except ImproperlyConfigured as exc:
        assert "supply the NAME" in str(exc)
        return "ImproperlyConfigured"
    return {key: str(value) for key, value in params.items() if key in _LIBPQ_KEYS and value}


BUILDERS = {"mcp": _mcp, "dbt": _dbt, "libpq": _libpq, "django": _django}

# The deploy URL shape (scripts/resolve-database-url.sh): encoded password, no sslmode.
ENCODED = "postgresql://plat%40form:p%40ss%2Fw%3Ard%25x%2B@db.example:6543/scout"
SSLMODE_NO_PORT = "postgresql://u:p@db.example/scout?sslmode=require"
QUERY_OPTIONS = (
    "postgresql://u:p@db.example:5432/scout?sslmode=verify-full&connect_timeout=5"
    "&application_name=scout-x&options=-c%20statement_timeout%3D5000"
)
MINIMAL = "postgresql://localhost/scout"
NO_DBNAME = "postgresql://u:p@db.example:5432/"
# An unencoded "+" stays literal on every path (it is not form-encoding for a space).
RAW_PLUS = "postgresql://u:p+q@db.example:5432/scout"
# A remote URL cannot opt out of certificate verification or swap the CA.
DOWNGRADE = (
    "postgresql://u:p@db.example:5432/scout?sslmode=disable&sslrootcert=/elsewhere/other.pem"
)
LOCAL_DISABLE = "postgresql://u:p@platform-db:5432/scout?sslmode=disable"

_TLS = {"sslmode": "verify-full", "sslrootcert": str(DEFAULT_DB_SSL_ROOT_CERT)}

_ENCODED_IDENTITY = {
    "host": "db.example",
    "port": "6543",
    "dbname": "scout",
    "user": "plat@form",
    "password": "p@ss/w:rd%x+",
}
_UP = {"host": "db.example", "dbname": "scout", "user": "u", "password": "p"}
_QUERY_OPTIONS_ALL = {
    **_UP,
    "port": "5432",
    **_TLS,
    "connect_timeout": "5",
    "application_name": "scout-x",
    "options": "-c statement_timeout=5000",
}

_UP_PORT = {**_UP, "port": "5432"}
_LOCAL_DISABLE = {
    "host": "platform-db",
    "port": "5432",
    "dbname": "scout",
    "user": "u",
    "password": "p",
    "sslmode": "disable",
}

EXPECTED = {
    ENCODED: {builder: {**_ENCODED_IDENTITY, **_TLS} for builder in BUILDERS},
    SSLMODE_NO_PORT: {
        "mcp": {**_UP_PORT, **_TLS},
        "dbt": {**_UP_PORT, **_TLS},
        "libpq": {**_UP, **_TLS},
        "django": {**_UP, **_TLS},
    },
    QUERY_OPTIONS: {
        # The MCP and dbt builders keep only the connection identity and TLS settings.
        "mcp": {**_UP_PORT, **_TLS},
        "dbt": {**_UP_PORT, **_TLS},
        "libpq": _QUERY_OPTIONS_ALL,
        "django": _QUERY_OPTIONS_ALL,
    },
    MINIMAL: {
        "mcp": {
            "host": "localhost",
            "port": "5432",
            "dbname": "scout",
            "user": "",
            "password": "",
            "sslmode": "prefer",
        },
        "dbt": {"host": "localhost", "port": "5432", "dbname": "scout", "user": "", "password": ""},
        "libpq": {"host": "localhost", "dbname": "scout"},
        "django": {"host": "localhost", "dbname": "scout"},
    },
    NO_DBNAME: {
        # Divergence: MCP connects to "scout", while dbt and the raw-URL paths leave
        # libpq to fall back to the user name, and Django refuses to start.
        "mcp": {**_UP_PORT, "dbname": "scout", **_TLS},
        "dbt": {**_UP_PORT, "dbname": "", **_TLS},
        "libpq": {"host": "db.example", "port": "5432", "user": "u", "password": "p", **_TLS},
        "django": "ImproperlyConfigured",
    },
    RAW_PLUS: {builder: {**_UP_PORT, "password": "p+q", **_TLS} for builder in BUILDERS},
    DOWNGRADE: {builder: {**_UP_PORT, **_TLS} for builder in BUILDERS},
    LOCAL_DISABLE: {builder: _LOCAL_DISABLE for builder in BUILDERS},
}


@pytest.fixture(autouse=True)
def _default_root_cert(monkeypatch):
    monkeypatch.delenv(DB_SSL_ROOT_CERT_ENV, raising=False)


@pytest.mark.parametrize("builder", sorted(BUILDERS))
@pytest.mark.parametrize(
    "url",
    [
        ENCODED,
        SSLMODE_NO_PORT,
        QUERY_OPTIONS,
        MINIMAL,
        NO_DBNAME,
        RAW_PLUS,
        DOWNGRADE,
        LOCAL_DISABLE,
    ],
    ids=[
        "encoded",
        "sslmode-no-port",
        "query-options",
        "minimal",
        "no-dbname",
        "raw-plus",
        "downgrade",
        "local-disable",
    ],
)
def test_builder_output(url, builder, tmp_path):
    assert BUILDERS[builder](url, tmp_path) == EXPECTED[url][builder]


def test_malformed_url_error_does_not_echo_the_password():
    with pytest.raises(ValueError) as excinfo:
        parse_pg_url("postgresql://u:s3cret%zz@db.example/scout")
    assert "s3cret" not in str(excinfo.value)


def test_built_url_round_trips_special_characters():
    # development.py derives MANAGED_DATABASE_URL from DATABASES with this.
    url = build_pg_url(
        host="localhost", port=5432, dbname="scout", user="plat@form", password="p@ss/w:?#%"
    )
    assert pg_connection_identity(url) == {
        "host": "localhost",
        "port": 5432,
        "dbname": "scout",
        "user": "plat@form",
        "password": "p@ss/w:?#%",
    }


@pytest.mark.parametrize("host", ["/var/run/postgresql", "::1", "[::1]"])
def test_built_url_round_trips_socket_and_ipv6_hosts(host):
    url = build_pg_url(host=host, port=5432, dbname="scout", user="u")
    identity = pg_connection_identity(url)
    expected_host = host.strip("[]")
    assert (identity["host"], identity["port"], identity["dbname"]) == (
        expected_host,
        5432,
        "scout",
    )


@pytest.mark.parametrize("port", ["5432,5433", "notaport"])
def test_unusable_port_raises_the_sanitised_error(port):
    with pytest.raises(ValueError, match="^Invalid Postgres connection URL$"):
        pg_connection_identity(f"host=h port={port} dbname=scout")
