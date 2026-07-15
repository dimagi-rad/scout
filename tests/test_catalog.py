"""Tests for the ONE reconciled catalog service (arch #251, Phase 3).

These pin the properties that close the #190 divergence class:

- ``list_catalog`` and ``describe`` agree for the SAME context — a listed table
  can never 404, and a terminal asset whose physical table is absent is never
  listed (uniform fail-closed reconciliation).
- Every surface (async ``list_catalog`` for the prompt + MCP tools, sync
  ``list_catalog_sync`` for the DRF dictionary) returns the SAME table set — same
  source-of-truth, reconciliation, and ``stg_*`` policy — for single- AND
  multi-tenant workspaces.
- The single ``stg_*`` policy (Decision 4a): intermediate staging models hidden,
  terminal assets surfaced when physically present.
- ``get_metadata`` for a multi-tenant ``ws_*`` view schema returns real
  columns/table_count (the lookup-miss regression), not 0.
- A transient ``information_schema`` failure yields a fail-closed empty catalog.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.transformations.models import TransformationAsset, TransformationScope
from apps.users.models import Tenant
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from apps.workspaces.services import catalog
from apps.workspaces.services.catalog import (
    CatalogContext,
    catalog_metadata,
    describe,
    list_catalog,
    list_catalog_sync,
)
from mcp_server.context import QueryContext
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig

RunState = MaterializationRun.RunState


def _pipeline(sources=None, dbt_models=None):
    from mcp_server.pipeline_registry import TransformConfig

    cfg = PipelineConfig(
        name="commcare_sync",
        description="",
        version="1.0",
        provider="commcare",
        sources=[SourceConfig(name=n, description=d) for n, d in (sources or [])],
    )
    if dbt_models:
        object.__setattr__(
            cfg, "transforms", TransformConfig(dbt_project="transforms/commcare", models=dbt_models)
        )
    return cfg


def _query_context(schema_name="t_test"):
    return QueryContext(
        tenant_id="test-domain",
        schema_name=schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params={},
    )


async def _make_tenant_schema(schema_name="t_test"):
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id=f"d-{uuid.uuid4().hex}", canonical_name="T"
    )
    ts = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=schema_name, state=SchemaState.ACTIVE
    )
    return tenant, ts


async def _completed_run(ts, sources):
    return await MaterializationRun.objects.acreate(
        tenant_schema=ts,
        pipeline="commcare_sync",
        state=RunState.COMPLETED,
        completed_at=timezone.now(),
        result={"sources": sources},
    )


def _fake_columns_exec(live: set[str]):
    """Return a fake _execute_async_parameterized that mirrors ``live``.

    information_schema.columns returns rows only for a table in ``live`` — so the
    describe path and the list path see the SAME physical reality, which is what
    makes a listed-but-undescribable table impossible by construction.
    """

    async def _exec(ctx, sql, params, timeout):  # noqa: ASYNC109 — fake mirrors the real executor signature
        if "information_schema.columns" in sql:
            table_name = params[1]
            if table_name in live:
                return {
                    "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                    "rows": [["id", "text", "NO", None]],
                    "row_count": 1,
                }
            return {"columns": [], "rows": [], "row_count": 0}
        # information_schema.tables (view-schema listing): return VIEWs from live.
        return {"rows": [[name] for name in sorted(live)]}

    return _exec


# ── #190: list ⇔ describe agreement + fail-closed terminal assets ─────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_listed_table_is_always_describable_and_absent_terminal_is_hidden():
    tenant, ts = await _make_tenant_schema()
    await _completed_run(ts, {"cases": {"state": "completed", "rows": 10}})

    # A terminal asset that replaces a staging model.
    stg = await TransformationAsset.objects.acreate(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
        description="staging",
    )
    await TransformationAsset.objects.acreate(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
        description="cleaned cases",
        replaces=stg,
    )

    pipeline = _pipeline(sources=[("cases", "Cases")])
    context = CatalogContext(
        query_context=_query_context(ts.schema_name),
        is_view_schema=False,
        tenant_schema=ts,
        pipeline_config=pipeline,
        tenant_ids=[tenant.id],
        workspace_id=None,
    )

    # (a) terminal asset physically PRESENT → listed AND describable (no 404).
    live_present = {"raw_cases", "cases_clean"}
    with (
        patch.object(catalog, "_live_tables_in_schema", new=AsyncMock(return_value=live_present)),
        patch(
            "mcp_server.services.metadata._execute_async_parameterized",
            new=AsyncMock(side_effect=_fake_columns_exec(live_present)),
        ),
    ):
        tables = await list_catalog(context)
        names = {t.name for t in tables}
        assert "cases_clean" in names
        for t in tables:
            detail = await describe(context, t.name)
            assert detail is not None, f"listed table {t.name} must be describable, never 404"

    # (b) terminal asset physically ABSENT → NOT listed (fail-closed).
    live_absent = {"raw_cases"}
    with (
        patch.object(catalog, "_live_tables_in_schema", new=AsyncMock(return_value=live_absent)),
        patch(
            "mcp_server.services.metadata._execute_async_parameterized",
            new=AsyncMock(side_effect=_fake_columns_exec(live_absent)),
        ),
    ):
        tables = await list_catalog(context)
        names = {t.name for t in tables}
        assert "cases_clean" not in names, "an absent terminal asset must not be advertised"
        assert "raw_cases" in names


# ── stg_* policy: staging hidden, terminal shown ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stg_hidden_terminal_shown_and_dbt_models_not_surfaced():
    tenant, ts = await _make_tenant_schema()
    await _completed_run(ts, {"cases": {"state": "completed", "rows": 10}})
    await TransformationAsset.objects.acreate(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
        description="cleaned",
    )
    # A staging-named terminal asset must still be hidden by the stg_* policy.
    await TransformationAsset.objects.acreate(
        name="stg_scratch",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
        description="staging scratch",
    )

    # Phase 5 (#251): declared dbt models (stg_ and non-stg) physically present
    # must NOT be surfaced — the pipeline dbt-model listing loop was removed.
    pipeline = _pipeline(sources=[("cases", "Cases")], dbt_models=["stg_cases", "dim_cases"])
    context = CatalogContext(
        query_context=_query_context(ts.schema_name),
        is_view_schema=False,
        tenant_schema=ts,
        pipeline_config=pipeline,
        tenant_ids=[tenant.id],
        workspace_id=None,
    )
    live = {"raw_cases", "stg_cases", "dim_cases", "stg_scratch", "cases_clean"}
    with patch.object(catalog, "_live_tables_in_schema", new=AsyncMock(return_value=live)):
        names = {t.name for t in await list_catalog(context)}

    assert "cases_clean" in names
    assert "raw_cases" in names
    assert "stg_scratch" not in names, "staging-named terminal assets are hidden everywhere"
    assert "dim_cases" not in names, "pipeline dbt models are never surfaced (Phase 5)"
    assert "stg_cases" not in names, "pipeline dbt models are never surfaced (Phase 5)"


# ── cross-surface agreement: async list_catalog == sync list_catalog_sync ────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_and_sync_single_tenant_agree():
    tenant, ts = await _make_tenant_schema()
    await _completed_run(
        ts,
        {
            "cases": {"state": "completed", "rows": 10},
            "forms": {"state": "failed", "rows": 0},
        },
    )
    await TransformationAsset.objects.acreate(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
        description="cleaned",
    )
    pipeline = _pipeline(sources=[("cases", "Cases"), ("forms", "Forms")], dbt_models=["dim_x"])
    live = {"raw_cases", "dim_x", "cases_clean"}

    context = CatalogContext(
        query_context=_query_context(ts.schema_name),
        is_view_schema=False,
        tenant_schema=ts,
        pipeline_config=pipeline,
        tenant_ids=[tenant.id],
        workspace_id=None,
    )
    with patch.object(catalog, "_live_tables_in_schema", new=AsyncMock(return_value=live)):
        async_names = {t.name for t in await list_catalog(context)}

    sync_rows = await sync_to_async(list_catalog_sync)(ts, pipeline, live)
    sync_names = {r["name"] for r in sync_rows}

    assert async_names == sync_names == {"raw_cases", "cases_clean"}, (
        "the DRF dictionary must return the same reconciled set as the prompt/MCP tools "
        "(failed source excluded, declared dbt model not surfaced, terminal asset surfaced)"
    )


# ── multi-tenant: get_metadata returns real columns (ws_* lookup-miss fix) ────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_multi_tenant_metadata_returns_real_columns():
    live = {"tenant_a__cases", "tenant_b__forms"}
    context = CatalogContext(
        query_context=_query_context("ws_abcdef123456"),
        is_view_schema=True,
        tenant_schema=None,
        pipeline_config=None,
        workspace_id=str(uuid.uuid4()),
    )
    with patch(
        "mcp_server.services.metadata._execute_async_parameterized",
        new=AsyncMock(side_effect=_fake_columns_exec(live)),
    ):
        tables = {t.name for t in await list_catalog(context)}
        metadata = await catalog_metadata(context)

    assert tables == live
    # The old ws_* lookup miss returned table_count=0; it must now be real.
    assert len(metadata["tables"]) == 2
    assert metadata["tables"]["tenant_a__cases"]["columns"], "columns must be populated for ws_*"


# ── failure mode: transient information_schema error → fail-closed empty ──────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_transient_live_lookup_failure_yields_empty_catalog():
    tenant, ts = await _make_tenant_schema()
    await _completed_run(ts, {"cases": {"state": "completed", "rows": 10}})
    pipeline = _pipeline(sources=[("cases", "Cases")])
    context = CatalogContext(
        query_context=_query_context(ts.schema_name),
        is_view_schema=False,
        tenant_schema=ts,
        pipeline_config=pipeline,
        tenant_ids=[tenant.id],
        workspace_id=None,
    )
    # _live_tables_in_schema swallows a transient DB error and returns an empty
    # set; the catalog must then surface nothing rather than a phantom table.
    with patch.object(catalog, "_live_tables_in_schema", new=AsyncMock(return_value=set())):
        tables = await list_catalog(context)
    assert tables == []
