# Phase 1: Django ORM Async Conversion

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `sync_to_async` wrappers around Django ORM calls by converting to native async ORM methods (`aget`, `afirst`, `asave`, etc.), and remove unnecessary `sync_to_async` around pure CPU functions.

**Architecture:** Django 5.2 provides native async ORM methods for all queryset operations. Each `@sync_to_async`-decorated function wrapping ORM queries becomes a plain `async def` using `a*` methods. The `touch()` model methods get async `atouch()` counterparts. Pure CPU functions (`_parse_db_url`, `encrypt_credential`, `decrypt_credential`) drop their wrappers entirely. Tests that use `sync_to_async(User.objects.create_user)` stay as-is since Django doesn't auto-generate `acreate_user` for custom manager methods.

**Tech Stack:** Django 5.2 async ORM, Python 3.13, pytest-asyncio

---

### Task 1: Add `atouch()` to `TenantSchema` and `WorkspaceViewSchema`

**Files:**
- Modify: `apps/workspaces/models.py:47-52` (TenantSchema.touch)
- Modify: `apps/workspaces/models.py:213-218` (WorkspaceViewSchema.touch)

- [ ] **Step 1: Add `atouch()` to `TenantSchema`**

In `apps/workspaces/models.py`, add after the existing `touch()` method on `TenantSchema` (after line 52):

```python
    async def atouch(self):
        """Async version of touch() — reset the inactivity TTL."""
        from django.utils import timezone

        self.last_accessed_at = timezone.now()
        await self.asave(update_fields=["last_accessed_at"])
```

- [ ] **Step 2: Add `atouch()` to `WorkspaceViewSchema`**

In `apps/workspaces/models.py`, add after the existing `touch()` method on `WorkspaceViewSchema` (after line 218):

```python
    async def atouch(self):
        """Async version of touch() — reset the inactivity TTL."""
        from django.utils import timezone

        self.last_accessed_at = timezone.now()
        await self.asave(update_fields=["last_accessed_at"])
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/ -x -q --timeout=30 2>&1 | tail -5`
Expected: All existing tests pass (no callers changed yet).

- [ ] **Step 4: Commit**

```
git add apps/workspaces/models.py
git commit -m "Add atouch() async method to TenantSchema and WorkspaceViewSchema"
```

---

### Task 2: Convert `apps/users/decorators.py` — use `request.auser()`

**Files:**
- Modify: `apps/users/decorators.py`

- [ ] **Step 1: Rewrite `get_user_if_authenticated` to use `request.auser()`**

Replace the entire file with:

```python
"""Authentication decorators and mixins for API views."""

from functools import wraps

from django.http import JsonResponse

_AUTH_REQUIRED = {"error": "Authentication required"}


async def get_user_if_authenticated(request):
    """Access request.user from async context using Django's native auser()."""
    user = await request.auser()
    if user.is_authenticated:
        return user
    return None


def async_login_required(view_func):
    """Require authentication for async Django views. Returns 401 JSON.

    Sets request._authenticated_user so the view can access the user
    without another sync_to_async call to request.user.
    """

    @wraps(view_func)
    async def wrapper(request, *args, **kwargs):
        user = await get_user_if_authenticated(request)
        if user is None:
            return JsonResponse(_AUTH_REQUIRED, status=401)
        request._authenticated_user = user
        return await view_func(request, *args, **kwargs)

    return wrapper


def login_required_json(view_func):
    """Require authentication for sync Django views. Returns 401 JSON.

    Unlike Django's @login_required which redirects, this returns a
    JSON 401 response suitable for API endpoints.
    """

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(_AUTH_REQUIRED, status=401)
        return view_func(request, *args, **kwargs)

    return wrapper


class LoginRequiredJsonMixin:
    """Mixin for Django CBVs that returns 401 JSON instead of redirect."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(_AUTH_REQUIRED, status=401)
        return super().dispatch(request, *args, **kwargs)
```

Key change: removed `from asgiref.sync import sync_to_async` and `@sync_to_async` decorator. `get_user_if_authenticated` is now a plain `async def` using `await request.auser()`.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_chat_csrf.py tests/test_resolve_workspace_membership.py -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS — these tests exercise the auth decorator indirectly.

- [ ] **Step 3: Commit**

```
git add apps/users/decorators.py
git commit -m "Replace sync_to_async with request.auser() in auth decorator"
```

---

### Task 3: Convert `apps/chat/helpers.py` — async ORM for workspace resolution

**Files:**
- Modify: `apps/chat/helpers.py`

- [ ] **Step 1: Rewrite `_resolve_workspace_and_membership`**

Replace the entire file with:

```python
"""Shared helpers for chat views."""

from apps.users.decorators import (  # noqa: F401 — re-exported for backwards compat
    LoginRequiredJsonMixin,
    async_login_required,
    get_user_if_authenticated,
    login_required_json,
)
from apps.workspaces.models import WorkspaceMembership


async def _resolve_workspace_and_membership(user, workspace_id):
    """Resolve workspace access for a user.

    Returns (workspace, tenant_membership, is_multi_tenant):
    - (None, None, False): workspace not found or user lacks WorkspaceMembership
    - (workspace, None, True): multi-tenant workspace; WorkspaceMembership is sufficient
    - (workspace, None, False): single-tenant workspace but user lacks TenantMembership
    - (workspace, tm, False): single-tenant workspace with a valid TenantMembership
    """
    try:
        wm = await WorkspaceMembership.objects.select_related("workspace").aget(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None, False

    workspace = wm.workspace

    is_multi_tenant = await workspace.workspace_tenants.acount() > 1
    if is_multi_tenant:
        return workspace, None, True

    tenant = await workspace.tenants.afirst()
    if tenant is None:
        return workspace, None, False

    from apps.users.models import TenantMembership

    try:
        tm = await TenantMembership.objects.aget(user=user, tenant=tenant)
    except TenantMembership.DoesNotExist:
        return workspace, None, False
    return workspace, tm, False
```

Key changes: removed `sync_to_async` import and decorator. Uses `aget()`, `acount()`, `afirst()` natively. Note: the original used `workspace.workspace_tenants.count()` but we now need `await workspace.workspace_tenants.acount()`. The original also used `workspace.tenant` which is a property — we replace with `await workspace.tenants.afirst()` which is the async equivalent for single-tenant workspaces.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_resolve_workspace_membership.py -x -v --timeout=30 2>&1 | tail -15`
Expected: All 5 tests PASS.

- [ ] **Step 3: Commit**

```
git add apps/chat/helpers.py
git commit -m "Convert _resolve_workspace_and_membership to native async ORM"
```

---

### Task 4: Convert `apps/chat/views.py` — `_upsert_thread`

**Files:**
- Modify: `apps/chat/views.py:17,36-59`

- [ ] **Step 1: Rewrite `_upsert_thread` and remove sync_to_async import**

Remove the `sync_to_async` import (line 17). Replace the `_upsert_thread` function (lines 36-59) with:

```python
async def _upsert_thread(thread_id, user, title, *, workspace):
    """Create or update a Thread record.

    Explicitly validates ownership before upserting: if the thread_id already
    exists and belongs to a different user or workspace, the upsert is skipped
    with a warning rather than relying on a PK IntegrityError as a side-effect.
    auto_now on updated_at handles the timestamp on every save.
    """
    existing = await Thread.objects.filter(id=thread_id).afirst()
    if existing is not None and (
        existing.user_id != user.pk or existing.workspace_id != workspace.pk
    ):
        logger.warning(
            "Thread %s belongs to a different user/workspace, skipping upsert",
            thread_id,
        )
        return
    # On create: set user, workspace, and title.
    # On update: no field changes needed — auto_now on updated_at handles the timestamp.
    await Thread.objects.aupdate_or_create(
        id=thread_id,
        create_defaults={"user": user, "workspace": workspace, "title": title[:200]},
    )
```

- [ ] **Step 2: Verify no remaining sync_to_async references**

Check: `grep sync_to_async apps/chat/views.py` should return nothing.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_chat_csrf.py -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 4: Commit**

```
git add apps/chat/views.py
git commit -m "Convert _upsert_thread to native async ORM"
```

---

### Task 5: Convert `apps/chat/thread_views.py` — all 5 wrapper functions

**Files:**
- Modify: `apps/chat/thread_views.py`

- [ ] **Step 1: Rewrite all sync_to_async functions**

Replace the entire file with:

```python
"""Thread CRUD endpoints: list, messages, share, public."""

import json
import logging

from django.http import JsonResponse

from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.helpers import (
    _resolve_workspace_and_membership,
    async_login_required,
)
from apps.chat.message_converter import langchain_messages_to_ui
from apps.chat.models import Thread

logger = logging.getLogger(__name__)


async def _get_thread(thread_id, user, *, workspace_id=None):
    """Load a thread ensuring ownership, optionally scoped to a workspace."""
    try:
        if workspace_id is not None:
            return await Thread.objects.aget(id=thread_id, user=user, workspace_id=workspace_id)
        return await Thread.objects.aget(id=thread_id, user=user)
    except Thread.DoesNotExist:
        return None


async def _get_public_thread(share_token):
    """Load a shared thread by share token."""
    try:
        return await Thread.objects.select_related("user").aget(
            share_token=share_token, is_shared=True
        )
    except Thread.DoesNotExist:
        return None


async def _update_thread_sharing(thread, is_shared=None):
    """Update sharing settings on a thread."""
    if is_shared is not None:
        thread.is_shared = is_shared
    await thread.asave()
    return {
        "id": str(thread.id),
        "is_shared": thread.is_shared,
        "share_token": thread.share_token,
    }


async def _get_thread_artifacts(thread_id):
    """Load artifacts associated with a thread."""
    from apps.artifacts.models import Artifact

    return [
        {
            "id": str(a.id),
            "title": a.title,
            "artifact_type": a.artifact_type,
            "code": a.code,
            "data": a.data,
            "version": a.version,
        }
        async for a in Artifact.objects.filter(conversation_id=str(thread_id)).order_by(
            "created_at"
        )
    ]


async def _list_threads(user, *, workspace_id):
    """Return recent threads for a workspace/user."""
    from apps.workspaces.workspace_resolver import aresolve_workspace

    workspace, err = await aresolve_workspace(user, workspace_id)
    if workspace is None:
        return None

    return [
        {
            "id": str(t.id),
            "title": t.title,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "is_shared": t.is_shared,
        }
        async for t in Thread.objects.filter(user=user, workspace=workspace).order_by(
            "-updated_at"
        )[:50]
    ]


async def _load_thread_messages(thread_id) -> list[dict]:
    """Load messages from checkpointer and convert to UI format."""
    try:
        checkpointer = await ensure_checkpointer()
        config = {"configurable": {"thread_id": str(thread_id)}}
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning("Failed to load checkpoint for thread %s", thread_id, exc_info=True)
        return []

    if checkpoint_tuple is None:
        return []

    checkpoint = checkpoint_tuple.checkpoint
    lc_messages = (checkpoint.get("channel_values") or {}).get("messages", [])
    return langchain_messages_to_ui(lc_messages)


@async_login_required
async def thread_list_view(request, workspace_id):
    """
    GET /api/workspaces/<workspace_id>/threads/

    Returns recent threads for the authenticated user in a workspace.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    threads = await _list_threads(user, workspace_id=workspace_id)
    if threads is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)
    return JsonResponse(threads, safe=False)


@async_login_required
async def thread_messages_view(request, workspace_id, thread_id):
    """
    GET /api/chat/threads/<thread_id>/messages/

    Loads conversation from the checkpointer and returns UIMessage format.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    workspace, _, _is_multi = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse([], safe=False)

    ui_messages = await _load_thread_messages(thread_id)
    return JsonResponse(ui_messages, safe=False)


@async_login_required
async def thread_share_view(request, workspace_id, thread_id):
    """
    GET  /api/chat/threads/<thread_id>/share/  — get sharing settings
    PATCH /api/chat/threads/<thread_id>/share/ — update sharing settings
    """
    user = request._authenticated_user

    workspace, _, _is_multi = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    if request.method == "GET":
        return JsonResponse(
            {
                "id": str(thread.id),
                "is_shared": thread.is_shared,
                "share_token": thread.share_token,
            }
        )

    if request.method == "PATCH":
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        result = await _update_thread_sharing(
            thread,
            is_shared=body.get("is_shared"),
        )
        return JsonResponse(result)

    return JsonResponse({"error": "Method not allowed"}, status=405)


async def public_thread_view(request, share_token):
    """
    GET /api/chat/threads/shared/<share_token>/

    Public read-only view of a shared thread's messages and artifacts.
    No authentication required.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    thread = await _get_public_thread(share_token)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    # Load messages from checkpointer
    messages = await _load_thread_messages(thread.id)

    # Load associated artifacts
    artifacts = await _get_thread_artifacts(thread.id)

    return JsonResponse(
        {
            "thread": {
                "id": str(thread.id),
                "title": thread.title,
                "created_at": thread.created_at.isoformat(),
            },
            "messages": messages,
            "artifacts": artifacts,
        }
    )
```

Key changes:
- Removed `from asgiref.sync import sync_to_async`
- All 5 `@sync_to_async` functions became plain `async def` with `aget()`, `asave()`, `async for`
- `_list_threads` now uses `aresolve_workspace` (already existed in `workspace_resolver.py`) instead of the sync `resolve_workspace`
- `_get_thread_artifacts` uses `async for` comprehension

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_chat_csrf.py -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 3: Commit**

```
git add apps/chat/thread_views.py
git commit -m "Convert all thread_views.py helpers to native async ORM"
```

---

### Task 6: Convert `apps/workspaces/services/workspace_service.py` — `touch_workspace_schemas`

**Files:**
- Modify: `apps/workspaces/services/workspace_service.py:60-85`

- [ ] **Step 1: Rewrite `touch_workspace_schemas` to use `atouch()`**

Replace lines 60-85 with:

```python
async def touch_workspace_schemas(workspace) -> None:
    """Reset the inactivity TTL for active schemas associated with a workspace.

    For single-tenant workspaces, touches the TenantSchema of the sole tenant.
    For multi-tenant workspaces, touches the WorkspaceViewSchema.
    """
    from apps.workspaces.models import TenantSchema

    tenant_count = await workspace.workspace_tenants.acount()
    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        ts = await TenantSchema.objects.filter(
            tenant=tenant,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if ts is not None:
            await ts.atouch()
    elif tenant_count > 1:
        vs = await WorkspaceViewSchema.objects.filter(
            workspace=workspace,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if vs is not None:
            await vs.atouch()
```

This removes the `sync_to_async` import that was inside the function body.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/ -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 3: Commit**

```
git add apps/workspaces/services/workspace_service.py
git commit -m "Use atouch() in touch_workspace_schemas, remove sync_to_async"
```

---

### Task 7: Convert `mcp_server/context.py` — remove `_parse_db_url` wrapper, use `atouch()`

**Files:**
- Modify: `mcp_server/context.py`

- [ ] **Step 1: Rewrite `load_tenant_context` and `load_workspace_context`**

In `load_tenant_context` (line 43), make these changes:
1. Remove `from asgiref.sync import sync_to_async` (line 52)
2. Replace `await sync_to_async(ts.touch)()` with `await ts.atouch()`
3. Replace `await sync_to_async(_parse_db_url)(url, ts.schema_name)` with `_parse_db_url(url, ts.schema_name)` (no await — it's a pure function)

In `load_workspace_context` (line 84), make these changes:
1. Remove `from asgiref.sync import sync_to_async` (line 93)
2. Replace `await sync_to_async(vs.touch)()` with `await vs.atouch()`
3. Replace `await sync_to_async(_parse_db_url)(url, vs.schema_name)` with `_parse_db_url(url, vs.schema_name)` (no await)

The complete `load_tenant_context`:

```python
async def load_tenant_context(tenant_id: str) -> QueryContext:
    """Load a QueryContext for a tenant from the managed database.

    Uses the tenant_id (domain name) to find the TenantSchema and builds
    a QueryContext pointing at the managed DB with the tenant's schema.
    Resets the schema's inactivity TTL via atouch().

    Raises ValueError if the tenant schema is not found or not active.
    """
    from django.conf import settings

    from apps.workspaces.models import SchemaState, TenantSchema

    ts = await TenantSchema.objects.filter(
        tenant__external_id=tenant_id,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()

    if ts is None:
        raise ValueError(
            f"No active schema for tenant '{tenant_id}'. Run materialization first to load data."
        )

    await ts.atouch()

    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise ValueError("MANAGED_DATABASE_URL is not configured")

    connection_params = _parse_db_url(url, ts.schema_name)

    return QueryContext(
        tenant_id=tenant_id,
        schema_name=ts.schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params=connection_params,
    )
```

The complete `load_workspace_context`:

```python
async def load_workspace_context(workspace_id: str) -> QueryContext:
    """Load a QueryContext for a workspace, routing correctly for multi-tenant.

    - Single-tenant workspace (1 tenant): delegates to load_tenant_context(tenant.external_id).
    - Multi-tenant workspace (2+ tenants): uses the WorkspaceViewSchema.

    Raises ValueError if the workspace has no tenants, or if multi-tenant and
    no active WorkspaceViewSchema exists.
    """
    from django.conf import settings

    from apps.workspaces.models import SchemaState, Workspace, WorkspaceViewSchema

    try:
        workspace = await Workspace.objects.aget(id=workspace_id)
    except Workspace.DoesNotExist:
        raise ValueError(f"Workspace '{workspace_id}' not found") from None

    tenant_count = await workspace.tenants.acount()

    if tenant_count == 0:
        raise ValueError(f"Workspace '{workspace_id}' has no tenants")

    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        return await load_tenant_context(tenant.external_id)

    # Multi-tenant: use the view schema
    try:
        vs = await WorkspaceViewSchema.objects.aget(
            workspace_id=workspace_id,
            state=SchemaState.ACTIVE,
        )
    except WorkspaceViewSchema.DoesNotExist:
        raise ValueError(
            f"No active view schema for workspace '{workspace_id}'. "
            "Trigger a rebuild via POST /api/workspaces/<id>/tenants/ or a data refresh."
        ) from None

    await vs.atouch()

    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise ValueError("MANAGED_DATABASE_URL is not configured")

    connection_params = _parse_db_url(url, vs.schema_name)

    return QueryContext(
        tenant_id=workspace_id,
        schema_name=vs.schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params=connection_params,
    )
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_mcp_workspace_context.py -x -v --timeout=30 2>&1 | tail -15`
Expected: PASS.

- [ ] **Step 3: Commit**

```
git add mcp_server/context.py
git commit -m "Remove sync_to_async from context.py: use atouch(), call _parse_db_url directly"
```

---

### Task 8: Convert `apps/agents/mcp_client.py` — `_get_tokens_sync` to async ORM

**Files:**
- Modify: `apps/agents/mcp_client.py:85-101`

- [ ] **Step 1: Rewrite token retrieval as async**

Replace the `get_user_oauth_tokens` and `_get_tokens_sync` functions (lines 85-101) with:

```python
async def get_user_oauth_tokens(user) -> dict[str, str]:
    """Retrieve OAuth tokens for a user's CommCare providers."""
    if user is None or not getattr(user, "pk", None):
        return {}
    return {
        st.account.provider: st.token
        async for st in SocialToken.objects.filter(
            account__user=user,
            account__provider__in=COMMCARE_PROVIDERS,
        ).select_related("account")
        if st.account.provider in COMMCARE_PROVIDERS
    }
```

Also remove `from asgiref.sync import sync_to_async` from the imports (line 15).

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_oauth_tokens.py -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS (tests use `async_to_sync` wrapper which still works).

- [ ] **Step 3: Commit**

```
git add apps/agents/mcp_client.py
git commit -m "Convert get_user_oauth_tokens to async ORM, remove sync_to_async"
```

---

### Task 9: Convert `apps/users/views.py` — token lookups, delete, save_credential

**Files:**
- Modify: `apps/users/views.py`

- [ ] **Step 1: Convert `_get_commcare_token` and `_get_connect_token` to async**

Replace the two functions (lines 27-46) with:

```python
async def _get_commcare_token(user) -> str | None:
    """Return the user's CommCare OAuth access token, or None."""
    token = (
        await SocialToken.objects.filter(
            account__user=user,
            account__provider__startswith="commcare",
        )
        .exclude(account__provider__startswith="commcare_connect")
        .afirst()
    )
    return token.token if token else None


async def _get_connect_token(user) -> str | None:
    """Return the user's Connect OAuth access token, or None."""
    token = await SocialToken.objects.filter(
        account__user=user,
        account__provider="commcare_connect",
    ).afirst()
    return token.token if token else None
```

- [ ] **Step 2: Update callers in `tenant_list_view`**

In `tenant_list_view` (around lines 62-80), remove `sync_to_async` wrappers:

Change `await sync_to_async(_get_commcare_token)(user)` to `await _get_commcare_token(user)`.
Change `await sync_to_async(_get_connect_token)(user)` to `await _get_connect_token(user)`.

Note: `resolve_commcare_domains` and `resolve_connect_opportunities` are still sync (they use `requests` — Phase 2 work), so keep their `sync_to_async` wrappers.

- [ ] **Step 3: Convert `_delete` in `tenant_credential_detail_view`**

Replace the `_delete` inner function + call (lines 235-243) with:

```python
        try:
            tm = await TenantMembership.objects.aget(id=membership_id, user=user)
        except TenantMembership.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
        await tm.adelete()  # cascades to TenantCredential
        return JsonResponse({"status": "deleted"})
```

Remove the `deleted = await sync_to_async(_delete)()` / `if not deleted:` pattern.

- [ ] **Step 4: Convert `_save_credential` in `tenant_credential_detail_view`**

Replace lines 291-296:

```python
    tm.credential.encrypted_credential = encrypted
    await tm.credential.asave(update_fields=["encrypted_credential"])
```

Remove the `_save_credential` inner function.

- [ ] **Step 5: Remove `encrypt_credential` / `decrypt_credential` wrappers**

In `tenant_credential_list_view` (around line 199):
Change `encrypted = await sync_to_async(encrypt_credential)(credential)` to `encrypted = encrypt_credential(credential)`.

In `tenant_credential_detail_view` (around line 287):
Change `encrypted = await sync_to_async(encrypt_credential)(credential)` to `encrypted = encrypt_credential(credential)`.

- [ ] **Step 6: Update `tenant_ensure_view` callers**

In `tenant_ensure_view` (around line 329):
Change `await sync_to_async(_get_connect_token)(user)` to `await _get_connect_token(user)`.

- [ ] **Step 7: Clean up imports**

Check if `sync_to_async` is still needed in the file (it is — for `verify_commcare_credential` and `resolve_*` calls which are Phase 2 work). Keep the import but verify no stale references.

- [ ] **Step 8: Run tests**

Run: `uv run pytest tests/ -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 9: Commit**

```
git add apps/users/views.py
git commit -m "Convert ORM helpers in users/views.py to async, remove unnecessary crypto wrappers"
```

---

### Task 10: Convert `mcp_server/server.py` — `run.save()` and `decrypt_credential`

**Files:**
- Modify: `mcp_server/server.py`

- [ ] **Step 1: Convert `run.save()` in `cancel_materialization`**

At line 458, replace:
```python
await sync_to_async(run.save)(update_fields=["state", "completed_at", "result"])
```
with:
```python
await run.asave(update_fields=["state", "completed_at", "result"])
```

- [ ] **Step 2: Remove `decrypt_credential` wrapper in `_materialize_tenant`**

At line 517, replace:
```python
decrypted = await sync_to_async(decrypt_credential)(cred_obj.encrypted_credential)
```
with:
```python
decrypted = decrypt_credential(cred_obj.encrypted_credential)
```

- [ ] **Step 3: Check remaining sync_to_async uses in server.py**

The remaining `sync_to_async` calls in server.py are for:
- `pipeline_list_tables`, `pipeline_describe_table`, `pipeline_get_metadata`, `workspace_list_tables` — blocked by psycopg sync (Phase 4)
- `_resolve_oauth_credential` — blocked by sync `requests` (Phase 2)
- `run_pipeline` — correct as-is (heavy sync orchestrator)
- `SchemaManager.teardown` / `teardown_view_schema` — correct as-is (psycopg DDL)
- `get_lineage_chain` — Task 11 below

Keep `from asgiref.sync import sync_to_async` in the top-level import — it's still needed.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_mcp_tenant_tools.py -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 5: Commit**

```
git add mcp_server/server.py
git commit -m "Use asave() and remove decrypt_credential wrapper in mcp_server"
```

---

### Task 11: Convert `apps/artifacts/views.py` — tenant lookup and touch

**Files:**
- Modify: `apps/artifacts/views.py:713-756`

- [ ] **Step 1: Replace FK traversal and touch**

At line 737, replace:
```python
tenant = await sync_to_async(lambda: artifact.workspace.tenant)()
```
with:
```python
tenant = await artifact.workspace.tenants.afirst()
```

This avoids the lazy FK traversal by using the async M2M query.

At line 756, replace:
```python
await sync_to_async(ts.touch)()
```
with:
```python
await ts.atouch()
```

Remove the `from asgiref.sync import sync_to_async` import at line 713.

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/ -x -q --timeout=30 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 3: Commit**

```
git add apps/artifacts/views.py
git commit -m "Convert artifact query data view to async ORM, use atouch()"
```

---

### Task 12: Convert `apps/agents/graph/base.py` — partial async for ORM-only calls

**Files:**
- Modify: `apps/agents/graph/base.py:148-234`
- Modify: `tests/agents/test_schema_context.py`

- [ ] **Step 1: Convert `_fetch_schema_context` ORM-only calls**

In `_fetch_schema_context`, the `sync_to_async` calls wrap two categories:
1. **ORM-only**: `get_terminal_assets` — can be converted
2. **Psycopg-dependent**: `pipeline_list_tables`, `pipeline_describe_table`, `transformation_aware_list_tables` — keep wrapped

Replace lines 179-187 with:

```python
    from apps.transformations.services.lineage import aget_terminal_assets

    terminal_assets = await aget_terminal_assets(tenant_ids=[tenant.id])

    if terminal_assets:
        tables = await sync_to_async(transformation_aware_list_tables)(
            ts, pipeline_config, tenant_ids=[tenant.id]
        )
    else:
        tables = await sync_to_async(pipeline_list_tables)(ts, pipeline_config)
```

And at line 205:
```python
            detail = await sync_to_async(pipeline_describe_table)(
```
Keep this one as-is — it depends on psycopg.

- [ ] **Step 2: Add `aget_terminal_assets` and `aget_lineage_chain` to lineage.py**

In `apps/transformations/services/lineage.py`, add async versions:

```python
async def aget_terminal_assets(
    tenant_ids: list,
    workspace_id=None,
) -> list[TransformationAsset]:
    """Async version of get_terminal_assets."""
    visible = TransformationAsset.objects.filter(tenant_id__in=tenant_ids)
    if workspace_id:
        visible = visible | TransformationAsset.objects.filter(workspace_id=workspace_id)

    replaced_ids = visible.filter(replaces__isnull=False).values_list("replaces_id", flat=True)

    return [asset async for asset in visible.exclude(id__in=replaced_ids)]


async def aget_lineage_chain(
    asset_name: str,
    tenant_ids: list,
    workspace_id=None,
) -> list[dict]:
    """Async version of get_lineage_chain."""
    q = models.Q(tenant_id__in=tenant_ids)
    if workspace_id:
        q = q | models.Q(workspace_id=workspace_id)

    try:
        asset = await TransformationAsset.objects.aget(q, name=asset_name)
    except TransformationAsset.DoesNotExist:
        return []
    except TransformationAsset.MultipleObjectsReturned:
        asset = await TransformationAsset.objects.filter(q, name=asset_name).order_by("-scope").afirst()

    chain = []
    current = asset
    visited = set()
    while current and current.id not in visited:
        visited.add(current.id)
        chain.append(
            {
                "name": current.name,
                "scope": current.scope,
                "description": current.description,
            }
        )
        next_id = current.replaces_id
        if next_id is None:
            break
        current = await TransformationAsset.objects.filter(q, id=next_id).afirst()

    return chain
```

- [ ] **Step 3: Update `mcp_server/server.py` to use `aget_lineage_chain`**

In `mcp_server/server.py`, at line 291, replace:
```python
        chain = await sync_to_async(get_lineage_chain)(
            model_name, tenant_ids=tenant_ids, workspace_id=workspace_id
        )
```
with:
```python
        from apps.transformations.services.lineage import aget_lineage_chain

        chain = await aget_lineage_chain(
            model_name, tenant_ids=tenant_ids, workspace_id=workspace_id
        )
```

Also remove `get_lineage_chain` from the import at line 275 if it was imported there (it's imported inside the function body, so just update that import).

- [ ] **Step 4: Update tests**

In `tests/agents/test_schema_context.py`, the tests mock `sync_to_async` globally. After this change, `get_terminal_assets` is called via `aget_terminal_assets` (no `sync_to_async`). Update the test patches:

For `test_fetch_schema_context_active_compact` (line 80):
- Replace `patch("apps.transformations.services.lineage.get_terminal_assets", return_value=[])` with `patch("apps.agents.graph.base.aget_terminal_assets", new=AsyncMock(return_value=[]))`
- Adjust `mock_s2a.side_effect` — remove the first entry (was for `get_terminal_assets`), keep only `pipeline_list_tables`:
```python
mock_s2a.side_effect = [
    AsyncMock(return_value=mock_tables),  # pipeline_list_tables
]
```

Apply the same pattern to `test_fetch_schema_context_active_full` and `test_fetch_schema_context_no_get_schema_status_instruction`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/agents/test_schema_context.py -x -v --timeout=30 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 6: Commit**

```
git add apps/transformations/services/lineage.py apps/agents/graph/base.py mcp_server/server.py tests/agents/test_schema_context.py
git commit -m "Add async lineage functions, convert ORM-only calls in agent graph"
```

---

### Task 13: Final verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -x -q --timeout=60 2>&1 | tail -15`
Expected: All tests PASS.

- [ ] **Step 2: Run linting**

Run: `uv run ruff check apps/ mcp_server/ tests/ 2>&1 | tail -20`
Expected: No errors (or only pre-existing ones).

- [ ] **Step 3: Audit remaining sync_to_async usage**

Run: `grep -rn 'sync_to_async\|async_to_sync' apps/ mcp_server/ --include='*.py' | grep -v __pycache__ | grep -v '.pyc'`

Expected remaining uses (all legitimate):
- `mcp_server/server.py` — psycopg-dependent metadata functions, `run_pipeline`, `SchemaManager`, `_resolve_oauth_credential`
- `apps/users/views.py` — `verify_commcare_credential`, `resolve_commcare_domains`, `resolve_connect_opportunities` (sync HTTP — Phase 2)
- `apps/recipes/services/runner.py` — `async_to_sync` for sync entry point (correct)

- [ ] **Step 4: Commit all remaining changes**

If any files were missed, stage and commit them.
