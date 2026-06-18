"""Integration test: discover phase persists form_definitions for commcare_connect."""

from unittest import mock

import pytest

from mcp_server.pipeline_registry import PipelineRegistry
from mcp_server.services import materializer


@pytest.mark.django_db(transaction=True)
def test_discover_persists_form_definitions(connect_tenant_membership):
    pipeline = PipelineRegistry().get_by_provider("commcare_connect")
    fake = {
        "opportunity": {"name": "Demo"},
        "organizations": [],
        "programs": [],
        "all_opportunities": [],
        "form_definitions": {"muac_visit": {"questions": []}},
    }
    with mock.patch.object(materializer.ConnectMetadataLoader, "load", return_value=fake):
        result = materializer._run_discover_phase(
            connect_tenant_membership, {"type": "api_key", "value": "t"}, pipeline
        )
    from apps.workspaces.models import TenantMetadata

    tm = TenantMetadata.objects.get(tenant_membership=connect_tenant_membership)
    assert "muac_visit" in tm.metadata["form_definitions"]
    assert result["form_definitions"]["muac_visit"] == {"questions": []}
