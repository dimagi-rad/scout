"""Workspace and dataset listings cost a fixed number of queries (C1).

``list_workspaces`` and ``list_datasets`` decide access for every membership the
user holds, not just the page returned, and some users hold hundreds. The
decision is batched, so the query count must not grow with the membership count,
while each workspace keeps the verdict its own access check would give it.
"""

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.users.models import Tenant
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from mcp_server import server
from tests.tenant_access import agrant_tenant_access
from tests.upstream_proofs import amake_proof_stale

User = get_user_model()

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def _workspace(user, name, tenants):
    ws = await Workspace.objects.acreate(name=name, created_by=user)
    for tenant in tenants:
        await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.READ)
    return ws


async def _tenant(key):
    return await Tenant.objects.acreate(
        provider="commcare", external_id=key, canonical_name=key.title()
    )


async def _member_of(n):
    """A user with ``n`` workspaces: most readable, some lost, some unverified."""
    user = await User.objects.acreate_user(email=f"member-of-{n}@example.com", password="pw")
    for i in range(n):
        tenant = await _tenant(f"domain-{n}-{i}")
        await _workspace(user, f"Workspace {i:03d}", [tenant])
        if i % 5 == 0:
            continue
        await agrant_tenant_access(user, tenant)
        if i % 5 == 1:
            await amake_proof_stale(user, tenant)
    return user


def _count_queries(tool, **kwargs):
    # One thread for the whole call, so the capture sees every ORM query it makes.
    with CaptureQueriesContext(connection) as captured:
        result = async_to_sync(tool)(**kwargs)
    assert result["success"] is True, result
    return len(captured.captured_queries)


@pytest.mark.parametrize("tool", [server.list_workspaces])
async def test_listing_cost_does_not_grow_with_memberships(tool, upstream_provider):
    few, many = await _member_of(5), await _member_of(50)

    few_queries = await sync_to_async(_count_queries)(tool, user_id=str(few.id), limit=10)
    many_queries = await sync_to_async(_count_queries)(tool, user_id=str(many.id), limit=10)

    assert many_queries == few_queries
    assert upstream_provider.requests == []


async def test_each_workspace_keeps_its_own_verdict():
    user = await _member_of(10)

    result = await server.list_workspaces(user_id=str(user.id), limit=3)

    names = {w.id: w.name async for w in Workspace.objects.filter(created_by=user)}
    by_index = {int(name.split()[-1]): str(ws_id) for ws_id, name in names.items()}
    data = result["data"]
    assert data["inaccessible_workspace_ids"] == sorted([by_index[0], by_index[5]])
    assert data["unverified_workspace_ids"] == sorted([by_index[1], by_index[6]])
    assert data["total"] == 6
    assert [w["id"] for w in data["workspaces"]] == [by_index[2], by_index[3], by_index[4]]
    assert data["has_more"] is True


async def test_one_stale_proof_on_a_shared_connection_blocks_only_its_workspaces():
    user = await User.objects.acreate_user(email="shared@example.com", password="pw")
    fresh, stale = await _tenant("fresh-domain"), await _tenant("stale-domain")
    await agrant_tenant_access(user, fresh)
    await agrant_tenant_access(user, stale)
    await amake_proof_stale(user, stale)
    only_fresh = await _workspace(user, "Only fresh", [fresh])
    only_stale = await _workspace(user, "Only stale", [stale])
    both = await _workspace(user, "Both", [fresh, stale])

    result = await server.list_workspaces(user_id=str(user.id))

    data = result["data"]
    assert [w["id"] for w in data["workspaces"]] == [str(only_fresh.id)]
    assert data["unverified_workspace_ids"] == sorted([str(only_stale.id), str(both.id)])
    assert data["inaccessible_workspace_ids"] == []
