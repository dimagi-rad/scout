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

from apps.semantic.models import SemanticModel
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


async def _listing_kwargs(tool, user):
    kwargs = {"user_id": str(user.id), "limit": 10}
    if tool is server.list_datasets:
        # list_datasets only spans the workspaces it is asked for.
        kwargs["workspace_ids"] = [
            str(ws_id)
            async for ws_id in WorkspaceMembership.objects.filter(user=user).values_list(
                "workspace_id", flat=True
            )
        ]
    return kwargs


@pytest.mark.parametrize("tool", [server.list_workspaces, server.list_datasets])
async def test_listing_cost_does_not_grow_with_memberships(tool, upstream_provider):
    few, many = await _member_of(5), await _member_of(50)

    few_queries = await sync_to_async(_count_queries)(tool, **await _listing_kwargs(tool, few))
    many_queries = await sync_to_async(_count_queries)(tool, **await _listing_kwargs(tool, many))

    assert many_queries == few_queries
    if tool is server.list_workspaces:
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


@pytest.mark.parametrize("all_of", [True, False])
async def test_partial_coverage_follows_the_rollout_switch(settings, all_of):
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = all_of
    user = await User.objects.acreate_user(email="partial@example.com", password="pw")
    covered, uncovered = await _tenant("covered"), await _tenant("uncovered")
    await agrant_tenant_access(user, covered)
    partial = await _workspace(user, "Partial", [covered, uncovered])
    none = await _workspace(user, "None", [uncovered])

    result = await server.list_workspaces(user_id=str(user.id))

    data = result["data"]
    listed = [] if all_of else [str(partial.id)]
    assert [w["id"] for w in data["workspaces"]] == listed
    assert set(data["inaccessible_workspace_ids"]) == {str(none.id)} | (
        {str(partial.id)} if all_of else set()
    )


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


async def test_datasets_listing_names_each_workspace_without_a_queryable_model():
    user = await User.objects.acreate_user(email="models@example.com", password="pw")
    workspaces = {}
    for name in ("Ready", "Draft", "Bare"):
        tenant = await _tenant(name.lower())
        await agrant_tenant_access(user, tenant)
        workspaces[name] = await _workspace(user, name, [tenant])
    await SemanticModel.objects.acreate(workspace=workspaces["Ready"], name="m")
    await SemanticModel.objects.acreate(
        workspace=workspaces["Draft"], name="m", status=SemanticModel.Status.DRAFT
    )

    result = await server.list_datasets(
        workspace_ids=[str(ws.id) for ws in workspaces.values()], user_id=str(user.id)
    )

    errors = result["data"]["workspace_errors"]
    assert [e["workspace_id"] for e in errors] == [
        str(workspaces["Bare"].id),
        str(workspaces["Draft"].id),
    ]
    assert {e["schema_status"] for e in errors} == {"unavailable"}
    assert {e["error"] for e in errors} == {"No active semantic model is available."}


async def test_issue_lists_are_capped_with_full_counts(upstream_provider):
    user = await _member_of(60)
    upstream_provider.failure = 503
    ids_by_name = {w.name: str(w.id) async for w in Workspace.objects.filter(created_by=user)}
    active = ids_by_name["Workspace 055"]

    workspaces = await server.list_workspaces(user_id=str(user.id), workspace_id=active)
    datasets = await server.list_datasets(
        workspace_ids=list(ids_by_name.values()), workspace_id=active, user_id=str(user.id)
    )

    for data in (workspaces["data"], datasets["data"]):
        assert data["inaccessible_workspace_count"] == 12
        assert len(data["inaccessible_workspace_ids"]) == server.MAX_LISTED_WORKSPACE_ISSUES
        assert data["inaccessible_workspace_ids"][0] == active
        assert data["unverified_workspace_count"] == 12
        assert len(data["unverified_workspace_ids"]) == server.MAX_LISTED_WORKSPACE_ISSUES
    errors = datasets["data"]["workspace_errors"]
    assert datasets["data"]["workspace_error_count"] == 36
    assert len(errors) == server.MAX_LISTED_WORKSPACE_ISSUES
