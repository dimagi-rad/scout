"""
Tests for mcp_server.services.semantic — mint_cube_jwt, semantic_query, semantic_catalog.

All tests are deterministic: no live Cube, no live database.
- mint_cube_jwt: round-trip JWT encode/decode.
- semantic_query: patched load_workspace_context + patched psycopg connection.
- semantic_catalog: patched load_workspace_context + patched httpx client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from mcp_server.services.semantic import mint_cube_jwt, semantic_catalog, semantic_query

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-cube-secret"
_WORKSPACE_ID = "00000000-0000-0000-0000-000000000042"
_SCHEMA_NAME = "t_abc"

# Fake QueryContext with only the attributes semantic.py touches.
_FAKE_CTX = SimpleNamespace(schema_name=_SCHEMA_NAME)


def _fake_load_workspace_context(workspace_id: str):
    """Async mock that returns the fake ctx for any workspace_id."""
    return _FAKE_CTX


# ---------------------------------------------------------------------------
# mint_cube_jwt
# ---------------------------------------------------------------------------


class TestMintCubeJwt:
    """mint_cube_jwt produces a valid, decodable JWT with the right claims."""

    def test_round_trip_claims(self):
        """Decoded token carries exactly workspace_id and schema_name."""
        with patch("mcp_server.services.semantic.settings") as mock_settings:
            mock_settings.CUBEJS_API_SECRET = _SECRET
            token = mint_cube_jwt(_WORKSPACE_ID, _SCHEMA_NAME)

        decoded = jwt.decode(token, _SECRET, algorithms=["HS256"])
        assert decoded["workspace_id"] == _WORKSPACE_ID
        assert decoded["schema_name"] == _SCHEMA_NAME
        assert "exp" in decoded

    def test_exp_is_in_the_future(self):
        """Token expiry is roughly TTL seconds from now."""
        with patch("mcp_server.services.semantic.settings") as mock_settings:
            mock_settings.CUBEJS_API_SECRET = _SECRET
            before = datetime.now(tz=UTC)
            token = mint_cube_jwt(_WORKSPACE_ID, _SCHEMA_NAME, ttl_seconds=300)
            after = datetime.now(tz=UTC)

        decoded = jwt.decode(token, _SECRET, algorithms=["HS256"])
        exp_dt = datetime.fromtimestamp(decoded["exp"], tz=UTC)
        assert exp_dt > before + timedelta(seconds=299)
        assert exp_dt < after + timedelta(seconds=301)

    def test_custom_ttl(self):
        """Passing ttl_seconds=60 yields an exp ~60 s from now."""
        with patch("mcp_server.services.semantic.settings") as mock_settings:
            mock_settings.CUBEJS_API_SECRET = _SECRET
            before = datetime.now(tz=UTC)
            token = mint_cube_jwt(_WORKSPACE_ID, _SCHEMA_NAME, ttl_seconds=60)

        decoded = jwt.decode(token, _SECRET, algorithms=["HS256"])
        exp_dt = datetime.fromtimestamp(decoded["exp"], tz=UTC)
        assert exp_dt > before + timedelta(seconds=59)
        assert exp_dt < before + timedelta(seconds=62)

    def test_wrong_secret_raises(self):
        """Decoding with the wrong secret raises InvalidSignatureError."""
        with patch("mcp_server.services.semantic.settings") as mock_settings:
            mock_settings.CUBEJS_API_SECRET = _SECRET
            token = mint_cube_jwt(_WORKSPACE_ID, _SCHEMA_NAME)

        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, "wrong-secret", algorithms=["HS256"])


# ---------------------------------------------------------------------------
# semantic_query
# ---------------------------------------------------------------------------


class TestSemanticQuery:
    """semantic_query resolves schema, connects to Cube with JWT, returns envelope."""

    @pytest.mark.asyncio
    async def test_resolves_schema_and_connects_with_jwt(self):
        """semantic_query calls load_workspace_context, mints a JWT, passes it as password."""
        sql = "SELECT MEASURE(Orders.count) FROM Orders"
        fake_columns = ["count"]

        # Fake async cursor
        fake_cursor = MagicMock()
        fake_cursor.__aenter__ = AsyncMock(return_value=fake_cursor)
        fake_cursor.__aexit__ = AsyncMock(return_value=False)
        fake_cursor.execute = AsyncMock()
        fake_cursor.description = [("count",)]
        fake_cursor.fetchall = AsyncMock(return_value=[(42,)])

        # Fake async connection that yields the cursor
        fake_conn = MagicMock()
        fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
        fake_conn.__aexit__ = AsyncMock(return_value=False)
        fake_conn.cursor = MagicMock(return_value=fake_cursor)

        connect_mock = AsyncMock(return_value=fake_conn)

        with (
            patch(
                "mcp_server.services.semantic.load_workspace_context",
                new=AsyncMock(return_value=_FAKE_CTX),
            ),
            patch("mcp_server.services.semantic.settings") as mock_settings,
            patch("psycopg.AsyncConnection.connect", connect_mock),
        ):
            mock_settings.CUBEJS_API_SECRET = _SECRET
            mock_settings.CUBE_SQL_HOST = "localhost"
            mock_settings.CUBE_SQL_PORT = 15432

            result = await semantic_query(sql, _WORKSPACE_ID)

        # Assert schema was resolved
        assert result["columns"] == fake_columns
        assert result["rows"] == [[42]]
        assert result["row_count"] == 1
        assert result["sql_executed"] == sql

        # Assert the connection was made with the JWT as the password
        _, connect_kwargs = connect_mock.call_args
        password = connect_kwargs["password"]
        decoded = jwt.decode(password, _SECRET, algorithms=["HS256"])
        assert decoded["workspace_id"] == _WORKSPACE_ID
        assert decoded["schema_name"] == _SCHEMA_NAME

    @pytest.mark.asyncio
    async def test_raises_value_error_when_workspace_id_empty(self):
        """semantic_query raises ValueError when workspace_id is empty."""
        with pytest.raises(ValueError, match="workspace_id is required"):
            await semantic_query("SELECT 1", "")

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_description(self):
        """semantic_query returns empty columns/rows when cursor has no description."""
        fake_cursor = MagicMock()
        fake_cursor.__aenter__ = AsyncMock(return_value=fake_cursor)
        fake_cursor.__aexit__ = AsyncMock(return_value=False)
        fake_cursor.execute = AsyncMock()
        fake_cursor.description = None
        fake_cursor.fetchall = AsyncMock(return_value=[])

        fake_conn = MagicMock()
        fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
        fake_conn.__aexit__ = AsyncMock(return_value=False)
        fake_conn.cursor = MagicMock(return_value=fake_cursor)

        connect_mock = AsyncMock(return_value=fake_conn)

        with (
            patch(
                "mcp_server.services.semantic.load_workspace_context",
                new=AsyncMock(return_value=_FAKE_CTX),
            ),
            patch("mcp_server.services.semantic.settings") as mock_settings,
            patch("psycopg.AsyncConnection.connect", connect_mock),
        ):
            mock_settings.CUBEJS_API_SECRET = _SECRET
            mock_settings.CUBE_SQL_HOST = "localhost"
            mock_settings.CUBE_SQL_PORT = 15432

            result = await semantic_query("SELECT 1", _WORKSPACE_ID)

        assert result["columns"] == []
        assert result["rows"] == []
        assert result["row_count"] == 0


# ---------------------------------------------------------------------------
# semantic_catalog
# ---------------------------------------------------------------------------


_META_RESPONSE = {
    "cubes": [
        {
            "name": "Orders",
            "title": "Orders",
            "measures": [
                {"name": "Orders.count", "title": "Orders Count", "type": "count"},
                {"name": "Orders.totalAmount", "title": "Orders Total Amount", "type": "sum"},
            ],
            "dimensions": [
                {"name": "Orders.status", "title": "Orders Status", "type": "string"},
                {"name": "Orders.createdAt", "title": "Orders Created At", "type": "time"},
            ],
        },
        {
            "name": "LineItems",
            "title": "Line Items",
            "measures": [
                {"name": "LineItems.count", "title": "Line Items Count", "type": "count"},
            ],
            "dimensions": [],
        },
    ]
}


class TestSemanticCatalog:
    """semantic_catalog fetches /v1/meta with Bearer JWT, returns compact dict."""

    @pytest.mark.asyncio
    async def test_returns_cubes_with_measures_and_dimensions(self, httpx_mock):
        """semantic_catalog returns structured cubes list from /v1/meta."""
        httpx_mock.add_response(
            url="http://localhost:4000/v1/meta",
            json=_META_RESPONSE,
            status_code=200,
        )

        with (
            patch(
                "mcp_server.services.semantic.load_workspace_context",
                new=AsyncMock(return_value=_FAKE_CTX),
            ),
            patch("mcp_server.services.semantic.settings") as mock_settings,
        ):
            mock_settings.CUBEJS_API_SECRET = _SECRET
            mock_settings.CUBE_REST_URL = "http://localhost:4000"

            result = await semantic_catalog(_WORKSPACE_ID)

        assert "cubes" in result
        assert len(result["cubes"]) == 2

        orders = result["cubes"][0]
        assert orders["name"] == "Orders"
        assert len(orders["measures"]) == 2
        assert orders["measures"][0]["name"] == "Orders.count"
        assert len(orders["dimensions"]) == 2
        assert orders["dimensions"][1]["name"] == "Orders.createdAt"

        line_items = result["cubes"][1]
        assert line_items["name"] == "LineItems"
        assert len(line_items["measures"]) == 1
        assert line_items["dimensions"] == []

    @pytest.mark.asyncio
    async def test_sends_authorization_bearer_header(self, httpx_mock):
        """semantic_catalog sends Authorization: Bearer <JWT> to /v1/meta."""
        httpx_mock.add_response(
            url="http://localhost:4000/v1/meta",
            json={"cubes": []},
            status_code=200,
        )

        with (
            patch(
                "mcp_server.services.semantic.load_workspace_context",
                new=AsyncMock(return_value=_FAKE_CTX),
            ),
            patch("mcp_server.services.semantic.settings") as mock_settings,
        ):
            mock_settings.CUBEJS_API_SECRET = _SECRET
            mock_settings.CUBE_REST_URL = "http://localhost:4000"

            await semantic_catalog(_WORKSPACE_ID)

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        auth_header = requests[0].headers.get("authorization", "")
        assert auth_header.startswith("Bearer ")

        token = auth_header.split(" ", 1)[1]
        decoded = jwt.decode(token, _SECRET, algorithms=["HS256"])
        assert decoded["workspace_id"] == _WORKSPACE_ID
        assert decoded["schema_name"] == _SCHEMA_NAME

    @pytest.mark.asyncio
    async def test_raises_value_error_when_workspace_id_empty(self):
        """semantic_catalog raises ValueError when workspace_id is empty."""
        with pytest.raises(ValueError, match="workspace_id is required"):
            await semantic_catalog("")

    @pytest.mark.asyncio
    async def test_empty_cubes_response(self, httpx_mock):
        """semantic_catalog handles an empty cubes list gracefully."""
        httpx_mock.add_response(
            url="http://localhost:4000/v1/meta",
            json={"cubes": []},
            status_code=200,
        )

        with (
            patch(
                "mcp_server.services.semantic.load_workspace_context",
                new=AsyncMock(return_value=_FAKE_CTX),
            ),
            patch("mcp_server.services.semantic.settings") as mock_settings,
        ):
            mock_settings.CUBEJS_API_SECRET = _SECRET
            mock_settings.CUBE_REST_URL = "http://localhost:4000"

            result = await semantic_catalog(_WORKSPACE_ID)

        assert result == {"cubes": []}
