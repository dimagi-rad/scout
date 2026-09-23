"""The refresh worker rechecks upstream before claiming and decides inside the claim.

The claim runs in a transaction, where the gate never contacts a provider, so the
worker's recheck just before it is what keeps proofs fresh for that decision.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, transaction

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantMembership
from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import SchemaState, TenantSchema
from apps.workspaces.services.access_freshness import (
    FRESHNESS_DENIAL_REASONS,
    FRESHNESS_ERROR_CODES,
)
from tests.upstream_proofs import amake_proof_stale


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
