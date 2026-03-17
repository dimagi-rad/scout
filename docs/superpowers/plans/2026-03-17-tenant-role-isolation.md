# Tenant Role Isolation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-tenant read-only PostgreSQL roles enforced via `SET ROLE` so user-facing MCP queries have database-level isolation, not just application-level isolation.

**Architecture:** Each tenant schema gets a `{schema_name}_ro` PostgreSQL role with `USAGE` + `SELECT` grants. The `_execute_sync` function in `query.py` wraps user-facing queries with `SET ROLE` / `RESET ROLE`. Roles are created during schema provisioning and dropped during teardown.

**Tech Stack:** PostgreSQL roles, psycopg3 `sql.Identifier`, Django management commands

**Spec:** `docs/superpowers/specs/2026-03-17-tenant-role-isolation-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `apps/workspaces/services/schema_manager.py` | Role lifecycle — create on provision, drop on teardown |
| `mcp_server/services/query.py` | `SET ROLE` / `RESET ROLE` around user-facing queries |
| `mcp_server/context.py` | `readonly_role` property on `QueryContext` |
| `apps/workspaces/management/commands/backfill_readonly_roles.py` | One-time backfill for existing schemas |
| `tests/test_schema_manager.py` | Role lifecycle tests |
| `tests/test_query_role_isolation.py` | Query-level role enforcement tests |
| `tests/test_backfill_readonly_roles.py` | Backfill command tests |

---

## Chunk 1: Role Naming Helper and Context Property

### Task 1: Add `readonly_role_name` helper to SchemaManager

**Files:**
- Modify: `apps/workspaces/services/schema_manager.py:1-19`
- Test: `tests/test_schema_manager.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_schema_manager.py`, add:

```python
from apps.workspaces.services.schema_manager import readonly_role_name


class TestReadonlyRoleName:
    def test_basic(self):
        assert readonly_role_name("tenant_abc123") == "tenant_abc123_ro"

    def test_view_schema(self):
        assert readonly_role_name("ws_abc1234def56789") == "ws_abc1234def56789_ro"

    def test_refresh_schema(self):
        assert readonly_role_name("test_domain_r1a2b3c4") == "test_domain_r1a2b3c4_ro"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schema_manager.py::TestReadonlyRoleName -v`
Expected: FAIL with `ImportError: cannot import name 'readonly_role_name'`

- [ ] **Step 3: Write minimal implementation**

In `apps/workspaces/services/schema_manager.py`, add after the imports (before `get_managed_db_connection`):

```python
def readonly_role_name(schema_name: str) -> str:
    """Derive the read-only PostgreSQL role name for a schema."""
    return f"{schema_name}_ro"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_schema_manager.py::TestReadonlyRoleName -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/services/schema_manager.py tests/test_schema_manager.py
git commit -m "feat: add readonly_role_name helper for tenant role isolation"
```

### Task 2: Add `readonly_role` property to QueryContext

**Files:**
- Modify: `mcp_server/context.py:14-22`
- Test: `tests/test_query_role_isolation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_query_role_isolation.py`:

```python
from mcp_server.context import QueryContext


class TestQueryContextReadonlyRole:
    def test_readonly_role_derived_from_schema_name(self):
        ctx = QueryContext(
            tenant_id="test-domain",
            schema_name="test_domain",
            connection_params={"host": "localhost"},
        )
        assert ctx.readonly_role == "test_domain_ro"

    def test_readonly_role_view_schema(self):
        ctx = QueryContext(
            tenant_id="workspace-123",
            schema_name="ws_abc1234def56789",
            connection_params={"host": "localhost"},
        )
        assert ctx.readonly_role == "ws_abc1234def56789_ro"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_query_role_isolation.py::TestQueryContextReadonlyRole -v`
Expected: FAIL with `AttributeError: 'QueryContext' object has no attribute 'readonly_role'`

- [ ] **Step 3: Write minimal implementation**

In `mcp_server/context.py`, add a property to `QueryContext` after the `connection_params` field:

```python
@property
def readonly_role(self) -> str:
    """Derive the read-only PostgreSQL role name for this context's schema."""
    return f"{self.schema_name}_ro"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_query_role_isolation.py::TestQueryContextReadonlyRole -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server/context.py tests/test_query_role_isolation.py
git commit -m "feat: add readonly_role property to QueryContext"
```

---

## Chunk 2: Role Lifecycle in SchemaManager

### Task 3: Add role creation to `provision()` and `create_physical_schema()`

**Files:**
- Modify: `apps/workspaces/services/schema_manager.py:33-112`
- Test: `tests/test_schema_manager.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_schema_manager.py`, add:

```python
@pytest.mark.django_db
class TestSchemaManagerRoleCreation:
    def test_provision_creates_readonly_role(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership.tenant)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls), (
            f"Expected CREATE ROLE for {role_name} in DDL calls"
        )
        assert any("GRANT USAGE ON SCHEMA" in c for c in calls)
        assert any("ALTER DEFAULT PRIVILEGES" in c for c in calls)

    def test_create_physical_schema_creates_readonly_role(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        from apps.workspaces.models import TenantSchema

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain_r1a2b3c4",
            state="provisioning",
        )

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.create_physical_schema(ts)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schema_manager.py::TestSchemaManagerRoleCreation -v`
Expected: FAIL — no CREATE ROLE in the DDL calls

- [ ] **Step 3: Add `_create_readonly_role` helper method**

In `apps/workspaces/services/schema_manager.py`, add a private method to `SchemaManager`:

```python
def _create_readonly_role(self, cursor, schema_name: str) -> None:
    """Create a read-only PostgreSQL role for a schema.

    Idempotent — checks pg_roles before creating. Grants USAGE on the
    schema and sets ALTER DEFAULT PRIVILEGES so tables created later by
    the materializer are automatically readable.
    """
    role_name = readonly_role_name(schema_name)
    # Idempotent role creation — pg doesn't have CREATE ROLE IF NOT EXISTS
    cursor.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s",
        (role_name,),
    )
    if not cursor.fetchone():
        cursor.execute(
            psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(
                psycopg.sql.Identifier(role_name)
            )
        )
    cursor.execute(
        psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            psycopg.sql.Identifier(schema_name),
            psycopg.sql.Identifier(role_name),
        )
    )
    cursor.execute(
        psycopg.sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA {} "
            "GRANT SELECT ON TABLES TO {}"
        ).format(
            psycopg.sql.Identifier(schema_name),
            psycopg.sql.Identifier(role_name),
        )
    )
```

**Note:** `CURRENT_USER` is used instead of hardcoding the managed DB username — it resolves to whatever user the `MANAGED_DATABASE_URL` connects as. This avoids needing a new setting.

- [ ] **Step 4: Call `_create_readonly_role` from `provision()`**

In `provision()`, after `CREATE SCHEMA IF NOT EXISTS` and before `cursor.close()` (around line 76), add:

```python
self._create_readonly_role(cursor, schema_name)
```

- [ ] **Step 5: Call `_create_readonly_role` from `create_physical_schema()`**

In `create_physical_schema()`, after `CREATE SCHEMA IF NOT EXISTS` and before `cursor.close()` (around line 109), add:

```python
self._create_readonly_role(cursor, tenant_schema.schema_name)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_schema_manager.py -v`
Expected: ALL PASS (existing tests + new role creation tests)

- [ ] **Step 7: Commit**

```bash
git add apps/workspaces/services/schema_manager.py tests/test_schema_manager.py
git commit -m "feat: create read-only PostgreSQL role on schema provision"
```

### Task 4: Add role drop to `teardown()` and `teardown_view_schema()`

**Files:**
- Modify: `apps/workspaces/services/schema_manager.py:128-144,312-324`
- Test: `tests/test_schema_manager.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_schema_manager.py`, add:

```python
@pytest.mark.django_db
class TestSchemaManagerRoleTeardown:
    def test_teardown_drops_readonly_role(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.teardown(ts)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("DROP ROLE IF EXISTS" in c and role_name in c for c in calls)

    def test_teardown_view_schema_drops_readonly_role(self, workspace):
        from apps.workspaces.models import WorkspaceViewSchema

        vs = WorkspaceViewSchema.objects.create(
            workspace=workspace,
            schema_name="ws_abc1234def56789",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.teardown_view_schema(vs)

        role_name = readonly_role_name(vs.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("DROP ROLE IF EXISTS" in c and role_name in c for c in calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schema_manager.py::TestSchemaManagerRoleTeardown -v`
Expected: FAIL — no DROP ROLE in calls

- [ ] **Step 3: Add `_drop_readonly_role` helper method**

In `SchemaManager`, add:

```python
def _drop_readonly_role(self, cursor, schema_name: str) -> None:
    """Drop the read-only PostgreSQL role for a schema."""
    role_name = readonly_role_name(schema_name)
    cursor.execute(
        psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
            psycopg.sql.Identifier(role_name)
        )
    )
```

- [ ] **Step 4: Call `_drop_readonly_role` from `teardown()`**

In `teardown()`, after `DROP SCHEMA IF EXISTS ... CASCADE` and before `cursor.close()` (around line 141), add:

```python
self._drop_readonly_role(cursor, tenant_schema.schema_name)
```

- [ ] **Step 5: Call `_drop_readonly_role` from `teardown_view_schema()`**

In `teardown_view_schema()`, after `DROP SCHEMA IF EXISTS ... CASCADE` and before `cursor.close()` (around line 321), add:

```python
self._drop_readonly_role(cursor, view_schema.schema_name)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_schema_manager.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add apps/workspaces/services/schema_manager.py tests/test_schema_manager.py
git commit -m "feat: drop read-only PostgreSQL role on schema teardown"
```

### Task 5: Add role creation to `build_view_schema()`

**Files:**
- Modify: `apps/workspaces/services/schema_manager.py:151-310`
- Test: `tests/test_schema_manager.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_schema_manager.py`, add:

```python
@pytest.mark.django_db
class TestViewSchemaRoleCreation:
    def test_build_view_schema_creates_readonly_role_with_tenant_grants(
        self, workspace, tenant_membership
    ):
        from apps.workspaces.models import TenantSchema

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.closed = False
        # Return empty columns result for information_schema query
        mock_cursor.fetchall.return_value = []

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            vs = mgr.build_view_schema(workspace)

        view_role_name = readonly_role_name(vs.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        # View schema role should be created
        assert any("CREATE ROLE" in c and view_role_name in c for c in calls), (
            f"Expected CREATE ROLE for {view_role_name}"
        )
        # Should grant USAGE on view schema
        assert any(
            "GRANT USAGE ON SCHEMA" in c and vs.schema_name in c for c in calls
        )
        # Should grant SELECT on constituent tenant schema tables
        assert any(
            "GRANT SELECT ON ALL TABLES IN SCHEMA" in c and ts.schema_name in c
            for c in calls
        )
        # Should grant USAGE on constituent tenant schema
        assert any(
            "GRANT USAGE ON SCHEMA" in c and ts.schema_name in c for c in calls
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schema_manager.py::TestViewSchemaRoleCreation -v`
Expected: FAIL — no CREATE ROLE for view schema role

- [ ] **Step 3: Implement view schema role creation**

In `build_view_schema()`, after the view creation loop (after `cursor.execute(view_sql)` loop, before `cursor.close()`), add:

```python
# Create read-only role for the view schema
self._create_readonly_role(cursor, view_schema_name)

# Grant read access to each constituent tenant schema
# (views reference tables in these schemas directly)
view_role = readonly_role_name(view_schema_name)
for tenant_schema_name, _ in tenant_schemas:
    cursor.execute(
        psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            psycopg.sql.Identifier(tenant_schema_name),
            psycopg.sql.Identifier(view_role),
        )
    )
    cursor.execute(
        psycopg.sql.SQL(
            "GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}"
        ).format(
            psycopg.sql.Identifier(tenant_schema_name),
            psycopg.sql.Identifier(view_role),
        )
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schema_manager.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/services/schema_manager.py tests/test_schema_manager.py
git commit -m "feat: create read-only role with tenant grants for view schemas"
```

---

## Chunk 3: SET ROLE in Query Execution

### Task 6: Wrap `_execute_sync` with `SET ROLE` / `RESET ROLE`

**Files:**
- Modify: `mcp_server/services/query.py:43-69`
- Test: `tests/test_query_role_isolation.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_query_role_isolation.py`, add:

```python
from unittest.mock import MagicMock, call, patch

from mcp_server.context import QueryContext
from mcp_server.services.query import _execute_sync


class TestSetRoleIsolation:
    def _make_ctx(self, schema_name="test_domain"):
        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            connection_params={"host": "localhost"},
        )

    @patch("mcp_server.services.query._get_connection")
    def test_execute_sync_sets_and_resets_role(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [("val1",)]
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        ctx = self._make_ctx()
        _execute_sync(ctx, "SELECT 1", 30)

        execute_calls = mock_cursor.execute.call_args_list
        # First call should be SET ROLE
        first_call_str = str(execute_calls[0])
        assert "SET ROLE" in first_call_str
        assert "test_domain_ro" in first_call_str
        # Last call before cursor.close should be RESET ROLE
        last_call_str = str(execute_calls[-1])
        assert "RESET ROLE" in last_call_str

    @patch("mcp_server.services.query._get_connection")
    def test_reset_role_on_query_error(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = [
            None,  # SET ROLE succeeds
            None,  # SET search_path succeeds
            None,  # SET statement_timeout succeeds
            Exception("query failed"),  # actual query fails
            None,  # RESET ROLE succeeds
        ]
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        ctx = self._make_ctx()
        try:
            _execute_sync(ctx, "SELECT bad", 30)
        except Exception:
            pass

        # RESET ROLE should still have been called
        last_call_str = str(mock_cursor.execute.call_args_list[-1])
        assert "RESET ROLE" in last_call_str
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query_role_isolation.py::TestSetRoleIsolation -v`
Expected: FAIL — no SET ROLE in the execute calls

- [ ] **Step 3: Implement SET ROLE in `_execute_sync`**

Replace `_execute_sync` in `mcp_server/services/query.py`:

```python
def _execute_sync(ctx: QueryContext, sql: str, timeout_seconds: int) -> dict[str, Any]:
    """Run a SQL query synchronously under the tenant's read-only role."""
    from psycopg import sql as psql

    with _get_connection(ctx) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                psql.SQL("SET ROLE {}").format(psql.Identifier(ctx.readonly_role))
            )
            try:
                cursor.execute(
                    psql.SQL("SET search_path TO {}").format(
                        psql.Identifier(ctx.schema_name)
                    )
                )
                cursor.execute(f"SET statement_timeout TO '{timeout_seconds}s'")
                cursor.execute(sql)

                columns: list[str] = []
                rows: list[list[Any]] = []

                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = [list(row) for row in cursor.fetchall()]

                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                }
            finally:
                cursor.execute("RESET ROLE")
        finally:
            cursor.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_query_role_isolation.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v --timeout=30`
Expected: ALL PASS (existing query tests should still work since `_execute_sync_parameterized` is unchanged)

- [ ] **Step 6: Commit**

```bash
git add mcp_server/services/query.py tests/test_query_role_isolation.py
git commit -m "feat: enforce SET ROLE for user-facing queries in MCP server"
```

### Task 7: Add SET ROLE error classification

**Files:**
- Modify: `mcp_server/services/query.py:159-180`
- Test: `tests/test_query_role_isolation.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_query_role_isolation.py`, add:

```python
import psycopg.errors

from mcp_server.services.query import _classify_error


class TestRoleErrorClassification:
    def test_invalid_role_classified_as_connection_error(self):
        exc = psycopg.errors.InsufficientPrivilege("role 'test_domain_ro' does not exist")
        code, message = _classify_error(exc)
        assert code == "CONNECTION_ERROR"
        assert "administrator" in message.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_query_role_isolation.py::TestRoleErrorClassification -v`
Expected: FAIL — `InsufficientPrivilege` is a `psycopg.Error`, so it currently returns a generic `"Query execution failed: ..."` message that exposes the role name.

- [ ] **Step 3: Add role error handling to `_classify_error`**

In `_classify_error`, add this check after the `QueryCanceled` check and before the generic `psycopg.Error` check:

```python
if isinstance(exc, psycopg.errors.InsufficientPrivilege):
    return (
        CONNECTION_ERROR,
        "Schema configuration error. Please contact an administrator.",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_query_role_isolation.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server/services/query.py tests/test_query_role_isolation.py
git commit -m "feat: classify SET ROLE failures as generic configuration errors"
```

---

## Chunk 4: Backfill Management Command

### Task 8: Create `backfill_readonly_roles` management command

**Files:**
- Create: `apps/workspaces/management/commands/backfill_readonly_roles.py`
- Test: `tests/test_backfill_readonly_roles.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_backfill_readonly_roles.py`:

```python
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from apps.workspaces.services.schema_manager import readonly_role_name


@pytest.mark.django_db
class TestBackfillReadonlyRoles:
    def test_backfills_active_tenant_schemas(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # role doesn't exist yet
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls)
        assert any("GRANT USAGE ON SCHEMA" in c for c in calls)
        assert any("ALTER DEFAULT PRIVILEGES" in c for c in calls)
        # Should also grant SELECT ON ALL TABLES for existing tables
        assert any("GRANT SELECT ON ALL TABLES" in c for c in calls)

    def test_skips_teardown_schemas(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="old_domain",
            state="teardown",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert not any("CREATE ROLE" in c for c in calls)

    def test_idempotent_existing_role(self, tenant_membership):
        from apps.workspaces.models import TenantSchema

        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (1,)  # role already exists
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            call_command("backfill_readonly_roles")

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        # Should NOT create the role (already exists)
        assert not any("CREATE ROLE" in c for c in calls)
        # But should still grant (idempotent grants are safe)
        assert any("GRANT USAGE ON SCHEMA" in c for c in calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backfill_readonly_roles.py -v`
Expected: FAIL with `CommandError: Unknown command: 'backfill_readonly_roles'`

- [ ] **Step 3: Write the management command**

Create `apps/workspaces/management/commands/backfill_readonly_roles.py`:

```python
"""Management command to backfill read-only PostgreSQL roles for existing schemas."""

import logging

import psycopg.sql
from django.core.management.base import BaseCommand

from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema
from apps.workspaces.services.schema_manager import (
    SchemaManager,
    get_managed_db_connection,
    readonly_role_name,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Create read-only PostgreSQL roles for all active tenant and view schemas. "
        "Idempotent — safe to run multiple times."
    )

    def handle(self, *args, **options):
        conn = get_managed_db_connection()
        cursor = conn.cursor()
        mgr = SchemaManager()

        try:
            # Backfill tenant schemas
            tenant_schemas = TenantSchema.objects.filter(
                state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
            )
            for ts in tenant_schemas:
                self._backfill_schema(cursor, mgr, ts.schema_name)
                self.stdout.write(f"  Backfilled role for schema: {ts.schema_name}")

            # Backfill view schemas
            view_schemas = WorkspaceViewSchema.objects.filter(
                state=SchemaState.ACTIVE,
            ).select_related("workspace")
            for vs in view_schemas:
                self._backfill_schema(cursor, mgr, vs.schema_name)
                # Grant access to constituent tenant schemas
                role = readonly_role_name(vs.schema_name)
                tenant_schemas_for_ws = TenantSchema.objects.filter(
                    tenant__in=vs.workspace.tenants.all(),
                    state=SchemaState.ACTIVE,
                )
                for ts in tenant_schemas_for_ws:
                    cursor.execute(
                        psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                            psycopg.sql.Identifier(ts.schema_name),
                            psycopg.sql.Identifier(role),
                        )
                    )
                    cursor.execute(
                        psycopg.sql.SQL(
                            "GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}"
                        ).format(
                            psycopg.sql.Identifier(ts.schema_name),
                            psycopg.sql.Identifier(role),
                        )
                    )
                self.stdout.write(f"  Backfilled role for view schema: {vs.schema_name}")

            self.stdout.write(self.style.SUCCESS("Done."))
        finally:
            cursor.close()
            conn.close()

    def _backfill_schema(self, cursor, mgr, schema_name: str) -> None:
        """Create role and grants for a single schema."""
        mgr._create_readonly_role(cursor, schema_name)
        # Also grant on existing tables (ALTER DEFAULT PRIVILEGES only covers future tables)
        role = readonly_role_name(schema_name)
        cursor.execute(
            psycopg.sql.SQL(
                "GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}"
            ).format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role),
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_backfill_readonly_roles.py -v`
Expected: ALL PASS (3 tests)

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add apps/workspaces/management/commands/backfill_readonly_roles.py tests/test_backfill_readonly_roles.py
git commit -m "feat: add backfill_readonly_roles management command"
```

---

## Chunk 5: Final Verification

### Task 9: Run full test suite and lint

- [ ] **Step 1: Run linter**

Run: `uv run ruff check .`
Expected: No errors

- [ ] **Step 2: Run formatter**

Run: `uv run ruff format --check .`
Expected: No changes needed (or run `uv run ruff format .` to fix)

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=60`
Expected: ALL PASS

- [ ] **Step 4: Review all changes**

Run: `git diff main --stat`

Verify only the expected files were modified:
- `apps/workspaces/services/schema_manager.py` — modified
- `mcp_server/services/query.py` — modified
- `mcp_server/context.py` — modified
- `apps/workspaces/management/commands/backfill_readonly_roles.py` — new
- `tests/test_schema_manager.py` — modified
- `tests/test_query_role_isolation.py` — new
- `tests/test_backfill_readonly_roles.py` — new

- [ ] **Step 5: Final commit (if any lint fixes)**

```bash
git add -A && git commit -m "chore: lint fixes for tenant role isolation"
```
