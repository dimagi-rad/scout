"""Freshness-aware additions to the MCP tools' per-call access recheck (#567).

Data tools already recheck the acting user per call; these cover what the
freshness gate adds on top: an outage reaches the agent as a retryable denial,
cancellation stays reachable during one, and dataset listings report workspaces
whose access could not yet be confirmed.
"""

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.test import AsyncClient

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantMembership
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from mcp_server.server import (
    cancel_materialization,
    list_datasets,
    query,
)
from tests.upstream_proofs import amake_proof_stale

PATCH_WORKSPACE_CONTEXT = "mcp_server.server.load_workspace_context"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_verification_outage_reaches_the_agent_as_a_retryable_denial(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503
    load = AsyncMock()

    with patch(PATCH_WORKSPACE_CONTEXT, load):
        result = await query("SELECT 1", workspace_id=str(workspace.id), user_id=str(user.id))

    assert result["error"]["code"] == ErrorCode.WORKSPACE_ACCESS_DENIED
    assert "retry" in result["error"]["message"].lower()
    load.assert_not_awaited()
    assert await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_stays_reachable_during_a_verification_outage(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503

    result = await cancel_materialization(
        run_id="00000000-0000-0000-0000-000000000000",
        workspace_id=str(workspace.id),
        user_id=str(user.id),
    )

    assert result["error"]["code"] == ErrorCode.NOT_FOUND
    assert upstream_provider.requests == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_http_cancel_stays_reachable_during_a_verification_outage(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503
    client = AsyncClient()
    await sync_to_async(client.force_login)(user)

    response = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert response.status_code == 200
    assert response.json()["status"] == "no_active_run"
    assert upstream_provider.requests == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_dataset_listing_hides_a_workspace_that_cannot_be_verified(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503

    result = await list_datasets(
        workspace_ids=[str(workspace.id)],
        workspace_id=str(workspace.id),
        user_id=str(user.id),
    )

    assert result["success"] is True
    assert result["data"]["inaccessible_workspace_ids"] == []
    assert result["data"]["datasets"] == []
    assert result["data"]["unverified_workspace_ids"] == [str(workspace.id)]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_unrequested_workspaces_that_cannot_be_verified_are_still_reported(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503

    result = await list_datasets(user_id=str(user.id))

    assert result["data"]["unverified_workspace_ids"] == [str(workspace.id)]
    assert upstream_provider.requests == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_requesting_an_unverified_workspace_rechecks_it(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.domains = [tenant.external_id]

    result = await list_datasets(workspace_ids=[str(workspace.id)], user_id=str(user.id))

    assert result["data"]["unverified_workspace_ids"] == []
    assert result["data"]["inaccessible_workspace_ids"] == []
    assert len(upstream_provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_cancel_of_an_unowned_run_needs_verified_access(
    workspace, tenant, user, upstream_provider
):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="mcp_orphan_guard", state=SchemaState.ACTIVE
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=778,
    )
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503

    result = await cancel_materialization(
        run_id=str(run.id), workspace_id=str(workspace.id), user_id=str(user.id)
    )

    assert result["error"]["code"] == ErrorCode.AUTH_ACCESS_DENIED
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.LOADING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_unverified_cancel_leaves_shared_orphan_runs_alone(
    workspace, tenant, user, upstream_provider
):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="orphan_guard", state=SchemaState.ACTIVE
    )
    orphan = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=777,
    )
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503
    client = AsyncClient()
    await sync_to_async(client.force_login)(user)

    with patch("apps.workspaces.api.materialization_views.app") as queue:
        queue.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        response = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert response.status_code == 403
    assert response.json()["retryable"] is True
    await orphan.arefresh_from_db()
    assert orphan.state == MaterializationRun.RunState.LOADING
    queue.job_manager.cancel_job_by_id_async.assert_not_awaited()
