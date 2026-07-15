"""Phase 4 (arch #251): one TenantMetadata read scope + no wrong-provider fallback.

Change A (Decision 5): ``TenantMetadata`` is read ONE way everywhere — the
most-recently-discovered LIVE membership for the tenant — so the same tenant/table
yields identical column annotations from the agent prompt, the MCP tools, and the
DRF dictionary, including for a user who never triggered materialization. Archived
(upstream-revoked) memberships never leak annotations.

Change B (Decision 7 / #256): an unresolvable pipeline returns a truthful error
instead of silently serving commcare metadata for a non-commcare tenant; a real
commcare tenant still resolves.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.api.views import _build_source_metadata, _get_tenant_metadata
from apps.workspaces.models import (
    SchemaState,
    TenantMetadata,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.catalog import TableDescription
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    resolve_pipeline_config_sync,
    select_pipeline_config,
)
from apps.workspaces.services.tenant_metadata import (
    aget_tenant_metadata,
    get_tenant_metadata_sync,
)
from mcp_server.context import QueryContext
from mcp_server.envelope import NOT_FOUND, PIPELINE_UNRESOLVED
from mcp_server.services.metadata import _build_jsonb_annotations

User = get_user_model()


def _tenant(provider="commcare"):
    return Tenant.objects.create(
        provider=provider, external_id=f"d-{uuid.uuid4().hex}", canonical_name="T"
    )


def _seed(tenant, email, *, discovered_at, case_type="pregnancy", archived=False):
    """Create a user + TenantMembership + TenantMetadata for the tenant."""
    user = User.objects.create_user(email=email, password="x")
    tm = TenantMembership.objects.create(user=user, tenant=tenant)
    if archived:
        tm.archived_at = timezone.now()
        tm.save(update_fields=["archived_at"])
    md = TenantMetadata.objects.create(
        tenant_membership=tm,
        metadata={"case_types": [{"name": case_type}]},
        discovered_at=discovered_at,
    )
    return user, tm, md


# ── Change A: one TenantMetadata read scope ──────────────────────────────────


@pytest.mark.django_db
def test_read_is_tenant_wide_not_user_scoped():
    """A user who never materialized still sees the tenant's annotations: the read
    is by tenant, not by acting user."""
    tenant = _tenant()
    _, _, md = _seed(tenant, "materializer@example.com", discovered_at=timezone.now())
    # A second member with NO metadata row of their own.
    User.objects.create_user(email="viewer@example.com", password="x")
    TenantMembership.objects.create(
        user=User.objects.get(email="viewer@example.com"), tenant=tenant
    )

    read = get_tenant_metadata_sync(tenant.id)
    assert read is not None
    assert read.pk == md.pk


@pytest.mark.django_db
def test_archived_membership_metadata_is_not_read():
    """A tenant whose ONLY annotated membership is archived yields no annotations —
    reading an archived (revoked) member's row would leak revoked context."""
    tenant = _tenant()
    _seed(tenant, "revoked@example.com", discovered_at=timezone.now(), archived=True)

    assert get_tenant_metadata_sync(tenant.id) is None
    # And a live member's row is preferred over an archived one when both exist.
    _, _, live_md = _seed(tenant, "live@example.com", discovered_at=timezone.now())
    read = get_tenant_metadata_sync(tenant.id)
    assert read is not None
    assert read.pk == live_md.pk


@pytest.mark.django_db
def test_read_picks_most_recently_discovered_live_membership():
    """With multiple live memberships carrying metadata, the read is deterministic:
    the most-recently-discovered one wins; a NULL discovered_at never outranks a
    real timestamp."""
    tenant = _tenant()
    older = timezone.now() - timezone.timedelta(days=2)
    newer = timezone.now()
    _seed(tenant, "old@example.com", discovered_at=older, case_type="old_type")
    _, _, newer_md = _seed(tenant, "new@example.com", discovered_at=newer, case_type="new_type")
    _seed(tenant, "never@example.com", discovered_at=None, case_type="null_type")

    for _ in range(3):  # stable across repeated reads
        read = get_tenant_metadata_sync(tenant.id)
        assert read.pk == newer_md.pk
        assert read.metadata["case_types"][0]["name"] == "new_type"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_and_sync_reads_agree_across_surfaces():
    """The prompt/MCP async read and the DRF sync read resolve the SAME row, so the
    annotations derived by every surface are identical."""
    tenant = await sync_to_async(_tenant)()
    _, _, md = await sync_to_async(_seed)(
        tenant, "seed@example.com", discovered_at=timezone.now()
    )

    async_md = await aget_tenant_metadata(tenant.id)
    sync_md = await sync_to_async(get_tenant_metadata_sync)(tenant.id)
    assert async_md.pk == sync_md.pk == md.pk

    # The MCP/prompt path (_build_jsonb_annotations on raw_cases) and the DRF path
    # (_build_source_metadata on cases) both derive from the same md.
    jsonb = _build_jsonb_annotations("raw_cases", async_md)
    source_meta = await sync_to_async(_build_source_metadata)("cases", sync_md)
    assert "pregnancy" in jsonb["properties"]
    assert source_meta["items"][0]["name"] == "pregnancy"


@pytest.mark.django_db
def test_drf_helper_uses_canonical_read():
    """The DRF ``_get_tenant_metadata`` returns the same canonical row as the
    service helper (proves the divergent any-membership read was removed)."""
    tenant = _tenant()
    _seed(tenant, "a@example.com", discovered_at=timezone.now() - timezone.timedelta(days=1))
    _, _, newest = _seed(tenant, "b@example.com", discovered_at=timezone.now())

    assert _get_tenant_metadata(tenant).pk == newest.pk == get_tenant_metadata_sync(tenant.id).pk


# ── Change B: no wrong-provider fallback ─────────────────────────────────────


@pytest.mark.django_db
def test_select_pipeline_config_resolves_by_provider():
    assert select_pipeline_config(None, "commcare").name == "commcare_sync"
    assert select_pipeline_config(None, "ocs").name == "ocs_sync"
    assert select_pipeline_config(None, "commcare_connect").name == "connect_sync"


@pytest.mark.django_db
def test_select_pipeline_config_prefers_last_run_pipeline():
    assert select_pipeline_config("ocs_sync", "commcare").name == "ocs_sync"


@pytest.mark.django_db
def test_select_pipeline_config_raises_when_unresolvable():
    with pytest.raises(PipelineResolutionError):
        select_pipeline_config(None, "no_such_provider")
    with pytest.raises(PipelineResolutionError):
        select_pipeline_config("no_such_pipeline", None)


@pytest.mark.django_db
def test_resolve_sync_raises_for_unresolvable_provider_tenant():
    tenant = _tenant(provider="no_such_provider")
    ts = TenantSchema.objects.create(
        tenant=tenant, schema_name="t_bogus", state=SchemaState.ACTIVE
    )
    with pytest.raises(PipelineResolutionError):
        resolve_pipeline_config_sync(ts, None)


@pytest.mark.django_db
def test_data_dictionary_unresolvable_pipeline_returns_truthful_error():
    """DRF dictionary must return an error (not commcare metadata) when the tenant's
    pipeline can't be resolved."""
    user = User.objects.create_user(email="owner@example.com", password="x")
    tenant = _tenant(provider="no_such_provider")
    ws = Workspace.objects.create(name="W", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    TenantMembership.objects.create(user=user, tenant=tenant)
    TenantSchema.objects.create(tenant=tenant, schema_name="t_bogus", state=SchemaState.ACTIVE)

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get(f"/api/workspaces/{ws.id}/data-dictionary/")

    assert resp.status_code == 503
    assert resp.json()["schema_status"] == "pipeline_unresolved"


# ── Change B at the MCP surface ──────────────────────────────────────────────

PATCH_WORKSPACE_CONTEXT = "mcp_server.server.load_workspace_context"


def _query_context(schema_name):
    return QueryContext(
        tenant_id="x",
        schema_name=schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params={},
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_describe_table_unresolvable_pipeline_returns_error():
    from mcp_server.server import describe_table

    tenant = await Tenant.objects.acreate(
        provider="no_such_provider", external_id=f"d-{uuid.uuid4().hex}", canonical_name="T"
    )
    ts = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="t_bogus_mcp", state=SchemaState.ACTIVE
    )

    with patch(
        PATCH_WORKSPACE_CONTEXT,
        new=AsyncMock(return_value=_query_context(ts.schema_name)),
    ):
        result = await describe_table("cases", workspace_id="ws-test")

    assert result["success"] is False
    assert result["error"]["code"] == PIPELINE_UNRESOLVED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_describe_table_real_commcare_tenant_still_resolves():
    """A real commcare tenant resolves normally (aresolve is NOT mocked here); with
    the column read stubbed, the tool succeeds rather than erroring on resolution."""
    from mcp_server.server import describe_table

    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id=f"d-{uuid.uuid4().hex}", canonical_name="T"
    )
    ts = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="t_commcare_mcp", state=SchemaState.ACTIVE
    )
    described = TableDescription(name="cases", description="Cases", columns=[{"name": "case_id"}])

    with (
        patch(
            PATCH_WORKSPACE_CONTEXT,
            new=AsyncMock(return_value=_query_context(ts.schema_name)),
        ),
        patch("mcp_server.server.describe", new=AsyncMock(return_value=described)),
    ):
        result = await describe_table("cases", workspace_id="ws-test")

    assert result["success"] is True
    assert result["data"]["name"] == "cases"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_describe_table_not_found_is_distinct_from_pipeline_error():
    """When the pipeline resolves but the table isn't in the schema, the tool
    returns NOT_FOUND — not the pipeline error."""
    from mcp_server.server import describe_table

    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id=f"d-{uuid.uuid4().hex}", canonical_name="T"
    )
    ts = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="t_commcare_nf", state=SchemaState.ACTIVE
    )

    with (
        patch(
            PATCH_WORKSPACE_CONTEXT,
            new=AsyncMock(return_value=_query_context(ts.schema_name)),
        ),
        patch("mcp_server.server.describe", new=AsyncMock(return_value=None)),
    ):
        result = await describe_table("missing", workspace_id="ws-test")

    assert result["success"] is False
    assert result["error"]["code"] == NOT_FOUND
