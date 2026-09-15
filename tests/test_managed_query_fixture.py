"""The live MCP test fixture must use and clean up only its managed database objects."""

import os
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from apps.common.identifiers import readonly_role_name
from mcp_server.context import _parse_db_url
from tests import managed_query_fixture
from tests.managed_query_fixture import managed_query_context
from tests.test_ci_integrity import test_managed_database_url_set_in_ci as assert_ci_managed_db

SYNTHETIC_MANAGED_URL = (
    "postgresql://fixture%40reader:synthetic%3Apass%2Fword@managed.example:5433/fixture"
    "?sslmode=require"
)


@pytest.fixture
def mock_connection(monkeypatch):
    monkeypatch.setenv("MANAGED_DATABASE_URL", SYNTHETIC_MANAGED_URL)
    with patch("tests.managed_query_fixture.psycopg.connect") as connect:
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connect.return_value = connection
        yield connect, connection


def _statements(connection):
    return [call.args[0].as_string() for call in connection.execute.call_args_list]


@pytest.mark.parametrize("platform_url", [None, "postgresql://platform.example/never_use"])
def test_managed_dsn_is_used_for_setup_and_executor_with_encoded_credentials(
    monkeypatch, mock_connection, platform_url
):
    if platform_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", platform_url)
    connect, _connection = mock_connection

    with managed_query_context() as ctx:
        assert_ci_managed_db()
        assert ctx.connection_params == _parse_db_url(SYNTHETIC_MANAGED_URL, ctx.schema_name)
        assert ctx.connection_params["host"] == "managed.example"
        assert ctx.connection_params["dbname"] == "fixture"
        assert ctx.connection_params["user"] == "fixture@reader"
        assert ctx.connection_params["password"] == "synthetic:pass/word"
        assert ctx.connection_params["sslmode"] == "require"
        connect.assert_called_once_with(**ctx.connection_params, autocommit=True)


@pytest.mark.parametrize("managed_url", [None, ""])
def test_missing_managed_dsn_is_guarded_and_never_falls_back_to_platform(
    monkeypatch, mock_connection, managed_url
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://platform.example/never_use")
    if managed_url is None:
        monkeypatch.delenv("MANAGED_DATABASE_URL")
    else:
        monkeypatch.setenv("MANAGED_DATABASE_URL", managed_url)
    connect, _connection = mock_connection

    with pytest.raises(AssertionError, match="MANAGED_DATABASE_URL is unset"):
        assert_ci_managed_db()
    with pytest.raises(pytest.skip.Exception, match="MANAGED_DATABASE_URL not set"):
        with managed_query_context():
            pytest.fail("A platform-only DSN must not initialize a managed fixture")
    connect.assert_not_called()


def test_identifiers_are_unique_even_across_databases_on_one_cluster(monkeypatch, mock_connection):
    with managed_query_context() as first:
        monkeypatch.setenv(
            "MANAGED_DATABASE_URL", SYNTHETIC_MANAGED_URL.replace("/fixture?", "/other_fixture?")
        )
        with managed_query_context() as second:
            assert first.connection_params["host"] == second.connection_params["host"]
            assert first.connection_params["dbname"] != second.connection_params["dbname"]
            assert first.schema_name != second.schema_name
            assert first.readonly_role != second.readonly_role
            for ctx in (first, second):
                assert len(ctx.schema_name.removeprefix("test_query_exec_")) == 32
                assert len(ctx.readonly_role.encode()) <= 63


@pytest.mark.parametrize("failing_statement", ["CREATE SCHEMA", "CREATE ROLE", "GRANT SELECT"])
def test_setup_failure_rolls_back_without_cleanup_of_unowned_objects(
    mock_connection, failing_statement
):
    _connect, connection = mock_connection

    def fail_setup(statement):
        if statement.as_string().startswith(failing_statement):
            raise RuntimeError("synthetic setup failure")

    connection.execute.side_effect = fail_setup
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        with managed_query_context():
            pytest.fail("Setup must complete before yielding")

    assert not any(statement.startswith("DROP") for statement in _statements(connection))
    connection.transaction.assert_called_once()
    assert connection.transaction.return_value.__exit__.call_args.args[0] is RuntimeError


def test_test_failure_still_cleans_only_the_owned_schema_and_role(mock_connection):
    _connect, connection = mock_connection
    with pytest.raises(RuntimeError, match="synthetic test failure"):
        with managed_query_context() as ctx:
            raise RuntimeError("synthetic test failure")

    assert _statements(connection)[-2:] == [
        f'DROP SCHEMA "{ctx.schema_name}" CASCADE',
        f'DROP ROLE "{ctx.readonly_role}"',
    ]
    assert connection.transaction.call_count == 2


@pytest.fixture
def managed_connection():
    url = os.environ.get("MANAGED_DATABASE_URL")
    if not url:
        pytest.skip("MANAGED_DATABASE_URL not set")
    with psycopg.connect(**_parse_db_url(url, "public"), autocommit=True) as connection:
        yield connection


def _objects_exist(connection, schema, role):
    return connection.execute(
        "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = %s), "
        "EXISTS (SELECT FROM pg_roles WHERE rolname = %s)",
        (schema, role),
    ).fetchone()


@pytest.mark.parametrize("existing_object", ["SCHEMA", "ROLE"])
def test_real_setup_collision_rolls_back_and_preserves_existing_object(
    monkeypatch, managed_connection, existing_object
):
    identifier = uuid4()
    schema = f"test_query_exec_{identifier.hex}"
    role = readonly_role_name(schema)
    name = sql.Identifier(schema if existing_object == "SCHEMA" else role)
    managed_connection.execute(sql.SQL("CREATE {} {}").format(sql.SQL(existing_object), name))
    try:
        monkeypatch.setattr(managed_query_fixture, "uuid4", lambda: identifier)
        with pytest.raises((psycopg.errors.DuplicateSchema, psycopg.errors.DuplicateObject)):
            with managed_query_context():
                pytest.fail("Existing objects must not be adopted")
        assert _objects_exist(managed_connection, schema, role) == (
            existing_object == "SCHEMA",
            existing_object == "ROLE",
        )
    finally:
        managed_connection.execute(sql.SQL("DROP {} {}").format(sql.SQL(existing_object), name))


def test_real_failed_test_cleanup_preserves_another_live_fixture(managed_connection):
    with managed_query_context() as first:
        with pytest.raises(RuntimeError, match="synthetic test failure"):
            with managed_query_context() as second:
                assert _objects_exist(
                    managed_connection, second.schema_name, second.readonly_role
                ) == (True, True)
                raise RuntimeError("synthetic test failure")

        assert _objects_exist(managed_connection, second.schema_name, second.readonly_role) == (
            False,
            False,
        )
        assert _objects_exist(managed_connection, first.schema_name, first.readonly_role) == (
            True,
            True,
        )
        assert managed_connection.execute(
            sql.SQL("SELECT count(*) FROM {}.items").format(sql.Identifier(first.schema_name))
        ).fetchone() == (3,)

    assert _objects_exist(managed_connection, first.schema_name, first.readonly_role) == (
        False,
        False,
    )
