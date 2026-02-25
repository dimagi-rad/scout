# Pipeline-Driven Metadata Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace `information_schema` introspection in `list_tables`, `describe_table`, and `get_metadata` with pipeline-aware enrichment: row counts + timestamps from `MaterializationRun`, JSONB column annotations from `TenantMetadata`, and table relationships from the pipeline YAML.

**Architecture:** New `mcp_server/services/metadata.py` module exposes three sync functions (`pipeline_list_tables`, `pipeline_describe_table`, `pipeline_get_metadata`) consumed by the MCP tool handlers. Server tools continue to call `load_tenant_context` for connection params, then fetch `TenantSchema`/`TenantMetadata` separately for pipeline enrichment. `pipeline_describe_table` reuses `_execute_sync_parameterized` from the query service for `information_schema` column queries.

**Tech Stack:** Django ORM (sync), psycopg2, pytest/AsyncMock, existing `PipelineRegistry`, `MaterializationRun`, `TenantMetadata` models.

---

### Task 1: Extend pipeline registry with RelationshipConfig

**Files:**
- Modify: `mcp_server/pipeline_registry.py`
- Modify: `tests/test_pipeline_registry.py`
- Modify: `pipelines/commcare_sync.yml`

**Step 1: Write the failing test**

Add to `tests/test_pipeline_registry.py`:

```python
def test_parses_relationships(self, tmp_path):
    yml = tmp_path / "rel.yml"
    yml.write_text("""
pipeline: rel_test
description: "Test"
version: "1.0"
provider: commcare
sources: []
relationships:
  - from_table: forms
    from_column: case_ids
    to_table: cases
    to_column: case_id
    description: "Forms reference cases"
""")
    registry = PipelineRegistry(pipelines_dir=str(tmp_path))
    config = registry.get("rel_test")
    assert len(config.relationships) == 1
    r = config.relationships[0]
    assert r.from_table == "forms"
    assert r.from_column == "case_ids"
    assert r.to_table == "cases"
    assert r.to_column == "case_id"
    assert r.description == "Forms reference cases"

def test_relationships_defaults_to_empty(self, tmp_path):
    yml = tmp_path / "no_rel.yml"
    yml.write_text(
        "pipeline: no_rel\ndescription: ''\nversion: '1.0'\nprovider: commcare\nsources: []\n"
    )
    registry = PipelineRegistry(pipelines_dir=str(tmp_path))
    config = registry.get("no_rel")
    assert config.relationships == []
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/test_pipeline_registry.py::TestPipelineRegistry::test_parses_relationships -v
```

Expected: `AttributeError: 'PipelineConfig' object has no attribute 'relationships'`

**Step 3: Add RelationshipConfig dataclass and update PipelineConfig**

In `mcp_server/pipeline_registry.py`, after the `TransformConfig` dataclass:

```python
@dataclass
class RelationshipConfig:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    description: str = ""
```

Add `relationships` field to `PipelineConfig`:

```python
@dataclass
class PipelineConfig:
    name: str
    description: str
    version: str
    provider: str
    sources: list[SourceConfig] = field(default_factory=list)
    metadata_discovery: MetadataDiscoveryConfig | None = None
    transforms: TransformConfig | None = None
    relationships: list[RelationshipConfig] = field(default_factory=list)
```

Update `_parse_pipeline` to read relationships (add after the `transforms` block):

```python
rel_raw = data.get("relationships", [])
relationships = [
    RelationshipConfig(
        from_table=r["from_table"],
        from_column=r["from_column"],
        to_table=r["to_table"],
        to_column=r["to_column"],
        description=r.get("description", ""),
    )
    for r in rel_raw
]
return PipelineConfig(
    name=data["pipeline"],
    description=data.get("description", ""),
    version=data.get("version", "1.0"),
    provider=data.get("provider", "commcare"),
    sources=sources,
    metadata_discovery=metadata_discovery,
    transforms=transforms,
    relationships=relationships,
)
```

**Step 4: Add relationships block to `pipelines/commcare_sync.yml`**

Append after `transforms`:

```yaml
relationships:
  - from_table: forms
    from_column: case_ids
    to_table: cases
    to_column: case_id
    description: "Form submissions reference the cases they update (case_ids is a JSON array)"
```

**Step 5: Run tests to verify pass**

```bash
uv run pytest tests/test_pipeline_registry.py -v
```

Expected: All tests PASS.

**Step 6: Commit**

```bash
git add mcp_server/pipeline_registry.py pipelines/commcare_sync.yml tests/test_pipeline_registry.py
git commit -m "feat: add RelationshipConfig to pipeline registry and commcare_sync.yml"
```

---

### Task 2: Create metadata service — `pipeline_list_tables`

**Files:**
- Create: `mcp_server/services/metadata.py`
- Create: `tests/test_metadata_service.py`

**Step 1: Write the failing tests**

Create `tests/test_metadata_service.py`:

```python
"""Tests for mcp_server/services/metadata.py."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest


def _make_pipeline_config(sources=None, dbt_models=None, relationships=None):
    """Build a minimal PipelineConfig for testing."""
    from mcp_server.pipeline_registry import (
        PipelineConfig,
        RelationshipConfig,
        SourceConfig,
    )

    return PipelineConfig(
        name="commcare_sync",
        description="Test pipeline",
        version="1.0",
        provider="commcare",
        sources=[SourceConfig(name=n, description=d) for n, d in (sources or [])],
        relationships=[
            RelationshipConfig(**r) for r in (relationships or [])
        ],
    )


def _set_dbt_models(config, models):
    """Attach a TransformConfig with the given model list."""
    from mcp_server.pipeline_registry import TransformConfig

    object.__setattr__(config, "transforms", TransformConfig(dbt_project="transforms/commcare", models=models))
    return config


class TestPipelineListTables:
    def test_returns_empty_when_no_completed_run(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(sources=[("cases", "CommCare cases")])

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = None

            result = pipeline_list_tables(mock_ts, pipeline_config)

        assert result == []

    def test_returns_table_entries_from_completed_run(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(
            sources=[("cases", "CommCare case records"), ("forms", "CommCare form records")]
        )

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {
            "sources": {
                "cases": {"rows": 4823},
                "forms": {"rows": 1200},
            }
        }

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = mock_run

            result = pipeline_list_tables(mock_ts, pipeline_config)

        assert len(result) == 2
        cases = next(t for t in result if t["name"] == "cases")
        assert cases["description"] == "CommCare case records"
        assert cases["row_count"] == 4823
        assert cases["materialized_at"] == completed_at.isoformat()
        assert cases["type"] == "table"

    def test_includes_dbt_models_with_null_row_count(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])
        pipeline_config = _set_dbt_models(pipeline_config, ["stg_cases", "stg_forms"])

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {"sources": {"cases": {"rows": 100}}}

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = mock_run

            result = pipeline_list_tables(mock_ts, pipeline_config)

        names = [t["name"] for t in result]
        assert "stg_cases" in names
        assert "stg_forms" in names
        stg = next(t for t in result if t["name"] == "stg_cases")
        assert stg["row_count"] is None
        assert stg["materialized_at"] == completed_at.isoformat()
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/test_metadata_service.py -v
```

Expected: `ModuleNotFoundError: No module named 'mcp_server.services.metadata'`

**Step 3: Implement `pipeline_list_tables`**

Create `mcp_server/services/metadata.py`:

```python
"""Pipeline-aware metadata service for MCP tools.

Provides enriched responses for list_tables, describe_table, and get_metadata
by combining MaterializationRun records with TenantMetadata discover-phase output
and pipeline registry definitions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apps.projects.models import MaterializationRun
from mcp_server.pipeline_registry import PipelineConfig

if TYPE_CHECKING:
    from apps.projects.models import TenantMetadata, TenantSchema
    from mcp_server.context import QueryContext

logger = logging.getLogger(__name__)


def pipeline_list_tables(
    tenant_schema: TenantSchema,
    pipeline_config: PipelineConfig,
) -> list[dict]:
    """Return enriched table list from the last completed MaterializationRun.

    Returns an empty list if no completed run exists.
    Each entry includes name, type, description, row_count, and materialized_at.
    """
    run = (
        MaterializationRun.objects.filter(
            tenant_schema=tenant_schema,
            state=MaterializationRun.RunState.COMPLETED,
        )
        .order_by("-completed_at")
        .first()
    )
    if run is None:
        return []

    materialized_at = run.completed_at.isoformat() if run.completed_at else None
    sources_result: dict[str, Any] = (run.result or {}).get("sources", {})
    source_descriptions = {s.name: s.description for s in pipeline_config.sources}

    tables = []
    for source_name, source_data in sources_result.items():
        tables.append(
            {
                "name": source_name,
                "type": "table",
                "description": source_descriptions.get(source_name, ""),
                "row_count": source_data.get("rows"),
                "materialized_at": materialized_at,
            }
        )

    for model_name in pipeline_config.dbt_models:
        tables.append(
            {
                "name": model_name,
                "type": "table",
                "description": "",
                "row_count": None,
                "materialized_at": materialized_at,
            }
        )

    return tables
```

**Step 4: Run to verify pass**

```bash
uv run pytest tests/test_metadata_service.py::TestPipelineListTables -v
```

Expected: All PASS.

**Step 5: Commit**

```bash
git add mcp_server/services/metadata.py tests/test_metadata_service.py
git commit -m "feat: add pipeline_list_tables to metadata service"
```

---

### Task 3: Add `pipeline_describe_table` to metadata service

**Files:**
- Modify: `mcp_server/services/metadata.py`
- Modify: `tests/test_metadata_service.py`

**Step 1: Write failing tests**

Add to `tests/test_metadata_service.py`:

```python
class TestPipelineDescribeTable:
    def _make_ctx(self, schema_name="test_schema"):
        from mcp_server.context import QueryContext

        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            max_rows_per_query=500,
            max_query_timeout_seconds=30,
            connection_params={},
        )

    def test_returns_none_when_table_not_found(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config()

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {"columns": [], "rows": [], "row_count": 0}
            result = pipeline_describe_table("nonexistent", ctx, None, pipeline_config)

        assert result is None

    def test_returns_column_structure(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "CommCare case records")])

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [
                    ["case_id", "text", "NO", None],
                    ["case_type", "text", "YES", None],
                ],
                "row_count": 2,
            }
            result = pipeline_describe_table("cases", ctx, None, pipeline_config)

        assert result is not None
        assert result["name"] == "cases"
        assert result["description"] == "CommCare case records"
        assert len(result["columns"]) == 2
        assert result["columns"][0] == {
            "name": "case_id", "type": "text", "nullable": False, "default": None, "description": "",
        }

    def test_annotates_properties_column_with_case_types(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])

        mock_tenant_metadata = MagicMock()
        mock_tenant_metadata.metadata = {
            "case_types": [
                {"name": "pregnancy"},
                {"name": "child"},
            ]
        }

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["properties", "jsonb", "YES", "'{}'::jsonb"]],
                "row_count": 1,
            }
            result = pipeline_describe_table("cases", ctx, mock_tenant_metadata, pipeline_config)

        col = result["columns"][0]
        assert "pregnancy" in col["description"]
        assert "child" in col["description"]
        assert col["description"].startswith("Contains case properties")

    def test_annotates_form_data_column_with_form_names(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("forms", "Forms")])

        mock_tenant_metadata = MagicMock()
        mock_tenant_metadata.metadata = {
            "form_definitions": {
                "http://openrosa.org/formdesigner/abc": {"name": "ANC Registration"},
                "http://openrosa.org/formdesigner/xyz": {"name": "Child Visit"},
            }
        }

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["form_data", "jsonb", "YES", "'{}'::jsonb"]],
                "row_count": 1,
            }
            result = pipeline_describe_table("forms", ctx, mock_tenant_metadata, pipeline_config)

        col = result["columns"][0]
        assert "ANC Registration" in col["description"]
        assert "Child Visit" in col["description"]
        assert col["description"].startswith("Contains form submission data")

    def test_graceful_when_tenant_metadata_is_none(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["properties", "jsonb", "YES", None]],
                "row_count": 1,
            }
            result = pipeline_describe_table("cases", ctx, None, pipeline_config)

        assert result is not None
        assert result["columns"][0]["description"] == ""
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/test_metadata_service.py::TestPipelineDescribeTable -v
```

Expected: `ImportError` — `pipeline_describe_table` not defined yet.

**Step 3: Implement `pipeline_describe_table` and `_build_jsonb_annotations`**

Add to `mcp_server/services/metadata.py`:

```python
from mcp_server.services.query import _execute_sync_parameterized


def pipeline_describe_table(
    table_name: str,
    ctx: QueryContext,
    tenant_metadata: TenantMetadata | None,
    pipeline_config: PipelineConfig,
) -> dict | None:
    """Describe a table using information_schema, enriched with discover-phase annotations.

    Returns None if the table does not exist in information_schema.
    JSONB columns (properties, form_data) receive descriptions derived from TenantMetadata.
    """
    result = _execute_sync_parameterized(
        ctx,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (ctx.schema_name, table_name),
        ctx.max_query_timeout_seconds,
    )

    if not result.get("rows"):
        return None

    source_descriptions = {s.name: s.description for s in pipeline_config.sources}
    jsonb_annotations = _build_jsonb_annotations(table_name, tenant_metadata)

    columns = []
    for row in result["rows"]:
        col_name, data_type, is_nullable, default = row
        columns.append(
            {
                "name": col_name,
                "type": data_type,
                "nullable": is_nullable == "YES",
                "default": default,
                "description": jsonb_annotations.get(col_name, ""),
            }
        )

    return {
        "name": table_name,
        "description": source_descriptions.get(table_name, ""),
        "columns": columns,
    }


def _build_jsonb_annotations(
    table_name: str, tenant_metadata: TenantMetadata | None
) -> dict[str, str]:
    """Build per-column description strings for known JSONB columns.

    Returns an empty dict if TenantMetadata is absent or the table has no annotations.
    """
    if tenant_metadata is None:
        return {}

    metadata = tenant_metadata.metadata or {}

    if table_name == "cases":
        case_types = metadata.get("case_types", [])
        if case_types:
            names = ", ".join(ct["name"] for ct in case_types)
            return {"properties": f"Contains case properties. Available case types: {names}"}

    elif table_name == "forms":
        form_definitions = metadata.get("form_definitions", {})
        if form_definitions:
            form_names = ", ".join(
                fd.get("name", xmlns) for xmlns, fd in form_definitions.items()
            )
            return {
                "form_data": f"Contains form submission data. Available forms: {form_names}"
            }

    return {}
```

**Step 4: Run to verify pass**

```bash
uv run pytest tests/test_metadata_service.py::TestPipelineDescribeTable -v
```

Expected: All PASS.

**Step 5: Commit**

```bash
git add mcp_server/services/metadata.py tests/test_metadata_service.py
git commit -m "feat: add pipeline_describe_table with JSONB annotation from discover phase"
```

---

### Task 4: Add `pipeline_get_metadata` to metadata service

**Files:**
- Modify: `mcp_server/services/metadata.py`
- Modify: `tests/test_metadata_service.py`

**Step 1: Write failing tests**

Add to `tests/test_metadata_service.py`:

```python
class TestPipelineGetMetadata:
    def _make_ctx(self, schema_name="test_schema"):
        from mcp_server.context import QueryContext

        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            max_rows_per_query=500,
            max_query_timeout_seconds=30,
            connection_params={},
        )

    def test_returns_empty_when_no_completed_run(self):
        from mcp_server.services.metadata import pipeline_get_metadata

        ctx = self._make_ctx()
        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config()

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = None

            result = pipeline_get_metadata(mock_ts, ctx, None, pipeline_config)

        assert result == {"tables": {}, "relationships": []}

    def test_includes_relationships_from_pipeline_config(self):
        from mcp_server.services.metadata import pipeline_get_metadata

        ctx = self._make_ctx()
        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(
            sources=[("cases", "Cases")],
            relationships=[
                {
                    "from_table": "forms",
                    "from_column": "case_ids",
                    "to_table": "cases",
                    "to_column": "case_id",
                    "description": "Forms reference cases",
                }
            ],
        )

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {"sources": {"cases": {"rows": 100}}}

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec,
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = mock_run
            mock_exec.return_value = {
                "rows": [["case_id", "text", "NO", None]],
                "row_count": 1,
            }

            result = pipeline_get_metadata(mock_ts, ctx, None, pipeline_config)

        assert "cases" in result["tables"]
        assert len(result["relationships"]) == 1
        rel = result["relationships"][0]
        assert rel["from_table"] == "forms"
        assert rel["to_table"] == "cases"
        assert rel["description"] == "Forms reference cases"
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/test_metadata_service.py::TestPipelineGetMetadata -v
```

Expected: `ImportError` — `pipeline_get_metadata` not defined yet.

**Step 3: Implement `pipeline_get_metadata`**

Add to `mcp_server/services/metadata.py`:

```python
def pipeline_get_metadata(
    tenant_schema: TenantSchema,
    ctx: QueryContext,
    tenant_metadata: TenantMetadata | None,
    pipeline_config: PipelineConfig,
) -> dict:
    """Return full metadata snapshot: tables with enriched columns and pipeline relationships.

    Returns {"tables": {}, "relationships": []} if no completed run exists.
    """
    tables_list = pipeline_list_tables(tenant_schema, pipeline_config)
    if not tables_list:
        return {"tables": {}, "relationships": []}

    tables = {}
    for t in tables_list:
        detail = pipeline_describe_table(t["name"], ctx, tenant_metadata, pipeline_config)
        if detail:
            tables[t["name"]] = detail

    relationships = [
        {
            "from_table": r.from_table,
            "from_column": r.from_column,
            "to_table": r.to_table,
            "to_column": r.to_column,
            "description": r.description,
        }
        for r in pipeline_config.relationships
    ]

    return {"tables": tables, "relationships": relationships}
```

**Step 4: Run all metadata service tests**

```bash
uv run pytest tests/test_metadata_service.py -v
```

Expected: All PASS.

**Step 5: Commit**

```bash
git add mcp_server/services/metadata.py tests/test_metadata_service.py
git commit -m "feat: add pipeline_get_metadata with relationship enrichment"
```

---

### Task 5: Migrate `list_tables` tool in server.py

**Files:**
- Modify: `mcp_server/server.py`
- Modify: `tests/test_mcp_tenant_tools.py`

**Step 1: Update tool tests to reflect new behaviour**

In `tests/test_mcp_tenant_tools.py`:

1. Delete the entire `TestTenantListTables` class (it tested the `_tenant_list_tables` helper which will be removed).

2. Replace `TestListTablesTool` with:

```python
PATCH_PIPELINE_LIST_TABLES = "mcp_server.server.pipeline_list_tables"


def _fake_sync_to_async(fn):
    """Test helper: makes sync_to_async a transparent pass-through."""
    from unittest.mock import AsyncMock

    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


class TestListTablesTool:
    async def test_success_returns_enriched_tables(self, tenant_id, tenant_context):
        from mcp_server.server import list_tables

        mock_ts = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline = "commcare_sync"
        mock_tables = [
            {
                "name": "cases",
                "type": "table",
                "description": "CommCare cases",
                "row_count": 100,
                "materialized_at": "2026-02-24T10:00:00Z",
            }
        ]

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.MaterializationRun") as mock_run_cls,
            patch(PATCH_PIPELINE_LIST_TABLES, return_value=mock_tables),
            patch("mcp_server.server.sync_to_async", side_effect=_fake_sync_to_async),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_run_qs = MagicMock()
            mock_run_qs.order_by.return_value.afirst = AsyncMock(return_value=mock_run)
            mock_run_cls.objects.filter.return_value = mock_run_qs
            mock_run_cls.RunState.COMPLETED = "completed"

            result = await list_tables(tenant_id)

        assert result["success"] is True
        assert len(result["data"]["tables"]) == 1
        assert result["data"]["tables"][0]["row_count"] == 100
        assert result["data"]["note"] is None

    async def test_empty_tables_when_no_completed_run(self, tenant_id, tenant_context):
        from mcp_server.server import list_tables

        mock_ts = MagicMock()

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.MaterializationRun") as mock_run_cls,
            patch(PATCH_PIPELINE_LIST_TABLES, return_value=[]),
            patch("mcp_server.server.sync_to_async", side_effect=_fake_sync_to_async),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_run_qs = MagicMock()
            mock_run_qs.order_by.return_value.afirst = AsyncMock(return_value=None)
            mock_run_cls.objects.filter.return_value = mock_run_qs
            mock_run_cls.RunState.COMPLETED = "completed"

            result = await list_tables(tenant_id)

        assert result["success"] is True
        assert result["data"]["tables"] == []
        assert "run_materialization" in result["data"]["note"]

    async def test_invalid_tenant_returns_validation_error(self):
        from mcp_server.server import list_tables

        with patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema for tenant 'bad'")

            result = await list_tables("bad")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    async def test_returns_empty_when_no_tenant_schema(self, tenant_id, tenant_context):
        from mcp_server.server import list_tables

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=None)

            result = await list_tables(tenant_id)

        assert result["success"] is True
        assert result["data"]["tables"] == []
```

**Step 2: Run to verify new tests fail**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestListTablesTool -v
```

Expected: Tests FAIL because the old implementation still uses `_tenant_list_tables`.

**Step 3: Migrate `list_tables` in `server.py`**

At the top of `mcp_server/server.py`, add to the existing imports:

```python
from mcp_server.services.metadata import (
    pipeline_describe_table,
    pipeline_get_metadata,
    pipeline_list_tables,
)
```

Replace the entire `list_tables` tool function:

```python
@mcp.tool()
async def list_tables(tenant_id: str) -> dict:
    """List all tables in the tenant's database schema.

    Returns table names, types, descriptions, row counts, and materialization timestamps.
    Returns an empty list if no materialization run has completed yet.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
    """
    async with tool_context("list_tables", tenant_id) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        from apps.projects.models import TenantSchema

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"tables": [], "note": None},
                tenant_id=tenant_id,
                schema=ctx.schema_name,
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema=ts,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        tables = await sync_to_async(pipeline_list_tables)(ts, pipeline_config)

        note = (
            "No completed materialization run found. Run run_materialization to load data."
            if not tables
            else None
        )
        tc["result"] = success_response(
            {"tables": tables, "note": note},
            tenant_id=tenant_id,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Step 4: Run to verify pass**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestListTablesTool -v
```

Expected: All PASS.

**Step 5: Commit**

```bash
git add mcp_server/server.py tests/test_mcp_tenant_tools.py
git commit -m "feat: migrate list_tables tool to pipeline-driven metadata service"
```

---

### Task 6: Migrate `describe_table` tool in server.py

**Files:**
- Modify: `mcp_server/server.py`
- Modify: `tests/test_mcp_tenant_tools.py`

**Step 1: Update tool tests**

In `tests/test_mcp_tenant_tools.py`:

1. Delete the entire `TestTenantDescribeTable` class.

2. Replace `TestDescribeTableTool` with:

```python
PATCH_PIPELINE_DESCRIBE_TABLE = "mcp_server.server.pipeline_describe_table"


class TestDescribeTableTool:
    async def test_success_returns_enriched_columns(self, tenant_id, tenant_context):
        from mcp_server.server import describe_table

        mock_ts = MagicMock()
        mock_ts.tenant_membership = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline = "commcare_sync"
        mock_table = {
            "name": "cases",
            "description": "CommCare case records",
            "columns": [
                {"name": "case_id", "type": "text", "nullable": False, "default": None,
                 "description": ""},
                {"name": "properties", "type": "jsonb", "nullable": True, "default": None,
                 "description": "Contains case properties. Available case types: pregnancy"},
            ],
        }

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.TenantMetadata") as mock_tm_cls,
            patch("mcp_server.server.MaterializationRun") as mock_run_cls,
            patch(PATCH_PIPELINE_DESCRIBE_TABLE, return_value=mock_table),
            patch("mcp_server.server.sync_to_async", side_effect=_fake_sync_to_async),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_tm_cls.objects.filter.return_value.afirst = AsyncMock(return_value=MagicMock())
            mock_run_qs = MagicMock()
            mock_run_qs.order_by.return_value.afirst = AsyncMock(return_value=mock_run)
            mock_run_cls.objects.filter.return_value = mock_run_qs
            mock_run_cls.RunState.COMPLETED = "completed"

            result = await describe_table(tenant_id, "cases")

        assert result["success"] is True
        assert result["data"]["name"] == "cases"
        assert result["data"]["description"] == "CommCare case records"
        assert "properties" in [c["name"] for c in result["data"]["columns"]]

    async def test_table_not_found(self, tenant_id, tenant_context):
        from mcp_server.server import describe_table

        mock_ts = MagicMock()
        mock_ts.tenant_membership = MagicMock()

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.TenantMetadata") as mock_tm_cls,
            patch("mcp_server.server.MaterializationRun") as mock_run_cls,
            patch(PATCH_PIPELINE_DESCRIBE_TABLE, return_value=None),
            patch("mcp_server.server.sync_to_async", side_effect=_fake_sync_to_async),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_tm_cls.objects.filter.return_value.afirst = AsyncMock(return_value=None)
            mock_run_qs = MagicMock()
            mock_run_qs.order_by.return_value.afirst = AsyncMock(return_value=None)
            mock_run_cls.objects.filter.return_value = mock_run_qs
            mock_run_cls.RunState.COMPLETED = "completed"

            result = await describe_table(tenant_id, "nonexistent")

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND

    async def test_invalid_tenant_returns_validation_error(self):
        from mcp_server.server import describe_table

        with patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema")
            result = await describe_table("bad", "cases")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
```

**Step 2: Run to verify new tests fail**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestDescribeTableTool -v
```

Expected: FAIL — old implementation doesn't set `description` on the table.

**Step 3: Replace `describe_table` in `server.py`**

`TenantMetadata` needs to be imported in server.py. Add it to the existing import at the top of the file:

```python
from apps.projects.models import MaterializationRun, TenantMetadata
```

Replace the entire `describe_table` tool function:

```python
@mcp.tool()
async def describe_table(tenant_id: str, table_name: str) -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default, description) and a table description.
    JSONB columns are annotated with summaries from the CommCare discover phase when available.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
        table_name: Name of the table to describe.
    """
    async with tool_context("describe_table", tenant_id, table_name=table_name) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        from apps.projects.models import TenantSchema

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()

        last_run = None
        tenant_metadata = None
        if ts is not None:
            last_run = (
                await MaterializationRun.objects.filter(
                    tenant_schema=ts,
                    state=MaterializationRun.RunState.COMPLETED,
                )
                .order_by("-completed_at")
                .afirst()
            )
            tenant_metadata = await TenantMetadata.objects.filter(
                tenant_membership=ts.tenant_membership
            ).afirst()

        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        table = await sync_to_async(pipeline_describe_table)(
            table_name, ctx, tenant_metadata, pipeline_config
        )
        if table is None:
            tc["result"] = error_response(
                NOT_FOUND, f"Table '{table_name}' not found in schema '{ctx.schema_name}'"
            )
            return tc["result"]

        tc["result"] = success_response(
            table,
            tenant_id=tenant_id,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Step 4: Run to verify pass**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestDescribeTableTool -v
```

Expected: All PASS.

**Step 5: Commit**

```bash
git add mcp_server/server.py tests/test_mcp_tenant_tools.py
git commit -m "feat: migrate describe_table tool to pipeline-driven metadata with JSONB annotations"
```

---

### Task 7: Migrate `get_metadata` tool and remove dead helpers

**Files:**
- Modify: `mcp_server/server.py`
- Modify: `tests/test_mcp_tenant_tools.py`

**Step 1: Update get_metadata tests**

Replace `TestGetMetadataTool` in `tests/test_mcp_tenant_tools.py`:

```python
PATCH_PIPELINE_GET_METADATA = "mcp_server.server.pipeline_get_metadata"


class TestGetMetadataTool:
    async def test_returns_tables_and_relationships(self, tenant_id, tenant_context):
        from mcp_server.server import get_metadata

        mock_ts = MagicMock()
        mock_ts.tenant_membership = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline = "commcare_sync"
        mock_result = {
            "tables": {
                "cases": {
                    "name": "cases",
                    "description": "CommCare cases",
                    "columns": [{"name": "case_id", "type": "text", "nullable": False,
                                 "default": None, "description": ""}],
                }
            },
            "relationships": [
                {"from_table": "forms", "from_column": "case_ids",
                 "to_table": "cases", "to_column": "case_id", "description": ""}
            ],
        }

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.TenantMetadata") as mock_tm_cls,
            patch("mcp_server.server.MaterializationRun") as mock_run_cls,
            patch(PATCH_PIPELINE_GET_METADATA, return_value=mock_result),
            patch("mcp_server.server.sync_to_async", side_effect=_fake_sync_to_async),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_tm_cls.objects.filter.return_value.afirst = AsyncMock(return_value=MagicMock())
            mock_run_qs = MagicMock()
            mock_run_qs.order_by.return_value.afirst = AsyncMock(return_value=mock_run)
            mock_run_cls.objects.filter.return_value = mock_run_qs
            mock_run_cls.RunState.COMPLETED = "completed"

            result = await get_metadata(tenant_id)

        assert result["success"] is True
        assert result["data"]["table_count"] == 1
        assert "cases" in result["data"]["tables"]
        assert len(result["data"]["relationships"]) == 1

    async def test_returns_empty_when_no_active_schema(self, tenant_id, tenant_context):
        from mcp_server.server import get_metadata

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=None)

            result = await get_metadata(tenant_id)

        assert result["success"] is True
        assert result["data"]["table_count"] == 0
        assert result["data"]["tables"] == {}
        assert result["data"]["relationships"] == []

    async def test_invalid_tenant_returns_validation_error(self):
        from mcp_server.server import get_metadata

        with patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema")
            result = await get_metadata("bad")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestGetMetadataTool -v
```

Expected: FAIL — old implementation doesn't return `relationships`.

**Step 3: Replace `get_metadata` in `server.py` and remove dead helpers**

Replace the entire `get_metadata` tool function:

```python
@mcp.tool()
async def get_metadata(tenant_id: str) -> dict:
    """Get a complete metadata snapshot for the tenant's database.

    Returns all tables with their columns, descriptions, and table relationships
    defined by the materialization pipeline.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
    """
    async with tool_context("get_metadata", tenant_id) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        from apps.projects.models import TenantSchema

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"schema": ctx.schema_name, "table_count": 0, "tables": {}, "relationships": []},
                tenant_id=tenant_id,
                schema=ctx.schema_name,
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema=ts,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        tenant_metadata = await TenantMetadata.objects.filter(
            tenant_membership=ts.tenant_membership
        ).afirst()

        metadata = await sync_to_async(pipeline_get_metadata)(
            ts, ctx, tenant_metadata, pipeline_config
        )

        tc["result"] = success_response(
            {
                "schema": ctx.schema_name,
                "table_count": len(metadata["tables"]),
                "tables": metadata["tables"],
                "relationships": metadata["relationships"],
            },
            tenant_id=tenant_id,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

Also **delete** the two dead helper functions from `server.py` — `_tenant_list_tables` and `_tenant_describe_table` (and their docblock comment `# --- Tenant metadata helpers ---`).

**Step 4: Run full test suite**

```bash
uv run pytest tests/test_mcp_tenant_tools.py tests/test_metadata_service.py tests/test_pipeline_registry.py -v
```

Expected: All PASS. If `TestTenantListTables` or `TestTenantDescribeTable` still exist and reference removed functions, delete them now.

**Step 5: Run all backend tests**

```bash
uv run pytest
```

Expected: All PASS (or same failures as before this feature branch).

**Step 6: Commit**

```bash
git add mcp_server/server.py tests/test_mcp_tenant_tools.py
git commit -m "feat: migrate get_metadata tool to pipeline service, remove _tenant_* helpers"
```
