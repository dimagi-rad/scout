"""Tests for Connect staging upsert + column-note wiring (Task 5).

Covers:
1. upsert_connect_assets — creates a stg_visits TransformationAsset, re-runs
   update rather than duplicate, and sweeps orphaned SYSTEM assets.
2. Column-note wiring — after upsert + loop, a workspace linked to the tenant
   has a TableKnowledge row with column_notes populated from form_definitions.
"""

from __future__ import annotations

import django.contrib.auth
import pytest
from asgiref.sync import async_to_sync

from apps.knowledge.models import TableKnowledge
from apps.knowledge.services.column_note_generator import sync_column_notes
from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.connect_staging import upsert_connect_assets
from apps.users.models import Tenant
from apps.workspaces.models import Workspace, WorkspaceTenant

FORM_DEFS = {
    "muac_visit": {
        "name": "MUAC Visit",
        "deliver_unit": "muac_visit",
        "questions": [
            {
                "label": "MUAC (cm)",
                "value": "/data/muac_group/muac",
                "type": "Decimal",
                "repeat": False,
                "options": None,
            },
            {
                "label": "Status",
                "value": "/data/status",
                "type": "Select",
                "repeat": False,
                "options": ["green", "yellow", "red"],
            },
        ],
    }
}


class _FakeTenantMeta:
    """Minimal stand-in for TenantMetadata.metadata container."""

    def __init__(self, form_definitions: dict):
        self.metadata = {"form_definitions": form_definitions}


@pytest.fixture
def connect_tenant(db):
    """Create a Tenant with provider commcare_connect."""
    return Tenant.objects.create(
        provider="commcare_connect",
        external_id="99001",
        canonical_name="Connect Opp 99001",
    )


@pytest.fixture
def connect_workspace(db, connect_tenant):
    """Create a Workspace linked to connect_tenant via WorkspaceTenant."""
    User = django.contrib.auth.get_user_model()
    user = User.objects.create_user(email="wiring@example.com", password="pass")
    ws = Workspace.objects.create(name="Wiring Test WS", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=connect_tenant)
    return ws


# ── upsert_connect_assets ─────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
def test_upsert_connect_assets_creates_stg_visits(connect_tenant):
    """upsert_connect_assets persists a SYSTEM stg_visits asset for the tenant."""
    tenant_meta = _FakeTenantMeta(FORM_DEFS)
    result = upsert_connect_assets(connect_tenant, tenant_meta)

    assert result["created"] == 1
    assert result["total"] >= 1

    asset = TransformationAsset.objects.get(
        name="stg_visits",
        scope=TransformationScope.SYSTEM,
        tenant=connect_tenant,
    )
    assert "form_json" in asset.sql_content
    assert "muac" in asset.sql_content


@pytest.mark.django_db(transaction=True)
def test_upsert_connect_assets_updates_not_duplicates(connect_tenant):
    """Running upsert_connect_assets twice updates, not re-creates."""
    tenant_meta = _FakeTenantMeta(FORM_DEFS)
    r1 = upsert_connect_assets(connect_tenant, tenant_meta)
    r2 = upsert_connect_assets(connect_tenant, tenant_meta)

    assert r1["created"] >= 1
    # Second run: all updates, no new creates
    assert r2["created"] == 0
    assert r2["updated"] >= 1

    # Exactly one stg_visits asset per tenant
    count = TransformationAsset.objects.filter(
        name="stg_visits",
        scope=TransformationScope.SYSTEM,
        tenant=connect_tenant,
    ).count()
    assert count == 1


@pytest.mark.django_db(transaction=True)
def test_upsert_connect_assets_sweeps_orphaned_assets(connect_tenant):
    """Assets no longer generated from current metadata are deleted."""
    # First upsert with a form that creates stg_visits
    tenant_meta = _FakeTenantMeta(FORM_DEFS)
    upsert_connect_assets(connect_tenant, tenant_meta)

    # Manually plant an orphan SYSTEM asset for this tenant
    TransformationAsset.objects.create(
        name="stg_visits__repeat_old_group",
        description="Orphaned repeat group",
        scope=TransformationScope.SYSTEM,
        tenant=connect_tenant,
        sql_content="SELECT 1",
        created_by=None,
    )
    assert TransformationAsset.objects.filter(
        name="stg_visits__repeat_old_group",
        scope=TransformationScope.SYSTEM,
        tenant=connect_tenant,
    ).exists()

    # Re-upsert with same (no-repeat) form_defs — orphan should be swept
    result = upsert_connect_assets(connect_tenant, tenant_meta)
    assert result["deleted"] == 1
    assert not TransformationAsset.objects.filter(
        name="stg_visits__repeat_old_group",
        scope=TransformationScope.SYSTEM,
        tenant=connect_tenant,
    ).exists()


# ── Column-note wiring loop ───────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
def test_column_note_wiring_loop_populates_table_knowledge(connect_tenant, connect_workspace):
    """The documented per-workspace loop creates a TableKnowledge row with column_notes."""
    tenant_meta = _FakeTenantMeta(FORM_DEFS)

    # Run the asset upsert
    upsert_connect_assets(connect_tenant, tenant_meta)

    # Run the column-note loop exactly as documented in the dispatch
    form_defs = (tenant_meta.metadata or {}).get("form_definitions", {})
    for ws in connect_tenant.workspaces.all():
        async_to_sync(sync_column_notes)(ws, "stg_visits", form_defs)

    # Assert TableKnowledge exists for the workspace.
    # "/data/status" collides with the base column "status" so visit_column_map
    # dedupes it to "status_2" — note key must match the actual staging column name.
    tk = TableKnowledge.objects.get(workspace=connect_workspace, table_name="stg_visits")
    assert "muac" in tk.column_notes
    assert "Decimal" in tk.column_notes["muac"]
    assert "status_2" in tk.column_notes
    assert "green" in tk.column_notes["status_2"]


@pytest.mark.django_db(transaction=True)
def test_column_note_wiring_no_workspace_is_noop(connect_tenant):
    """If the tenant has no workspaces, the loop runs zero iterations without error."""
    tenant_meta = _FakeTenantMeta(FORM_DEFS)
    upsert_connect_assets(connect_tenant, tenant_meta)

    form_defs = (tenant_meta.metadata or {}).get("form_definitions", {})
    count = 0
    for ws in connect_tenant.workspaces.all():
        async_to_sync(sync_column_notes)(ws, "stg_visits", form_defs)
        count += 1

    # connect_tenant has no workspace in this test — loop body never runs
    assert count == 0
    assert not TableKnowledge.objects.filter(table_name="stg_visits").exists()
