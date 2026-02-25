"""Three-phase materialization orchestrator: Discover → Load → Transform.

Design notes:
- All source writes share a single psycopg connection, committed in one
  transaction. A mid-run failure rolls back all sources atomically.
- Loaders expose load_pages() iterators; rows are written page-by-page so the
  full dataset is never held in memory. Inserts use executemany for efficiency.
- Transform failures are isolated — run is marked COMPLETED; error stored in result.
- The final COMPLETED state is written via a conditional UPDATE (filter on TRANSFORMING)
  so that a concurrent cancel_materialization call is not overwritten. Note: cancellation
  during DISCOVER/LOAD phases will be overwritten by subsequent state transitions;
  full cancellation support requires Celery workers (see TODO.md).
- A step count check at the end guards against total_steps / report() drift.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from psycopg import sql as psql

from apps.projects.models import MaterializationRun, TenantMetadata
from apps.projects.services.schema_manager import SchemaManager, get_managed_db_connection
from mcp_server.loaders.commcare_cases import CommCareCaseLoader
from mcp_server.loaders.commcare_forms import CommCareFormLoader
from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader
from mcp_server.pipeline_registry import PipelineConfig

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


def run_pipeline(
    tenant_membership: Any,
    credential: dict[str, str],
    pipeline: PipelineConfig,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Run a three-phase materialization pipeline.

    Phases:
      1. DISCOVER — Fetch CommCare metadata, store in TenantMetadata (survives teardown).
      2. LOAD    — Execute loaders for each source, stream-write to tenant schema tables.
      3. TRANSFORM — Run DBT (if configured), or no-op. Failures are isolated.

    Args:
        tenant_membership: The TenantMembership to sync.
        credential: {"type": "oauth"|"api_key", "value": str}
        pipeline: Pipeline configuration from the registry.
        progress_callback: Optional callable(current, total, message).

    Returns a summary dict with run_id, status, and per-source row counts.
    """
    # total steps: provision + discover + N sources + transform/skip
    total_steps = 2 + len(pipeline.sources) + 1
    step = 0

    def report(message: str) -> None:
        nonlocal step
        step += 1
        if progress_callback:
            progress_callback(step, total_steps, message)

    # ── 1. PROVISION ──────────────────────────────────────────────────────────
    report(f"Provisioning schema for {tenant_membership.tenant_id}...")
    tenant_schema = SchemaManager().provision(tenant_membership)
    schema_name = tenant_schema.schema_name

    run = MaterializationRun.objects.create(
        tenant_schema=tenant_schema,
        pipeline=pipeline.name,
        state=MaterializationRun.RunState.DISCOVERING,
    )

    source_results: dict[str, dict] = {}

    try:
        # ── 2. DISCOVER ───────────────────────────────────────────────────────
        report("Discovering tenant metadata from CommCare...")
        _run_discover_phase(tenant_membership, credential, pipeline)

        # ── 3. LOAD ───────────────────────────────────────────────────────────
        run.state = MaterializationRun.RunState.LOADING
        run.save(update_fields=["state"])

        conn = get_managed_db_connection()
        conn.autocommit = False
        try:
            for source in pipeline.sources:
                report(f"Loading {source.name} from CommCare API...")
                rows = _load_source(source.name, tenant_membership, credential, schema_name, conn)
                source_results[source.name] = {"state": "loaded", "rows": rows}
                logger.info("Loaded %d rows into %s.%s", rows, schema_name, source.name)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    except Exception:
        run.state = MaterializationRun.RunState.FAILED
        run.completed_at = datetime.now(UTC)
        run.result = {"error": "Pipeline failed", "sources": source_results}
        run.save(update_fields=["state", "completed_at", "result"])
        raise

    # ── 4. TRANSFORM ──────────────────────────────────────────────────────────
    # Transform errors are isolated — failure here does NOT mark the run FAILED.
    run.state = MaterializationRun.RunState.TRANSFORMING
    run.save(update_fields=["state"])
    transform_result: dict = {}

    if pipeline.transforms and pipeline.dbt_models:
        report("Running DBT transforms...")
        try:
            transform_result = _run_transform_phase(pipeline, schema_name)
        except Exception as e:
            logger.error("Transform phase failed for schema %s: %s", schema_name, e)
            transform_result = {"error": str(e)}
    else:
        report("No DBT transforms configured — skipping")

    # ── 5. COMPLETE ───────────────────────────────────────────────────────────
    # Conditional UPDATE: only transition to COMPLETED if still in TRANSFORMING
    # state. This preserves a FAILED state written by cancel_materialization
    # while the transform phase was running.
    final_result = {
        "sources": source_results,
        "pipeline": pipeline.name,
        "transforms": transform_result,
    }
    now = datetime.now(UTC)
    rows_updated = MaterializationRun.objects.filter(
        id=run.id, state=MaterializationRun.RunState.TRANSFORMING
    ).update(
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=now,
        result=final_result,
    )
    if rows_updated:
        run.state = MaterializationRun.RunState.COMPLETED  # reflect DB update locally
    else:
        logger.info(
            "Run %s state changed externally (cancelled?); preserving current DB state", run.id
        )

    tenant_schema.state = "active"
    tenant_schema.save(update_fields=["state", "last_accessed_at"])

    total_rows = sum(s.get("rows", 0) for s in source_results.values())
    logger.info("Pipeline '%s' complete for '%s': %d rows", pipeline.name, schema_name, total_rows)

    if step != total_steps:
        raise RuntimeError(
            f"Progress step count mismatch: expected {total_steps}, got {step}. "
            "Update total_steps if you add/remove report() calls."
        )

    transform_error = transform_result.get("error")
    result: dict = {
        "status": "completed",
        "run_id": str(run.id),
        "schema": schema_name,
        "pipeline": pipeline.name,
        "sources": source_results,
        "rows_loaded": total_rows,
    }
    if transform_error:
        result["transform_error"] = transform_error
    return result


def _run_discover_phase(
    tenant_membership: Any, credential: dict[str, str], pipeline: PipelineConfig
) -> None:
    """Fetch CommCare metadata and upsert into TenantMetadata."""
    from django.utils import timezone

    if not pipeline.has_metadata_discovery:
        return

    loader = CommCareMetadataLoader(domain=tenant_membership.tenant_id, credential=credential)
    metadata = loader.load()

    TenantMetadata.objects.update_or_create(
        tenant_membership=tenant_membership,
        defaults={"metadata": metadata, "discovered_at": timezone.now()},
    )
    logger.info(
        "Stored metadata for tenant %s: %d apps, %d case types",
        tenant_membership.tenant_id,
        len(metadata.get("app_definitions", [])),
        len(metadata.get("case_types", [])),
    )


def _load_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
) -> int:
    domain = tenant_membership.tenant_id
    if source_name == "cases":
        loader = CommCareCaseLoader(domain=domain, credential=credential)
        return _write_cases(loader.load_pages(), schema_name, conn)
    if source_name == "forms":
        loader = CommCareFormLoader(domain=domain, credential=credential)
        return _write_forms(loader.load_pages(), schema_name, conn)
    raise ValueError(f"Unknown source '{source_name}'. Known sources: cases, forms")


def _run_transform_phase(pipeline: PipelineConfig, schema_name: str) -> dict:
    import tempfile

    from django.conf import settings

    from mcp_server.services.dbt_runner import generate_profiles_yml, run_dbt

    db_url = getattr(settings, "MANAGED_DATABASE_URL", "")
    repo_root = pathlib.Path(__file__).parent.parent.parent
    dbt_project_dir = str(repo_root / pipeline.transforms.dbt_project)

    with tempfile.TemporaryDirectory() as tmpdir:
        profiles_path = pathlib.Path(tmpdir) / "profiles.yml"
        generate_profiles_yml(output_path=profiles_path, schema_name=schema_name, db_url=db_url)
        return run_dbt(
            dbt_project_dir=dbt_project_dir, profiles_dir=tmpdir, models=pipeline.dbt_models
        )


# ── Table writers ──────────────────────────────────────────────────────────────
# Writers accept a shared psycopg connection managed by the caller.
# The caller owns commit/rollback; writers only cursor.execute.

_CASES_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.cases
        (case_id, case_type, case_name, external_id, owner_id,
         date_opened, last_modified, server_last_modified, indexed_on,
         closed, date_closed, properties, indices)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (case_id) DO UPDATE SET
        case_name=EXCLUDED.case_name, owner_id=EXCLUDED.owner_id,
        last_modified=EXCLUDED.last_modified,
        server_last_modified=EXCLUDED.server_last_modified,
        indexed_on=EXCLUDED.indexed_on, closed=EXCLUDED.closed,
        date_closed=EXCLUDED.date_closed, properties=EXCLUDED.properties,
        indices=EXCLUDED.indices
    """
)

_FORMS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.forms
        (form_id, xmlns, received_on, server_modified_on, app_id, form_data, case_ids)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (form_id) DO UPDATE SET
        received_on=EXCLUDED.received_on,
        server_modified_on=EXCLUDED.server_modified_on,
        form_data=EXCLUDED.form_data,
        case_ids=EXCLUDED.case_ids
    """
)


def _write_cases(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the cases table and bulk-insert all pages. Returns total row count."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.cases CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.cases (
            case_id TEXT PRIMARY KEY,
            case_type TEXT,
            case_name TEXT,
            external_id TEXT,
            owner_id TEXT,
            date_opened TEXT,
            last_modified TEXT,
            server_last_modified TEXT,
            indexed_on TEXT,
            closed BOOLEAN DEFAULT FALSE,
            date_closed TEXT,
            properties JSONB DEFAULT '{{}}'::jsonb,
            indices JSONB DEFAULT '{{}}'::jsonb
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CASES_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                c.get("case_id"),
                c.get("case_type", ""),
                c.get("case_name", ""),
                c.get("external_id", ""),
                c.get("owner_id", ""),
                c.get("date_opened", ""),
                c.get("last_modified", ""),
                c.get("server_last_modified", ""),
                c.get("indexed_on", ""),
                c.get("closed", False),
                c.get("date_closed") or "",
                json.dumps(c.get("properties", {})),
                json.dumps(c.get("indices", {})),
            )
            for c in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_forms(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the forms table and bulk-insert all pages. Returns total row count."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.forms CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.forms (
            form_id TEXT PRIMARY KEY,
            xmlns TEXT,
            received_on TEXT,
            server_modified_on TEXT,
            app_id TEXT,
            form_data JSONB DEFAULT '{{}}'::jsonb,
            case_ids JSONB DEFAULT '[]'::jsonb
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _FORMS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                f.get("form_id", ""),
                f.get("xmlns", ""),
                f.get("received_on", ""),
                f.get("server_modified_on", ""),
                f.get("app_id", ""),
                json.dumps(f.get("form_data", {})),
                json.dumps(f.get("case_ids", [])),
            )
            for f in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


# ── Backwards-compatible shim ──────────────────────────────────────────────────


def run_commcare_sync(tenant_membership: Any, credential: dict[str, str]) -> dict:
    """Legacy entry point — delegates to run_pipeline with the default registry."""
    from mcp_server.pipeline_registry import get_registry

    pipeline = get_registry().get("commcare_sync")
    if pipeline is None:
        raise ValueError("commcare_sync pipeline not found in registry")
    return run_pipeline(tenant_membership, credential, pipeline)
