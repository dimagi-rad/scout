"""The headless waiter blocks on any active run on the workspace's tenants, whoever owns it."""

import logging

import pytest

from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from apps.workspaces.services.materialize import await_in_progress_materializations

RunState = MaterializationRun.RunState

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def _workspace_with_tenant(user, name="w"):
    workspace = await Workspace.objects.acreate(name=name, created_by=user)
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id=f"t-{name}", canonical_name=name
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    return workspace, tenant


async def _run(tenant, state, **markers):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"s_{tenant.external_id}_{state}", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=state, **markers
    )


async def _waits(workspace, caplog) -> bool:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="apps.workspaces.services.materialize"):
        await await_in_progress_materializations(
            str(workspace.id), poll_interval=0.01, max_wait_seconds=0.03
        )
    return any("still waiting" in record.getMessage() for record in caplog.records)


async def test_returns_at_once_for_a_workspace_with_no_sources(user, caplog):
    workspace = await Workspace.objects.acreate(name="empty", created_by=user)

    assert not await _waits(workspace, caplog)


async def test_returns_at_once_when_every_run_has_finished(user, caplog):
    workspace, tenant = await _workspace_with_tenant(user)
    await _run(tenant, RunState.COMPLETED)

    assert not await _waits(workspace, caplog)


async def test_waits_on_an_active_run_of_its_tenant(user, caplog):
    workspace, tenant = await _workspace_with_tenant(user)
    await _run(tenant, RunState.LOADING)

    assert await _waits(workspace, caplog)


async def test_waits_on_a_siblings_run_of_a_shared_tenant(user, caplog):
    workspace, tenant = await _workspace_with_tenant(user)
    sibling = await Workspace.objects.acreate(name="sibling", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=sibling, tenant=tenant)
    await _run(tenant, RunState.LOADING, procrastinate_job_id=1)

    assert await _waits(workspace, caplog)


async def test_ignores_an_active_run_on_an_unrelated_tenant(user, caplog):
    workspace, _ = await _workspace_with_tenant(user)
    _, other_tenant = await _workspace_with_tenant(user, name="other")
    await _run(other_tenant, RunState.LOADING)

    assert not await _waits(workspace, caplog)
