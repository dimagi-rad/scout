"""Background work rechecks upstream freshness before protected work.

The worker entry, post-wait and between-tenant checkpoints go through the same
authorizer as HTTP requests, with the background budget.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant, TenantMembership
from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import WorkspaceTenant
from tests.pipeline_doubles import completed_pipeline_run
from tests.upstream_proofs import (
    agrant_fresh_upstream_access,
    amake_proof_stale,
    make_proof_stale,
)


@pytest.fixture(autouse=True)
def _no_candidate_ddl():
    with (
        patch("apps.workspaces.tasks.SchemaManager.create_physical_schema", return_value=None),
        patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None),
    ):
        yield


def _registry():
    pipeline = MagicMock()
    pipeline.provider = "commcare"
    pipeline.name = "commcare_sync"
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline
    return registry


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stale_and_revoked_actor_is_denied_before_loading(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.domains = []
    pipeline = MagicMock()

    with patch("apps.workspaces.tasks._run_pipeline_with_progress", pipeline):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert result["status"] == "denied"
    assert result["error_code"] == ErrorCode.AUTH_ACCESS_DENIED
    pipeline.assert_not_called()
    membership = await TenantMembership.all_objects.aget(user=user, tenant=tenant)
    assert membership.archived_at is not None
    assert len(upstream_provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_provider_outage_denies_the_job_without_touching_memberships(
    workspace, tenant, user, upstream_provider
):
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503
    pipeline = MagicMock()

    with patch("apps.workspaces.tasks._run_pipeline_with_progress", pipeline):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert result["status"] == "denied"
    assert result["error_code"] == ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE
    assert result["tenants"][0]["error_code"] == ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE
    assert "retry shortly" in " ".join(result["guidance"])
    pipeline.assert_not_called()
    assert await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_proof_expiring_mid_run_stops_before_the_next_tenant(
    workspace, tenant, user, upstream_provider
):
    second = await Tenant.objects.acreate(
        provider="commcare", external_id="second-domain", canonical_name="Second"
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=second)
    await agrant_fresh_upstream_access(user, second)
    upstream_provider.domains = []
    loaded = []

    def expire_during_first_load(membership, *args, **kwargs):
        loaded.append(membership.tenant_id)
        make_proof_stale(user, tenant)
        make_proof_stale(user, second)
        return completed_pipeline_run(membership, *args, **kwargs)

    with (
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_registry()),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=expire_during_first_load,
        ),
        patch("apps.workspaces.tasks.SchemaManager", return_value=MagicMock()),
    ):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert len(loaded) == 1
    denied = [entry for entry in result["tenants"] if not entry.get("success")]
    assert len(denied) == 1
    assert denied[0]["error_code"] == ErrorCode.AUTH_ACCESS_DENIED
    assert result["all_succeeded"] is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_one_tenant_revoked_mid_run_is_skipped_while_the_other_stays_loaded(
    workspace, tenant, user, upstream_provider
):
    # Memberships load in canonical-name order, so this tenant is loaded second.
    second = await Tenant.objects.acreate(
        provider="commcare", external_id="second-domain", canonical_name="Zeta Second"
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=second)
    await agrant_fresh_upstream_access(user, second)
    # Only the second tenant is revoked upstream; the first stays listed.
    upstream_provider.domains = [tenant.external_id]
    loaded = []

    def expire_during_first_load(membership, *args, **kwargs):
        loaded.append(membership.tenant_id)
        make_proof_stale(user, second)
        return completed_pipeline_run(membership, *args, **kwargs)

    with (
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_registry()),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=expire_during_first_load,
        ),
        patch("apps.workspaces.tasks.SchemaManager", return_value=MagicMock()),
    ):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert loaded == [tenant.id]
    skipped = [entry for entry in result["tenants"] if not entry.get("success")]
    assert len(skipped) == 1
    assert skipped[0]["tenant_id"] == str(second.id)
    assert skipped[0]["error_code"] == ErrorCode.AUTH_ACCESS_DENIED
    assert result["all_succeeded"] is False
    assert not await TenantMembership.objects.filter(user=user, tenant=second).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_fresh_actor_loads_without_any_provider_call(
    workspace, tenant, user, upstream_provider
):
    with (
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_registry()),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=completed_pipeline_run,
        ),
    ):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert result["all_succeeded"] is True
    assert upstream_provider.requests == []
