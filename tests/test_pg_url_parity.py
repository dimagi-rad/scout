"""Parity of every Python builder that turns a Postgres URL into connection settings.

Each builder is run over the same URLs and its output normalised to libpq keys
with string values, so a divergence between builders shows up as a row here.
"""

import environ
import pytest
import yaml
from django.db.utils import ConnectionHandler
from psycopg.conninfo import conninfo_to_dict

from apps.common.db_urls import build_pg_url, parse_pg_url, pg_connection_identity
from mcp_server.context import _parse_db_url
from mcp_server.services.dbt_runner import generate_profiles_yml

_LIBPQ_KEYS = {
    "host",
    "port",
    "dbname",
    "user",
    "password",
    "sslmode",
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
    # DATABASE_URL path hand to psycopg: the raw URL, parsed by libpq.
    return conninfo_to_dict(url)


def _django(url, tmp_path):
    # settings.DATABASES["default"] (env.db) as psycopg receives it, which is also
    # what data_operation._connection_params passes to psycopg.connect.
    wrapper = ConnectionHandler({"default": environ.Env.db_url_config(url)})["default"]
    params = wrapper.get_connection_params()
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
    "sslmode": "verify-full",
    "connect_timeout": "5",
    "application_name": "scout-x",
    "options": "-c statement_timeout=5000",
}

EXPECTED = {
    ENCODED: {
        "mcp": {**_ENCODED_IDENTITY, "sslmode": "prefer"},
        "dbt": _ENCODED_IDENTITY,
        "libpq": _ENCODED_IDENTITY,
        "django": _ENCODED_IDENTITY,
    },
    SSLMODE_NO_PORT: {
        "mcp": {**_UP, "port": "5432", "sslmode": "require"},
        "dbt": {**_UP, "port": "5432", "sslmode": "require"},
        "libpq": {**_UP, "sslmode": "require"},
        "django": {**_UP, "sslmode": "require"},
    },
    QUERY_OPTIONS: {
        # The MCP and dbt builders keep only the connection identity and sslmode.
        "mcp": {**_UP, "port": "5432", "sslmode": "verify-full"},
        "dbt": {**_UP, "port": "5432", "sslmode": "verify-full"},
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
}


@pytest.mark.parametrize("builder", sorted(BUILDERS))
@pytest.mark.parametrize(
    "url",
    [ENCODED, SSLMODE_NO_PORT, QUERY_OPTIONS, MINIMAL],
    ids=["encoded", "sslmode-no-port", "query-options", "minimal"],
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
        host="db", port=5432, dbname="scout", user="plat@form", password="p@ss/w:?#%"
    )
    assert pg_connection_identity(url) == {
        "host": "db",
        "port": 5432,
        "dbname": "scout",
        "user": "plat@form",
        "password": "p@ss/w:?#%",
    }
