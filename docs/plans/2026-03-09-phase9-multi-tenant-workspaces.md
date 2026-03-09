# Phase 9: Multi-Tenant Workspaces Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable a single Scout workspace to query data from multiple tenants via a PostgreSQL view schema that unions their individual schemas, and route the agent's MCP tool calls through workspace_id rather than tenant_id.

**Architecture:** A `WorkspaceViewSchema` model tracks a PostgreSQL schema (`ws_<uuid_prefix>`) containing `CREATE OR REPLACE VIEW` definitions that UNION ALL the corresponding tables from each tenant's schema. The view adds a `_tenant` discriminator column. The MCP server gains a `load_workspace_context(workspace_id)` path that returns either the single-tenant schema (for single-tenant workspaces, unchanged) or the view schema (for multi-tenant). The agent injects `workspace_id` alongside `tenant_id` so tools can route correctly. Single-tenant workspaces remain unaffected.

**Tech Stack:** Django 5, DRF, PostgreSQL, psycopg, Celery, FastMCP, LangGraph

---

## Design Decisions

Read before touching any code.

### View schema naming

`ws_` + first 16 hex chars of workspace UUID (no hyphens). Example: workspace `a1b2c3d4-e5f6-7890-abcd-ef1234567890` → schema name `ws_a1b2c3d4e5f67890`. Always sanitize with a regex check before embedding in SQL.

### Column alignment in UNION ALL views

For each table appearing in one or more tenant schemas:
1. Collect all column names across all tenant schemas that contain this table (ordered: columns from first schema first, new columns from subsequent schemas appended in `ordinal_position` order).
2. Add `_tenant TEXT` as the last column.
3. For each tenant schema:
   - If the table exists: `SELECT col1, col2, NULL AS missing_col, 'tenant_a' AS _tenant FROM schema_a.table`
   - If the table does not exist in that tenant's schema: skip it in the UNION (do not pad with all-NULL rows — only include schemas that actually have the table).
4. Use `CREATE OR REPLACE VIEW` for idempotency.

**Why skip missing tables instead of all-NULL rows?** A UNION member with all NULLs for real columns is misleading and produces no analytical value. If a table only exists in one tenant schema it is still queryable; it just shows data from one tenant.

### Recovery when view schema is expired

The `rebuild_workspace_view_schema` Celery task checks all tenant schemas:
- If all are ACTIVE → rebuild the views and mark `WorkspaceViewSchema.state = ACTIVE`.
- If any are expired → return an error. The recovery that re-fetches expired tenant schemas first is **deferred** (requires knowing which TenantMembership to use for each tenant; that's added complexity best done after the base implementation works). For now, users trigger a per-tenant data refresh first, then the view schema auto-rebuilds.

### Agent graph: single-tenant workspaces stay unchanged

`_resolve_workspace_and_membership` in `chat/views.py` currently returns `(workspace, tenant_membership)`. For single-tenant workspaces it works as-is. For multi-tenant workspaces it returns `(workspace, None)` (because `workspace.tenant` only returns the first tenant and the membership lookup is unreliable).

**Fix:** When `workspace.tenants.count() > 1`, return `(workspace, None)` intentionally and let the chat view handle it by building the agent graph with `workspace_id` routing instead of `tenant_membership` routing.

### `AgentState` backward compatibility

Add `workspace_id: str` to `AgentState`. The injecting node injects it alongside `tenant_id`. MCP tools accept `workspace_id` as an optional parameter; when non-empty, they use `load_workspace_context(workspace_id)` and ignore `tenant_id`.

---

## Review Amendments

> These amendments were agreed during a pre-implementation review. They **override or extend** the task steps below. Read this section before any task.

### Amendment A — Add UUID PK to `WorkspaceTenant` (affects Task 9.1 and 9.5)

`WorkspaceTenant` uses Django's default integer AutoField. The tenant delete URL (`DELETE /api/workspaces/<workspace_id>/tenants/<wt_id>/`) uses `<uuid:wt_id>`, which would never match an integer PK.

**Fix:** In Task 9.1 (model additions), also add a UUID PK to `WorkspaceTenant`:

```python
class WorkspaceTenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)  # ADD THIS
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="workspace_tenants"
    )
    tenant = models.ForeignKey(
        "users.Tenant", on_delete=models.CASCADE, related_name="workspace_tenants"
    )

    class Meta:
        unique_together = [["workspace", "tenant"]]
```

Generate a single migration that includes both `WorkspaceViewSchema` and the UUID PK addition to `WorkspaceTenant`.

---

### Amendment B — Fix `_resolve_workspace_and_membership` for multi-tenant (new sub-task before Task 9.8)

The plan's Design Decision says "when `workspace.tenants.count() > 1`, return `(workspace, None)` intentionally." But the function is never modified to do this. Currently it calls `workspace.tenant` (returns `tenants.first()`) and finds a `TenantMembership` for the first tenant — returning a non-None membership even for multi-tenant workspaces.

**Add a new sub-task 9.7.5** (between Task 9.7 and 9.8):

**Files:**
- Modify: `apps/chat/views.py`

**Step 1: Write the failing tests**

```python
# tests/test_resolve_workspace_membership.py
import pytest
from django.contrib.auth import get_user_model
from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.users.models import Tenant, TenantMembership

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="resolve@example.com", password="pass")


@pytest.fixture
def single_tenant_workspace(db, user):
    t = Tenant.objects.create(provider="commcare", external_id="single-domain", canonical_name="Single")
    ws = Workspace.objects.create(name="Single WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t)
    TenantMembership.objects.create(user=user, tenant=t)
    return ws, t


@pytest.fixture
def multi_tenant_workspace(db, user):
    t1 = Tenant.objects.create(provider="commcare", external_id="mt-domain-1", canonical_name="MT1")
    t2 = Tenant.objects.create(provider="commcare", external_id="mt-domain-2", canonical_name="MT2")
    ws = Workspace.objects.create(name="Multi WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t2)
    TenantMembership.objects.create(user=user, tenant=t1)
    TenantMembership.objects.create(user=user, tenant=t2)
    return ws, t1, t2


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_single_tenant_workspace_returns_membership(user, single_tenant_workspace):
    from apps.chat.views import _resolve_workspace_and_membership
    ws, t = single_tenant_workspace
    workspace, tm = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert tm is not None
    assert tm.tenant.external_id == "single-domain"


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_multi_tenant_workspace_returns_none_membership(user, multi_tenant_workspace):
    """Multi-tenant workspaces must return None for tenant_membership so routing uses workspace_id."""
    from apps.chat.views import _resolve_workspace_and_membership
    ws, t1, t2 = multi_tenant_workspace
    workspace, tm = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert tm is None  # critical: must be None even though user has TenantMembership for both tenants
```

Run: `uv run pytest tests/test_resolve_workspace_membership.py -v`
Expected: FAIL — `_resolve_workspace_and_membership` currently returns a non-None membership for multi-tenant.

**Step 2: Fix `_resolve_workspace_and_membership`**

In `apps/chat/views.py`, update the `_resolve_workspace_and_membership` function:

```python
@sync_to_async
def _resolve_workspace_and_membership(user, workspace_id):
    """Resolve Workspace and TenantMembership from workspace_id.

    Returns (workspace, tenant_membership) or (None, None) if access denied.
    For multi-tenant workspaces (2+ tenants), always returns (workspace, None)
    so the chat view routes via workspace_id instead.
    """
    from apps.projects.models import WorkspaceMembership

    try:
        wm = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None

    workspace = wm.workspace

    # Multi-tenant workspaces route via workspace_id — no single TenantMembership applies
    if workspace.workspace_tenants.count() > 1:
        return workspace, None

    from apps.users.models import TenantMembership

    tenant = workspace.tenant  # single-tenant compat
    if tenant is None:
        return workspace, None
    try:
        tm = TenantMembership.objects.select_related("tenant").get(user=user, tenant=tenant)
    except TenantMembership.DoesNotExist:
        return workspace, None
    return workspace, tm
```

**Step 3: Run tests**

```bash
uv run pytest tests/test_resolve_workspace_membership.py -v
```

Expected: all pass.

**Step 4: Commit**

```bash
git add apps/chat/views.py tests/test_resolve_workspace_membership.py
git commit -m "fix: _resolve_workspace_and_membership returns None membership for multi-tenant workspaces"
```

---

### Amendment C — `build_agent_graph` must use `Workspace` as primary object (affects Task 9.8)

`build_agent_graph` currently creates a `TenantWorkspace` (legacy 1:1 per-tenant model) from `tenant_membership.tenant`. For multi-tenant workspaces, `tenant_membership` is `None` — this crashes at line 298. The non-MCP tools (`save_learning`, `create_artifact`, `recipe`) also take `TenantWorkspace`.

**What Task 9.8 must do (in addition to the existing steps):**

1. Update `build_agent_graph` signature to accept `workspace: Workspace` directly (passed from `chat_view`, which already has it):

```python
async def build_agent_graph(
    workspace: Workspace,            # NEW: user-facing Workspace (not TenantWorkspace)
    tenant_membership=None,          # None for multi-tenant workspaces
    user=None,
    checkpointer=None,
    mcp_tools=None,
    oauth_tokens=None,
    workspace_id: str = "",
):
```

2. Remove the `TenantWorkspace.objects.aget_or_create(...)` call. The agent's system_prompt and data_dictionary come from `workspace` (the `Workspace` model) directly — this also fixes a pre-existing bug where `Workspace.system_prompt` (what the PATCH endpoint updates) was being ignored by the agent.

3. Update `_build_tools(workspace, user, mcp_tools)` so the `workspace` parameter is now a `Workspace` instance (not `TenantWorkspace`). Update `create_save_learning_tool`, `create_artifact_tools`, `create_recipe_tool` to accept `Workspace`.

4. Update all logger calls that reference `tenant_membership.tenant.external_id` to handle `None`:
   ```python
   tenant_label = tenant_membership.tenant.external_id if tenant_membership else f"workspace:{workspace.id}"
   ```

5. Update `chat_view` call site to pass `workspace` explicitly:
   ```python
   agent = await build_agent_graph(
       workspace=workspace,
       tenant_membership=tenant_membership,
       ...
       workspace_id=str(workspace.id),
   )
   ```

6. Update `_build_system_prompt` to accept `workspace: Workspace` instead of `TenantWorkspace`, reading `workspace.system_prompt` from the `Workspace` model.

---

### Amendment D — `on_commit` in Celery tasks is misleading; remove it (affects Task 9.3, 9.4)

Celery tasks run in Django's autocommit mode (no open transaction). `transaction.on_commit(callback)` fires **immediately** when there's no active transaction — making it a confusing no-op wrapper in tasks.

**Fix in Task 9.3 (`rebuild_workspace_view_schema` task):** Call `teardown_view_schema_task.delay(...)` directly without `on_commit`:

```python
# In expire_inactive_schemas, replace:
transaction.on_commit(lambda vid=view_id: teardown_view_schema_task.delay(vid))
# With:
teardown_view_schema_task.delay(view_id)
```

Same for `teardown_schema.delay(sid)` in the existing `expire_inactive_schemas` (cleanup of pre-existing pattern, optional but recommended for clarity).

**Keep `transaction.on_commit` in API views** (`WorkspaceTenantView.post/delete`) — correct protection if `ATOMIC_REQUESTS=True`.

**Tests that assert `.delay()` was called after `on_commit`** must use `@pytest.mark.django_db(transaction=True)` so the callback fires.

---

### Amendment E — `build_view_schema` must clean up partial schema on failure (affects Task 9.2)

With `autocommit=True`, a failure mid-loop leaves partial views in the schema. The `except` block must drop the schema before setting FAILED:

```python
except Exception:
    # Drop the partial schema immediately before marking FAILED
    try:
        if not conn.closed:
            c = conn.cursor()
            c.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(view_schema_name)
                )
            )
            c.close()
    except Exception:
        logger.exception("Failed to drop partial view schema '%s' during cleanup", view_schema_name)
    if not conn.closed:
        conn.close()
    vs.state = SchemaState.FAILED
    vs.save(update_fields=["state"])
    raise
finally:
    if not conn.closed:
        conn.close()
```

---

### Amendment F — Remove redundant pre-check from `rebuild_workspace_view_schema` task (affects Task 9.3)

The task pre-checks all tenant schemas are ACTIVE before calling `build_view_schema`. `build_view_schema` performs the same check and raises `ValueError`. The duplication creates two error paths for one condition.

**Remove the `for tenant in tenants:` pre-check loop from the task.** Let `build_view_schema` be the single source of truth. The `except Exception` block at the end already catches the `ValueError`.

---

### Amendment G — URL registration in `config/urls.py` (affects Task 9.5)

The plan says to add tenant endpoints to `apps/projects/api/urls.py`. Instead, register them directly in `config/urls.py` inside `workspace_urlpatterns`, consistent with how member endpoints are registered:

```python
# In config/urls.py workspace_urlpatterns:
path("tenants/", WorkspaceTenantView.as_view(), name="workspace_tenants"),
path("tenants/<uuid:wt_id>/", WorkspaceTenantView.as_view(), name="workspace_tenant_detail"),
```

Import `WorkspaceTenantView` at the top of `config/urls.py` alongside the existing workspace view imports.

---

### Amendment H — Set `WorkspaceViewSchema.state = PROVISIONING` synchronously on add/remove (affects Task 9.5)

After creating/deleting the `WorkspaceTenant` record but before dispatching the rebuild task, update the existing view schema (if any) to `PROVISIONING`:

```python
# In WorkspaceTenantView.post() after wt is created:
WorkspaceViewSchema.objects.filter(workspace=workspace).update(state=SchemaState.PROVISIONING)
transaction.on_commit(lambda: rebuild_workspace_view_schema.delay(workspace_id_str))
```

Same in `delete()`. This ensures `schema_status` shows "provisioning" immediately after add/remove rather than a stale "available."

---

### Amendment I — `load_workspace_context` calls `vs.touch()` internally (affects Task 9.6)

Instead of calling `vs.touch()` in each MCP tool after context load, call it inside `load_workspace_context` after finding the active `WorkspaceViewSchema`. Any context load resets the TTL automatically — no chance of forgetting it in a future tool.

```python
# In load_workspace_context, after vs = await WorkspaceViewSchema.objects.aget(...):
await sync_to_async(vs.touch)()
return QueryContext(...)
```

Remove the per-tool `vs.touch()` call from Task 9.7 `query` tool.

---

### Amendment J — Fix `WorkspaceDetailView.schema_status` for multi-tenant (new step in Task 9.5 or standalone)

Add a `WorkspaceViewSchema` state check to `WorkspaceDetailView.get()` for multi-tenant workspaces. Without this, a multi-tenant workspace shows "available" even when the view schema hasn't been built yet.

```python
# In WorkspaceDetailView.get(), after computing schema_status:
if len(tenants) > 1:
    try:
        vs = workspace.view_schema  # OneToOne accessor
        if vs.state == SchemaState.ACTIVE:
            schema_status = "available"
        else:
            schema_status = "provisioning"
    except WorkspaceViewSchema.DoesNotExist:
        schema_status = "provisioning"
```

Import `WorkspaceViewSchema` at the top of the view file.

---

### Amendment K — Test fixes (affects all test files)

1. **Async DB tests** (`test_mcp_workspace_context.py`, `test_resolve_workspace_membership.py`): Use `@pytest.mark.django_db` decorator instead of `db` fixture for async test functions.

2. **Dispatch tests** (`test_view_schema_ttl.py`, `test_multitenant_smoke.py`, new tests in `test_workspace_tenant_api.py`): Add `@pytest.mark.django_db(transaction=True)` so `transaction.on_commit` callbacks fire.

3. **Integration test cleanup** (`test_view_schema_builder.py`): Wrap all schema cleanup in `try/finally` so it runs even if assertions fail.

4. **`test_injecting_node_includes_workspace_id`**: Convert to `async def` with `@pytest.mark.asyncio` and `await node(state)` instead of `asyncio.get_event_loop().run_until_complete()`.

5. **New tests for `_resolve_workspace_and_membership`**: Added in Amendment B above.

6. **New dispatch tests for `test_workspace_tenant_api.py`**: Add tests verifying `rebuild_workspace_view_schema.delay()` is called on both add and remove operations (with `transaction=True`).

---

## Prerequisites Check

Before starting, confirm Phases 1–4 are merged and passing:

```bash
uv run pytest -v
```

Expected: all tests pass.

---

## Task 9.1: WorkspaceViewSchema model and migration

**Files:**
- Modify: `apps/projects/models.py`
- Create: migration (auto-generated)

### Step 1: Write the failing test

```python
# tests/test_workspace_view_schema.py
import pytest
from apps.projects.models import SchemaState, Workspace, WorkspaceTenant, WorkspaceViewSchema
from apps.users.models import Tenant


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )


@pytest.fixture
def tenant2(db):
    return Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other Domain"
    )


@pytest.fixture
def workspace(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.create_user(email="user@example.com", password="pass")
    from apps.projects.models import WorkspaceMembership, WorkspaceRole
    ws = Workspace.objects.create(name="Multi-Tenant WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    return ws


def test_workspace_view_schema_can_be_created(workspace):
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_abc123",
        state=SchemaState.PROVISIONING,
    )
    assert vs.id is not None
    assert vs.schema_name == "ws_abc123"
    assert vs.state == SchemaState.PROVISIONING
    assert vs.last_accessed_at is None


def test_workspace_view_schema_touch_updates_last_accessed_at(workspace):
    import freezegun
    from django.utils import timezone
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_touch_test",
        state=SchemaState.ACTIVE,
    )
    with freezegun.freeze_time("2026-01-01 12:00:00"):
        vs.touch()
    vs.refresh_from_db()
    assert vs.last_accessed_at == timezone.datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_workspace_view_schema_is_one_to_one(workspace):
    from django.db import IntegrityError
    WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_first",
        state=SchemaState.PROVISIONING,
    )
    with pytest.raises(IntegrityError):
        WorkspaceViewSchema.objects.create(
            workspace=workspace,
            schema_name="ws_second",
            state=SchemaState.PROVISIONING,
        )
```

Run: `uv run pytest tests/test_workspace_view_schema.py -v`
Expected: FAIL — `WorkspaceViewSchema` does not exist.

### Step 2: Add the model

Add to `apps/projects/models.py` after the `WorkspaceMembership` class:

```python
class WorkspaceViewSchema(models.Model):
    """Tracks the PostgreSQL view schema for a multi-tenant workspace.

    For workspaces with 2+ tenants, a physical PostgreSQL schema is created
    containing UNION ALL views that merge the per-tenant schemas.
    """

    workspace = models.OneToOneField(
        Workspace,
        on_delete=models.CASCADE,
        related_name="view_schema",
    )
    schema_name = models.CharField(max_length=255, unique=True)
    state = models.CharField(
        max_length=20,
        choices=SchemaState.choices,
        default=SchemaState.PROVISIONING,
    )
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ViewSchema({self.schema_name}, {self.state})"

    def touch(self):
        """Reset the inactivity TTL for this view schema."""
        from django.utils import timezone

        self.last_accessed_at = timezone.now()
        self.save(update_fields=["last_accessed_at"])
```

### Step 3: Generate migration

```bash
uv run python manage.py makemigrations projects --name workspace_view_schema
```

Expected output: `Migrations for 'projects': apps/projects/migrations/0021_workspace_view_schema.py`

### Step 4: Apply and verify

```bash
uv run python manage.py migrate
uv run pytest tests/test_workspace_view_schema.py -v
```

Expected: All tests pass.

### Step 5: Commit

```bash
git add apps/projects/models.py apps/projects/migrations/0021_workspace_view_schema.py tests/test_workspace_view_schema.py
git commit -m "feat: WorkspaceViewSchema model for multi-tenant view schemas"
```

---

## Task 9.2: SchemaManager.build_view_schema()

This is the core SQL engine: creates a PostgreSQL schema with UNION ALL views.

**Files:**
- Modify: `apps/projects/services/schema_manager.py`

### Step 1: Write the failing test

This test requires a real managed database connection. Use a pytest marker to skip when `MANAGED_DATABASE_URL` is not set.

```python
# tests/test_view_schema_builder.py
import os
import pytest
from apps.projects.models import SchemaState, Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.users.models import Tenant


pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"),
    reason="MANAGED_DATABASE_URL not set",
)


@pytest.fixture
def managed_db_connection():
    from apps.projects.services.schema_manager import get_managed_db_connection
    conn = get_managed_db_connection()
    yield conn
    conn.close()


@pytest.fixture
def two_tenant_workspace(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.create_user(email="builder@example.com", password="pass")
    t1 = Tenant.objects.create(provider="commcare", external_id="build-domain-a", canonical_name="A")
    t2 = Tenant.objects.create(provider="commcare", external_id="build-domain-b", canonical_name="B")
    ws = Workspace.objects.create(name="Build WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t2)
    return ws, t1, t2


def test_build_view_schema_creates_record(two_tenant_workspace, managed_db_connection):
    from apps.projects.models import TenantSchema, WorkspaceViewSchema
    from apps.projects.services.schema_manager import SchemaManager
    ws, t1, t2 = two_tenant_workspace

    # Create physical tenant schemas with a test table
    ts1 = TenantSchema.objects.create(tenant=t1, schema_name="build_domain_a_test", state=SchemaState.ACTIVE)
    ts2 = TenantSchema.objects.create(tenant=t2, schema_name="build_domain_b_test", state=SchemaState.ACTIVE)
    conn = managed_db_connection
    c = conn.cursor()
    c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_a_test")
    c.execute("CREATE TABLE IF NOT EXISTS build_domain_a_test.cases (id TEXT, name TEXT)")
    c.execute("INSERT INTO build_domain_a_test.cases VALUES ('1', 'Alice')")
    c.execute("CREATE SCHEMA IF NOT EXISTS build_domain_b_test")
    c.execute("CREATE TABLE IF NOT EXISTS build_domain_b_test.cases (id TEXT, name TEXT, status TEXT)")
    c.execute("INSERT INTO build_domain_b_test.cases VALUES ('2', 'Bob', 'active')")
    c.close()

    vs = SchemaManager().build_view_schema(ws)

    assert vs is not None
    assert vs.schema_name.startswith("ws_")
    assert WorkspaceViewSchema.objects.filter(workspace=ws).exists()

    # Verify the view exists and unions both tenants
    c2 = conn.cursor()
    c2.execute(f"SELECT id, name, _tenant FROM {vs.schema_name}.cases ORDER BY id")
    rows = c2.fetchall()
    c2.close()
    assert len(rows) == 2
    tenants_seen = {r[2] for r in rows}
    assert "build-domain-a" in tenants_seen
    assert "build-domain-b" in tenants_seen

    # Cleanup
    c3 = conn.cursor()
    c3.execute(f"DROP SCHEMA IF EXISTS {vs.schema_name} CASCADE")
    c3.execute("DROP SCHEMA IF EXISTS build_domain_a_test CASCADE")
    c3.execute("DROP SCHEMA IF EXISTS build_domain_b_test CASCADE")
    c3.close()
    vs.delete()
    ts1.delete()
    ts2.delete()
```

Run: `uv run pytest tests/test_view_schema_builder.py -v`
Expected: FAIL — `SchemaManager` has no `build_view_schema`.

### Step 2: Implement build_view_schema

Add to `SchemaManager` class in `apps/projects/services/schema_manager.py`:

```python
def _view_schema_name(self, workspace_id) -> str:
    """Generate a PostgreSQL schema name for a workspace's view schema."""
    hex_id = str(workspace_id).replace("-", "")[:16]
    return f"ws_{hex_id}"

def build_view_schema(self, workspace) -> "WorkspaceViewSchema":
    """Create (or replace) the PostgreSQL view schema for a multi-tenant workspace.

    Fetches all active TenantSchema objects for the workspace's tenants,
    collects their tables and columns, then creates UNION ALL views in a
    dedicated schema.  Raises ValueError if any tenant has no active schema.

    Returns the WorkspaceViewSchema model instance (state still PROVISIONING;
    caller is responsible for marking ACTIVE on success).
    """
    import re

    import psycopg.sql

    from apps.projects.models import SchemaState, TenantSchema, WorkspaceViewSchema

    tenants = list(workspace.tenants.all())
    if not tenants:
        raise ValueError(f"Workspace {workspace.id} has no tenants")

    # Resolve active TenantSchema for each tenant
    tenant_schemas: list[tuple[str, str]] = []  # (schema_name, tenant_external_id)
    for tenant in tenants:
        ts = TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).first()
        if ts is None:
            raise ValueError(
                f"Tenant '{tenant.external_id}' has no active schema. "
                "Run a data refresh for this tenant before building the view schema."
            )
        tenant_schemas.append((ts.schema_name, tenant.external_id))

    view_schema_name = self._view_schema_name(workspace.id)

    # Get or create the WorkspaceViewSchema record
    vs, _ = WorkspaceViewSchema.objects.get_or_create(
        workspace=workspace,
        defaults={"schema_name": view_schema_name, "state": SchemaState.PROVISIONING},
    )
    # Always update schema_name in case it changed (e.g. record recreated after teardown)
    if vs.schema_name != view_schema_name:
        vs.schema_name = view_schema_name
    vs.state = SchemaState.PROVISIONING
    vs.save(update_fields=["schema_name", "state"])

    conn = get_managed_db_connection()
    try:
        cursor = conn.cursor()

        # Validate schema name before embedding
        if not re.match(r"^ws_[a-f0-9]{16}$", view_schema_name):
            raise ValueError(f"Invalid view schema name: {view_schema_name!r}")

        # Step 1: Create the physical schema
        cursor.execute(
            psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                psycopg.sql.Identifier(view_schema_name)
            )
        )

        # Step 2: Collect tables and columns per tenant schema
        all_tables: dict[str, dict[str, list[str]]] = {}
        # all_tables[table_name][schema_name] = [col1, col2, ...]

        for schema_name, tenant_id in tenant_schemas:
            cursor.execute(
                "SELECT table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_schema = %s "
                "ORDER BY table_name, ordinal_position",
                (schema_name,),
            )
            for table_name, col_name in cursor.fetchall():
                all_tables.setdefault(table_name, {})
                all_tables[table_name].setdefault(schema_name, []).append(col_name)

        # Step 3: Build UNION ALL views
        for table_name, schema_cols in all_tables.items():
            # Union of all column names (preserving first-seen order)
            seen: set[str] = set()
            union_cols: list[str] = []
            for schema_name, _ in tenant_schemas:
                for col in schema_cols.get(schema_name, []):
                    if col not in seen:
                        union_cols.append(col)
                        seen.add(col)

            # Build one SELECT per tenant schema that has this table
            select_parts: list[psycopg.sql.Composed] = []
            for schema_name, tenant_external_id in tenant_schemas:
                if schema_name not in schema_cols:
                    continue  # This tenant doesn't have this table — skip
                existing = set(schema_cols[schema_name])
                select_list = []
                for col in union_cols:
                    if col in existing:
                        select_list.append(
                            psycopg.sql.SQL("{}.{}").format(
                                psycopg.sql.Identifier(table_name),
                                psycopg.sql.Identifier(col),
                            )
                        )
                    else:
                        select_list.append(
                            psycopg.sql.SQL("NULL AS {}").format(psycopg.sql.Identifier(col))
                        )
                select_list.append(
                    psycopg.sql.SQL("{} AS _tenant").format(psycopg.sql.Literal(tenant_external_id))
                )
                select_parts.append(
                    psycopg.sql.SQL("SELECT {} FROM {}.{}").format(
                        psycopg.sql.SQL(", ").join(select_list),
                        psycopg.sql.Identifier(schema_name),
                        psycopg.sql.Identifier(table_name),
                    )
                )

            if not select_parts:
                continue

            view_sql = psycopg.sql.SQL(
                "CREATE OR REPLACE VIEW {}.{} AS {}"
            ).format(
                psycopg.sql.Identifier(view_schema_name),
                psycopg.sql.Identifier(table_name),
                psycopg.sql.SQL(" UNION ALL ").join(select_parts),
            )
            cursor.execute(view_sql)

        cursor.close()
    except Exception:
        conn.close()
        vs.state = SchemaState.FAILED
        vs.save(update_fields=["state"])
        raise
    finally:
        if not conn.closed:
            conn.close()

    logger.info(
        "Built view schema '%s' for workspace '%s' (%d tenants, %d tables)",
        view_schema_name,
        workspace.id,
        len(tenant_schemas),
        len(all_tables),
    )
    return vs


def teardown_view_schema(self, view_schema: "WorkspaceViewSchema") -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema."""
    conn = get_managed_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(view_schema.schema_name)
            )
        )
        cursor.close()
    finally:
        conn.close()
```

You will also need to add the `WorkspaceViewSchema` import at the top of the method (it is already imported lazily inside the function body above).

### Step 3: Run test

```bash
uv run pytest tests/test_view_schema_builder.py -v
```

Expected: PASS (requires `MANAGED_DATABASE_URL` to be set). If skipped, proceed to next step.

### Step 4: Commit

```bash
git add apps/projects/services/schema_manager.py tests/test_view_schema_builder.py
git commit -m "feat: SchemaManager.build_view_schema() for multi-tenant UNION views"
```

---

## Task 9.3: rebuild_workspace_view_schema Celery task

**Files:**
- Modify: `apps/projects/tasks.py`

### Step 1: Write the failing test

```python
# tests/test_rebuild_view_schema_task.py
import pytest
from unittest.mock import patch, MagicMock
from apps.projects.models import (
    SchemaState, Workspace, WorkspaceMembership, WorkspaceRole,
    WorkspaceTenant, WorkspaceViewSchema,
)
from apps.users.models import Tenant


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(email="task@example.com", password="pass")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(provider="commcare", external_id="task-domain", canonical_name="Task Domain")


@pytest.fixture
def workspace(db, user, tenant):
    from apps.projects.models import TenantSchema
    ws = Workspace.objects.create(name="Task WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    TenantSchema.objects.create(tenant=tenant, schema_name="task_domain", state=SchemaState.ACTIVE)
    return ws


def test_rebuild_view_schema_calls_build_view_schema(workspace):
    from apps.projects.tasks import rebuild_workspace_view_schema

    with patch("apps.projects.tasks.SchemaManager") as MockSM:
        mock_vs = MagicMock()
        mock_vs.schema_name = "ws_abc123"
        MockSM.return_value.build_view_schema.return_value = mock_vs

        result = rebuild_workspace_view_schema(str(workspace.id))

    assert result["status"] == "active"
    mock_vs.save.assert_called_once()


def test_rebuild_view_schema_fails_if_no_active_tenant_schema(workspace):
    from apps.projects.models import TenantSchema
    from apps.projects.tasks import rebuild_workspace_view_schema

    TenantSchema.objects.filter(
        tenant__workspace_tenants__workspace=workspace
    ).update(state=SchemaState.EXPIRED)

    result = rebuild_workspace_view_schema(str(workspace.id))
    assert "error" in result


def test_rebuild_view_schema_marks_failed_on_exception(workspace):
    from apps.projects.tasks import rebuild_workspace_view_schema

    with patch("apps.projects.tasks.SchemaManager") as MockSM:
        MockSM.return_value.build_view_schema.side_effect = Exception("boom")

        result = rebuild_workspace_view_schema(str(workspace.id))

    assert "error" in result
    # WorkspaceViewSchema state should be FAILED (if it exists)
    try:
        vs = WorkspaceViewSchema.objects.get(workspace=workspace)
        assert vs.state == SchemaState.FAILED
    except WorkspaceViewSchema.DoesNotExist:
        pass  # acceptable — was never created
```

Run: `uv run pytest tests/test_rebuild_view_schema_task.py -v`
Expected: FAIL — `rebuild_workspace_view_schema` does not exist.

### Step 2: Implement the task

Add to `apps/projects/tasks.py`:

```python
@shared_task
def rebuild_workspace_view_schema(workspace_id: str) -> dict:
    """Build (or rebuild) the UNION ALL view schema for a multi-tenant workspace.

    Checks that all tenant schemas are ACTIVE before proceeding.
    On success: marks WorkspaceViewSchema.state = ACTIVE.
    On failure: marks state = FAILED.
    """
    from apps.projects.models import SchemaState, TenantSchema, Workspace, WorkspaceViewSchema
    from apps.projects.services.schema_manager import SchemaManager

    try:
        workspace = Workspace.objects.prefetch_related("tenants").get(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.error("rebuild_workspace_view_schema: workspace %s not found", workspace_id)
        return {"error": "Workspace not found"}

    tenants = list(workspace.tenants.all())
    for tenant in tenants:
        ts = TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).first()
        if ts is None:
            logger.warning(
                "rebuild_workspace_view_schema: tenant '%s' has no active schema",
                tenant.external_id,
            )
            try:
                vs = WorkspaceViewSchema.objects.get(workspace=workspace)
                vs.state = SchemaState.FAILED
                vs.save(update_fields=["state"])
            except WorkspaceViewSchema.DoesNotExist:
                pass
            return {
                "error": f"Tenant '{tenant.external_id}' has no active schema. "
                "Refresh that tenant's data first."
            }

    try:
        vs = SchemaManager().build_view_schema(workspace)
    except Exception:
        logger.exception("Failed to build view schema for workspace %s", workspace_id)
        try:
            vs = WorkspaceViewSchema.objects.get(workspace=workspace)
            vs.state = SchemaState.FAILED
            vs.save(update_fields=["state"])
        except WorkspaceViewSchema.DoesNotExist:
            pass
        return {"error": "Failed to build view schema"}

    vs.state = SchemaState.ACTIVE
    vs.save(update_fields=["state"])

    logger.info(
        "View schema '%s' is now active for workspace %s",
        vs.schema_name,
        workspace_id,
    )
    return {"status": "active", "schema_name": vs.schema_name}
```

Also add `teardown_view_schema_task`:

```python
@shared_task
def teardown_view_schema_task(view_schema_id: str) -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema and mark EXPIRED."""
    from apps.projects.models import SchemaState, WorkspaceViewSchema
    from apps.projects.services.schema_manager import SchemaManager

    try:
        vs = WorkspaceViewSchema.objects.get(id=view_schema_id)
    except WorkspaceViewSchema.DoesNotExist:
        logger.error("teardown_view_schema_task: view schema %s not found", view_schema_id)
        return

    try:
        SchemaManager().teardown_view_schema(vs)
    except Exception:
        logger.exception("Failed to drop view schema '%s'", vs.schema_name)
        raise

    vs.state = SchemaState.EXPIRED
    vs.save(update_fields=["state"])
```

### Step 3: Run tests

```bash
uv run pytest tests/test_rebuild_view_schema_task.py -v
```

Expected: all pass.

### Step 4: Commit

```bash
git add apps/projects/tasks.py tests/test_rebuild_view_schema_task.py
git commit -m "feat: rebuild_workspace_view_schema Celery task"
```

---

## Task 9.4: WorkspaceViewSchema TTL

Extend `expire_inactive_schemas` to also expire view schemas with stale `last_accessed_at`.

**Files:**
- Modify: `apps/projects/tasks.py`

### Step 1: Write the failing test

```python
# tests/test_view_schema_ttl.py
import pytest
from datetime import timedelta
from unittest.mock import patch
from django.utils import timezone
from apps.projects.models import (
    SchemaState, Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceViewSchema,
)


@pytest.fixture
def workspace_with_view_schema(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.create_user(email="ttl@example.com", password="pass")
    ws = Workspace.objects.create(name="TTL WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    vs = WorkspaceViewSchema.objects.create(
        workspace=ws,
        schema_name="ws_ttltest123456ab",
        state=SchemaState.ACTIVE,
    )
    return ws, vs


def test_expire_inactive_schemas_also_expires_stale_view_schemas(workspace_with_view_schema):
    from apps.projects.tasks import expire_inactive_schemas

    ws, vs = workspace_with_view_schema
    vs.last_accessed_at = timezone.now() - timedelta(hours=25)
    vs.save()

    with patch("apps.projects.tasks.teardown_view_schema_task") as mock_teardown:
        expire_inactive_schemas()

    vs.refresh_from_db()
    assert vs.state == SchemaState.TEARDOWN
    mock_teardown.delay.assert_called_once_with(str(vs.id))


def test_recently_accessed_view_schema_not_expired(workspace_with_view_schema):
    from apps.projects.tasks import expire_inactive_schemas

    ws, vs = workspace_with_view_schema
    vs.last_accessed_at = timezone.now() - timedelta(hours=1)
    vs.save()

    expire_inactive_schemas()

    vs.refresh_from_db()
    assert vs.state == SchemaState.ACTIVE


def test_view_schema_with_null_last_accessed_not_expired(workspace_with_view_schema):
    from apps.projects.tasks import expire_inactive_schemas

    ws, vs = workspace_with_view_schema
    vs.last_accessed_at = None
    vs.save()

    expire_inactive_schemas()

    vs.refresh_from_db()
    assert vs.state == SchemaState.ACTIVE
```

Run: `uv run pytest tests/test_view_schema_ttl.py -v`
Expected: FAIL — `expire_inactive_schemas` does not handle view schemas.

### Step 2: Update expire_inactive_schemas

In `apps/projects/tasks.py`, update the `expire_inactive_schemas` task:

```python
@shared_task
def expire_inactive_schemas() -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    Handles both TenantSchema and WorkspaceViewSchema records.
    Schemas with null last_accessed_at are never auto-expired.
    """
    from apps.projects.models import SchemaState, TenantSchema, WorkspaceViewSchema

    cutoff = timezone.now() - timedelta(hours=settings.SCHEMA_TTL_HOURS)

    # Expire stale tenant schemas
    stale_tenant = TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    )
    for schema in stale_tenant:
        schema.state = SchemaState.TEARDOWN
        schema.save(update_fields=["state"])
        schema_id = str(schema.id)
        transaction.on_commit(lambda sid=schema_id: teardown_schema.delay(sid))

    # Expire stale view schemas
    stale_views = WorkspaceViewSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    )
    for vs in stale_views:
        vs.state = SchemaState.TEARDOWN
        vs.save(update_fields=["state"])
        view_id = str(vs.id)
        transaction.on_commit(lambda vid=view_id: teardown_view_schema_task.delay(vid))
```

### Step 3: Run tests

```bash
uv run pytest tests/test_view_schema_ttl.py -v
```

Expected: all pass.

### Step 4: Commit

```bash
git add apps/projects/tasks.py tests/test_view_schema_ttl.py
git commit -m "feat: extend expire_inactive_schemas to handle WorkspaceViewSchema TTL"
```

---

## Task 9.5: Tenant add/remove API endpoints (Task 9.1 from spec)

**Files:**
- Modify: `apps/projects/api/workspace_views.py`
- Modify: `apps/projects/api/urls.py`
- Modify: `config/urls.py` (check if WorkspaceTenantViews need registration)

### Step 1: Write the failing tests

```python
# tests/test_workspace_tenant_api.py
import pytest
from rest_framework.test import APIClient
from apps.projects.models import (
    SchemaState, Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant,
)
from apps.users.models import Tenant, TenantMembership


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(email="api@example.com", password="pass")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(provider="commcare", external_id="api-domain", canonical_name="API Domain")


@pytest.fixture
def tenant2(db):
    return Tenant.objects.create(provider="commcare", external_id="api-domain-2", canonical_name="API Domain 2")


@pytest.fixture
def workspace(db, user, tenant):
    ws = Workspace.objects.create(name="API WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


@pytest.fixture
def tenant_membership(db, user, tenant2):
    return TenantMembership.objects.create(user=user, tenant=tenant2)


def test_add_tenant_to_workspace(api_client, user, workspace, tenant2, tenant_membership):
    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 202, resp.data
    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant2).exists()


def test_add_tenant_requires_manage_role(api_client, user, workspace, tenant2):
    from django.contrib.auth import get_user_model
    other = get_user_model().objects.create_user(email="other@example.com", password="pass")
    WorkspaceMembership.objects.create(workspace=workspace, user=other, role=WorkspaceRole.READ_WRITE)
    api_client.force_login(other)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 403


def test_add_tenant_user_lacks_tenant_membership_is_rejected(api_client, user, workspace, tenant2):
    # user has no TenantMembership for tenant2
    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 400
    assert "access" in resp.data["error"].lower()


def test_add_tenant_already_in_workspace_is_idempotent(api_client, user, workspace, tenant):
    # tenant is already in workspace
    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant.id)},
        format="json",
    )
    assert resp.status_code == 200  # idempotent OK


def test_remove_tenant_from_workspace(api_client, user, workspace, tenant2, tenant_membership):
    wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    api_client.force_login(user)
    resp = api_client.delete(f"/api/workspaces/{workspace.id}/tenants/{wt.id}/")
    assert resp.status_code == 204
    assert not WorkspaceTenant.objects.filter(id=wt.id).exists()


def test_cannot_remove_last_tenant_from_workspace(api_client, user, workspace, tenant):
    wt = WorkspaceTenant.objects.get(workspace=workspace, tenant=tenant)
    api_client.force_login(user)
    resp = api_client.delete(f"/api/workspaces/{workspace.id}/tenants/{wt.id}/")
    assert resp.status_code == 400
    assert "last" in resp.data["error"].lower()
```

Run: `uv run pytest tests/test_workspace_tenant_api.py -v`
Expected: FAIL — endpoints do not exist (404).

### Step 2: Add WorkspaceTenantViews

Add to `apps/projects/api/workspace_views.py`:

```python
class WorkspaceTenantView(APIView):
    """
    POST   /api/workspaces/<workspace_id>/tenants/      — add tenant (manage only)
    DELETE /api/workspaces/<workspace_id>/tenants/<wt_id>/ — remove tenant (manage only)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can add tenants."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tenant_id = request.data.get("tenant_id")
        if not tenant_id:
            return Response({"error": "tenant_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Validate user has TenantMembership for this tenant
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response(
                {"error": "Tenant not found or not accessible."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.users.models import TenantMembership

        if not TenantMembership.objects.filter(user=request.user, tenant=tenant).exists():
            return Response(
                {"error": "You do not have access to this tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Idempotent: if already in workspace return 200
        wt, created = WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)
        if not created:
            return Response(
                {"id": str(wt.id), "tenant_id": str(tenant.id), "tenant_name": tenant.canonical_name},
                status=status.HTTP_200_OK,
            )

        # Trigger view schema rebuild asynchronously
        from apps.projects.tasks import rebuild_workspace_view_schema

        workspace_id_str = str(workspace.id)
        transaction.on_commit(lambda: rebuild_workspace_view_schema.delay(workspace_id_str))

        return Response(
            {"id": str(wt.id), "tenant_id": str(tenant.id), "tenant_name": tenant.canonical_name},
            status=status.HTTP_202_ACCEPTED,
        )

    def delete(self, request, workspace_id, wt_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role != WorkspaceRole.MANAGE:
            return Response(
                {"error": "Only workspace managers can remove tenants."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            wt = WorkspaceTenant.objects.get(id=wt_id, workspace=workspace)
        except WorkspaceTenant.DoesNotExist:
            return Response({"error": "Tenant not found in workspace."}, status=status.HTTP_404_NOT_FOUND)

        # Block removal of last tenant
        if workspace.workspace_tenants.count() <= 1:
            return Response(
                {"error": "Cannot remove the last tenant from a workspace."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        wt.delete()

        # Trigger view schema rebuild (or teardown if now single-tenant)
        from apps.projects.tasks import rebuild_workspace_view_schema

        workspace_id_str = str(workspace.id)
        transaction.on_commit(lambda: rebuild_workspace_view_schema.delay(workspace_id_str))

        return Response(status=status.HTTP_204_NO_CONTENT)
```

Add `from django.db import transaction` to the imports at the top of `workspace_views.py`.

### Step 3: Register URL

In `apps/projects/api/urls.py`, add:

```python
from .workspace_views import (
    WorkspaceDetailView, WorkspaceListView,
    WorkspaceMemberDetailView, WorkspaceMemberListView,
    WorkspaceTenantView,
)

# Add to urlpatterns:
path("tenants/", WorkspaceTenantView.as_view(), name="workspace_tenants"),
path("tenants/<uuid:wt_id>/", WorkspaceTenantView.as_view(), name="workspace_tenant_detail"),
```

Wait — `WorkspaceTenantView` has both `post` (no `wt_id`) and `delete` (needs `wt_id`). These should be two separate URL entries pointing to the same view. Check `urls.py` for the existing pattern and add alongside it.

The existing `apps/projects/api/urls.py` is included under `/api/workspaces/<workspace_id>/` in `config/urls.py`. Add both paths there.

### Step 4: Run tests

```bash
uv run pytest tests/test_workspace_tenant_api.py -v
```

Expected: all pass.

### Step 5: Commit

```bash
git add apps/projects/api/workspace_views.py apps/projects/api/urls.py tests/test_workspace_tenant_api.py
git commit -m "feat: workspace tenant add/remove API endpoints"
```

---

## Task 9.6: MCP context routing — load_workspace_context

**Files:**
- Modify: `mcp_server/context.py`

### Step 1: Write the failing test

```python
# tests/test_mcp_workspace_context.py
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from apps.projects.models import SchemaState, Workspace, WorkspaceTenant, WorkspaceViewSchema


@pytest.mark.asyncio
async def test_load_workspace_context_single_tenant_delegates_to_tenant_context(db):
    from django.contrib.auth import get_user_model
    from apps.users.models import Tenant
    from apps.projects.models import WorkspaceMembership, WorkspaceRole
    User = get_user_model()
    user = await User.objects.acreate_user(email="ctx@example.com", password="pass")
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="ctx-domain", canonical_name="CTX"
    )
    ws = await Workspace.objects.acreate(name="CTX WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)

    with patch("mcp_server.context.load_tenant_context", new_callable=AsyncMock) as mock_ltc:
        mock_ltc.return_value = MagicMock(schema_name="ctx_domain")
        from mcp_server.context import load_workspace_context
        result = await load_workspace_context(str(ws.id))

    mock_ltc.assert_called_once_with("ctx-domain")
    assert result.schema_name == "ctx_domain"


@pytest.mark.asyncio
async def test_load_workspace_context_multi_tenant_uses_view_schema(db):
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from apps.users.models import Tenant
    from apps.projects.models import WorkspaceMembership, WorkspaceRole
    User = get_user_model()
    user = await User.objects.acreate_user(email="ctx2@example.com", password="pass")
    t1 = await Tenant.objects.acreate(provider="commcare", external_id="ctx-d1", canonical_name="D1")
    t2 = await Tenant.objects.acreate(provider="commcare", external_id="ctx-d2", canonical_name="D2")
    ws = await Workspace.objects.acreate(name="Multi CTX WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)
    vs = await WorkspaceViewSchema.objects.acreate(
        workspace=ws,
        schema_name="ws_multitest123456",
        state=SchemaState.ACTIVE,
    )

    with patch.object(settings, "MANAGED_DATABASE_URL", "postgresql://test/db"):
        with patch("mcp_server.context._parse_db_url") as mock_parse:
            mock_parse.return_value = {"host": "localhost", "dbname": "db"}
            from mcp_server.context import load_workspace_context
            ctx = await load_workspace_context(str(ws.id))

    assert ctx.schema_name == "ws_multitest123456"
    assert ctx.tenant_id == str(ws.id)


@pytest.mark.asyncio
async def test_load_workspace_context_multi_tenant_raises_if_no_active_view_schema(db):
    from django.contrib.auth import get_user_model
    from apps.users.models import Tenant
    from apps.projects.models import WorkspaceMembership, WorkspaceRole
    User = get_user_model()
    user = await User.objects.acreate_user(email="ctx3@example.com", password="pass")
    t1 = await Tenant.objects.acreate(provider="commcare", external_id="ctx-noview-1", canonical_name="NV1")
    t2 = await Tenant.objects.acreate(provider="commcare", external_id="ctx-noview-2", canonical_name="NV2")
    ws = await Workspace.objects.acreate(name="NoView WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)

    from mcp_server.context import load_workspace_context
    with pytest.raises(ValueError, match="No active view schema"):
        await load_workspace_context(str(ws.id))
```

Run: `uv run pytest tests/test_mcp_workspace_context.py -v`
Expected: FAIL — `load_workspace_context` does not exist.

### Step 2: Implement load_workspace_context

Add to `mcp_server/context.py`:

```python
async def load_workspace_context(workspace_id: str) -> QueryContext:
    """Load a QueryContext for a workspace, routing correctly for multi-tenant.

    - Single-tenant workspace (1 tenant): delegates to load_tenant_context(tenant.external_id).
    - Multi-tenant workspace (2+ tenants): uses the WorkspaceViewSchema.

    Raises ValueError if the workspace has no tenants, or if multi-tenant and
    no active WorkspaceViewSchema exists.
    """
    from asgiref.sync import sync_to_async
    from django.conf import settings

    from apps.projects.models import SchemaState, Workspace, WorkspaceViewSchema

    try:
        workspace = await Workspace.objects.aget(id=workspace_id)
    except Workspace.DoesNotExist:
        raise ValueError(f"Workspace '{workspace_id}' not found")

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
        )

    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise ValueError("MANAGED_DATABASE_URL is not configured")

    connection_params = await sync_to_async(_parse_db_url)(url, vs.schema_name)

    return QueryContext(
        tenant_id=workspace_id,
        schema_name=vs.schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params=connection_params,
    )
```

### Step 3: Run tests

```bash
uv run pytest tests/test_mcp_workspace_context.py -v
```

Expected: all pass.

### Step 4: Commit

```bash
git add mcp_server/context.py tests/test_mcp_workspace_context.py
git commit -m "feat: load_workspace_context for MCP workspace-ID routing"
```

---

## Task 9.7: MCP tools accept workspace_id

Add `workspace_id: str = ""` to tools that query data. When non-empty, route through `load_workspace_context`.

**Files:**
- Modify: `mcp_server/server.py`

The tools to update: `list_tables`, `describe_table`, `query`, `get_metadata`, `get_schema_status`.

**Do NOT** change `run_materialization`, `teardown_schema`, `cancel_materialization` — those are tenant-level operations.

### Step 1: Write a failing test

```python
# tests/test_mcp_workspace_routing.py
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_query_tool_uses_workspace_context_when_workspace_id_provided():
    """When workspace_id is non-empty, query should call load_workspace_context."""
    mock_ctx = MagicMock()
    mock_ctx.schema_name = "ws_abc123"
    mock_ctx.max_rows_per_query = 500
    mock_ctx.max_query_timeout_seconds = 30

    with patch("mcp_server.server.load_workspace_context", new_callable=AsyncMock) as mock_lwc:
        mock_lwc.return_value = mock_ctx
        with patch("mcp_server.server.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "success": True,
                "columns": ["id"],
                "rows": [["1"]],
                "row_count": 1,
                "truncated": False,
                "sql_executed": "SELECT 1",
                "tables_accessed": [],
            }
            from mcp_server.server import query
            result = await query(
                tenant_id="old-tenant",
                sql="SELECT 1",
                workspace_id="some-workspace-uuid",
            )

    mock_lwc.assert_called_once_with("some-workspace-uuid")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_query_tool_falls_back_to_tenant_context_when_no_workspace_id():
    """When workspace_id is empty, query should call load_tenant_context."""
    mock_ctx = MagicMock()
    mock_ctx.schema_name = "tenant_schema"
    mock_ctx.max_rows_per_query = 500
    mock_ctx.max_query_timeout_seconds = 30

    with patch("mcp_server.server.load_tenant_context", new_callable=AsyncMock) as mock_ltc:
        mock_ltc.return_value = mock_ctx
        with patch("mcp_server.server.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "success": True,
                "columns": ["id"],
                "rows": [["1"]],
                "row_count": 1,
                "truncated": False,
                "sql_executed": "SELECT 1",
                "tables_accessed": [],
            }
            from mcp_server.server import query
            result = await query(tenant_id="my-tenant", sql="SELECT 1", workspace_id="")

    mock_ltc.assert_called_once_with("my-tenant")
    assert result["success"] is True
```

Run: `uv run pytest tests/test_mcp_workspace_routing.py -v`
Expected: FAIL — `query` does not accept `workspace_id`.

### Step 2: Update tool signatures

The pattern to apply to each tool is the same. Shown here for `query`; apply the identical change to `list_tables`, `describe_table`, `get_metadata`, and `get_schema_status`.

**Context resolution helper** — add at the top of the server module (after the existing imports):

```python
from mcp_server.context import load_tenant_context, load_workspace_context
```

**Context resolution inline in each tool** — replace:

```python
ctx = await load_tenant_context(tenant_id)
```

With:

```python
if workspace_id:
    ctx = await load_workspace_context(workspace_id)
else:
    ctx = await load_tenant_context(tenant_id)
```

**Updated `query` signature:**

```python
@mcp.tool()
async def query(tenant_id: str, sql: str, workspace_id: str = "") -> dict:
    """Execute a read-only SQL query against the tenant's (or workspace's) database.
    ...
    Args:
        tenant_id: The tenant identifier (used when workspace_id is not provided).
        sql: A SQL SELECT query to execute.
        workspace_id: Optional workspace UUID. When provided, routes to the
            workspace's view schema (multi-tenant workspaces) or the single
            tenant's schema.
    """
```

Apply the same `workspace_id: str = ""` parameter addition to:
- `list_tables`
- `describe_table`
- `get_metadata`
- `get_schema_status`

Also add `workspace_id` to `MCP_TOOL_NAMES` injection in `apps/agents/graph/base.py` (Task 9.8).

**For `get_schema_status`**, the response when `workspace_id` routes to a view schema should check `WorkspaceViewSchema.state` instead of `TenantSchema.state`. Update accordingly:

```python
if workspace_id:
    from apps.projects.models import WorkspaceViewSchema
    vs = await WorkspaceViewSchema.objects.filter(
        workspace_id=workspace_id,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()
    if vs is None:
        tc["result"] = success_response(
            {"exists": False, "state": "not_provisioned", "last_materialized_at": None, "tables": []},
            tenant_id=workspace_id, schema="",
        )
        return tc["result"]
    # ... return view schema status
```

### Step 3: Touch WorkspaceViewSchema.last_accessed_at on query

In the `query` tool, after a successful query when `workspace_id` is non-empty, also touch the `WorkspaceViewSchema`:

```python
if workspace_id:
    from apps.projects.models import WorkspaceViewSchema
    vs = await WorkspaceViewSchema.objects.filter(
        workspace_id=workspace_id, state=SchemaState.ACTIVE
    ).afirst()
    if vs is not None:
        await sync_to_async(vs.touch)()
```

### Step 4: Run tests

```bash
uv run pytest tests/test_mcp_workspace_routing.py -v
```

Expected: all pass.

### Step 5: Commit

```bash
git add mcp_server/server.py tests/test_mcp_workspace_routing.py
git commit -m "feat: MCP tools accept workspace_id for workspace-scoped routing"
```

---

## Task 9.8: Agent graph and chat view multi-tenant routing

Wire `workspace_id` through the agent state so it is injected into MCP tool calls.

**Files:**
- Modify: `apps/agents/graph/state.py`
- Modify: `apps/agents/graph/base.py`
- Modify: `apps/chat/views.py`

### Step 1: Write the failing test

```python
# tests/test_agent_workspace_routing.py
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


def test_agent_state_has_workspace_id_field():
    from apps.agents.graph.state import AgentState
    # AgentState is a TypedDict — check the field exists
    assert "workspace_id" in AgentState.__annotations__


def test_injecting_node_includes_workspace_id(db):
    """The injecting node must inject workspace_id into MCP tool calls."""
    import asyncio
    from langchain_core.messages import AIMessage

    # Build a fake state with workspace_id
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "call_1",
                    "name": "query",
                    "args": {"tenant_id": "old-tenant", "sql": "SELECT 1"},
                }],
            )
        ],
        "tenant_id": "old-tenant",
        "tenant_membership_id": "membership-123",
        "workspace_id": "ws-uuid-456",
        "user_id": "user-1",
        "user_role": "analyst",
        "needs_correction": False,
        "retry_count": 0,
        "correction_context": {},
        "tenant_name": "Old Tenant",
    }

    # Use the _make_injecting_tool_node factory with a mock base ToolNode
    from apps.agents.graph.base import _make_injecting_tool_node

    mock_base_node = AsyncMock()
    mock_base_node.ainvoke.return_value = {"messages": []}

    node = _make_injecting_tool_node(
        mock_base_node,
        injections={
            "tenant_id": "tenant_id",
            "tenant_membership_id": "tenant_membership_id",
            "workspace_id": "workspace_id",
        },
    )

    result = asyncio.get_event_loop().run_until_complete(node(state))

    # Check that workspace_id was injected
    call_args = mock_base_node.ainvoke.call_args[0][0]
    last_msg = call_args["messages"][-1]
    assert last_msg.tool_calls[0]["args"]["workspace_id"] == "ws-uuid-456"
```

Run: `uv run pytest tests/test_agent_workspace_routing.py::test_agent_state_has_workspace_id_field -v`
Expected: FAIL — `workspace_id` not in `AgentState`.

### Step 2: Add workspace_id to AgentState

In `apps/agents/graph/state.py`, add to `AgentState`:

```python
# Workspace context — scopes multi-tenant routing
workspace_id: str
```

Add a brief comment near `tenant_id`:

```python
# Tenant context - scopes all data access (single-tenant workspaces)
tenant_id: str
tenant_name: str
tenant_membership_id: str

# Workspace context - for multi-tenant workspace routing via MCP
workspace_id: str
```

### Step 3: Update build_agent_graph injections

In `apps/agents/graph/base.py`, update the `injections` dict:

```python
injections = {
    "tenant_id": "tenant_id",
    "tenant_membership_id": "tenant_membership_id",
    "workspace_id": "workspace_id",
}
```

Also add `"workspace_id"` to `hidden_params` (it is already included because `hidden_params = list(injections.keys())`).

### Step 4: Update chat view to include workspace_id in initial state

In `apps/chat/views.py`, find the `input_state` dict (around line 674) and add `workspace_id`:

```python
input_state = {
    "messages": [HumanMessage(content=user_content)],
    "tenant_id": tenant_membership.tenant.external_id if tenant_membership else "",
    "tenant_name": tenant_membership.tenant.canonical_name if tenant_membership else "",
    "tenant_membership_id": str(tenant_membership.id) if tenant_membership else "",
    "workspace_id": str(workspace_id),   # <-- add this
    "user_id": str(user.id),
    "user_role": "analyst",
    "needs_correction": False,
    "retry_count": 0,
    "correction_context": {},
}
```

Also update `_resolve_workspace_and_membership` to handle multi-tenant workspaces (currently returns `(workspace, None)` when `workspace.tenant` yields only the first tenant and TenantMembership lookup fails). For multi-tenant, returning `(workspace, None)` is correct — just make the chat view tolerate `tenant_membership is None` when the workspace has 2+ tenants:

In `apps/chat/views.py`, find the guard that returns 403 when `tenant_membership is None`:

```python
if workspace is None:
    return JsonResponse({"error": "Workspace not found or access denied"}, status=403)
if tenant_membership is None:
    return JsonResponse({"error": "No tenant membership for this workspace"}, status=403)
```

Change to:

```python
if workspace is None:
    return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

# For multi-tenant workspaces, tenant_membership may be None
# The agent will route via workspace_id instead
tenant_count = await workspace.tenants.acount()
if tenant_membership is None and tenant_count <= 1:
    return JsonResponse({"error": "No tenant membership for this workspace"}, status=403)
```

Also update `build_agent_graph` call to not crash when `tenant_membership` is None. Since `build_agent_graph` currently requires `tenant_membership` for `_fetch_schema_context`, add a guard:

In `apps/agents/graph/base.py`, `_fetch_schema_context` is called with `tenant_membership`. For multi-tenant workspaces where `tenant_membership` might be None, add a fallback:

In `_build_system_prompt`:
```python
if tenant_membership is not None:
    schema_context = await _fetch_schema_context(tenant_membership)
else:
    schema_context = (
        "This is a multi-tenant workspace. Use workspace_id when calling MCP tools. "
        "Call `list_tables` to see available tables."
    )
sections.append(f"\n## Data Availability\n\n{schema_context}\n")
```

And update `build_agent_graph` signature to accept `workspace_id: str = ""` and `tenant_membership` as optional:

```python
async def build_agent_graph(
    tenant_membership,          # TenantMembership | None
    user=None,
    checkpointer=None,
    mcp_tools=None,
    oauth_tokens=None,
    workspace_id: str = "",
):
```

Pass `workspace_id` through from the chat view call site:

```python
agent = await build_agent_graph(
    tenant_membership=tenant_membership,
    user=user,
    checkpointer=checkpointer,
    mcp_tools=mcp_tools,
    oauth_tokens=oauth_tokens,
    workspace_id=str(workspace_id),
)
```

### Step 5: Run tests

```bash
uv run pytest tests/test_agent_workspace_routing.py -v
uv run pytest tests/ -v  # full suite
```

Expected: all pass.

### Step 6: Commit

```bash
git add apps/agents/graph/state.py apps/agents/graph/base.py apps/chat/views.py tests/test_agent_workspace_routing.py
git commit -m "feat: inject workspace_id into agent state for multi-tenant MCP routing"
```

---

## Task 9.9: End-to-end integration test (smoke test)

Write one test that confirms the full flow for a multi-tenant workspace: adding a second tenant triggers a rebuild task.

```python
# tests/test_multitenant_smoke.py
import pytest
from unittest.mock import patch
from rest_framework.test import APIClient
from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.users.models import Tenant, TenantMembership


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def setup(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.create_user(email="smoke@example.com", password="pass")
    t1 = Tenant.objects.create(provider="commcare", external_id="smoke-1", canonical_name="Smoke 1")
    t2 = Tenant.objects.create(provider="commcare", external_id="smoke-2", canonical_name="Smoke 2")
    TenantMembership.objects.create(user=user, tenant=t1)
    TenantMembership.objects.create(user=user, tenant=t2)
    ws = Workspace.objects.create(name="Smoke WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    return user, ws, t2


def test_adding_tenant_dispatches_rebuild_task(api_client, setup):
    user, ws, t2 = setup

    with patch("apps.projects.tasks.rebuild_workspace_view_schema") as mock_task:
        api_client.force_login(user)
        resp = api_client.post(
            f"/api/workspaces/{ws.id}/tenants/",
            {"tenant_id": str(t2.id)},
            format="json",
        )

    assert resp.status_code == 202
    assert WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()
    mock_task.delay.assert_called_once_with(str(ws.id))
```

Run: `uv run pytest tests/test_multitenant_smoke.py -v`
Expected: PASS.

```bash
git add tests/test_multitenant_smoke.py
git commit -m "test: multi-tenant workspace smoke test"
```

---

## Final Verification

Run the entire test suite and confirm no regressions:

```bash
uv run pytest -v 2>&1 | tail -30
```

Run linting:

```bash
uv run ruff check apps/projects/ mcp_server/ apps/agents/ apps/chat/
uv run ruff format apps/projects/ mcp_server/ apps/agents/ apps/chat/
```

---

## What This Plan Does NOT Include

These are deliberately deferred:

- **Recovery when expired tenant schemas need re-fetch before view rebuild** — `rebuild_workspace_view_schema` currently fails fast if any tenant schema is expired. The recovery that triggers per-tenant `refresh_tenant_schema` tasks first and chains the view rebuild requires a Celery chord; deferred.
- **Workspace detail API `schema_status` for multi-tenant** — `WorkspaceDetailView.get()` computes `schema_status` from `TenantSchema` objects. It should also check `WorkspaceViewSchema.state` for multi-tenant workspaces. Simple follow-on.
- **`DataDictionaryView` for multi-tenant workspaces** — currently uses `workspace.tenant` (single-tenant). For multi-tenant, it should enumerate all tenant schemas. Deferred.
- **Audit log (Phase 6)** — `TENANT_ADDED`/`TENANT_REMOVED` log events are left out since `AuditLog` model doesn't exist yet. Add `# TODO(phase6): log_action(workspace, request.user, AuditAction.TENANT_ADDED)` comment at the call sites.
