"""Unit tests for mcp_server.context value objects (no DB, no async)."""

from apps.common.identifiers import readonly_role_name
from mcp_server.context import QueryContext


class TestQueryContextReadonlyRole:
    """The readonly_role property must route through the shared helper so an
    overflow schema name yields a <=63-byte role (arch #235)."""

    def test_short_schema_role_is_verbatim(self):
        ctx = QueryContext(tenant_id="t", schema_name="t_123", connection_params={})
        assert ctx.readonly_role == "t_123_ro"
        assert ctx.readonly_role == readonly_role_name("t_123")

    def test_overflow_schema_role_is_bounded(self):
        long_schema = readonly_role_name("a" * 80)  # already a fitted, <=63-byte name
        ctx = QueryContext(tenant_id="t", schema_name=long_schema, connection_params={})
        assert len(ctx.readonly_role.encode("utf-8")) <= 63
        assert ctx.readonly_role == readonly_role_name(long_schema)
