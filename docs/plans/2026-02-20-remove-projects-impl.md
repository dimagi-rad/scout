# Remove Projects Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the Project model with TenantWorkspace as the anchor for all workspace features (artifacts, learnings, recipes, knowledge), delete all project-only code, and simplify the MCP context layer.

**Architecture:** TenantWorkspace is a new per-tenant model holding system_prompt and data_dictionary. All workspace models (Artifact, Recipe, AgentLearning, KnowledgeEntry, TableKnowledge) get their FK changed from Project to TenantWorkspace. The agent graph drops its project branch and always uses the tenant flow with all tools (including local tools that were previously project-only). MCP context is renamed from ProjectContext to QueryContext with allowed/excluded tables removed.

**Tech Stack:** Django 5, DRF, LangGraph, FastMCP, React 19, Zustand, TypeScript

**Constraints:** No data migration needed. No backwards compatibility. Drop allowed_tables/excluded_tables entirely.

---

### Task 1: Create TenantWorkspace model and migration

**Files:**
- Modify: `apps/projects/models.py`
- Create: new migration via `makemigrations`

**Step 1: Add TenantWorkspace model to projects/models.py**

Add after the `MaterializationRun` class (line ~265), before `ProjectRole`:

```python
class TenantWorkspace(models.Model):
    """Per-tenant workspace holding agent config and serving as FK target for workspace models."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Domain name (CommCare) or organization ID. One workspace per tenant.",
    )
    tenant_name = models.CharField(max_length=255)
    system_prompt = models.TextField(
        blank=True,
        help_text="Tenant-specific system prompt. Merged with the base agent prompt.",
    )
    data_dictionary = models.JSONField(
        null=True,
        blank=True,
        help_text="Auto-generated schema documentation.",
    )
    data_dictionary_generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant_name"]

    def __str__(self):
        return f"{self.tenant_name} ({self.tenant_id})"
```

**Step 2: Run makemigrations and migrate**

Run: `uv run python manage.py makemigrations projects --name add_tenant_workspace`
Run: `uv run python manage.py migrate`

**Step 3: Commit**

```
git add apps/projects/models.py apps/projects/migrations/
git commit -m "feat: add TenantWorkspace model"
```

---

### Task 2: Rename ProjectContext to QueryContext and simplify

**Files:**
- Modify: `mcp_server/context.py`
- Modify: `mcp_server/services/query.py`
- Modify: `mcp_server/services/sql_validator.py`
- Modify: `mcp_server/server.py`

**Step 2a: Rewrite mcp_server/context.py**

Replace the entire file. Key changes:
- `ProjectContext` → `QueryContext`
- Remove `project_id`, `project_name`, `allowed_tables`, `excluded_tables`, `readonly_role`
- Add `tenant_id`, `schema_name`
- Delete `load_project_context` and `from_project`
- `load_tenant_context` returns `QueryContext`

```python
"""Context for the MCP server.

Holds configuration as an immutable snapshot for tenant-based queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueryContext:
    """Immutable snapshot of tenant query configuration for tool handlers."""

    tenant_id: str
    schema_name: str
    max_rows_per_query: int = 500
    max_query_timeout_seconds: int = 30
    connection_params: dict[str, Any] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class TenantContext:
    """Immutable snapshot of tenant context for tool handlers."""

    tenant_id: str
    user_id: str
    provider: str
    schema_name: str
    oauth_tokens: dict[str, str] = None  # type: ignore[assignment]
    max_rows_per_query: int = 500
    max_query_timeout_seconds: int = 30


async def load_tenant_context(tenant_id: str) -> QueryContext:
    """Load a QueryContext for a tenant from the managed database.

    Uses the tenant_id (domain name) to find the TenantSchema and builds
    a QueryContext pointing at the managed DB with the tenant's schema.

    Raises ValueError if the tenant schema is not found or not active.
    """
    from urllib.parse import urlparse

    from asgiref.sync import sync_to_async
    from django.conf import settings

    from apps.projects.models import SchemaState, TenantSchema

    ts = await TenantSchema.objects.filter(
        tenant_membership__tenant_id=tenant_id,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()

    if ts is None:
        raise ValueError(
            f"No active schema for tenant '{tenant_id}'. "
            f"Run materialization first to load data."
        )

    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise ValueError("MANAGED_DATABASE_URL is not configured")

    connection_params = await sync_to_async(_parse_db_url)(url, ts.schema_name)

    return QueryContext(
        tenant_id=tenant_id,
        schema_name=ts.schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params=connection_params,
    )


def _parse_db_url(url: str, schema: str) -> dict:
    """Parse a database URL into psycopg2 connection params."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") or "scout",
        "user": parsed.username or "",
        "password": parsed.password or "",
        "options": f"-c search_path={schema},public -c statement_timeout=30000",
    }
```

**Step 2b: Update mcp_server/services/query.py**

Replace all `ProjectContext` references with `QueryContext`. Remove `_ProjectShim` and `get_pool_manager` dependency. Use direct psycopg2 connections instead of the connection pool manager (which was designed for user-provided DB credentials).

Key changes:
- `from mcp_server.context import QueryContext` (was ProjectContext)
- Remove `from apps.projects.services.db_manager import get_pool_manager`
- Replace `_ProjectShim` + pool manager with direct psycopg2 connections using `ctx.connection_params`
- Remove `ctx.allowed_tables` / `ctx.excluded_tables` from `_build_validator`
- Use `ctx.schema_name` instead of `ctx.db_schema`
- Remove `ctx.project_name` from log messages, use `ctx.tenant_id`

```python
"""
Query execution service for the MCP server.

Validates and executes read-only SQL queries against a tenant's database schema.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async

from mcp_server.context import QueryContext
from mcp_server.envelope import (
    CONNECTION_ERROR,
    INTERNAL_ERROR,
    QUERY_TIMEOUT,
    VALIDATION_ERROR,
    error_response,
)
from mcp_server.services.sql_validator import SQLValidationError, SQLValidator

logger = logging.getLogger(__name__)


def _build_validator(ctx: QueryContext) -> SQLValidator:
    """Create a SQLValidator configured from the query context."""
    return SQLValidator(
        schema=ctx.schema_name,
        allowed_schemas=[],
        max_limit=ctx.max_rows_per_query,
    )


def _get_connection(ctx: QueryContext):
    """Create a psycopg2 connection from context params."""
    import psycopg2

    return psycopg2.connect(**ctx.connection_params)


def _execute_sync(ctx: QueryContext, sql: str, timeout_seconds: int) -> dict[str, Any]:
    """Run a SQL query synchronously."""
    from psycopg2 import sql as psql

    with _get_connection(ctx) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                psql.SQL("SET search_path TO {}").format(psql.Identifier(ctx.schema_name))
            )
            cursor.execute("SET statement_timeout TO %s", (f"{timeout_seconds}s",))
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
            cursor.close()


def _execute_sync_parameterized(
    ctx: QueryContext, sql: str, params: tuple, timeout_seconds: int
) -> dict[str, Any]:
    """Run a parameterized SQL query synchronously. No validation or LIMIT injection."""
    from psycopg2 import sql as psql

    with _get_connection(ctx) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                psql.SQL("SET search_path TO {}").format(psql.Identifier(ctx.schema_name))
            )
            cursor.execute("SET statement_timeout TO %s", (f"{timeout_seconds}s",))
            cursor.execute(sql, params)

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
            cursor.close()


async def execute_internal_query(
    ctx: QueryContext, sql: str, params: tuple = ()
) -> dict[str, Any]:
    """Execute a trusted internal query, bypassing SQL validation."""
    try:
        return await sync_to_async(_execute_sync_parameterized)(
            ctx, sql, params, ctx.max_query_timeout_seconds
        )
    except Exception as e:
        code, message = _classify_error(e)
        logger.error("Internal query error: %s", message, exc_info=True)
        return error_response(code, message)


async def execute_query(ctx: QueryContext, sql: str) -> dict[str, Any]:
    """Validate and execute a SQL query, returning a structured result dict."""
    validator = _build_validator(ctx)

    try:
        statement = validator.validate(sql)
    except SQLValidationError as e:
        logger.warning("SQL validation failed for tenant %s: %s", ctx.tenant_id, e.message)
        return error_response(VALIDATION_ERROR, e.message)

    tables_accessed = validator.get_tables_accessed(statement)

    modified = validator.inject_limit(statement)
    sql_executed = modified.sql(dialect=validator.dialect)

    truncated = False
    original_limit = statement.args.get("limit")
    if original_limit:
        limit_val = validator._get_limit_value(original_limit)
        if limit_val and limit_val > validator.max_limit:
            truncated = True

    try:
        result = await sync_to_async(_execute_sync)(ctx, sql_executed, ctx.max_query_timeout_seconds)
    except Exception as e:
        code, message = _classify_error(e)
        logger.error("Query error for tenant %s: %s", ctx.tenant_id, message, exc_info=True)
        return error_response(code, message)

    if result["row_count"] == validator.max_limit:
        truncated = True

    return {
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "truncated": truncated,
        "sql_executed": sql_executed,
        "tables_accessed": tables_accessed,
    }


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify a database exception into an error code and user-safe message."""
    import psycopg2
    import psycopg2.errors

    if isinstance(exc, psycopg2.errors.QueryCanceled):
        return QUERY_TIMEOUT, "Query timed out. Consider adding filters or limiting the data range."

    if isinstance(exc, psycopg2.Error):
        msg = str(exc)
        if "password authentication failed" in msg.lower():
            return CONNECTION_ERROR, "Database authentication failed. Please contact an administrator."
        if "could not connect" in msg.lower():
            return CONNECTION_ERROR, "Could not connect to the database. Please try again later."
        if "does not exist" in msg.lower():
            return VALIDATION_ERROR, f"Database error: {msg}"
        return CONNECTION_ERROR, f"Query execution failed: {msg}"

    return INTERNAL_ERROR, "An unexpected error occurred while executing the query."
```

**Step 2c: Update mcp_server/services/sql_validator.py**

Remove `allowed_tables` and `excluded_tables` fields and all related validation logic.

In the `SQLValidator` dataclass (~line 155-162), remove:
```python
    allowed_tables: list[str] = field(default_factory=list)
    excluded_tables: list[str] = field(default_factory=list)
```

In `_validate_tables` method, remove the allowed/excluded table checks (the method that checks `self.excluded_tables` and `self.allowed_tables`). Keep only the schema validation.

**Step 2d: Update mcp_server/server.py**

Change import: `from mcp_server.context import load_tenant_context` (no change needed since it already only imports this).

**Step 2e: Run tests to verify MCP changes**

Run: `uv run pytest tests/test_mcp_tenant_tools.py tests/test_sql_validator.py -v`

**Step 2f: Commit**

```
git add mcp_server/
git commit -m "refactor: rename ProjectContext to QueryContext, remove allowed/excluded tables"
```

---

### Task 3: Re-scope workspace models to TenantWorkspace

**Files:**
- Modify: `apps/knowledge/models.py` — Change FK on TableKnowledge, KnowledgeEntry, AgentLearning from Project to TenantWorkspace. Delete GoldenQuery and EvalRun.
- Modify: `apps/artifacts/models.py` — Change FK on Artifact from Project to TenantWorkspace. Change SharedArtifact access level from PROJECT to TENANT.
- Modify: `apps/recipes/models.py` — Change FK on Recipe from Project to TenantWorkspace.
- Modify: `apps/chat/models.py` — Remove `project` FK from Thread. Keep `tenant_membership`.
- Create: migrations for all four apps

**Step 3a: Update apps/knowledge/models.py**

For `TableKnowledge`, `KnowledgeEntry`, `AgentLearning`: change the FK field from:
```python
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="..."
    )
```
to:
```python
    workspace = models.ForeignKey(
        "projects.TenantWorkspace", on_delete=models.CASCADE, related_name="..."
    )
```

Delete the `GoldenQuery` and `EvalRun` model classes entirely.

Update `__str__` methods that reference `self.project.name` → `self.workspace.tenant_name`.

Update unique_together on TableKnowledge: `["workspace", "table_name"]`.

Update index on AgentLearning: `fields=["workspace", "is_active", "-confidence_score"]`.

**Step 3b: Update apps/artifacts/models.py**

Change `Artifact.project` FK to `Artifact.workspace`:
```python
    workspace = models.ForeignKey(
        "projects.TenantWorkspace",
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
```

Update indexes: `fields=["workspace", "-created_at"]`.

Change `AccessLevel.PROJECT` to `AccessLevel.TENANT`:
```python
    TENANT = "tenant", "Tenant Members Only"
```

Update `SharedArtifact.can_access()`:
```python
    if self.access_level == AccessLevel.TENANT:
        from apps.users.models import TenantMembership
        return TenantMembership.objects.filter(
            user=user, tenant_id=self.artifact.workspace.tenant_id
        ).exists()
```

Update `Artifact.create_new_version` to use `workspace` instead of `project`.

**Step 3c: Update apps/recipes/models.py**

Change `Recipe.project` FK to `Recipe.workspace`:
```python
    workspace = models.ForeignKey(
        "projects.TenantWorkspace",
        on_delete=models.CASCADE,
        related_name="recipes",
    )
```

Update indexes: `["workspace", "is_shared"]`, `["workspace", "created_by"]`.

Update `__str__`: `self.workspace.tenant_name`.

**Step 3d: Update apps/chat/models.py**

Remove the `project` FK entirely. The `tenant_membership` FK is sufficient.

Remove the index on `["project", "user", "-updated_at"]`. Add index on `["tenant_membership", "user", "-updated_at"]`.

**Step 3e: Generate and run migrations**

Run: `uv run python manage.py makemigrations knowledge artifacts recipes chat --name rescope_to_workspace`
Run: `uv run python manage.py migrate`

**Step 3f: Commit**

```
git add apps/knowledge/models.py apps/artifacts/models.py apps/recipes/models.py apps/chat/models.py
git add apps/knowledge/migrations/ apps/artifacts/migrations/ apps/recipes/migrations/ apps/chat/migrations/
git commit -m "refactor: re-scope workspace models from Project to TenantWorkspace"
```

---

### Task 4: Update tool factories to use TenantWorkspace

**Files:**
- Modify: `apps/agents/tools/learning_tool.py`
- Modify: `apps/agents/tools/artifact_tool.py`
- Modify: `apps/agents/tools/recipe_tool.py`

**Step 4a: Update learning_tool.py**

Change the TYPE_CHECKING import:
```python
if TYPE_CHECKING:
    from apps.projects.models import TenantWorkspace
    from apps.users.models import User
```

Change function signature: `def create_save_learning_tool(workspace: TenantWorkspace, user: User):`

Inside `save_learning`:
- Change `project.data_dictionary` → `workspace.data_dictionary`
- Change `AgentLearning.objects.filter(project=project, ...)` → `AgentLearning.objects.filter(workspace=workspace, ...)`
- Change `AgentLearning.objects.create(project=project, ...)` → `AgentLearning.objects.create(workspace=workspace, ...)`
- Change log messages from `project.slug` → `workspace.tenant_id`

**Step 4b: Update artifact_tool.py**

Change TYPE_CHECKING import and function signature similarly.

Inside `create_artifact`:
- `Artifact.objects.create(project=project, ...)` → `Artifact.objects.create(workspace=workspace, ...)`
- Log messages: `project.slug` → `workspace.tenant_id`

Inside `update_artifact`:
- `Artifact.objects.get(id=artifact_id, project=project)` → `Artifact.objects.get(id=artifact_id, workspace=workspace)`
- Log messages: `project.slug` → `workspace.tenant_id`

**Step 4c: Update recipe_tool.py**

Same pattern:
- `Recipe.objects.create(project=project, ...)` → `Recipe.objects.create(workspace=workspace, ...)`
- Log messages: `project.slug` → `workspace.tenant_id`

**Step 4d: Commit**

```
git add apps/agents/tools/
git commit -m "refactor: update tool factories to use TenantWorkspace"
```

---

### Task 5: Update agent graph to tenant-only flow

**Files:**
- Modify: `apps/agents/graph/base.py`
- Modify: `apps/agents/graph/state.py`

**Step 5a: Simplify build_agent_graph in base.py**

Remove `project` parameter entirely. Remove `_build_system_prompt(project)`. Remove `_build_tools(project, ...)`. Merge local tools into the main flow.

New signature:
```python
def build_agent_graph(
    tenant_membership: "TenantMembership",
    user: "User | None" = None,
    checkpointer: "BaseCheckpointSaver | None" = None,
    mcp_tools: list | None = None,
    oauth_tokens: dict | None = None,
):
```

Key changes:
- Remove the `if project:` / `else:` branching — always use tenant flow
- Create local tools using workspace: `_build_tools(workspace, user, mcp_tools or [])`
- Get or create TenantWorkspace at the top of the function
- Build system prompt from workspace: `_build_system_prompt(workspace, tenant_membership)`
- Injection map always: `{"tenant_id": "tenant_id", "tenant_membership_id": "tenant_membership_id"}`
- Remove `from apps.projects.models import Project` TYPE_CHECKING import
- Remove `from apps.projects.services.data_dictionary import DataDictionaryGenerator`

Update `_build_tools` to accept workspace:
```python
def _build_tools(workspace: TenantWorkspace, user: User | None, mcp_tools: list) -> list:
    tools = list(mcp_tools)
    tools.append(create_save_learning_tool(workspace, user))
    tools.extend(create_artifact_tools(workspace, user))
    tools.append(create_recipe_tool(workspace, user))
    return tools
```

Update `_build_system_prompt` → merge the old `_build_system_prompt(project)` and `_build_tenant_system_prompt`:
```python
def _build_system_prompt(workspace: TenantWorkspace, tenant_membership: TenantMembership) -> str:
    sections = [BASE_SYSTEM_PROMPT]
    sections.append(ARTIFACT_PROMPT_ADDITION)

    if workspace.system_prompt:
        sections.append(f"\n## Tenant-Specific Instructions\n\n{workspace.system_prompt}\n")

    retriever = KnowledgeRetriever(workspace)
    knowledge_context = retriever.retrieve()
    if knowledge_context:
        sections.append(f"\n## Knowledge Base\n\n{knowledge_context}\n")

    sections.append(f"""
## Tenant Context

- Tenant: {tenant_membership.tenant_name} ({tenant_membership.tenant_id})
- Provider: {tenant_membership.provider}

## Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.

You can materialize data from CommCare using the `run_materialization` tool.
""")
    return "\n".join(sections)
```

**Step 5b: Clean up state.py**

Remove `project_id` from `AgentState` if present. Keep tenant fields.

**Step 5c: Commit**

```
git add apps/agents/graph/
git commit -m "refactor: simplify agent graph to tenant-only flow with all tools"
```

---

### Task 6: Update KnowledgeRetriever to use TenantWorkspace

**Files:**
- Modify: `apps/knowledge/services/retriever.py`

**Step 6a: Update retriever**

Change constructor to accept workspace:
```python
class KnowledgeRetriever:
    def __init__(self, workspace):
        self.workspace = workspace

    def retrieve(self) -> str:
        ...
        KnowledgeEntry.objects.filter(workspace=self.workspace)
        TableKnowledge.objects.filter(workspace=self.workspace)
        AgentLearning.objects.filter(workspace=self.workspace, is_active=True)
        ...
```

**Step 6b: Commit**

```
git add apps/knowledge/services/retriever.py
git commit -m "refactor: update KnowledgeRetriever to use TenantWorkspace"
```

---

### Task 7: Update chat views

**Files:**
- Modify: `apps/chat/views.py`

**Step 7a: Remove project references from chat views**

- Remove `from apps.projects.models import ProjectMembership` import
- Remove `_get_membership()` helper function
- In `_upsert_thread`: remove `project_id` parameter. Only use `tenant_membership`.
- In `_list_threads`: remove `project_id` parameter. Only filter by `tenant_membership_id`.
- In `thread_list_view`: remove `project_id` support. Only accept `tenant_id`.
- In `_get_public_thread`: remove `select_related("project")`.
- In `chat_view`: add get-or-create for TenantWorkspace:

```python
from apps.projects.models import TenantWorkspace

workspace, _ = await TenantWorkspace.objects.aget_or_create(
    tenant_id=tenant_membership.tenant_id,
    defaults={"tenant_name": tenant_membership.tenant_name},
)
```

Pass workspace context if needed, but build_agent_graph now handles this internally by looking up workspace from tenant_membership.

**Step 7b: Commit**

```
git add apps/chat/views.py
git commit -m "refactor: remove project references from chat views"
```

---

### Task 8: Delete project-only code

**Files to delete entirely:**
- `apps/projects/api/views.py` — Project CRUD views
- `apps/projects/api/serializers.py` — Project serializers
- `apps/projects/api/permissions.py` — ProjectPermissionMixin
- `apps/projects/api/connections.py` — DatabaseConnectionViewSet
- `apps/projects/api/csv_import.py` — CSV import
- `apps/projects/api/data_dictionary.py` — Project-scoped data dictionary views
- `apps/projects/views.py` — ProjectListView (keep `health_check` if it's here)
- `apps/projects/services/db_manager.py` — ConnectionPoolManager
- `apps/projects/services/rate_limiter.py` — Project rate limiter
- `apps/projects/management/commands/bootstrap_sample_data.py`
- `apps/projects/management/commands/generate_data_dictionary.py`
- `apps/knowledge/management/commands/run_eval.py`
- `apps/knowledge/management/commands/import_knowledge.py`
- `apps/knowledge/services/eval_runner.py`
- `apps/artifacts/api/views.py` — Project-scoped artifact views (ProjectArtifactListView, ProjectArtifactDetailView)
- `apps/artifacts/views.py` — Same if it contains ProjectArtifactListView

**Files to modify:**
- `apps/projects/models.py` — Delete Project, ProjectMembership, ProjectRole, DatabaseConnection classes. Keep TenantWorkspace, TenantSchema, MaterializationRun, SchemaState.
- `apps/projects/admin.py` — Remove admin registrations for deleted models. Add TenantWorkspace admin.
- `apps/projects/urls.py` — Remove all project CRUD URLs. Can be emptied or deleted.

**Step 8a: Clean up models.py**

Remove these classes from `apps/projects/models.py`:
- `DatabaseConnection` (lines 20-111)
- `Project` (lines 114-203)
- `ProjectRole` (lines 268-274)
- `ProjectMembership` (lines 276-299)
- `schema_validator` (lines 14-17)
- The `from cryptography.fernet import Fernet` import
- The `from django.core.validators import RegexValidator` import

Keep: `TenantWorkspace`, `TenantSchema`, `SchemaState`, `MaterializationRun`, and the new import for uuid and django.

**Step 8b: Delete files listed above**

Use `git rm` for each file.

**Step 8c: Update apps/projects/admin.py**

Rewrite to only register TenantWorkspace, TenantSchema, MaterializationRun:

```python
from django.contrib import admin
from .models import MaterializationRun, TenantSchema, TenantWorkspace


@admin.register(TenantWorkspace)
class TenantWorkspaceAdmin(admin.ModelAdmin):
    list_display = ["tenant_name", "tenant_id", "created_at", "updated_at"]
    search_fields = ["tenant_name", "tenant_id"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(TenantSchema)
class TenantSchemaAdmin(admin.ModelAdmin):
    list_display = ["schema_name", "state", "tenant_membership", "created_at"]
    list_filter = ["state"]
    readonly_fields = ["id", "created_at"]


@admin.register(MaterializationRun)
class MaterializationRunAdmin(admin.ModelAdmin):
    list_display = ["pipeline", "state", "tenant_schema", "started_at", "completed_at"]
    list_filter = ["state", "pipeline"]
    readonly_fields = ["id", "started_at"]
```

**Step 8d: Update config/urls.py**

Remove:
- `from apps.artifacts.views import ProjectArtifactDetailView, ProjectArtifactListView`
- `path("api/projects/", include("apps.projects.urls"))`
- `path("api/projects/<uuid:project_id>/knowledge/", include("apps.knowledge.urls"))`
- Both `api/projects/<uuid:project_id>/artifacts/` paths
- `path("api/projects/<uuid:project_id>/recipes/", include("apps.recipes.urls"))`

Keep workspace-scoped API routes if needed (can be added later). For now, remove all project-scoped routes.

**Step 8e: Update apps/projects/urls.py**

Empty it or delete it since no project routes remain. If `health_check` was imported from `apps/projects/views.py`, move it to a standalone location or inline it.

**Step 8f: Generate migration for model deletions**

Run: `uv run python manage.py makemigrations projects --name remove_project_models`
Run: `uv run python manage.py migrate`

**Step 8g: Commit**

```
git add -A
git commit -m "refactor: delete Project, DatabaseConnection, ProjectMembership and all project-only code"
```

---

### Task 9: Update and fix tests

**Files:**
- Modify: `tests/conftest.py` — Replace `db_connection` fixture with `workspace` fixture
- Modify: `tests/test_models.py` — Remove project/GoldenQuery tests, add TenantWorkspace tests
- Modify: `tests/test_mcp_server.py` — Use QueryContext instead of ProjectContext
- Modify: `tests/test_mcp_tenant_tools.py` — Use QueryContext instead of ProjectContext
- Modify: `tests/test_sql_validator.py` — Remove allowed/excluded table tests
- Modify: `tests/test_knowledge_retriever.py` — Use workspace fixture
- Modify: `tests/test_artifacts.py` — Use workspace fixture
- Modify: `tests/test_recipes.py` — Use workspace fixture
- Modify: `tests/test_data_dictionary.py` — Update to use workspace
- Modify: `tests/test_mcp_chat_integration.py` — Use tenant flow
- Delete: `tests/test_eval_runner.py` — Eval models removed
- Delete: `tests/test_data_dictionary_api.py` — Project-scoped API removed
- Delete or update: `apps/artifacts/tests/test_share_api.py` — Use workspace

**Step 9a: Update conftest.py**

Replace `db_connection` fixture with:
```python
@pytest.fixture
def workspace(db):
    from apps.projects.models import TenantWorkspace
    return TenantWorkspace.objects.create(
        tenant_id="test-domain",
        tenant_name="Test Domain",
    )
```

Add `tenant_membership` fixture:
```python
@pytest.fixture
def tenant_membership(db, user):
    from apps.users.models import TenantMembership
    return TenantMembership.objects.create(
        user=user,
        provider="commcare",
        tenant_id="test-domain",
        tenant_name="Test Domain",
    )
```

**Step 9b: Update test_mcp_server.py and test_mcp_tenant_tools.py**

Change all `ProjectContext(...)` to `QueryContext(...)` with the new field names:
```python
from mcp_server.context import QueryContext

QueryContext(
    tenant_id="test-tenant",
    schema_name="test_schema",
    max_rows_per_query=500,
    max_query_timeout_seconds=30,
    connection_params={...},
)
```

Remove tests for `load_project_context`. Keep tests for `load_tenant_context`.

**Step 9c: Update test_sql_validator.py**

Remove all tests for `allowed_tables` and `excluded_tables`:
- `test_allowed_tables`
- `test_reject_non_allowed_tables`
- `test_excluded_tables`
- `test_allowed_tables_precedence`
- Any tests passing `allowed_tables=` or `excluded_tables=` to `SQLValidator`

**Step 9d: Update remaining test files**

For each: replace `project=project` fixtures/kwargs with `workspace=workspace`.

**Step 9e: Run full test suite**

Run: `uv run pytest -v`
Fix any remaining failures.

**Step 9f: Commit**

```
git add tests/ apps/artifacts/tests/
git commit -m "test: update all tests from Project to TenantWorkspace"
```

---

### Task 10: Update frontend

**Files:**
- Delete: `frontend/src/store/projectSlice.ts`
- Modify: `frontend/src/store/store.ts` — Remove project slice
- Modify: `frontend/src/store/index.ts` — Remove project exports
- Delete: `frontend/src/pages/ProjectsPage/` (entire directory)
- Delete: `frontend/src/components/ProjectSelector/`
- Modify: `frontend/src/router.tsx` — Remove project routes
- Modify: `frontend/src/components/AppLayout/AppLayout.tsx` — Remove project fetch
- Modify: `frontend/src/store/artifactSlice.ts` — Change from projectId to workspaceId URL pattern (or remove if not yet wired to new API)
- Modify: `frontend/src/store/knowledgeSlice.ts` — Same
- Modify: `frontend/src/store/recipeSlice.ts` — Same
- Modify: `frontend/src/store/dictionarySlice.ts` — Same
- Modify: `frontend/src/pages/DataSourcesPage/DataSourcesPage.tsx` — Remove project CSV import
- Modify: `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx` — Remove project dependency
- Modify: `frontend/src/pages/ArtifactsPage/ArtifactsPage.tsx` — Remove activeProjectId
- Modify: `frontend/src/pages/KnowledgePage/KnowledgePage.tsx` — Remove activeProjectId
- Modify: `frontend/src/pages/RecipesPage/` — Remove activeProjectId
- Modify: `frontend/src/components/ChatPanel/ChatPanel.tsx` — Change "project" sharing label to "tenant"

**Step 10a: Delete project-only frontend files**

```bash
rm -rf frontend/src/store/projectSlice.ts
rm -rf frontend/src/pages/ProjectsPage/
rm -rf frontend/src/components/ProjectSelector/
```

**Step 10b: Update store**

Remove project slice from `store.ts` and `index.ts`.

**Step 10c: Update router**

Remove project routes from `router.tsx`.

**Step 10d: Update remaining pages**

For pages that used `activeProjectId`, either:
- Remove the feature temporarily (if backend API not yet re-scoped to workspace)
- Or update to use workspace-scoped APIs when those are added later

For now, remove the `activeProjectId` dependencies and any "No project selected" guards. These features (knowledge page, recipes page, artifacts page, data dictionary page, data sources page) can be re-wired to workspace-scoped APIs in a follow-up task.

**Step 10e: Run frontend build to verify**

Run: `cd frontend && bun run build`

**Step 10f: Commit**

```
git add -A frontend/
git commit -m "refactor: remove all project references from frontend"
```

---

### Task 11: Final cleanup and verification

**Step 11a: Run ruff**

Run: `uv run ruff check . --fix`
Run: `uv run ruff format .`

**Step 11b: Run full test suite**

Run: `uv run pytest -v`

**Step 11c: Run frontend lint**

Run: `cd frontend && bun run lint`

**Step 11d: Verify no remaining project references**

Run: `grep -r "from apps.projects.models import.*Project[^a-zA-Z]" apps/ mcp_server/ tests/ --include="*.py" | grep -v TenantWorkspace | grep -v migration`

This should return nothing.

**Step 11e: Final commit if any fixes**

```
git add -A
git commit -m "chore: final cleanup after project removal"
```
