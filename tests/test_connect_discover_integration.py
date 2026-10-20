"""Integration test: discover phase persists form_definitions for commcare_connect."""

from unittest import mock

import pytest

from apps.workspaces.models import TenantMetadata
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
    tm = TenantMetadata.objects.get(tenant=connect_tenant_membership.tenant)
    assert "muac_visit" in tm.metadata["form_definitions"]
    assert result["form_definitions"]["muac_visit"] == {"questions": []}


@pytest.mark.django_db(transaction=True)
def test_discover_stores_the_opportunity_description(connect_tenant_membership):
    pipeline = PipelineRegistry().get_by_provider("commcare_connect")
    fake = {"opportunity": {"name": "Demo", "description": "  Nutrition visits in Kenya.  "}}
    with mock.patch.object(materializer.ConnectMetadataLoader, "load", return_value=fake):
        materializer._run_discover_phase(
            connect_tenant_membership, {"type": "api_key", "value": "t"}, pipeline
        )

    connect_tenant_membership.tenant.refresh_from_db()
    assert connect_tenant_membership.tenant.description == "Nutrition visits in Kenya."


@pytest.mark.django_db(transaction=True)
def test_discover_without_a_description_keeps_the_stored_one(connect_tenant_membership):
    tenant = connect_tenant_membership.tenant
    tenant.description = "Earlier description"
    tenant.save(update_fields=["description"])
    pipeline = PipelineRegistry().get_by_provider("commcare_connect")
    with mock.patch.object(
        materializer.ConnectMetadataLoader, "load", return_value={"opportunity": {"name": "Demo"}}
    ):
        materializer._run_discover_phase(
            connect_tenant_membership, {"type": "api_key", "value": "t"}, pipeline
        )

    tenant.refresh_from_db()
    assert tenant.description == "Earlier description"
