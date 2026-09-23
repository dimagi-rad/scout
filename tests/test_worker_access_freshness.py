"""Background work rechecks upstream freshness before protected work.

The worker entry, post-wait and between-tenant checkpoints go through the same
authorizer as HTTP requests, with the background budget.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, transaction

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant, TenantMembership
from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant
from apps.workspaces.services.access_freshness import (
    FRESHNESS_DENIAL_REASONS,
    FRESHNESS_ERROR_CODES,
)
from tests.upstream_proofs import (
    agrant_fresh_upstream_access,
    amake_proof_stale,
    make_proof_stale,
)


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
    pipeline = AsyncMock()

    with patch("apps.workspaces.tasks._run_pipeline_with_progress", pipeline):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert result["status"] == "denied"
    assert result["error_code"] == ErrorCode.AUTH_ACCESS_DENIED
    pipeline.assert_not_awaited()
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
    pipeline = AsyncMock()

    with patch("apps.workspaces.tasks._run_pipeline_with_progress", pipeline):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert result["status"] == "denied"
    assert result["error_code"] == ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE
    assert result["tenants"][0]["error_code"] == ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE
    assert "retry shortly" in " ".join(result["guidance"])
    pipeline.assert_not_awaited()
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

    def expire_during_first_load(membership, *_args, **_kwargs):
        loaded.append(membership.tenant_id)
        make_proof_stale(user, tenant)
        make_proof_stale(user, second)
        return {"status": "completed"}

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

    def expire_during_first_load(membership, *_args, **_kwargs):
        loaded.append(membership.tenant_id)
        make_proof_stale(user, second)
        return {"status": "completed"}

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
            return_value={"status": "completed"},
        ),
    ):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert result["all_succeeded"] is True
    assert upstream_provider.requests == []


@sync_to_async
def _bound_refresh_candidate(tenant, workspace, membership, schema_name):
    with transaction.atomic():
        schema = TenantSchema.objects.create(
            tenant=tenant,
            schema_name=schema_name,
            state=SchemaState.PROVISIONING,
            refresh_workspace_id=workspace.id,
            refresh_actor_user_id=membership.user_id,
            refresh_membership_id=membership.id,
        )
        args = {
            "schema_id": str(schema.id),
            "membership_id": str(membership.id),
            "actor_user_id": str(membership.user_id),
            "workspace_id": str(workspace.id),
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args) "
                "VALUES (%s, %s, %s::procrastinate_job_status, %s::jsonb) RETURNING id",
                [
                    "default",
                    "apps.workspaces.tasks.refresh_tenant_schema",
                    "doing",
                    json.dumps(args),
                ],
            )
            job_id = cursor.fetchone()[0]
        schema.refresh_job_id = job_id
        schema.save(update_fields=["refresh_job_id"])
        return schema, args, job_id


async def _run_refresh(schema_args, job_id):
    return await workspaces_tasks.refresh_tenant_schema(
        context=MagicMock(job=MagicMock(id=job_id)), **schema_args
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tenant_refresh_is_refused_when_revoked(workspace, tenant, user, upstream_provider):
    membership = await TenantMembership.objects.aget(user=user, tenant=tenant)
    schema, args, job_id = await _bound_refresh_candidate(
        tenant, workspace, membership, "test_domain_r1"
    )
    await amake_proof_stale(user, tenant)
    upstream_provider.domains = []
    create_schema = MagicMock()

    with patch.object(workspaces_tasks.SchemaManager, "create_physical_schema", create_schema):
        result = await _run_refresh(args, job_id)

    await schema.arefresh_from_db()
    assert schema.state == SchemaState.FAILED
    assert result["status"] == "denied"
    create_schema.assert_not_called()
    assert not await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tenant_refresh_outage_is_retryable_and_keeps_the_membership(
    workspace, tenant, user, upstream_provider
):
    membership = await TenantMembership.objects.aget(user=user, tenant=tenant)
    _schema, args, job_id = await _bound_refresh_candidate(
        tenant, workspace, membership, "test_domain_r2"
    )
    await amake_proof_stale(user, tenant)
    upstream_provider.failure = 503
    create_schema = MagicMock()

    with patch.object(workspaces_tasks.SchemaManager, "create_physical_schema", create_schema):
        result = await _run_refresh(args, job_id)

    assert result["error_code"] == ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE
    assert "retry" in result["error"].lower()
    create_schema.assert_not_called()
    assert await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


def test_every_freshness_denial_reason_has_a_registry_code():
    assert set(FRESHNESS_ERROR_CODES) == set(FRESHNESS_DENIAL_REASONS)
