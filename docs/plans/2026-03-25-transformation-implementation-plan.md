# Transformation Architecture Implementation Plan

## Overview

This plan implements the Transformation Architecture v3 spec. It moves Scout from raw JSON-heavy source tables to a managed analytical model using dbt, with three transformation scopes: **system** (provider standardization), **tenant** (shared business rules), and **workspace** (workspace-specific compositions).

The plan is organized into **7 milestones**, each a single pull request. Milestones 2-7 are sequential (each depends on the prior). Milestone 1 is independent and can land in parallel with any of them. Each milestone is broken into discrete tasks suitable for an LLM coding agent to execute one at a time.

---

## Current State Summary

**What exists today:**
- Raw data ingestion: `materializer.py` loads CommCare cases/forms and Connect sources into per-tenant PostgreSQL schemas via hardcoded table writers. Tables are named `cases`, `forms`, `visits`, `users`, etc.
- Metadata discovery: `CommCareMetadataLoader` fetches app definitions (case types, form structures, questions) and stores them in `TenantMetadata.metadata` as a JSON blob
- dbt infrastructure: `dbt_runner.py` provides programmatic dbt execution via `dbtRunner` Python API with `profiles.yml` generation and a module-level thread lock for serialization
- Pipeline registry: YAML-based pipeline configs (`commcare_sync.yml`, `connect_sync.yml`) with source/transform/relationship definitions. The `transforms` section in `commcare_sync.yml` references `transforms/commcare` which does not exist on disk — the transform phase is effectively a no-op today
- Schema management: `SchemaManager` provisions per-tenant PG schemas with read-only roles
- Multi-tenant workspaces: `WorkspaceViewSchema` creates UNION ALL views across tenant schemas with a `_tenant` discriminator column
- Agent context: System prompt injects schema metadata (tables + columns) via `pipeline_list_tables`/`pipeline_describe_table` from `mcp_server/services/metadata.py`

**What this plan adds:**
- `apps/transformations/` Django app with `TransformationAsset`, `TransformationRun`, `TransformationAssetRun` models
- `raw_*` table prefix convention for ingested source data
- Dynamic SQL generation for CommCare staging models (one table per case type, one per form, child tables for repeat groups)
- Three-stage dbt execution pipeline: system → tenant → workspace
- `replaces` chain resolution for agent context (terminal model preference)
- `get_lineage` MCP tool for progressive discoverability
- Per-tenant namespaced views for multi-tenant workspaces (replacing UNION ALL)
- Data quality tests via dbt YAML schema tests
- REST API for authoring and managing transformation assets

---

## Milestone 1: Multi-Tenant Namespace Views

**Goal:** Replace the UNION ALL view strategy in `WorkspaceViewSchema` with per-tenant namespaced views (`{canonical_name}__{table_name}`).

**Why first / independent:** This touches only `SchemaManager.build_view_schema()` and its tests. Zero overlap with the transformation asset work. It can land whenever it's ready.

### Task 1.1: Replace UNION ALL with namespaced aliasing views

**File to modify:** `apps/workspaces/services/schema_manager.py`

**Current behavior** (`build_view_schema` starting at line 159): For each table that exists in any tenant schema, creates a `UNION ALL` view combining rows from all tenant schemas, with a synthetic `_tenant` column to discriminate. Tenant schemas are identified by `(schema_name, tenant_external_id)` tuples.

**New behavior:** For each tenant, create simple aliasing views in the workspace schema with the naming pattern `{prefix}__{table_name}`, where `prefix` is derived from `Tenant.canonical_name` (slugified via `_sanitize_schema_name`).

**Changes to `build_view_schema`:**

Replace the inner loop (the "Step 2" and "Step 3" sections, roughly lines 217-281) with:

```python
# Step 2+3: Create per-tenant namespaced views
for schema_name, tenant_external_id in tenant_schemas:
    # Resolve the Tenant to get canonical_name for the prefix
    from apps.users.models import Tenant
    tenant_obj = Tenant.objects.get(external_id=tenant_external_id)
    prefix = self._sanitize_schema_name(tenant_obj.canonical_name)

    # Discover all tables in this tenant's schema
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')",
        (schema_name,),
    )
    for (table_name,) in cursor.fetchall():
        view_name = f"{prefix}__{table_name}"
        cursor.execute(
            psycopg.sql.SQL(
                "CREATE OR REPLACE VIEW {}.{} AS SELECT * FROM {}.{}"
            ).format(
                psycopg.sql.Identifier(view_schema_name),
                psycopg.sql.Identifier(view_name),
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(table_name),
            )
        )
```

The `_tenant` discriminator column is no longer added — tables are disambiguated by prefix.

The read-only role grants section (lines 288-301) stays the same.

Update the log message at the end to count the actual views created rather than `len(all_tables)`.

### Task 1.2: Update agent system prompt for multi-tenant workspaces

**File to modify:** `apps/agents/graph/base.py`

In `_build_system_prompt()`, the `elif tenant_count > 1:` branch (line 493) currently says "Use workspace_id when calling MCP tools. Call `list_tables` to see available tables."

Update to explain the namespace convention:

```python
sections.append(
    "\n## Data Availability\n\n"
    "This is a multi-tenant workspace. Tables are prefixed with the tenant name "
    "using double underscore: `{tenant_name}__{table_name}`.\n"
    "To query across tenants, use explicit JOINs between namespaced tables.\n"
    "Call `list_tables` to see all available tables.\n"
)
```

### Task 1.3: Update tests

**File to modify:** `tests/test_view_schema_builder.py`

Update existing tests and add new ones:

- Two tenants with the same table names produce `{tenant_a}__{table}` and `{tenant_b}__{table}` — no UNION ALL, no `_tenant` column
- View names use slugified `canonical_name`, not `external_id`
- Double underscore separator is consistent
- Views are simple `SELECT * FROM schema.table` aliases
- Read-only role has access to the view schema and both underlying tenant schemas
- A tenant with 3 tables produces 3 namespaced views (not unions)
- Tables added by future transformation assets (e.g. `stg_*`) also get namespaced views (they're just tables in the schema — no special handling needed)

### Task 1.4: Update `pipeline_list_tables` for workspace view schemas

**File to modify:** `mcp_server/services/metadata.py`

When listing tables for a workspace with a view schema, the table names in `information_schema` will now be `{prefix}__{table_name}` instead of just `{table_name}`. The existing `pipeline_list_tables` function queries by `MaterializationRun.result.sources` keys (e.g., `"cases"`, `"forms"`), which won't match the namespaced view names.

Add a helper or update `pipeline_list_tables` to handle workspace context: when called for a workspace view schema, query `information_schema.tables` directly for the view schema instead of relying on `MaterializationRun` source names.

**File to modify:** `mcp_server/server.py`

In `list_tables()` (line 68), when `workspace_id` is provided and a `WorkspaceViewSchema` exists, use the new workspace-aware table listing.

---

## Milestone 2: Raw Table Prefix Convention

**Goal:** Rename ingested source tables from `cases`/`forms`/`visits`/etc. to `raw_cases`/`raw_forms`/`raw_visits`/etc. This establishes the `raw_*` → `stg_*` → user-named layering convention before any transformation assets exist.

**Why separate:** This is a small, focused behavioral change. It affects the materializer, metadata service, and pipeline config — all code that will be further modified in later milestones. Landing it now means later milestones start from a clean baseline where `raw_*` tables already exist.

**Backwards compatibility:** The materializer does `DROP TABLE IF EXISTS ... CASCADE` before recreating tables. The next materialization run after this PR merges will create `raw_*` tables. No data migration needed.

### Task 2.1: Add `physical_table_name` to `SourceConfig`

**File to modify:** `mcp_server/pipeline_registry.py`

Add a computed property to `SourceConfig` (line 17):

```python
@dataclass
class SourceConfig:
    name: str
    description: str = ""
    table_name: str = ""  # Explicit override; empty = use default

    @property
    def physical_table_name(self) -> str:
        """Physical PostgreSQL table name. Defaults to raw_{name}."""
        return self.table_name or f"raw_{self.name}"
```

Update `_parse_pipeline()` to parse `table_name` from YAML if present:
```python
SourceConfig(
    name=s["name"],
    description=s.get("description", ""),
    table_name=s.get("table_name", ""),
)
```

### Task 2.2: Rename table writers in materializer

**File to modify:** `mcp_server/services/materializer.py`

For CommCare:
- `_write_cases()`: Change all references from `cases` to `raw_cases` — the `CREATE TABLE`, `INSERT`, and `DROP TABLE IF EXISTS` statements (lines 292-377)
- `_write_forms()`: Change all references from `forms` to `raw_forms` (lines 309-422)
- The `_CASES_INSERT` and `_FORMS_INSERT` SQL templates: update `{schema}.cases` → `{schema}.raw_cases`, `{schema}.forms` → `{schema}.raw_forms`

For Connect:
- `_write_connect_visits()`: `visits` → `raw_visits`
- `_write_connect_users()`: `users` → `raw_users`
- `_write_connect_completed_works()`: `completed_works` → `raw_completed_works`
- `_write_connect_payments()`: `payments` → `raw_payments`
- `_write_connect_invoices()`: `invoices` → `raw_invoices`
- `_write_connect_assessments()`: `assessments` → `raw_assessments`
- `_write_connect_completed_modules()`: `completed_modules` → `raw_completed_modules`

Same pattern for each: update `DROP TABLE`, `CREATE TABLE`, and `INSERT` statements.

### Task 2.3: Update metadata service for raw table names

**File to modify:** `mcp_server/services/metadata.py`

- `pipeline_list_tables()` (line 24): Use `source.physical_table_name` instead of `source.name` for the table name in results
- `pipeline_describe_table()` (line 74): No changes needed (it takes `table_name` as a parameter)
- `_build_jsonb_annotations()` (line 121): Update `table_name == "cases"` → `table_name == "raw_cases"` and `table_name == "forms"` → `table_name == "raw_forms"`

### Task 2.4: Update pipeline YAML relationships

**File to modify:** `pipelines/commcare_sync.yml`

```yaml
relationships:
  - from_table: raw_forms
    from_column: case_ids
    to_table: raw_cases
    to_column: case_id
    description: "Form submissions reference the cases they update (case_ids is a JSON array)"
```

**File to modify:** `pipelines/connect_sync.yml`

Update all relationship `from_table`/`to_table` entries to use `raw_` prefix.

### Task 2.5: Update agent schema context

**File to modify:** `apps/agents/graph/base.py`

In `_fetch_schema_context()`, the tables returned by `pipeline_list_tables()` will now be named `raw_cases`, `raw_forms`, etc. No code changes needed in this file — the function already uses the table names as returned. But verify that the rendering functions (`_render_compact_schema`, `_render_full_schema`) still work correctly with the new names.

### Task 2.6: Update tests

**Files to modify:**
- `tests/test_materializer.py` — Update expected table names in assertions
- `tests/test_metadata_service.py` — Update expected table names
- `tests/test_pipeline_registry.py` — Add test for `SourceConfig.physical_table_name` property
- `tests/test_mcp_server.py` — Update any table name assertions

Add a new test: `SourceConfig` with no `table_name` defaults to `raw_{name}`.

---

## Milestone 3: Transformations Django App & Models

**Goal:** Create `apps/transformations/` with the three core models. Pure schema — no behavioral changes to existing code.

### Task 3.1: Create the Django app scaffolding

**Files to create:**
- `apps/transformations/__init__.py` — empty
- `apps/transformations/apps.py`
- `apps/transformations/models.py`
- `apps/transformations/admin.py`
- `apps/transformations/migrations/__init__.py` — empty

**`apps.py`:**
```python
from django.apps import AppConfig

class TransformationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.transformations"
    verbose_name = "Transformations"
```

**Register in `config/settings/base.py`:** Add `"apps.transformations"` to `INSTALLED_APPS` after `"apps.chat"` (line 65).

### Task 3.2: Define `TransformationAsset` model

**File:** `apps/transformations/models.py`

```python
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class TransformationScope(models.TextChoices):
    SYSTEM = "system"
    TENANT = "tenant"
    WORKSPACE = "workspace"


class TransformationAsset(models.Model):
    """A dbt model definition stored as a first-class asset.

    Each asset corresponds to one .sql file that dbt will execute,
    producing one table or view.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255,
        help_text="dbt model name. Must be unique within scope+container.",
    )
    description = models.TextField(blank=True)
    scope = models.CharField(max_length=20, choices=TransformationScope.choices)
    tenant = models.ForeignKey(
        "users.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="transformation_assets",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="transformation_assets",
    )
    sql_content = models.TextField(
        help_text="dbt model SQL. Uses ref() within scope, direct table names across scopes.",
    )
    replaces = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replaced_by",
        help_text="The TransformationAsset this model supersedes for querying.",
    )
    test_yaml = models.TextField(
        blank=True,
        help_text="dbt schema test YAML for this model.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(tenant__isnull=False, workspace__isnull=True)
                    | models.Q(tenant__isnull=True, workspace__isnull=False)
                ),
                name="transformation_asset_one_container",
            ),
            models.UniqueConstraint(
                fields=["name", "scope", "tenant"],
                condition=models.Q(tenant__isnull=False),
                name="unique_asset_name_per_tenant_scope",
            ),
            models.UniqueConstraint(
                fields=["name", "scope", "workspace"],
                condition=models.Q(workspace__isnull=False),
                name="unique_asset_name_per_workspace_scope",
            ),
        ]
        ordering = ["scope", "name"]

    def __str__(self):
        container = self.tenant or self.workspace
        return f"{self.scope}:{self.name} ({container})"

    def clean(self):
        if self.scope in (TransformationScope.SYSTEM, TransformationScope.TENANT):
            if not self.tenant_id:
                raise ValidationError("System and tenant scoped assets require a tenant.")
            if self.workspace_id:
                raise ValidationError("System and tenant scoped assets must not have a workspace.")
        elif self.scope == TransformationScope.WORKSPACE:
            if not self.workspace_id:
                raise ValidationError("Workspace scoped assets require a workspace.")
            if self.tenant_id:
                raise ValidationError("Workspace scoped assets must not have a tenant.")
```

### Task 3.3: Define `TransformationRun` model

**File:** `apps/transformations/models.py` (append)

```python
class TransformationRunStatus(models.TextChoices):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TransformationRun(models.Model):
    """Pipeline-level execution record for a full transformation cycle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "users.Tenant",
        on_delete=models.CASCADE,
        related_name="transformation_runs",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="transformation_runs",
    )
    status = models.CharField(
        max_length=20,
        choices=TransformationRunStatus.choices,
        default=TransformationRunStatus.PENDING,
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"TransformationRun({self.tenant}, {self.status})"
```

### Task 3.4: Define `TransformationAssetRun` model

**File:** `apps/transformations/models.py` (append)

```python
class AssetRunStatus(models.TextChoices):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class TransformationAssetRun(models.Model):
    """Per-model execution record within a pipeline run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        TransformationRun,
        on_delete=models.CASCADE,
        related_name="asset_runs",
    )
    asset = models.ForeignKey(
        TransformationAsset,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    status = models.CharField(
        max_length=20,
        choices=AssetRunStatus.choices,
        default=AssetRunStatus.PENDING,
    )
    duration_ms = models.IntegerField(null=True, blank=True)
    logs = models.TextField(blank=True)
    test_results = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at"]

    def __str__(self):
        return f"AssetRun({self.asset.name}, {self.status})"
```

### Task 3.5: Create migration and admin

Run: `uv run python manage.py makemigrations transformations`

**File:** `apps/transformations/admin.py`

```python
from django.contrib import admin

from .models import TransformationAsset, TransformationAssetRun, TransformationRun


class TransformationAssetRunInline(admin.TabularInline):
    model = TransformationAssetRun
    readonly_fields = ("asset", "status", "duration_ms", "started_at", "completed_at")
    extra = 0


@admin.register(TransformationAsset)
class TransformationAssetAdmin(admin.ModelAdmin):
    list_display = ("name", "scope", "tenant", "workspace", "replaces", "updated_at")
    list_filter = ("scope",)
    search_fields = ("name", "description")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(TransformationRun)
class TransformationRunAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "workspace", "status", "started_at", "completed_at")
    list_filter = ("status",)
    inlines = [TransformationAssetRunInline]
    readonly_fields = ("id", "started_at")


@admin.register(TransformationAssetRun)
class TransformationAssetRunAdmin(admin.ModelAdmin):
    list_display = ("asset", "run", "status", "duration_ms", "started_at")
    list_filter = ("status",)
    readonly_fields = ("id", "started_at")
```

### Task 3.6: Write model tests

**File to create:** `tests/test_transformation_models.py`

Test cases:
- Create system-scoped asset with tenant set, workspace null — succeeds
- Create tenant-scoped asset with tenant set, workspace null — succeeds
- Create workspace-scoped asset with workspace set, tenant null — succeeds
- Create asset with both tenant and workspace set — fails (DB check constraint via `IntegrityError`)
- Create asset with neither tenant nor workspace — fails (DB check constraint)
- `clean()` validates scope-to-container mapping: system scope with workspace raises `ValidationError`
- Unique constraint: two assets with same name+scope+tenant — fails
- Unique constraint: same name in different scopes on same tenant — succeeds
- `replaces` FK: can point to another asset; `replaced_by` reverse relation works
- `TransformationRun` status transitions: pending → running → completed
- `TransformationAssetRun` links correctly to both run and asset
- `TransformationAssetRun.test_results` round-trips JSON correctly

Use `pytest-django` with `@pytest.mark.django_db`. Create `Tenant` and `Workspace` instances directly for FK setup (follow existing test patterns in `tests/test_workspace_models.py`).

---

## Milestone 4: Dynamic SQL Generation for CommCare Staging

**Goal:** Generate `TransformationAsset` records (scope=system) from CommCare metadata. One dbt model per case type, one per form xmlns, one per repeat group. Assets are stored in the database but **not yet executed** — execution comes in Milestone 5.

**Why this works without execution:** After this milestone, the system generates and stores the SQL models. Since the existing transform phase is already a no-op (the `transforms/commcare` directory doesn't exist), adding asset records doesn't change runtime behavior. Milestone 5 will wire up the executor.

### Task 4.1: Create the staging SQL generator

**Files to create:**
- `apps/transformations/services/__init__.py` — empty
- `apps/transformations/services/commcare_staging.py`

**Core interface:**

```python
def generate_system_assets(tenant, metadata: dict) -> list[TransformationAsset]:
    """Generate unsaved TransformationAsset instances for all system staging models.

    Reads the metadata dict (from TenantMetadata.metadata) which has:
    - case_types: list of {"name": str, "app_id": str, "app_name": str, ...}
    - form_definitions: dict keyed by xmlns, each with "name", "questions", etc.
    - app_definitions: list of raw app JSON

    Returns unsaved TransformationAsset instances with scope=SYSTEM.
    """

def upsert_system_assets(tenant, tenant_metadata) -> dict:
    """Generate and upsert system staging TransformationAssets for a tenant.

    Calls generate_system_assets(), then update_or_create for each.
    Returns {"created": int, "updated": int, "total": int}.
    """
```

### Task 4.2: Implement case type SQL generation

**File:** `apps/transformations/services/commcare_staging.py`

```python
def _generate_case_type_asset(
    tenant, case_type_name: str, properties: list[str], metadata: dict
) -> TransformationAsset:
    """Generate a staging asset for a single case type.

    Produces SQL like:
        SELECT
            case_id,
            case_type,
            case_name,
            owner_id,
            date_opened::timestamp AS date_opened,
            last_modified::timestamp AS last_modified,
            closed,
            properties->>'prop_name' AS prop_name,
            ...
        FROM raw_cases
        WHERE case_type = 'case_type_name'

    Model name: stg_case_{slugified_case_type}
    """
```

**Extracting case properties from metadata:** Walk `metadata["app_definitions"]` → `modules[]`. For each module where `module.case_type == case_type_name`, collect property names from `module.case_properties` (a list of dicts with `"key"` fields, or sometimes just a list of strings). Union across all modules/apps for that case type.

**Raw table references use direct table names, not `ref()`.** The `raw_cases` and `raw_forms` tables are created by the materializer, not by dbt — so they aren't dbt models and can't be referenced via `ref()` or `source()`. Since dbt runs with `search_path` set to the tenant schema, a bare `FROM raw_cases` resolves correctly. Repeat group child tables, by contrast, reference their parent staging model (which *is* a dbt model) via `{{ ref('stg_form_...') }}` — see Task 4.4.

### Task 4.3: Implement form SQL generation

**File:** `apps/transformations/services/commcare_staging.py`

```python
def _generate_form_asset(
    tenant, form_xmlns: str, form_def: dict
) -> TransformationAsset:
    """Generate a staging asset for a single form.

    Produces SQL extracting typed columns from raw_forms.form_data JSONB.
    Model name: stg_form_{slugified_form_name}
    """
```

**Question parsing:** `form_def["questions"]` is a list of question dicts. Each has:
- `value` — question path (e.g., `/data/patient_name`)
- `label` — display label
- `type` — question type
- `repeat` — repeat group path if inside a repeat group (or null/absent)

For non-repeat questions, generate:
```sql
form_data #>> '{data,patient_name}' AS patient_name
```

The `#>>` operator navigates nested JSON paths. Convert the `/data/patient_name` path to the `{data,patient_name}` PostgreSQL JSON path format.

**Type casting:** Map CommCare question types to PostgreSQL:
- `Text`, `Barcode`, `PhoneNumber`, `Select`, `MultiSelect` → no cast (TEXT)
- `Int` → `::integer` with `NULLIF(..., '')` guard
- `Double`, `Decimal` → `::numeric` with `NULLIF` guard
- `Date` → `::date` with `NULLIF` guard
- `DateTime` → `::timestamp` with `NULLIF` guard
- `GeoPoint` → TEXT (preserve as-is)

**Disambiguation:** If two forms across different apps have the same slugified name, append `_{app_slug}` to the second one. Track seen names during generation.

### Task 4.4: Implement repeat group SQL generation

**File:** `apps/transformations/services/commcare_staging.py`

```python
def _generate_repeat_group_asset(
    tenant, form_name_slug: str, group_path: str, child_questions: list[dict]
) -> TransformationAsset:
    """Generate a staging asset for a repeat group child table.

    Produces SQL using jsonb_array_elements to unnest the repeat array.
    Model name: stg_form_{form_name}__repeat_{group_name}
    """
```

Identify repeat groups by scanning questions where `question.get("repeat")` is set. Group these questions by their repeat path. For each repeat group:

```sql
SELECT
    f.form_id,
    row_number() OVER (PARTITION BY f.form_id ORDER BY elem.ordinality) AS repeat_index,
    elem.value->>'child_question' AS child_question,
    ...
FROM {{ ref('stg_form_{form_name}') }} f,
LATERAL jsonb_array_elements(
    f.form_data #> '{data,repeat_group_path}'
) WITH ORDINALITY AS elem(value, ordinality)
WHERE f.form_data #> '{data,repeat_group_path}' IS NOT NULL
```

**Parent reference uses `ref()`.** Unlike raw tables (which are materializer-created and referenced by direct name), the parent `stg_form_*` model IS a dbt model in the same system scope. Using `{{ ref('stg_form_...') }}` ensures dbt builds the parent before the child via its DAG.

### Task 4.5: Implement slugification and the upsert function

**File:** `apps/transformations/services/commcare_staging.py`

```python
def slugify_model_name(name: str) -> str:
    """Convert a form/case name to a valid dbt model name.

    - Lowercase
    - Replace spaces, hyphens, dots with underscores
    - Strip non-alphanumeric (except underscores)
    - Collapse consecutive underscores
    - Strip leading/trailing underscores
    """

def upsert_system_assets(tenant, tenant_metadata) -> dict:
    """Main entry point. Generates all system assets and upserts them.

    Uses TransformationAsset.objects.update_or_create() with lookup on
    (name=..., scope='system', tenant=tenant).

    Sets created_by=None for system assets.
    """
```

### Task 4.6: Integrate into the discover phase

**File to modify:** `mcp_server/services/materializer.py`

In `run_pipeline()`, after the DISCOVER phase successfully stores metadata (line 94, after `_run_discover_phase` returns), add:

```python
# Generate system staging assets from discovered metadata
if pipeline.provider == "commcare":
    from apps.transformations.services.commcare_staging import upsert_system_assets
    tenant_meta = TenantMetadata.objects.filter(
        tenant_membership=tenant_membership
    ).first()
    if tenant_meta:
        asset_result = upsert_system_assets(tenant_membership.tenant, tenant_meta)
        logger.info(
            "System assets for %s: %d created, %d updated",
            tenant_membership.tenant.external_id,
            asset_result["created"],
            asset_result["updated"],
        )
```

This is a safe addition — it only creates/updates DB records, doesn't execute anything. The assets sit dormant until Milestone 5 wires up the executor.

### Task 4.7: Write tests

**File to create:** `tests/test_commcare_staging_generator.py`

Create a metadata fixture based on the actual `CommCareMetadataLoader` output structure (reference `tests/test_commcare_metadata_loader.py` for the shape).

Test cases:
- Metadata with 2 case types → generates 2 `stg_case_*` assets with correct SQL
- Metadata with 3 forms → generates 3 `stg_form_*` assets
- Form with questions inside a repeat group → generates a `stg_form_*__repeat_*` child asset
- Generated SQL for case type extracts properties from JSONB with correct paths
- Generated SQL for form extracts questions from `form_data` JSONB
- Generated SQL for repeat group uses `jsonb_array_elements` with `WITH ORDINALITY`
- Repeat group SQL references parent via `{{ ref('stg_form_...') }}`
- Slugification: `"Follow-up Visit"` → `follow_up_visit`
- Slugification: `"Visit #2"` → `visit_2`
- Duplicate form names across apps: second gets `_{app_slug}` suffix
- Type mapping: `Int` question → `::integer` cast in SQL, `Date` → `::date`, etc.
- `upsert_system_assets` first run → all created
- `upsert_system_assets` second run with same metadata → all updated (0 created)
- `upsert_system_assets` second run with additional case type → creates the new one, updates existing
- Empty metadata (no case types, no forms) → returns `{"created": 0, "updated": 0, "total": 0}`

---

## Milestone 5: Three-Stage dbt Execution Pipeline

**Goal:** Replace the current no-op transform phase with a real three-stage executor that materializes `TransformationAsset` records as dbt models: system → tenant → workspace. This is where transforms actually start running.

### Task 5.1: Create the ephemeral dbt project writer

**File to create:** `apps/transformations/services/dbt_project.py`

```python
def write_dbt_project(
    output_dir: Path,
    project_name: str,
    assets: list[TransformationAsset],
) -> Path:
    """Write a complete dbt project directory from TransformationAsset records.

    Creates:
    - output_dir/dbt_project.yml
    - output_dir/models/{asset.name}.sql  (one per asset)
    - output_dir/models/schema.yml  (merged test YAML from assets that have test_yaml)

    Returns output_dir for convenience.
    """
```

**`dbt_project.yml` content:**
```yaml
name: '{project_name}'
version: '1.0.0'
config-version: 2
profile: 'data_explorer'
model-paths: ["models"]
test-paths: ["tests"]
models:
  +materialized: table
```

**Model files:** For each asset, write `models/{asset.name}.sql` with the asset's `sql_content`.

**Schema test merging:** For assets that have `test_yaml`, parse each as YAML and merge into a single `models/schema.yml`. Each asset's `test_yaml` is expected to be a YAML fragment defining tests for that specific model:
```yaml
models:
  - name: stg_case_patient
    columns:
      - name: case_id
        tests:
          - unique
          - not_null
```

Merge by concatenating the `models` lists from each fragment.

### Task 5.2: Add `run_dbt_test` to dbt_runner

**File to modify:** `mcp_server/services/dbt_runner.py`

Add a new function alongside `run_dbt()`:

```python
def run_dbt_test(
    dbt_project_dir: str,
    profiles_dir: str,
    models: list[str] | None = None,
) -> dict:
    """Run dbt tests via the programmatic Python API.

    Returns:
        {"success": bool, "tests": {test_unique_id: {"status": str, "message": str}}, "error": str | None}
    """
    cli_args = ["test", "--project-dir", dbt_project_dir, "--profiles-dir", profiles_dir]
    if models:
        cli_args.extend(["--select", " ".join(models)])

    with _dbt_lock:
        dbt = dbtRunner()
        res = dbt.invoke(cli_args)

    # ... parse res.result for test outcomes ...
```

### Task 5.3: Implement the three-stage executor

**File to create:** `apps/transformations/services/executor.py`

```python
from datetime import UTC, datetime
import logging
import tempfile
from pathlib import Path

from django.conf import settings

from apps.transformations.models import (
    AssetRunStatus,
    TransformationAsset,
    TransformationAssetRun,
    TransformationRun,
    TransformationRunStatus,
    TransformationScope,
)
from apps.transformations.services.dbt_project import write_dbt_project
from mcp_server.services.dbt_runner import generate_profiles_yml, run_dbt, run_dbt_test

logger = logging.getLogger(__name__)


def run_transformation_pipeline(
    tenant,
    schema_name: str,
    workspace=None,
    progress_callback=None,
) -> TransformationRun:
    """Execute the three-stage transformation pipeline.

    Stages run in order: system → tenant → workspace.
    Each stage writes an ephemeral dbt project to a temp dir and runs dbt.
    Per-model results are recorded in TransformationAssetRun.
    """
    run = TransformationRun.objects.create(
        tenant=tenant,
        workspace=workspace,
        status=TransformationRunStatus.RUNNING,
    )

    stages = [
        ("system", TransformationScope.SYSTEM, {"tenant": tenant, "scope": TransformationScope.SYSTEM}),
        ("tenant", TransformationScope.TENANT, {"tenant": tenant, "scope": TransformationScope.TENANT}),
    ]
    if workspace:
        stages.append(
            ("workspace", TransformationScope.WORKSPACE, {"workspace": workspace, "scope": TransformationScope.WORKSPACE})
        )

    try:
        for stage_name, scope, filters in stages:
            assets = list(TransformationAsset.objects.filter(**filters))
            if not assets:
                logger.info("Stage '%s': no assets, skipping", stage_name)
                continue
            if progress_callback:
                progress_callback(f"Running {stage_name} transforms ({len(assets)} models)...")
            _run_stage(run, assets, schema_name, stage_name)

        run.status = TransformationRunStatus.COMPLETED
        run.completed_at = datetime.now(UTC)
        run.save(update_fields=["status", "completed_at"])

    except Exception as e:
        logger.error("Transformation pipeline failed: %s", e)
        run.status = TransformationRunStatus.FAILED
        run.completed_at = datetime.now(UTC)
        run.error_message = str(e)
        run.save(update_fields=["status", "completed_at", "error_message"])
        # Don't re-raise — transform failures are isolated from the data load

    return run
```

**`_run_stage` implementation:**
```python
def _run_stage(run, assets, schema_name, stage_name):
    """Run a single stage: write dbt project, execute, record results."""
    # Create AssetRun records
    asset_runs = {}
    for asset in assets:
        ar = TransformationAssetRun.objects.create(
            run=run,
            asset=asset,
            status=AssetRunStatus.RUNNING,
        )
        asset_runs[asset.name] = ar

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir) / "project"
        profiles_dir = Path(tmpdir) / "profiles"
        profiles_dir.mkdir()

        write_dbt_project(
            output_dir=project_dir,
            project_name=f"scout_{stage_name}",
            assets=assets,
        )

        db_url = getattr(settings, "MANAGED_DATABASE_URL", "")
        generate_profiles_yml(
            output_path=profiles_dir / "profiles.yml",
            schema_name=schema_name,
            db_url=db_url,
        )

        # Run models
        model_names = [a.name for a in assets]
        result = run_dbt(
            dbt_project_dir=str(project_dir),
            profiles_dir=str(profiles_dir),
            models=model_names,
        )

        # Run tests if any assets define them
        test_results = {}
        if any(a.test_yaml for a in assets):
            test_results = run_dbt_test(
                dbt_project_dir=str(project_dir),
                profiles_dir=str(profiles_dir),
                models=model_names,
            )

        # Record per-asset results
        now = datetime.now(UTC)
        for asset in assets:
            ar = asset_runs[asset.name]
            model_status = result.get("models", {}).get(asset.name, "unknown")

            if model_status in ("success", "pass"):
                ar.status = AssetRunStatus.SUCCESS
            elif result.get("success") and model_status == "unknown":
                # dbt reported overall success but didn't list this model specifically
                ar.status = AssetRunStatus.SUCCESS
            else:
                ar.status = AssetRunStatus.FAILED
                ar.logs = result.get("error", f"Model status: {model_status}")

            if asset.name in test_results.get("tests", {}):
                ar.test_results = test_results["tests"][asset.name]

            ar.completed_at = now
            ar.save(update_fields=["status", "logs", "test_results", "completed_at"])

        if not result.get("success"):
            logger.warning(
                "Stage '%s' had failures: %s", stage_name, result.get("error")
            )
```

### Task 5.4: Integrate executor into materializer

**File to modify:** `mcp_server/services/materializer.py`

Replace `_run_transform_phase()` (lines 269-285):

```python
def _run_transform_phase(pipeline, schema_name, tenant, workspace=None):
    """Run the three-stage transformation pipeline using TransformationAsset records."""
    from apps.transformations.services.executor import run_transformation_pipeline

    run = run_transformation_pipeline(
        tenant=tenant,
        schema_name=schema_name,
        workspace=workspace,
    )

    result = {
        "run_id": str(run.id),
        "status": run.status,
        "asset_count": run.asset_runs.count(),
    }
    if run.error_message:
        result["error"] = run.error_message
    return result
```

Update the call site in `run_pipeline()` (around line 135):

**Before:**
```python
if pipeline.transforms and pipeline.dbt_models:
    report("Running DBT transforms...")
    try:
        transform_result = _run_transform_phase(pipeline, schema_name)
    except Exception as e:
        ...
else:
    report("No DBT transforms configured — skipping")
```

**After:**
```python
# Check if there are any TransformationAssets to execute
from apps.transformations.models import TransformationAsset
has_assets = TransformationAsset.objects.filter(tenant=tenant_membership.tenant).exists()
if has_assets:
    report("Running transforms...")
    try:
        transform_result = _run_transform_phase(
            pipeline, schema_name, tenant=tenant_membership.tenant
        )
    except Exception as e:
        logger.error("Transform phase failed for schema %s: %s", schema_name, e)
        transform_result = {"error": str(e)}
else:
    report("No transforms configured — skipping")
```

Note: the `tenant` variable needs to be threaded through. Add it as a parameter to `_run_transform_phase()`. The `pipeline.transforms` and `pipeline.dbt_models` check is replaced with the asset existence check.

### Task 5.5: Remove the legacy `transforms/` config from pipeline YAML

**File to modify:** `pipelines/commcare_sync.yml`

Remove the `transforms` section entirely:
```yaml
# Remove these lines:
# transforms:
#   dbt_project: transforms/commcare
#   models:
#     - stg_cases
#     - stg_forms
```

The `TransformConfig` dataclass in `pipeline_registry.py` can stay for now (it's harmless and removing it would require more changes to `PipelineConfig`). It will be effectively unused since no YAML defines a `transforms` section.

### Task 5.6: Write tests

**File to create:** `tests/test_transformation_executor.py`

Test cases (mock `run_dbt` and `run_dbt_test` for unit tests):
- System stage with 3 assets → creates 3 AssetRun records, all SUCCESS
- Tenant stage runs after system stage (sequential in one pipeline call)
- Workspace stage runs only if workspace is provided
- Empty stage (no assets for scope) → skipped, no error
- dbt failure on one model → that AssetRun is FAILED, others are SUCCESS, pipeline completes
- dbt total failure → all AssetRuns FAILED, TransformationRun is FAILED
- Test YAML compiled into `schema.yml` and `run_dbt_test` is called
- Test results stored in `AssetRun.test_results`
- Progress callback is called with stage names

**File to create:** `tests/test_dbt_project_writer.py`

Test cases:
- `write_dbt_project()` creates `dbt_project.yml` with correct content
- Creates one `.sql` file per asset in `models/`
- Assets with `test_yaml` → `models/schema.yml` is created with merged content
- Assets without `test_yaml` → no `schema.yml` created
- `dbt_project.yml` profile name matches `data_explorer` (matching existing `generate_profiles_yml`)

---

## Milestone 6: Agent Context Engineering & Lineage

**Goal:** Make the agent prefer terminal models (models not replaced by anything downstream), add a `get_lineage` MCP tool, and update the metadata service to be transformation-aware.

### Task 6.1: Implement terminal model resolution

**File to create:** `apps/transformations/services/lineage.py`

```python
def get_terminal_assets(
    tenant_ids: list,
    workspace_id=None,
) -> list[TransformationAsset]:
    """Return TransformationAssets that are not replaced by any downstream asset.

    Terminal = no other asset has replaces=this_asset.

    Returns assets visible in this context (by tenant_ids and/or workspace_id).
    """
    from apps.transformations.models import TransformationAsset

    # All assets visible to this context
    visible = TransformationAsset.objects.filter(
        models.Q(tenant_id__in=tenant_ids)
    )
    if workspace_id:
        visible = visible | TransformationAsset.objects.filter(workspace_id=workspace_id)

    # IDs that are pointed to by some other asset's `replaces`
    replaced_ids = TransformationAsset.objects.filter(
        replaces__isnull=False
    ).values_list("replaces_id", flat=True)

    return list(visible.exclude(id__in=replaced_ids))


def get_lineage_chain(
    asset_name: str,
    tenant_ids: list,
    workspace_id=None,
) -> list[dict]:
    """Follow the replaces chain backward from an asset to its root.

    Given model "cases_clean" which replaces "stg_case_patient":
    Returns [
        {"name": "cases_clean", "scope": "tenant", "description": "Cleaned cases..."},
        {"name": "stg_case_patient", "scope": "system", "description": "Staging..."},
    ]

    If the asset has no replaces chain, returns just the single asset.
    The raw source table is appended as the final entry if identifiable
    from the SQL (i.e., the terminal system model references raw_cases).
    """
    from apps.transformations.models import TransformationAsset

    try:
        asset = TransformationAsset.objects.get(
            name=asset_name,
            models.Q(tenant_id__in=tenant_ids) | models.Q(workspace_id=workspace_id),
        )
    except TransformationAsset.DoesNotExist:
        return []

    chain = []
    current = asset
    visited = set()  # Guard against cycles
    while current and current.id not in visited:
        visited.add(current.id)
        chain.append({
            "name": current.name,
            "scope": current.scope,
            "description": current.description,
        })
        current = current.replaces

    return chain
```

### Task 6.2: Update metadata service for transformation awareness

**File to modify:** `mcp_server/services/metadata.py`

Add a new function:

```python
def transformation_aware_list_tables(
    tenant_schema,
    pipeline_config,
    tenant_ids: list,
    workspace_id=None,
) -> list[dict]:
    """List tables combining raw sources with terminal transformation assets.

    If TransformationAsset records exist for the tenant, terminal models
    replace their upstream tables in the listing. Otherwise falls back
    to the standard pipeline_list_tables behavior.
    """
    from apps.transformations.services.lineage import get_terminal_assets

    terminal_assets = get_terminal_assets(tenant_ids, workspace_id)

    if not terminal_assets:
        # No transformation assets — use existing pipeline-based listing
        return pipeline_list_tables(tenant_schema, pipeline_config)

    # Build set of replaced table names (walk replaces chains)
    replaced_names = set()
    for asset in terminal_assets:
        current = asset.replaces
        while current:
            replaced_names.add(current.name)
            current = current.replaces

    # Start with raw tables, excluding replaced ones
    raw_tables = pipeline_list_tables(tenant_schema, pipeline_config)
    result = [t for t in raw_tables if t["name"] not in replaced_names]

    # Add terminal transformation assets
    for asset in terminal_assets:
        result.append({
            "name": asset.name,
            "type": "table",
            "description": asset.description,
            "row_count": None,
            "materialized_at": None,
            "scope": asset.scope,
        })

    return result
```

### Task 6.3: Update agent system prompt to prefer terminal models

**File to modify:** `apps/agents/graph/base.py`

Modify `_fetch_schema_context()` to try transformation-aware listing first:

```python
async def _fetch_schema_context(tenant, user) -> str:
    # ... existing TenantSchema check (lines 149-166) stays the same ...

    # Try transformation-aware listing
    from apps.transformations.services.lineage import get_terminal_assets

    terminal_assets = await sync_to_async(get_terminal_assets)(
        tenant_ids=[tenant.id]
    )

    if terminal_assets:
        # Use transformation-aware table listing
        from mcp_server.services.metadata import transformation_aware_list_tables
        tables = await sync_to_async(transformation_aware_list_tables)(
            ts, pipeline_config, tenant_ids=[tenant.id]
        )
        # ... render schema with a note about lineage tool ...
        # Add to the schema block:
        # "These tables are produced by a transformation pipeline.
        #  Use the `get_lineage` tool to explore how any table was built."
    else:
        # Fall back to existing pipeline_list_tables
        tables = await sync_to_async(pipeline_list_tables)(ts, pipeline_config)

    # ... rest of the rendering logic stays the same ...
```

### Task 6.4: Add `get_lineage` MCP tool

**File to modify:** `mcp_server/server.py`

Add a new tool (after `get_metadata`):

```python
@mcp.tool()
async def get_lineage(tenant_id: str, model_name: str, workspace_id: str | None = None) -> dict:
    """Get the transformation lineage for a model.

    Returns the chain of transformations from the given model back to the raw
    source data, showing what each step does and why. Use this when the user
    asks about data provenance, how a table was created, or what cleaning
    or transformations were applied to the data.

    Args:
        tenant_id: The tenant identifier.
        model_name: Name of the model to trace lineage for.
        workspace_id: Optional workspace UUID.
    """
    from apps.transformations.services.lineage import get_lineage_chain
    from apps.users.models import Tenant

    async with tool_context("get_lineage", tenant_id, model_name=model_name) as tc:
        try:
            tenant = await Tenant.objects.aget(external_id=tenant_id)
        except Tenant.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Tenant '{tenant_id}' not found")
            return tc["result"]

        chain = await sync_to_async(get_lineage_chain)(
            model_name, tenant_ids=[tenant.id], workspace_id=workspace_id
        )

        if not chain:
            tc["result"] = error_response(
                NOT_FOUND, f"No transformation asset named '{model_name}' found"
            )
            return tc["result"]

        tc["result"] = success_response(
            {"model": model_name, "lineage": chain},
            tenant_id=tenant_id,
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Register for context injection:** Add `"get_lineage"` to `MCP_TOOL_NAMES` in `apps/agents/graph/base.py` (line 55).

### Task 6.5: Write tests

**File to create:** `tests/test_lineage.py`

Test cases for `get_terminal_assets`:
- No assets → returns empty list
- Single asset, no replaces → it is terminal
- Asset A replaced by B → terminal is B, not A
- Chain: A → B → C (C replaces B, B replaces A) → terminal is C
- Multiple independent chains → returns all terminal nodes
- Assets from different tenants → filtered correctly by `tenant_ids`
- Workspace assets included when `workspace_id` provided

Test cases for `get_lineage_chain`:
- Asset with no replaces → chain is `[self]`
- Asset replacing another → chain is `[self, replaced]`
- Three-deep chain → chain is `[self, middle, root]`
- Asset not found → returns `[]`
- Cycle guard: if somehow A replaces B replaces A, doesn't infinite loop

Test cases for `transformation_aware_list_tables`:
- No assets → falls back to `pipeline_list_tables` output
- Terminal asset replacing a raw table → raw table excluded, terminal included
- Mixed: some raw tables have no replacing asset → they appear alongside terminals

Test cases for agent context:
- When terminal assets exist, system prompt includes them (not the replaced tables)
- System prompt includes lineage tool hint text
- When no assets exist, falls back to existing behavior

---

## Milestone 7: REST API for Transformation Assets

**Goal:** Expose CRUD endpoints for transformation assets, run listing, and manual run triggering. This enables the future management and authoring UX.

### Task 7.1: Create serializers

**File to create:** `apps/transformations/serializers.py`

```python
from rest_framework import serializers
from .models import TransformationAsset, TransformationAssetRun, TransformationRun


class TransformationAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransformationAsset
        fields = [
            "id", "name", "description", "scope", "tenant", "workspace",
            "sql_content", "replaces", "test_yaml", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]


class TransformationAssetRunSerializer(serializers.ModelSerializer):
    asset_name = serializers.CharField(source="asset.name", read_only=True)

    class Meta:
        model = TransformationAssetRun
        fields = [
            "id", "asset", "asset_name", "status", "duration_ms",
            "logs", "test_results", "started_at", "completed_at",
        ]
        read_only_fields = fields


class TransformationRunSerializer(serializers.ModelSerializer):
    asset_runs = TransformationAssetRunSerializer(many=True, read_only=True)

    class Meta:
        model = TransformationRun
        fields = [
            "id", "tenant", "workspace", "status", "started_at",
            "completed_at", "error_message", "asset_runs",
        ]
        read_only_fields = fields


class LineageResponseSerializer(serializers.Serializer):
    """Read-only serializer for lineage chain entries."""
    name = serializers.CharField()
    scope = serializers.CharField()
    description = serializers.CharField()
```

### Task 7.2: Create views with permission enforcement

**File to create:** `apps/transformations/views.py`

```python
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db import models

from apps.workspaces.models import WorkspaceRole
from .models import TransformationAsset, TransformationRun, TransformationScope
from .serializers import (
    TransformationAssetSerializer,
    TransformationRunSerializer,
    LineageResponseSerializer,
)


class TransformationAssetViewSet(viewsets.ModelViewSet):
    """CRUD for transformation assets with scope-based permissions.

    - System assets: read-only (403 on create/update/delete)
    - Tenant assets: any user with TenantMembership for that tenant
    - Workspace assets: users with read_write or manage role
    """
    serializer_class = TransformationAssetSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_ids = user.tenant_memberships.values_list("tenant_id", flat=True)
        workspace_ids = user.workspace_memberships.values_list("workspace_id", flat=True)

        qs = TransformationAsset.objects.filter(
            models.Q(tenant_id__in=tenant_ids) | models.Q(workspace_id__in=workspace_ids)
        )

        # Optional filters
        scope = self.request.query_params.get("scope")
        if scope:
            qs = qs.filter(scope=scope)
        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        workspace_id = self.request.query_params.get("workspace_id")
        if workspace_id:
            qs = qs.filter(workspace_id=workspace_id)

        return qs

    def perform_create(self, serializer):
        scope = serializer.validated_data.get("scope")
        if scope == TransformationScope.SYSTEM:
            raise PermissionDenied("System assets cannot be created via the API.")
        self._check_write_permission(serializer.validated_data)
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.scope == TransformationScope.SYSTEM:
            raise PermissionDenied("System assets cannot be modified.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.scope == TransformationScope.SYSTEM:
            raise PermissionDenied("System assets cannot be deleted.")
        instance.delete()

    def _check_write_permission(self, data):
        """Verify the user has write access to the target container."""
        user = self.request.user
        if data.get("workspace"):
            has_write = user.workspace_memberships.filter(
                workspace=data["workspace"],
                role__in=[WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE],
            ).exists()
            if not has_write:
                raise PermissionDenied("You need read_write or manage role on this workspace.")

    @action(detail=True, methods=["get"])
    def lineage(self, request, pk=None):
        """GET /api/transformations/assets/{id}/lineage/"""
        from .services.lineage import get_lineage_chain
        asset = self.get_object()
        tenant_ids = list(request.user.tenant_memberships.values_list("tenant_id", flat=True))
        workspace_id = asset.workspace_id
        chain = get_lineage_chain(asset.name, tenant_ids, workspace_id)
        serializer = LineageResponseSerializer(chain, many=True)
        return Response(serializer.data)


class TransformationRunViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only access to transformation run history."""
    serializer_class = TransformationRunSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_ids = user.tenant_memberships.values_list("tenant_id", flat=True)
        workspace_ids = user.workspace_memberships.values_list("workspace_id", flat=True)

        qs = TransformationRun.objects.filter(
            models.Q(tenant_id__in=tenant_ids) | models.Q(workspace_id__in=workspace_ids)
        )

        tenant_id = self.request.query_params.get("tenant_id")
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)

        return qs.prefetch_related("asset_runs__asset")

    @action(detail=False, methods=["post"])
    def trigger(self, request):
        """POST /api/transformations/runs/trigger/

        Body: {"tenant_id": "...", "workspace_id": "..." (optional)}
        Triggers a transformation run asynchronously.
        """
        from apps.users.models import Tenant
        from apps.workspaces.models import TenantSchema
        from .services.executor import run_transformation_pipeline

        tenant_id = request.data.get("tenant_id")
        if not tenant_id:
            return Response({"error": "tenant_id is required"}, status=400)

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=404)

        ts = TenantSchema.objects.filter(tenant=tenant, state="active").first()
        if not ts:
            return Response({"error": "No active schema for this tenant"}, status=400)

        workspace = None
        workspace_id = request.data.get("workspace_id")
        if workspace_id:
            from apps.workspaces.models import Workspace
            workspace = Workspace.objects.filter(id=workspace_id).first()

        # Run synchronously for now (Celery integration is future work)
        run = run_transformation_pipeline(
            tenant=tenant,
            schema_name=ts.schema_name,
            workspace=workspace,
        )

        serializer = TransformationRunSerializer(run)
        return Response(serializer.data, status=201)
```

### Task 7.3: Wire up URLs

**File to create:** `apps/transformations/urls.py`

```python
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"assets", views.TransformationAssetViewSet, basename="transformation-asset")
router.register(r"runs", views.TransformationRunViewSet, basename="transformation-run")

urlpatterns = [
    path("", include(router.urls)),
]
```

**File to modify:** `config/urls.py`

Add: `path("api/transformations/", include("apps.transformations.urls"))`

### Task 7.4: Write API tests

**File to create:** `tests/test_transformation_api.py`

Test cases:
- **List assets:** Authenticated user sees assets for their tenants and workspaces
- **List with filters:** `?scope=tenant`, `?tenant_id=...`, `?workspace_id=...` work correctly
- **Create tenant asset:** User with TenantMembership can create a tenant-scoped asset
- **Create workspace asset:** User with `read_write` role can create a workspace-scoped asset
- **Create system asset → 403:** Cannot create system-scoped assets via API
- **Update system asset → 403:** Cannot modify system-scoped assets
- **Delete system asset → 403:** Cannot delete system-scoped assets
- **Update tenant asset:** Tenant member can update
- **Delete tenant asset:** Tenant member can delete
- **Workspace read role → 403 on create:** User with `read` role cannot create workspace assets
- **Lineage endpoint:** `GET /api/transformations/assets/{id}/lineage/` returns correct chain
- **List runs:** Returns paginated results with nested asset runs
- **Trigger endpoint:** `POST /api/transformations/runs/trigger/` creates a run
- **Trigger without active schema → 400**
- **Unauthenticated → 401 / 403**

---

## Cross-Cutting Concerns

### Database Migrations

All new models are in `apps/transformations/`. A single migration covers `TransformationAsset`, `TransformationRun`, and `TransformationAssetRun`. Created in Milestone 3 via `makemigrations`.

### Backwards Compatibility

**Raw table rename (Milestone 2):** The materializer does `DROP TABLE IF EXISTS ... CASCADE` before recreating tables. The next materialization run after the PR merges will create `raw_*` tables. Old `cases`/`forms` tables become orphaned until the schema is torn down or re-materialized. No data migration needed.

**Pipeline YAML `transforms` removal (Milestone 5):** The old transform phase was a no-op (directory didn't exist). Removing the config section changes nothing at runtime.

**Agent context (Milestone 6):** Falls back to existing `pipeline_list_tables` behavior when no `TransformationAsset` records exist. No agent behavior changes until a tenant actually has assets.

### Error Handling Strategy

- **System asset generation failures** (Milestone 4): Logged but do not fail the pipeline. The DISCOVER phase stores metadata regardless; if SQL generation fails for one form, others still succeed.
- **dbt execution failures** (Milestone 5): Per-model. A failed model records `FAILED` in its `TransformationAssetRun`. Other models in the same stage still run (dbt continues past individual failures). The `TransformationRun` is marked `FAILED` only if dbt itself crashes, not if individual models fail.
- **Transform phase failures** (Milestone 5): Isolated from the data load, matching existing behavior. The `MaterializationRun` is still marked `COMPLETED` if load succeeded.

### Performance

- dbt execution is serialized via `_dbt_lock` — concurrent tenants queue. Acceptable for v1.
- System asset SQL generation is pure Python string templating from metadata — fast.
- Terminal model resolution is a single DB query with a subquery — no N+1.
- Ephemeral dbt projects are written to temp dirs (tmpfs on Linux) — no persistent disk I/O.

### Security

- System assets are immutable via API (application-level enforcement in views).
- Tenant assets require `TenantMembership` (filtered in `get_queryset`).
- Workspace assets require `read_write` or `manage` role (checked in `perform_create`).
- dbt SQL executes in the tenant's schema. The `profiles.yml` targets the tenant schema via `search_path`. Read-only role isolation is preserved for agent queries.
- User-authored SQL defines dbt models (DDL), not runtime queries. The SQL is executed by dbt as `CREATE TABLE ... AS SELECT ...` under the materializer's database role, not the read-only query role.

---

## File Change Summary

### New Files

| File | Milestone | Description |
|------|-----------|-------------|
| `apps/transformations/__init__.py` | 3 | App init |
| `apps/transformations/apps.py` | 3 | Django AppConfig |
| `apps/transformations/models.py` | 3 | TransformationAsset, TransformationRun, TransformationAssetRun |
| `apps/transformations/admin.py` | 3 | Admin registrations |
| `apps/transformations/migrations/__init__.py` | 3 | Migrations package |
| `apps/transformations/migrations/0001_initial.py` | 3 | Auto-generated migration |
| `apps/transformations/services/__init__.py` | 4 | Services package |
| `apps/transformations/services/commcare_staging.py` | 4 | Dynamic SQL generation from CommCare metadata |
| `apps/transformations/services/dbt_project.py` | 5 | Ephemeral dbt project writer |
| `apps/transformations/services/executor.py` | 5 | Three-stage dbt execution pipeline |
| `apps/transformations/services/lineage.py` | 6 | Terminal model resolution and lineage chains |
| `apps/transformations/serializers.py` | 7 | DRF serializers |
| `apps/transformations/views.py` | 7 | DRF viewsets with permission enforcement |
| `apps/transformations/urls.py` | 7 | URL routing |
| `tests/test_transformation_models.py` | 3 | Model constraint and lifecycle tests |
| `tests/test_commcare_staging_generator.py` | 4 | SQL generation tests |
| `tests/test_dbt_project_writer.py` | 5 | dbt project writer tests |
| `tests/test_transformation_executor.py` | 5 | Three-stage pipeline tests |
| `tests/test_lineage.py` | 6 | Lineage resolution tests |
| `tests/test_transformation_api.py` | 7 | REST API tests |

### Modified Files

| File | Milestone(s) | Change |
|------|-------------|--------|
| `config/settings/base.py` | 3 | Add `apps.transformations` to `INSTALLED_APPS` |
| `config/urls.py` | 7 | Add transformation API URL route |
| `apps/workspaces/services/schema_manager.py` | 1 | Replace UNION ALL with namespaced aliasing views |
| `mcp_server/pipeline_registry.py` | 2 | Add `physical_table_name` to `SourceConfig` |
| `mcp_server/services/materializer.py` | 2, 4, 5 | M2: raw table rename. M4: call `upsert_system_assets` after discover. M5: replace transform phase with executor |
| `mcp_server/services/metadata.py` | 2, 6 | M2: update for `raw_*` table names. M6: add `transformation_aware_list_tables` |
| `mcp_server/services/dbt_runner.py` | 5 | Add `run_dbt_test()` function |
| `mcp_server/server.py` | 1, 6 | M1: workspace table listing. M6: add `get_lineage` tool |
| `apps/agents/graph/base.py` | 1, 6 | M1: multi-tenant prompt update. M6: terminal model preference, `get_lineage` in `MCP_TOOL_NAMES` |
| `pipelines/commcare_sync.yml` | 2, 5 | M2: raw table names in relationships. M5: remove `transforms` section |
| `pipelines/connect_sync.yml` | 2 | Raw table names in relationships |
| `tests/test_view_schema_builder.py` | 1 | Update for namespaced views |
| `tests/test_materializer.py` | 2 | Update expected table names |
| `tests/test_metadata_service.py` | 2 | Update expected table names |
| `tests/test_pipeline_registry.py` | 2 | Add `physical_table_name` tests |
