"""Truthful pipeline resolution (#155, arch 01#9).

Every read surface used to end its resolution chain with
``registry.get("commcare_sync")``, serving an OCS or Connect tenant the wrong
provider's table names and descriptions with nothing logged. These tests pin the
replacement behaviour per surface: resolve, or say so — never guess.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from rest_framework.test import APIClient

from apps.common.error_codes import ErrorCode
from apps.common.errors import ExpectedStateError
from apps.semantic.models import SemanticModel
from apps.semantic.services import catalog as catalog_service
from apps.semantic.services.catalog import SemanticCatalogUnavailable, ensure_semantic_model
from apps.workspaces.models import SchemaState, TenantSchema
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    select_pipeline_config,
)
from mcp_server import server as mcp_server_module
from mcp_server.context import QueryContext

UNKNOWN_PROVIDER = "mystery_provider"


@pytest.fixture
def unresolvable_tenant_schema(db, tenant):
    """A tenant whose provider has no pipeline YAML, with a live schema."""
    tenant.provider = UNKNOWN_PROVIDER
    tenant.save(update_fields=["provider"])
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="t_mystery",
        state=SchemaState.ACTIVE,
    )


class TestSelectPipelineConfig:
    def test_prefers_the_last_run_pipeline(self):
        config = select_pipeline_config(last_run_pipeline="ocs_sync", provider="commcare")
        assert config.name == "ocs_sync"

    def test_falls_back_to_the_provider_pipeline(self):
        config = select_pipeline_config(last_run_pipeline="retired_pipeline", provider="ocs")
        assert config.name == "ocs_sync"

    def test_unknown_provider_does_not_resolve_to_commcare_sync(self):
        with pytest.raises(PipelineResolutionError) as exc:
            select_pipeline_config(provider=UNKNOWN_PROVIDER)
        assert UNKNOWN_PROVIDER in str(exc.value)

    def test_raises_when_there_is_nothing_to_resolve_from(self):
        with pytest.raises(PipelineResolutionError):
            select_pipeline_config()

    def test_error_carries_a_stable_code(self):
        assert PipelineResolutionError.code == ErrorCode.PIPELINE_UNRESOLVED

    def test_is_not_an_expected_state(self):
        """A missing pipeline for a supported provider is a deploy defect: it must
        keep reaching Sentry rather than being filtered as routine."""
        assert not issubclass(PipelineResolutionError, ExpectedStateError)


class TestSemanticModelBuild:
    """A wrong pipeline here is cached and promoted, so the build must fail."""

    @pytest.mark.django_db(transaction=True)
    def test_unresolvable_pipeline_never_reads_commcare_sync_tables(
        self, monkeypatch, workspace, unresolvable_tenant_schema
    ):
        monkeypatch.setattr(
            catalog_service,
            "load_workspace_context",
            AsyncMock(
                return_value=QueryContext(
                    tenant_id=str(unresolvable_tenant_schema.tenant_id),
                    schema_name=unresolvable_tenant_schema.schema_name,
                    connection_params={},
                )
            ),
        )
        listed_with = []

        async def spy_pipeline_list_tables(ts, pipeline_config):
            listed_with.append(pipeline_config)
            return [{"name": "raw_cases", "type": "table"}]

        monkeypatch.setattr(catalog_service, "pipeline_list_tables", spy_pipeline_list_tables)

        with pytest.raises(SemanticCatalogUnavailable) as exc:
            ensure_semantic_model(workspace)

        assert listed_with == [], (
            "resolution failed, so no pipeline may be read from — "
            f"got {[getattr(c, 'name', c) for c in listed_with]}"
        )
        assert UNKNOWN_PROVIDER in str(exc.value)
        assert exc.value.schema_status == "failed"
        assert not SemanticModel.objects.filter(workspace=workspace).exists()


@pytest.mark.django_db(transaction=True)
class TestMcpMetadataTools:
    """The agent must be told resolution failed, not handed another provider's tables."""

    def _patch_context(self, monkeypatch, schema_name):
        monkeypatch.setattr(
            mcp_server_module,
            "load_workspace_context",
            AsyncMock(
                return_value=QueryContext(
                    tenant_id="whoever",
                    schema_name=schema_name,
                    connection_params={},
                )
            ),
        )

    @pytest.mark.asyncio
    async def test_list_tables_reports_the_failure_instead_of_commcare_tables(
        self, monkeypatch, unresolvable_tenant_schema
    ):
        self._patch_context(monkeypatch, unresolvable_tenant_schema.schema_name)
        listed_with = []

        async def spy_pipeline_list_tables(ts, pipeline_config):
            listed_with.append(pipeline_config)
            return []

        monkeypatch.setattr(mcp_server_module, "pipeline_list_tables", spy_pipeline_list_tables)

        result = await mcp_server_module.list_tables(workspace_id=str(uuid.uuid4()))

        assert result["success"] is False
        assert result["error"]["code"] == ErrorCode.PIPELINE_UNRESOLVED
        assert UNKNOWN_PROVIDER in result["error"]["message"]
        assert listed_with == []

    @pytest.mark.asyncio
    async def test_describe_table_on_a_view_schema_passes_no_pipeline(self, monkeypatch):
        """A ws_* view schema has no tenant, so None is the truthful answer — not
        commcare_sync's source descriptions."""
        self._patch_context(monkeypatch, "ws_multi")
        described_with = []

        async def spy_describe(table_name, ctx, tenant_metadata, pipeline_config):
            described_with.append(pipeline_config)
            return {"name": table_name, "description": "", "columns": []}

        monkeypatch.setattr(mcp_server_module, "pipeline_describe_table", spy_describe)

        result = await mcp_server_module.describe_table(
            "raw_visits", workspace_id=str(uuid.uuid4())
        )

        assert result["success"] is True
        assert described_with == [None]


@pytest.mark.django_db
class TestDataDictionaryViews:
    """A degraded read, reported truthfully — not a 500 and not a wrong catalog."""

    @pytest.fixture
    def auth_client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_data_dictionary_reports_the_unresolved_pipeline(
        self, auth_client, workspace, unresolvable_tenant_schema
    ):
        resp = auth_client.get(f"/api/workspaces/{workspace.id}/data-dictionary/")

        assert resp.status_code == 503
        assert resp.data["code"] == ErrorCode.PIPELINE_UNRESOLVED
        assert UNKNOWN_PROVIDER in resp.data["error"]
        assert "tables" not in resp.data

    def test_table_detail_reports_the_unresolved_pipeline_not_a_404(
        self, auth_client, workspace, unresolvable_tenant_schema
    ):
        """404 "Table not found" would be a lie about a table Scout cannot describe."""
        qualified_name = f"{unresolvable_tenant_schema.schema_name}.raw_cases"
        resp = auth_client.get(
            f"/api/workspaces/{workspace.id}/data-dictionary/tables/{qualified_name}/"
        )

        assert resp.status_code == 503
        assert resp.data["code"] == ErrorCode.PIPELINE_UNRESOLVED
