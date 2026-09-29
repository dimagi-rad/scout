"""Workspace access decisions are reused within one request, and only there.

A chat turn resolves the same (user, workspace) for the graph and again at each
local tool's sink; with the all-of gate every resolution costs several queries.
These pin that repeats inside a request scope cost only a live membership read,
that nothing is cached outside one (workers, direct calls), that entries expire,
and that a request's scope ends with it (including a streamed response's body).
"""

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant, TenantMembership
from apps.users.services.upstream_denial import record_validated_upstream_denial
from apps.workspaces import access as access_module
from apps.workspaces import access_cache
from apps.workspaces.access import (
    NOT_MEMBER,
    TENANT_ACCESS_LOST,
    WorkspaceAccess,
    aresolve_workspace_access_ex,
    aworkspace_write_allowed,
    missing_tenants_for_member,
    resolve_workspace_access_ex,
    workspace_write_allowed,
)
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.access_freshness import VerificationBudget
from apps.workspaces.services.workspace_service import remove_workspace_tenant
from config.middleware.workspace_access_cache import WorkspaceAccessCacheMiddleware
from tests.tenant_access import grant_tenant_access

READ_KEY = (WorkspaceRole.READ, VerificationBudget.INTERACTIVE)


@pytest.fixture
def scope():
    opened, token = access_cache.open_scope()
    yield opened
    access_cache.close_scope(opened)
    access_cache.detach_scope(token)


@pytest.mark.django_db
def test_repeat_resolution_in_a_scope_rereads_only_the_membership(
    scope, user, workspace, django_assert_num_queries
):
    first = resolve_workspace_access_ex(user, workspace.id)

    with django_assert_num_queries(1):
        again = resolve_workspace_access_ex(user, workspace.id)

    assert first.granted
    assert again is first
    assert access_cache.lookup(user, workspace.id, READ_KEY) is first


@pytest.mark.django_db
def test_no_scope_means_no_caching(user, workspace):
    resolve_workspace_access_ex(user, workspace.id)

    with CaptureQueriesContext(connection) as queries:
        resolve_workspace_access_ex(user, workspace.id)

    assert len(queries.captured_queries) > 0


@pytest.mark.django_db
def test_user_role_and_workspace_are_all_part_of_the_key(
    scope, user, workspace, read_user, other_user
):
    other = Workspace.objects.create(name="Other", created_by=user)

    assert resolve_workspace_access_ex(read_user, workspace.id).granted
    assert not resolve_workspace_access_ex(
        read_user, workspace.id, minimum_role=WorkspaceRole.READ_WRITE
    ).granted
    assert resolve_workspace_access_ex(user, workspace.id).granted
    assert not resolve_workspace_access_ex(read_user, other.id).granted
    # A granted decision for one user never answers for another.
    assert not resolve_workspace_access_ex(other_user, workspace.id).granted


@pytest.mark.django_db
def test_verification_budget_is_part_of_the_key(scope, user, workspace, monkeypatch):
    """A recovery-metadata decision (no freshness) must never stand in for a
    protected-data one, or a stale proof would read as fresh."""
    assert resolve_workspace_access_ex(user, workspace.id, verification=None).granted
    denied = WorkspaceAccess(denied_reason="verification_unavailable")
    monkeypatch.setattr(access_module, "_resolve_with_freshness", lambda *a, **k: denied)

    assert resolve_workspace_access_ex(user, workspace.id) is denied


@pytest.mark.django_db
def test_retryable_denials_are_not_cached(scope, user, workspace, monkeypatch):
    denied = WorkspaceAccess(denied_reason="verification_unavailable")
    assert denied.retryable
    monkeypatch.setattr(access_module, "_resolve_with_freshness", lambda *a, **k: denied)
    assert resolve_workspace_access_ex(user, workspace.id) is denied
    monkeypatch.undo()

    assert resolve_workspace_access_ex(user, workspace.id).granted


@pytest.mark.django_db
def test_the_coverage_exemption_never_reaches_a_data_path_through_the_cache(scope, user):
    t1 = Tenant.objects.create(provider="commcare", external_id="c1", canonical_name="One")
    t2 = Tenant.objects.create(provider="commcare", external_id="c2", canonical_name="Two")
    ws = Workspace.objects.create(name="Partial", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    for tenant in (t1, t2):
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    grant_tenant_access(user, t1)

    exempt = resolve_workspace_access_ex(user, ws.id, require_coverage=False)
    gated = resolve_workspace_access_ex(user, ws.id)
    exempt_again = resolve_workspace_access_ex(user, ws.id, require_coverage=False)

    assert exempt.granted
    assert not gated.granted
    assert [t.tenant_name for t in gated.missing_tenants] == ["Two"]
    assert exempt_again.granted


@pytest.mark.django_db
def test_entries_expire(scope, user, workspace, monkeypatch):
    resolve_workspace_access_ex(user, workspace.id)
    monkeypatch.setattr(access_cache, "MAX_AGE_SECONDS", -1)

    with CaptureQueriesContext(connection) as queries:
        resolve_workspace_access_ex(user, workspace.id)

    assert len(queries.captured_queries) > 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_resolution_shares_the_scope_with_sync_threads(user, workspace):
    """Local tools resolve via sync_to_async too; the copied context must share
    one cache rather than fork it."""
    opened, token = access_cache.open_scope()
    try:
        first = await aresolve_workspace_access_ex(user, workspace.id)
        from_thread = await sync_to_async(resolve_workspace_access_ex)(user, workspace.id)
    finally:
        access_cache.close_scope(opened)
        access_cache.detach_scope(token)

    assert from_thread is first


@pytest.mark.django_db
def test_middleware_caches_within_a_request_and_closes_after(
    user, workspace, django_assert_num_queries
):
    scopes = []

    def view(_request):
        resolve_workspace_access_ex(user, workspace.id)
        with django_assert_num_queries(1):
            resolve_workspace_access_ex(user, workspace.id)
        scopes.append(access_cache._scope.get())
        return HttpResponse("ok")

    WorkspaceAccessCacheMiddleware(view)(RequestFactory().get("/"))

    # Contexts copied out of the request hold the scope object itself, so it must
    # be closed, not merely detached from the variable.
    [scope] = scopes
    assert scope.closed
    assert not scope


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_middleware_keeps_the_scope_for_a_streamed_body(user, workspace):
    """A chat turn runs while its response streams, after the view returned."""
    seen = []

    async def body():
        first = await aresolve_workspace_access_ex(user, workspace.id)
        seen.append(await aresolve_workspace_access_ex(user, workspace.id) is first)
        yield b"done"

    async def view(_request):
        return StreamingHttpResponse(body())

    response = await WorkspaceAccessCacheMiddleware(view)(RequestFactory().get("/"))
    async for _chunk in response.streaming_content:
        pass

    assert seen == [True]
    assert access_cache.lookup(user, workspace.id, READ_KEY) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_middleware_closes_the_scope_after_a_plain_response(user, workspace):
    async def view(_request):
        await aresolve_workspace_access_ex(user, workspace.id)
        return HttpResponse("ok")

    await WorkspaceAccessCacheMiddleware(view)(RequestFactory().get("/"))

    assert access_cache.lookup(user, workspace.id, READ_KEY) is None


@pytest.mark.django_db
def test_sync_middleware_closes_the_scope_when_the_view_raises(user, workspace):
    scopes = []

    def view(_request):
        resolve_workspace_access_ex(user, workspace.id)
        scopes.append(access_cache._scope.get())
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        WorkspaceAccessCacheMiddleware(view)(RequestFactory().get("/"))

    [scope] = scopes
    assert scope.closed
    assert not scope


def test_sync_streamed_response_closes_at_view_return_and_is_not_wrapped():
    scopes = []

    def chunks():
        yield b"file"

    def view(_request):
        scopes.append(access_cache._scope.get())
        return StreamingHttpResponse(chunks())

    response = WorkspaceAccessCacheMiddleware(view)(RequestFactory().get("/"))

    assert scopes[0].closed
    assert list(response.streaming_content) == [b"file"]


@pytest.mark.asyncio
async def test_sync_streamed_bodies_are_left_untouched():
    scopes = []

    def chunks():
        yield b"file"

    async def view(_request):
        scopes.append(access_cache._scope.get())
        return StreamingHttpResponse(chunks())

    response = await WorkspaceAccessCacheMiddleware(view)(RequestFactory().get("/"))

    assert scopes[0].closed
    assert list(response.streaming_content) == [b"file"]


@pytest.mark.django_db
def test_a_removed_member_loses_a_cached_grant(scope, user, workspace):
    """A6: removal lands on the very next check, not up to MAX_AGE_SECONDS later,
    even though the removal happened outside this request's scope."""
    assert resolve_workspace_access_ex(user, workspace.id).granted

    WorkspaceMembership.objects.filter(workspace=workspace, user=user).delete()

    assert resolve_workspace_access_ex(user, workspace.id).denied_reason == NOT_MEMBER


@pytest.mark.django_db
def test_a_demoted_member_loses_a_cached_write_grant(scope, user, workspace):
    assert workspace_write_allowed(user, workspace.id)
    read_grant = resolve_workspace_access_ex(user, workspace.id)

    WorkspaceMembership.objects.filter(workspace=workspace, user=user).update(
        role=WorkspaceRole.READ
    )

    assert not workspace_write_allowed(user, workspace.id)
    again = resolve_workspace_access_ex(user, workspace.id)
    assert again.granted
    # Views branch on membership.role (MANAGE-only actions), so it must be live too.
    assert read_grant.membership.role == again.membership.role == WorkspaceRole.READ


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_cached_write_grant_rechecks_the_role(user, workspace):
    opened, token = access_cache.open_scope()
    try:
        assert await aworkspace_write_allowed(user, workspace.id)
        await WorkspaceMembership.objects.filter(workspace=workspace, user=user).aupdate(
            role=WorkspaceRole.READ
        )
        allowed = await aworkspace_write_allowed(user, workspace.id)
    finally:
        access_cache.close_scope(opened)
        access_cache.detach_scope(token)

    assert not allowed


@pytest.mark.django_db
def test_an_upstream_denial_drops_the_users_cached_decisions(scope, user, workspace, tenant):
    """A6: a tool call that sees the provider revoke access must not leave the rest
    of the turn running on the grant cached before it."""
    assert resolve_workspace_access_ex(user, workspace.id).granted
    connection = TenantMembership.objects.get(user=user, tenant=tenant).connection

    record_validated_upstream_denial(
        connection, code=ErrorCode.AUTH_ACCESS_DENIED, tenant_id=tenant.id
    )

    assert resolve_workspace_access_ex(user, workspace.id).denied_reason == TENANT_ACCESS_LOST


@pytest.fixture
def uncovered_manager(user):
    t1 = Tenant.objects.create(provider="commcare", external_id="u1", canonical_name="One")
    t2 = Tenant.objects.create(provider="commcare", external_id="u2", canonical_name="Two")
    ws = Workspace.objects.create(name="Partial", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    for tenant in (t1, t2):
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    grant_tenant_access(user, t1)
    return ws, t2


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["", "members/"])
def test_exempt_pages_evaluate_readiness_once_per_request(
    client, user, uncovered_manager, mocker, path
):
    """A5: the gate already computed what the caller is missing; the exempt
    handler narrowing on it must not run the readiness batch again."""
    ws, _missing = uncovered_manager
    client.force_login(user)
    spy = mocker.spy(access_module, "member_coverage_gaps")

    resp = client.get(f"/api/workspaces/{ws.id}/{path}")

    assert resp.status_code == 200
    assert spy.call_count == 1


@pytest.mark.django_db
def test_removing_the_missing_source_evaluates_readiness_once(
    client, user, uncovered_manager, mocker
):
    ws, missing = uncovered_manager
    wt = WorkspaceTenant.objects.get(workspace=ws, tenant=missing)
    client.force_login(user)
    spy = mocker.spy(access_module, "member_coverage_gaps")

    resp = client.delete(f"/api/workspaces/{ws.id}/tenants/{wt.id}/")

    assert resp.status_code == 204
    assert spy.call_count == 1


@pytest.mark.django_db
def test_changing_a_workspaces_sources_drops_its_cached_coverage(scope, user, uncovered_manager):
    ws, missing = uncovered_manager
    assert [t.tenant_name for t in missing_tenants_for_member(user, ws)] == ["Two"]

    remove_workspace_tenant(ws, WorkspaceTenant.objects.get(workspace=ws, tenant=missing))

    assert missing_tenants_for_member(user, ws) == ()
