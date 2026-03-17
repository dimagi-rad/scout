from mcp_server.context import QueryContext


class TestQueryContextReadonlyRole:
    def test_readonly_role_derived_from_schema_name(self):
        ctx = QueryContext(
            tenant_id="test-domain",
            schema_name="test_domain",
            connection_params={"host": "localhost"},
        )
        assert ctx.readonly_role == "test_domain_ro"

    def test_readonly_role_view_schema(self):
        ctx = QueryContext(
            tenant_id="workspace-123",
            schema_name="ws_abc1234def56789",
            connection_params={"host": "localhost"},
        )
        assert ctx.readonly_role == "ws_abc1234def56789_ro"
