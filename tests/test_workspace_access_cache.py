"""Workspace access decisions are reused within one request, and only there.

A chat turn resolves the same (user, workspace) for the graph and again at each
local tool's sink; with the all-of gate every resolution costs several queries.
These pin that repeats inside a request scope cost none, that nothing is cached
outside one (workers, direct calls), that entries expire, and that a request's
scope ends with it (including a streamed response's body).
"""

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.workspaces import access_cache
from apps.workspaces.access import aresolve_workspace_access_ex, resolve_workspace_access_ex
from apps.workspaces.models import Workspace, WorkspaceRole
from apps.workspaces.services.access_freshness import VerificationBudget
from config.middleware.workspace_access_cache import WorkspaceAccessCacheMiddleware

READ_KEY = (WorkspaceRole.READ, VerificationBudget.INTERACTIVE)


@pytest.fixture
def scope():
    opened, token = access_cache.open_scope()
    yield opened
    access_cache.close_scope(opened)
    access_cache.detach_scope(token)


@pytest.mark.django_db
def test_repeat_resolution_in_a_scope_costs_no_queries(
    scope, user, workspace, django_assert_num_queries
):
    first = resolve_workspace_access_ex(user, workspace.id)

    with django_assert_num_queries(0):
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
def test_user_role_and_workspace_are_all_part_of_the_key(scope, user, workspace, read_user):
    other = Workspace.objects.create(name="Other", created_by=user)

    assert resolve_workspace_access_ex(read_user, workspace.id).granted
    assert not resolve_workspace_access_ex(
        read_user, workspace.id, minimum_role=WorkspaceRole.READ_WRITE
    ).granted
    assert resolve_workspace_access_ex(user, workspace.id).granted
    assert not resolve_workspace_access_ex(read_user, other.id).granted


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
        with django_assert_num_queries(0):
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
