"""List metadata is not a semantic-readiness check."""

import pytest
from asgiref.sync import async_to_sync

from apps.semantic.models import SemanticModel
from apps.workspaces.models import SchemaState, TenantSchema
from apps.workspaces.services.load_activity import workspace_schema_statuses
from apps.workspaces.services.query_state import semantic_layer_state


@pytest.mark.django_db
@pytest.mark.parametrize("has_model", [False, True], ids=["no-model", "no-active-cube"])
def test_available_load_metadata_does_not_certify_semantic_readiness(workspace, tenant, has_model):
    TenantSchema.objects.create(tenant=tenant, schema_name="recorded", state=SchemaState.ACTIVE)
    if has_model:
        SemanticModel.objects.create(
            workspace=workspace, name="Serving", status=SemanticModel.Status.ACTIVE
        )

    assert workspace_schema_statuses([workspace.id]) == {workspace.id: "available"}
    state, _error = async_to_sync(semantic_layer_state)(workspace)
    assert state == ("unavailable" if has_model else "unknown")
