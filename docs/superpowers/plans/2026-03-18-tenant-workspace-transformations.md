# Tenant and Workspace Transformations Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dbt-first transformation system to Scout: CommCare gets a Scout-owned standardized dbt layer, tenant/workspace custom transformations are stored as dbt model and YAML files, dbt resolves ordering with `ref()`, and dbt tests provide data-quality checks.

**Architecture:** Use dbt as the execution engine for both Scout-owned standardization and user-authored transformations. Keep raw and system-standardized tables immutable, but allow custom dbt models to create preferred cleaned/derived replacements that Scout can prioritize in its context layer. Defer versioning, draft/publish workflows, AI SQL generation, and hard rollback semantics to future phases.

**Tech Stack:** Django 5, Django REST Framework, Celery, PostgreSQL via `psycopg`, dbt Core/dbt-postgres, existing MCP materializer pipeline, React 19, TypeScript, Zustand, Tailwind CSS 4, Radix UI.

---

## Scope and decomposition

This revised plan makes three intentional simplifications:

1. **dbt is the custom transformation runtime.**
   One `.sql` file = one dbt model. Dependencies come from `ref()`. We do **not** build a parallel custom SQL executor.

2. **No versioning in phase 1.**
   There are no drafts, publish flows, revision trees, or "last successful published pointer" models yet. Editing a stored dbt file updates the current definition. Harder environment/version semantics move to a future plan.

3. **AI authoring is deferred and will happen through Scout chat later.**
   This plan only adds manual entry/editing of dbt model SQL and YAML test/config files.

Land the work in this order:

1. Transformation file storage + provider contract
2. CommCare system-owned dbt standardization
3. Tenant/workspace dbt project rendering and execution
4. Data dictionary/context-layer semantics for preferred cleaned models
5. Manual UI for dbt model and YAML file editing

## File structure to introduce

Lock these boundaries before implementation:

- `apps/transformations/`
  Stores tenant/workspace dbt projects as first-class DB records and exposes CRUD/run APIs. Keep this separate from `apps/knowledge` and `apps/workspaces`.
- `mcp_server/providers/`
  Provider contract and provider-specific standardization hooks.
- `transforms/commcare/`
  Scout-owned dbt project for raw + standardized CommCare models.
- `apps/transformations/services/project_renderer.py`
  Renders DB-backed transformation files into a temporary dbt project directory.
- `apps/transformations/services/runner.py`
  Runs `dbt run` and `dbt test` for tenant/workspace projects.
- `apps/workspaces/api/views.py`
  Continues to own the data dictionary surface, but consumes transformation metadata rather than executing transformations.
- `frontend/src/pages/DataDictionaryPage/`
  Gains a transformation browser/editor so the data dictionary becomes the shared management surface.

## Chunk 1: Transformation storage and provider contract

This chunk introduces the persistent representation of dbt files and the provider seam, but does not change runtime behavior yet.

### Task 1: Create `apps.transformations` with file-based dbt project storage

**Files:**
- Create: `apps/transformations/__init__.py`
- Create: `apps/transformations/apps.py`
- Create: `apps/transformations/models.py`
- Create: `apps/transformations/admin.py`
- Create: `apps/transformations/migrations/0001_initial.py`
- Modify: `config/settings/base.py`
- Test: `tests/test_transformation_models.py`

- [ ] **Step 1: Write the failing model tests**

Create `tests/test_transformation_models.py`:

```python
import pytest


@pytest.mark.django_db
def test_can_create_tenant_scoped_dbt_project(user, tenant):
    from apps.transformations.models import TransformationProject

    project = TransformationProject.objects.create(
        scope=TransformationProject.Scope.TENANT,
        tenant=tenant,
        name="CommCare cleanup",
        slug="commcare-cleanup",
        created_by=user,
    )

    assert project.tenant_id == tenant.id
    assert project.workspace_id is None


@pytest.mark.django_db
def test_project_can_store_model_and_schema_files(user, tenant):
    from apps.transformations.models import TransformationFile, TransformationProject

    project = TransformationProject.objects.create(
        scope=TransformationProject.Scope.TENANT,
        tenant=tenant,
        name="CommCare cleanup",
        slug="commcare-cleanup",
        created_by=user,
    )
    model = TransformationFile.objects.create(
        project=project,
        kind=TransformationFile.Kind.MODEL_SQL,
        path="models/forms_clean.sql",
        body="select * from {{ ref('std_forms') }}",
    )
    schema = TransformationFile.objects.create(
        project=project,
        kind=TransformationFile.Kind.SCHEMA_YML,
        path="models/schema.yml",
        body="version: 2",
    )

    assert model.kind == TransformationFile.Kind.MODEL_SQL
    assert schema.kind == TransformationFile.Kind.SCHEMA_YML


@pytest.mark.django_db
def test_transformation_run_tracks_dbt_command(user, tenant):
    from apps.transformations.models import TransformationProject, TransformationRun

    project = TransformationProject.objects.create(
        scope=TransformationProject.Scope.TENANT,
        tenant=tenant,
        name="CommCare cleanup",
        slug="commcare-cleanup",
        created_by=user,
    )
    run = TransformationRun.objects.create(
        project=project,
        trigger=TransformationRun.Trigger.MANUAL,
        command=TransformationRun.Command.RUN,
        state=TransformationRun.State.SUCCEEDED,
    )

    assert run.command == TransformationRun.Command.RUN
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_transformation_models.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'apps.transformations'`.

- [ ] **Step 3: Implement the new models**

In `apps/transformations/models.py`, define:

```python
class TransformationProject(models.Model):
    class Scope(models.TextChoices):
        TENANT = "tenant"
        WORKSPACE = "workspace"

    tenant = models.ForeignKey("users.Tenant", null=True, blank=True, ...)
    workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True, ...)
    scope = models.CharField(max_length=20, choices=Scope.choices)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
```

```python
class TransformationFile(models.Model):
    class Kind(models.TextChoices):
        MODEL_SQL = "model_sql"
        SCHEMA_YML = "schema_yml"

    project = models.ForeignKey(TransformationProject, on_delete=models.CASCADE, related_name="files")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    path = models.CharField(max_length=255)
    body = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)
```

```python
class TransformationRun(models.Model):
    class Trigger(models.TextChoices):
        REFRESH = "refresh"
        MANUAL = "manual"

    class Command(models.TextChoices):
        RUN = "run"
        TEST = "test"
        BUILD = "build"

    class State(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"

    project = models.ForeignKey(TransformationProject, on_delete=models.CASCADE, related_name="runs")
    trigger = models.CharField(max_length=20, choices=Trigger.choices)
    command = models.CharField(max_length=20, choices=Command.choices)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    error = models.TextField(blank=True)
    log_excerpt = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
```

Validation rules:
- tenant-scoped projects require `tenant` and forbid `workspace`
- workspace-scoped projects require `workspace` and forbid `tenant`
- `path` must end in `.sql` for `MODEL_SQL` and `.yml`/`.yaml` for `SCHEMA_YML`
- `path` must live under `models/`
- `(project, path)` must be unique

- [ ] **Step 4: Register the app**

Add `"apps.transformations"` to `INSTALLED_APPS` in `config/settings/base.py`.

Register the three models in `apps/transformations/admin.py`.

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_transformation_models.py -v`

- [ ] **Step 6: Commit**

```bash
git add config/settings/base.py apps/transformations tests/test_transformation_models.py
git commit -m "feat: add dbt transformation project storage models"
```

---

### Task 2: Introduce a provider contract with an explicit standardization phase

**Files:**
- Create: `mcp_server/providers/__init__.py`
- Create: `mcp_server/providers/base.py`
- Create: `mcp_server/providers/registry.py`
- Create: `mcp_server/providers/commcare.py`
- Modify: `mcp_server/services/materializer.py`
- Test: `tests/test_provider_contract.py`

- [ ] **Step 1: Write the failing provider tests**

Create `tests/test_provider_contract.py`:

```python
def test_commcare_provider_exposes_discover_load_standardize_hooks():
    from mcp_server.providers.commcare import CommCareProvider

    provider = CommCareProvider()

    assert callable(provider.discover_metadata)
    assert callable(provider.load_sources)
    assert callable(provider.standardize)


def test_registry_returns_commcare_provider():
    from mcp_server.providers.registry import get_provider

    provider = get_provider("commcare")
    assert provider.name == "commcare"
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_provider_contract.py -v`

Expected: FAIL because `mcp_server.providers` does not exist.

- [ ] **Step 3: Implement the provider contract**

In `mcp_server/providers/base.py`, define:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class StandardizationResult:
    models: list[str]
    test_summary: dict


class Provider:
    name: str

    def discover_metadata(self, tenant_membership, credential, pipeline) -> dict:
        raise NotImplementedError

    def load_sources(self, tenant_membership, credential, schema_name, conn, pipeline) -> dict[str, dict]:
        raise NotImplementedError

    def standardize(self, tenant_membership, credential, schema_name, pipeline) -> StandardizationResult:
        raise NotImplementedError
```

In `mcp_server/providers/commcare.py`, adapt current discovery/load behavior and leave `standardize()` as the hook that will run the Scout-owned dbt project in Chunk 2.

- [ ] **Step 4: Update `run_pipeline()` to use the provider seam**

`mcp_server/services/materializer.py` should call:
- `provider.discover_metadata(...)`
- `provider.load_sources(...)`
- `provider.standardize(...)`

Do not add tenant/workspace custom project execution yet.

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_provider_contract.py tests/test_materializer.py -v`

- [ ] **Step 6: Commit**

```bash
git add mcp_server/providers mcp_server/services/materializer.py tests/test_provider_contract.py
git commit -m "refactor: add provider contract with standardization hook"
```

## Chunk 2: Scout-owned CommCare dbt standardization

This chunk makes dbt real in the repo and upgrades CommCare from raw JSON-heavy tables to a standardized system layer.

### Task 3: Expand CommCare metadata discovery to support dbt flattening

**Files:**
- Modify: `mcp_server/loaders/commcare_metadata.py`
- Test: `tests/test_commcare_metadata_loader.py`

- [ ] **Step 1: Write the failing metadata tests**

Add tests covering:
- normalized question catalog entries
- repeat-group extraction
- stable form/module/app naming used by downstream models

Example:

```python
def test_extracts_question_catalog_entries():
    from mcp_server.loaders.commcare_metadata import _extract_form_definitions

    apps = [{
        "name": {"en": "Maternal Health"},
        "modules": [{
            "name": {"en": "ANC"},
            "case_type": "pregnancy",
            "forms": [{
                "xmlns": "http://openrosa.org/formdesigner/anc",
                "name": {"en": "ANC Visit"},
                "questions": [
                    {"value": "/data/visit_date", "label": {"en": "Visit date"}, "type": "Date"},
                ],
            }],
        }],
    }]

    forms = _extract_form_definitions(apps)
    question = forms["http://openrosa.org/formdesigner/anc"]["questions"][0]

    assert question["path"] == "/data/visit_date"
    assert question["label"] == "Visit date"
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_commcare_metadata_loader.py -v`

Expected: FAIL because current metadata extraction preserves raw questions without a normalized catalog.

- [ ] **Step 3: Normalize the metadata payload**

Update `mcp_server/loaders/commcare_metadata.py` so the stored `TenantMetadata.metadata` includes:
- `app_definitions`
- `case_types`
- `form_definitions`
- `question_catalog`
- `repeat_groups`

Normalize each question to:
- `path`
- `label`
- `question_type`
- `repeat_path`
- `app_name`
- `module_name`
- `form_name`
- `xmlns`

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_commcare_metadata_loader.py -v`

- [ ] **Step 5: Commit**

```bash
git add mcp_server/loaders/commcare_metadata.py tests/test_commcare_metadata_loader.py
git commit -m "feat: enrich commcare metadata for dbt standardization"
```

---

### Task 4: Add the Scout-owned CommCare dbt project

**Files:**
- Create: `transforms/commcare/dbt_project.yml`
- Create: `transforms/commcare/models/raw/raw_cases.sql`
- Create: `transforms/commcare/models/raw/raw_forms.sql`
- Create: `transforms/commcare/models/standardized/std_cases.sql`
- Create: `transforms/commcare/models/standardized/std_forms.sql`
- Create: `transforms/commcare/models/standardized/std_form_questions.sql`
- Create: `transforms/commcare/models/standardized/std_form_repeats.sql`
- Create: `transforms/commcare/models/schema.yml`
- Modify: `pipelines/commcare_sync.yml`
- Test: `tests/test_dbt_runner.py`
- Test: `tests/test_metadata_service.py`

- [ ] **Step 1: Write the failing tests**

Add a metadata-service test asserting the pipeline exposes:
- `raw_cases`
- `raw_forms`
- `std_cases`
- `std_forms`
- `std_form_questions`
- `std_form_repeats`

Add a dbt-runner test asserting those model names are selected.

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_metadata_service.py tests/test_dbt_runner.py -v`

Expected: FAIL because the project path and model list do not exist yet.

- [ ] **Step 3: Create the dbt project**

Create a real dbt project under `transforms/commcare/`.

Model responsibilities:
- `raw_cases.sql`
  Stable select over the raw physical cases table
- `raw_forms.sql`
  Stable select over the raw physical forms table
- `std_cases.sql`
  Typed case core columns
- `std_forms.sql`
  Typed form core columns
- `std_form_questions.sql`
  One row per answer/question event
- `std_form_repeats.sql`
  One row per repeat group instance

Use `ref()` throughout so dbt owns dependency ordering.

In `models/schema.yml`, add dbt data-quality tests for system tables:
- `not_null`
- `unique`
- `accepted_values` where appropriate

Update `pipelines/commcare_sync.yml` to point to:

```yaml
sources:
  - name: raw_cases
  - name: raw_forms

transforms:
  dbt_project: transforms/commcare
  models:
    - raw_cases
    - raw_forms
    - std_cases
    - std_forms
    - std_form_questions
    - std_form_repeats
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_metadata_service.py tests/test_dbt_runner.py -v`

- [ ] **Step 5: Commit**

```bash
git add transforms/commcare pipelines/commcare_sync.yml tests/test_metadata_service.py tests/test_dbt_runner.py
git commit -m "feat: add scout-owned commcare dbt project"
```

---

### Task 5: Wire the CommCare provider to run `dbt run` and `dbt test`

**Files:**
- Modify: `mcp_server/providers/commcare.py`
- Modify: `mcp_server/services/materializer.py`
- Test: `tests/test_materializer.py`

- [ ] **Step 1: Write the failing workflow tests**

Add tests asserting:
- the CommCare provider runs dbt standardization after raw load
- dbt tests run after `dbt run`
- failed dbt tests are reported in the materialization result without erasing raw load success

Example:

```python
def test_standardization_result_includes_dbt_test_summary():
    ...
    result = run_pipeline(...)
    assert "standardization" in result
    assert "test_summary" in result["standardization"]
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_materializer.py -v`

Expected: FAIL because current results do not include dbt test output.

- [ ] **Step 3: Update provider/materializer**

In `mcp_server/providers/commcare.py`, `standardize()` should:
- generate runtime `profiles.yml`
- run `dbt run --select ...`
- run `dbt test --select ...`
- return a `StandardizationResult` containing model statuses and test summary

In `mcp_server/services/materializer.py`, add a `standardization` block to the result:

```python
{
    "sources": {...},
    "standardization": {
        "models": {...},
        "tests": {...},
    },
    "pipeline": pipeline.name,
}
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_materializer.py -v`

- [ ] **Step 5: Commit**

```bash
git add mcp_server/providers/commcare.py mcp_server/services/materializer.py tests/test_materializer.py
git commit -m "feat: run commcare dbt models and tests during standardization"
```

## Chunk 3: Tenant/workspace custom dbt projects

This chunk makes stored transformation files executable as dbt models and tests.

### Task 6: Render stored transformation files into runtime dbt projects

**Files:**
- Create: `apps/transformations/services/__init__.py`
- Create: `apps/transformations/services/project_renderer.py`
- Test: `tests/test_transformation_project_renderer.py`

- [ ] **Step 1: Write the failing renderer tests**

Create `tests/test_transformation_project_renderer.py`:

```python
from pathlib import Path


def test_renderer_writes_model_and_schema_files(tmp_path, user, tenant, db):
    from apps.transformations.models import TransformationFile, TransformationProject
    from apps.transformations.services.project_renderer import render_project

    project = TransformationProject.objects.create(
        scope=TransformationProject.Scope.TENANT,
        tenant=tenant,
        name="Cleanup",
        slug="cleanup",
        created_by=user,
    )
    TransformationFile.objects.create(
        project=project,
        kind=TransformationFile.Kind.MODEL_SQL,
        path="models/forms_clean.sql",
        body="select * from {{ ref('std_forms') }}",
    )
    TransformationFile.objects.create(
        project=project,
        kind=TransformationFile.Kind.SCHEMA_YML,
        path="models/schema.yml",
        body="version: 2",
    )

    outdir = render_project(project, tmp_path)

    assert (Path(outdir) / "models/forms_clean.sql").exists()
    assert (Path(outdir) / "models/schema.yml").exists()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_transformation_project_renderer.py -v`

Expected: FAIL because no renderer exists.

- [ ] **Step 3: Implement the renderer**

`apps/transformations/services/project_renderer.py` should:
- create a temporary dbt project directory
- write `dbt_project.yml`
- write all stored `TransformationFile` rows to disk at their `path`
- include the Scout-owned CommCare project as a dependency base for tenant projects
- include tenant project outputs as upstream relations for workspace projects

For phase 1, implement this with deterministic file rendering only. Do not add version snapshots or branching behavior.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_transformation_project_renderer.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/services/project_renderer.py tests/test_transformation_project_renderer.py
git commit -m "feat: render stored transformation files into dbt projects"
```

---

### Task 7: Execute tenant and workspace custom dbt models and dbt tests

**Files:**
- Create: `apps/transformations/services/runner.py`
- Create: `apps/transformations/services/orchestrator.py`
- Modify: `apps/workspaces/tasks.py`
- Modify: `mcp_server/services/materializer.py`
- Test: `tests/test_transformation_runner.py`
- Test: `tests/test_workspace_view_schema.py`

- [ ] **Step 1: Write the failing runner tests**

Create tests covering:
- tenant project can reference system models with `ref('std_forms')`
- workspace project can reference tenant custom models
- `dbt run` followed by `dbt test` is recorded in `TransformationRun`

Example:

```python
def test_runner_executes_run_then_test(monkeypatch, user, tenant, db):
    ...
    summary = run_project(project, trigger="manual")
    assert summary["run"]["success"] is True
    assert summary["test"]["success"] is True
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_transformation_runner.py -v`

Expected: FAIL because no runner/orchestrator exists.

- [ ] **Step 3: Implement the runner and orchestrator**

`apps/transformations/services/runner.py`
- render the dbt project
- run `dbt run`
- run `dbt test`
- persist `TransformationRun` rows for both commands or one summary row with both outcomes

`apps/transformations/services/orchestrator.py`
- `run_tenant_projects_for_tenant(tenant, trigger="refresh")`
- `run_workspace_projects_for_workspace(workspace, trigger="refresh")`
- `run_project_now(project, trigger="manual")`

Wire it in:
- `mcp_server/services/materializer.py` runs tenant-scoped transformation projects after provider standardization
- `apps/workspaces/tasks.py` runs workspace-scoped transformation projects after the workspace view schema is rebuilt

Do **not** add version pinning, publish flows, or rollback semantics.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_transformation_runner.py tests/test_workspace_view_schema.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/services/runner.py apps/transformations/services/orchestrator.py apps/workspaces/tasks.py mcp_server/services/materializer.py tests/test_transformation_runner.py tests/test_workspace_view_schema.py
git commit -m "feat: execute tenant and workspace dbt transformation projects"
```

## Chunk 4: Preferred cleaned models, data dictionary, and context-layer behavior

This chunk answers the data-cleaning question explicitly: do not mutate or delete system tables, but allow custom dbt models to declare themselves as the preferred cleaned replacements so Scout uses them by default.

### Task 8: Add preferred-model metadata and expose it in the data dictionary

**Files:**
- Modify: `apps/transformations/models.py`
- Modify: `mcp_server/services/metadata.py`
- Modify: `apps/workspaces/api/views.py`
- Modify: `tests/test_metadata_service.py`

- [ ] **Step 1: Write the failing metadata tests**

Add tests asserting:
- a custom model can declare `supersedes: std_forms`
- metadata responses expose the replacement relationship
- the superseded table is still visible for audit/debug
- the preferred cleaned model is marked as preferred

Example:

```python
assert forms_clean["preferred_for"] == "std_forms"
assert std_forms["superseded_by"] == "forms_clean"
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_metadata_service.py -v`

Expected: FAIL because metadata responses do not carry replacement semantics.

- [ ] **Step 3: Implement preferred cleaned-model semantics**

Use dbt YAML/config metadata rather than naming conventions.

Phase 1 rule:
- custom models may declare `meta.scout.supersedes: "<table_name>"`
- only one model may supersede a given upstream table within a given scope

Expose in `mcp_server/services/metadata.py` and `apps/workspaces/api/views.py`:
- `preferred_for`
- `superseded_by`
- `layer`
- `scope`

Do not hide system tables entirely. Instead, mark them as superseded and teach the context layer to prefer the custom cleaned model.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_metadata_service.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/models.py mcp_server/services/metadata.py apps/workspaces/api/views.py tests/test_metadata_service.py
git commit -m "feat: expose preferred cleaned models in metadata"
```

---

### Task 9: Update the agent/context layer to prefer cleaned replacement models

**Files:**
- Modify: `apps/agents/graph/base.py`
- Modify: `tests/agents/test_schema_context.py`

- [ ] **Step 1: Write the failing schema-context tests**

Add tests asserting the rendered schema context:
- includes the preferred cleaned model
- notes that the original table is superseded
- does not instruct the agent to use the superseded table when a preferred replacement exists

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/agents/test_schema_context.py -v`

Expected: FAIL because the schema context has no concept of preferred replacements.

- [ ] **Step 3: Update schema-context rendering**

In `apps/agents/graph/base.py`, when building schema context:
- surface preferred cleaned models prominently
- annotate superseded tables as fallback/audit-only
- keep the original tables available, but deprioritized

Do not add complicated prompt logic. Keep it to a simple preference rule based on metadata.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/agents/test_schema_context.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/agents/graph/base.py tests/agents/test_schema_context.py
git commit -m "feat: prefer cleaned dbt models in schema context"
```

## Chunk 5: Manual dbt file authoring UI

This chunk keeps the product surface simple: manual editing of dbt SQL and YAML files in the existing data dictionary area. Scout-chat-driven authoring comes later.

### Task 10: Add transformation project CRUD/run APIs

**Files:**
- Create: `apps/transformations/api/__init__.py`
- Create: `apps/transformations/api/urls.py`
- Create: `apps/transformations/api/views.py`
- Modify: `config/urls.py`
- Test: `tests/test_transformation_api.py`

- [ ] **Step 1: Write the failing API tests**

Create `tests/test_transformation_api.py` covering:
- list/create transformation project
- add/update/delete model SQL file
- add/update/delete YAML schema file
- run project now
- `READ` users cannot mutate
- `READ_WRITE` and `MANAGE` users can mutate

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `uv run pytest tests/test_transformation_api.py -v`

Expected: FAIL because no transformation API routes exist.

- [ ] **Step 3: Implement the API**

Add workspace-scoped routes:

- `GET /api/workspaces/<workspace_id>/transformations/projects/`
- `POST /api/workspaces/<workspace_id>/transformations/projects/`
- `GET /api/workspaces/<workspace_id>/transformations/projects/<uuid:project_id>/`
- `PATCH /api/workspaces/<workspace_id>/transformations/projects/<uuid:project_id>/`
- `POST /api/workspaces/<workspace_id>/transformations/projects/<uuid:project_id>/run/`
- `POST /api/workspaces/<workspace_id>/transformations/projects/<uuid:project_id>/files/`
- `PATCH /api/workspaces/<workspace_id>/transformations/files/<uuid:file_id>/`
- `DELETE /api/workspaces/<workspace_id>/transformations/files/<uuid:file_id>/`

Creation payload must choose `scope = tenant|workspace`.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_transformation_api.py -v`

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/api config/urls.py tests/test_transformation_api.py
git commit -m "feat: add transformation project APIs"
```

---

### Task 11: Extend the data dictionary UI with a transformation browser and file editor

**Files:**
- Create: `frontend/src/api/transformations.ts`
- Create: `frontend/src/pages/DataDictionaryPage/TransformationsPanel.tsx`
- Create: `frontend/src/pages/DataDictionaryPage/TransformationProjectDetail.tsx`
- Modify: `frontend/src/store/dictionarySlice.ts`
- Modify: `frontend/src/store/store.ts`
- Modify: `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx`
- Modify: `frontend/src/pages/DataDictionaryPage/SchemaTree.tsx`

- [ ] **Step 1: Add the frontend API module**

Create `frontend/src/api/transformations.ts` with:

```ts
export interface TransformationProjectSummary { ... }
export interface TransformationProjectDetail { ... }
export interface TransformationFile { ... }
export interface TransformationRun { ... }

export function listProjects(workspaceId: string) { ... }
export function createProject(workspaceId: string, data: ...) { ... }
export function updateProject(workspaceId: string, projectId: string, data: ...) { ... }
export function createFile(workspaceId: string, projectId: string, data: ...) { ... }
export function updateFile(workspaceId: string, fileId: string, data: ...) { ... }
export function deleteFile(workspaceId: string, fileId: string) { ... }
export function runProject(workspaceId: string, projectId: string) { ... }
```

- [ ] **Step 2: Extend the dictionary store**

In `frontend/src/store/dictionarySlice.ts`, add state for:
- transformation project list
- selected transformation project
- current project files
- latest run statuses

- [ ] **Step 3: Add a transformation browser panel**

In `frontend/src/pages/DataDictionaryPage/TransformationsPanel.tsx`, list:
- tenant projects
- workspace projects
- last run state
- simple badges for scope

Keep the left pane split between:
- `Tables`
- `Transformations`

If no tabs primitive exists, add `frontend/src/components/ui/tabs.tsx` using the existing Radix pattern.

- [ ] **Step 4: Add a manual file editor**

In `frontend/src/pages/DataDictionaryPage/TransformationProjectDetail.tsx`, use existing primitives only:
- `Input`
- `Textarea`
- `Button`
- `Badge`

Editor capabilities for v1:
- create/edit `.sql` model files
- create/edit `.yml` schema/test files
- run the project
- show latest run result

Do **not** add CodeMirror, SQL generation, or AI helpers in this plan.

- [ ] **Step 5: Verify the frontend builds**

Run: `cd frontend && npm run build`

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/transformations.ts frontend/src/store/dictionarySlice.ts frontend/src/store/store.ts frontend/src/pages/DataDictionaryPage frontend/src/components/ui/tabs.tsx
git commit -m "feat: add manual dbt project browser and file editor"
```

## Deferred follow-up plans

Write separate plans later for:

- transformation versioning/drafts/publish flows
- rollback/last-known-good output guarantees
- AI-assisted creation/editing through Scout chat
- richer dbt testing support beyond YAML generic tests
- CommCare Connect-specific standardization
- SQL editor enhancements (CodeMirror, linting, autocomplete)

## Verification checklist for the implementing agent

Before claiming the overall plan complete, run:

```bash
uv run pytest tests/test_transformation_models.py tests/test_provider_contract.py tests/test_commcare_metadata_loader.py tests/test_materializer.py tests/test_metadata_service.py tests/test_transformation_project_renderer.py tests/test_transformation_runner.py tests/test_transformation_api.py tests/agents/test_schema_context.py tests/test_workspace_view_schema.py -v
cd frontend && npm run build
```

Expected:
- all listed backend tests pass
- frontend build exits 0

If a chunk adds more focused tests, run those first before the larger verification sweep.
