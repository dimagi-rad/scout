"""List metadata is not a semantic-readiness check (no database required)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.semantic.models import CubeSchema, SemanticModel
from apps.workspaces.api.workspace_views import _schema_status_for_workspaces
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema
from apps.workspaces.services.query_state import semantic_layer_state


@pytest.mark.asyncio
@pytest.mark.parametrize("has_model", [False, True], ids=["no-model", "no-active-cube"])
async def test_available_load_metadata_does_not_certify_semantic_readiness(monkeypatch, has_model):
    tenant_id = "source-with-recorded-active-schema"
    workspace = SimpleNamespace(
        id="workspace-with-recorded-load",
        workspace_tenants=SimpleNamespace(all=lambda: [SimpleNamespace(tenant_id=tenant_id)]),
    )
    monkeypatch.setattr(
        TenantSchema.objects,
        "filter",
        Mock(
            return_value=SimpleNamespace(
                values_list=lambda *args: [(tenant_id, SchemaState.ACTIVE)]
            )
        ),
    )
    monkeypatch.setattr(
        WorkspaceViewSchema.objects,
        "filter",
        Mock(return_value=SimpleNamespace(values_list=lambda *args: [])),
    )
    model = SimpleNamespace(status=SemanticModel.Status.ACTIVE, metadata={}) if has_model else None
    monkeypatch.setattr(
        SemanticModel.objects,
        "filter",
        Mock(return_value=SimpleNamespace(afirst=AsyncMock(return_value=model))),
    )
    monkeypatch.setattr(
        CubeSchema.objects,
        "filter",
        Mock(return_value=SimpleNamespace(aexists=AsyncMock(return_value=False))),
    )

    assert _schema_status_for_workspaces([workspace]) == {workspace.id: "available"}
    state, _error = await semantic_layer_state(workspace)
    assert state == ("unavailable" if has_model else "unknown")
