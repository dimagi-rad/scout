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
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from django.utils import timezone
from psycopg import sql as psql

from apps.transformations.models import TransformationAsset
from apps.transformations.services.commcare_staging import upsert_system_assets
from apps.transformations.services.executor import run_transformation_pipeline
from apps.workspaces.models import MaterializationRun, TenantMetadata
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection
from mcp_server.loaders.commcare_cases import CommCareCaseLoader
from mcp_server.loaders.commcare_forms import CommCareFormLoader
from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader
from mcp_server.loaders.connect_assessments import ConnectAssessmentLoader
from mcp_server.loaders.connect_completed_modules import ConnectCompletedModuleLoader
from mcp_server.loaders.connect_completed_works import ConnectCompletedWorkLoader
from mcp_server.loaders.connect_invoices import ConnectInvoiceLoader
from mcp_server.loaders.connect_metadata import ConnectMetadataLoader
from mcp_server.loaders.connect_payments import ConnectPaymentLoader
from mcp_server.loaders.connect_users import ConnectUserLoader
from mcp_server.loaders.connect_visits import ConnectVisitLoader
from mcp_server.loaders.ocs_experiments import OCSExperimentLoader
from mcp_server.loaders.ocs_messages import OCSMessageLoader
from mcp_server.loaders.ocs_metadata import OCSMetadataLoader
from mcp_server.loaders.ocs_participants import OCSParticipantLoader
from mcp_server.loaders.ocs_sessions import OCSSessionLoader
from mcp_server.pipeline_registry import PipelineConfig, get_registry

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


def provision_run(
    tenant_membership: Any,
    pipeline: PipelineConfig,
) -> tuple["MaterializationRun", str]:
    """Provision the tenant schema and create a MaterializationRun record.

    Separates the synchronous "start" step from the long-running pipeline so
    callers can obtain a run_id immediately and launch the rest in the background.

    Returns:
        (run, schema_name) tuple.
    """
    tenant_schema = SchemaManager().provision(tenant_membership.tenant)
    run = MaterializationRun.objects.create(
        tenant_schema=tenant_schema,
        pipeline=pipeline.name,
        state=MaterializationRun.RunState.DISCOVERING,
    )
    return run, tenant_schema.schema_name


def run_pipeline(
    tenant_membership: Any,
    credential: dict[str, str],
    pipeline: PipelineConfig,
    progress_callback: ProgressCallback | None = None,
    existing_run: "MaterializationRun | None" = None,
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
    report(f"Provisioning schema for {tenant_membership.tenant.external_id}...")
    tenant_schema = SchemaManager().provision(tenant_membership.tenant)
    schema_name = tenant_schema.schema_name

    if existing_run is not None:
        # Caller pre-created the run (e.g. to obtain a run_id before launching
        # this function in a background task). Reuse it instead of creating a
        # duplicate record.
        run = existing_run
    else:
        run = MaterializationRun.objects.create(
            tenant_schema=tenant_schema,
            pipeline=pipeline.name,
            state=MaterializationRun.RunState.DISCOVERING,
        )

    source_results: dict[str, dict] = {}

    try:
        # ── 2. DISCOVER ───────────────────────────────────────────────────────
        report(f"Discovering tenant metadata from {pipeline.provider}...")
        _run_discover_phase(tenant_membership, credential, pipeline)

        # Generate system staging assets from discovered metadata.
        # Failures are logged but do not fail the pipeline — the discover phase
        # has already stored metadata, and load can proceed without assets.
        if pipeline.provider == "commcare":
            try:
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
            except Exception:
                logger.exception(
                    "Failed to generate system assets for %s; continuing pipeline",
                    tenant_membership.tenant.external_id,
                )

        # ── 3. LOAD ───────────────────────────────────────────────────────────
        run.state = MaterializationRun.RunState.LOADING
        run.save(update_fields=["state"])

        conn = get_managed_db_connection()
        conn.autocommit = False
        try:
            for source in pipeline.sources:
                report(f"Loading {source.name} from {pipeline.provider} API...")
                rows = _load_source(
                    source.name,
                    tenant_membership,
                    credential,
                    schema_name,
                    conn,
                    provider=pipeline.provider,
                )
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

    # Check if there are any TransformationAssets to execute
    has_assets = TransformationAsset.objects.filter(tenant=tenant_membership.tenant).exists()
    if has_assets:
        report("Running transforms...")
        try:
            transform_result = _run_transform_phase(
                pipeline, schema_name, tenant=tenant_membership.tenant
            )
        except Exception as e:
            logger.exception("Transform phase failed for schema %s", schema_name)
            transform_result = {"error": str(e)}
    else:
        report("No transforms configured — skipping")

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
    """Fetch provider metadata and upsert into TenantMetadata."""
    if not pipeline.has_metadata_discovery:
        return

    if pipeline.provider == "commcare_connect":
        loader = ConnectMetadataLoader(
            opportunity_id=int(tenant_membership.tenant.external_id),
            credential=credential,
        )
    elif pipeline.provider == "ocs":
        loader = OCSMetadataLoader(
            experiment_id=tenant_membership.tenant.external_id,
            credential=credential,
        )
    else:
        loader = CommCareMetadataLoader(
            domain=tenant_membership.tenant.external_id, credential=credential
        )
    metadata = loader.load()

    TenantMetadata.objects.update_or_create(
        tenant_membership=tenant_membership,
        defaults={"metadata": metadata, "discovered_at": timezone.now()},
    )
    logger.info("Stored metadata for tenant %s", tenant_membership.tenant.external_id)


def _load_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
    provider: str = "commcare",
) -> int:
    if provider == "commcare_connect":
        return _load_connect_source(source_name, tenant_membership, credential, schema_name, conn)
    if provider == "ocs":
        return _load_ocs_source(source_name, tenant_membership, credential, schema_name, conn)
    # Existing CommCare dispatch
    domain = tenant_membership.tenant.external_id
    if source_name == "cases":
        loader = CommCareCaseLoader(domain=domain, credential=credential)
        return _write_cases(loader.load_pages(), schema_name, conn)
    if source_name == "forms":
        loader = CommCareFormLoader(domain=domain, credential=credential)
        return _write_forms(loader.load_pages(), schema_name, conn)
    raise ValueError(f"Unknown source '{source_name}'. Known sources: cases, forms")


def _load_connect_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
) -> int:
    opp_id = int(tenant_membership.tenant.external_id)
    loader_map = {
        "visits": (ConnectVisitLoader, _write_connect_visits),
        "users": (ConnectUserLoader, _write_connect_users),
        "completed_works": (ConnectCompletedWorkLoader, _write_connect_completed_works),
        "payments": (ConnectPaymentLoader, _write_connect_payments),
        "invoices": (ConnectInvoiceLoader, _write_connect_invoices),
        "assessments": (ConnectAssessmentLoader, _write_connect_assessments),
        "completed_modules": (ConnectCompletedModuleLoader, _write_connect_completed_modules),
    }
    if source_name not in loader_map:
        known = ", ".join(loader_map.keys())
        raise ValueError(f"Unknown Connect source '{source_name}'. Known: {known}")

    loader_cls, writer_fn = loader_map[source_name]
    loader = loader_cls(opportunity_id=opp_id, credential=credential)
    return writer_fn(loader.load_pages(), schema_name, conn)


def _load_ocs_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
) -> int:
    experiment_id = tenant_membership.tenant.external_id
    loader_map = {
        "experiments": (OCSExperimentLoader, _write_ocs_experiments),
        "sessions": (OCSSessionLoader, _write_ocs_sessions),
        "messages": (OCSMessageLoader, _write_ocs_messages),
        "participants": (OCSParticipantLoader, _write_ocs_participants),
    }
    if source_name not in loader_map:
        known = ", ".join(loader_map.keys())
        raise ValueError(f"Unknown OCS source '{source_name}'. Known: {known}")

    loader_cls, writer_fn = loader_map[source_name]
    loader = loader_cls(experiment_id=experiment_id, credential=credential)
    return writer_fn(loader.load_pages(), schema_name, conn)


def _write_ocs_experiments(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the experiments table and bulk-insert all pages."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_experiments CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_experiments (
            experiment_id TEXT PRIMARY KEY,
            name TEXT,
            url TEXT,
            version_number INTEGER
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _OCS_EXPERIMENTS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("experiment_id", ""),
                r.get("name", ""),
                r.get("url", ""),
                r.get("version_number"),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_ocs_sessions(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the sessions table and bulk-insert all pages."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_sessions CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_sessions (
            session_id TEXT PRIMARY KEY,
            experiment_id TEXT,
            participant_identifier TEXT,
            participant_platform TEXT,
            created_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ,
            tags JSONB
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _OCS_SESSIONS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("session_id", ""),
                r.get("experiment_id", ""),
                r.get("participant_identifier", ""),
                r.get("participant_platform", ""),
                r.get("created_at"),
                r.get("updated_at"),
                json.dumps(r.get("tags") or []),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_ocs_messages(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the messages table and bulk-insert all pages."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_messages CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_messages (
            message_id TEXT PRIMARY KEY,
            session_id TEXT,
            message_index INTEGER,
            role TEXT,
            content TEXT,
            created_at TIMESTAMPTZ,
            metadata JSONB,
            tags JSONB
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _OCS_MESSAGES_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("message_id", ""),
                r.get("session_id", ""),
                r.get("message_index", 0),
                r.get("role", ""),
                r.get("content", ""),
                r.get("created_at"),
                json.dumps(r.get("metadata") or {}),
                json.dumps(r.get("tags") or []),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_ocs_participants(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the participants table and bulk-insert all pages."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_participants CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_participants (
            identifier TEXT PRIMARY KEY,
            platform TEXT,
            remote_id TEXT
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _OCS_PARTICIPANTS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("identifier", ""),
                r.get("platform", ""),
                r.get("remote_id", ""),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _run_transform_phase(pipeline: PipelineConfig, schema_name: str, tenant=None) -> dict:
    """Run the three-stage transformation pipeline using TransformationAsset records."""
    run = run_transformation_pipeline(
        tenant=tenant,
        schema_name=schema_name,
    )

    result = {
        "run_id": str(run.id),
        "status": run.status,
        "asset_count": run.asset_runs.count(),
    }
    if run.error_message:
        result["error"] = run.error_message
    return result


# ── Table writers ──────────────────────────────────────────────────────────────
# Writers accept a shared psycopg connection managed by the caller.
# The caller owns commit/rollback; writers only cursor.execute.

_CASES_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_cases
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
    INSERT INTO {schema}.raw_forms
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

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_cases CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_cases (
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

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_forms CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_forms (
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


# ── Connect table writers ──────────────────────────────────────────────────────


def _json_or_none(value: Any) -> str | None:
    """Serialize a value to a JSON string, or return None for SQL NULL.

    Use for nullable JSONB columns (flag_reason, claim_limits) where the
    v2 export may return ``null``. json.dumps(None) would produce the
    literal string "null", which inserts as JSONB ``null`` — not the
    same as SQL NULL. This helper preserves the distinction.
    """
    if value is None:
        return None
    return json.dumps(value)


_CONNECT_VISITS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_visits
        (visit_id, opportunity_id, username, deliver_unit, entity_id, entity_name,
         visit_date, status, reason, location, flagged, flag_reason, form_json,
         completed_work, status_modified_date, review_status, review_created_on,
         justification, date_created, completed_work_id, deliver_unit_id, images)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (visit_id) DO UPDATE SET
        status=EXCLUDED.status, form_json=EXCLUDED.form_json,
        review_status=EXCLUDED.review_status, images=EXCLUDED.images
    """
)

_CONNECT_USERS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_users
        (username, name, phone, date_learn_started, user_invite_status,
         payment_accrued, suspended, suspension_date, suspension_reason,
         invited_date, completed_learn_date, last_active, date_claimed, claim_limits)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (username) DO UPDATE SET
        name=EXCLUDED.name, phone=EXCLUDED.phone,
        payment_accrued=EXCLUDED.payment_accrued, last_active=EXCLUDED.last_active
    """
)

_CONNECT_COMPLETED_WORKS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_completed_works
        (username, opportunity_id, payment_unit_id, status, last_modified,
         entity_id, entity_name, reason, status_modified_date, payment_date,
         date_created, saved_completed_count, saved_approved_count,
         saved_payment_accrued, saved_payment_accrued_usd,
         saved_org_payment_accrued, saved_org_payment_accrued_usd)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
)

_CONNECT_PAYMENTS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_payments
        (username, opportunity_id, created_at, amount, amount_usd, date_paid,
         payment_unit, confirmed, confirmation_date, organization, invoice_id,
         payment_method, payment_operator)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
)

_CONNECT_INVOICES_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_invoices
        (opportunity_id, amount, amount_usd, date, invoice_number,
         service_delivery, exchange_rate)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
)

_CONNECT_ASSESSMENTS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_assessments
        (username, app, opportunity_id, date, score, passing_score, passed)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
)

_CONNECT_COMPLETED_MODULES_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_completed_modules
        (username, module, opportunity_id, date, duration)
    VALUES (%s, %s, %s, %s, %s)
    """
)

_OCS_EXPERIMENTS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_experiments
        (experiment_id, name, url, version_number)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (experiment_id) DO UPDATE SET
        name=EXCLUDED.name, url=EXCLUDED.url,
        version_number=EXCLUDED.version_number
    """
)

_OCS_SESSIONS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_sessions
        (session_id, experiment_id, participant_identifier, participant_platform,
         created_at, updated_at, tags)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (session_id) DO UPDATE SET
        experiment_id=EXCLUDED.experiment_id,
        participant_identifier=EXCLUDED.participant_identifier,
        participant_platform=EXCLUDED.participant_platform,
        updated_at=EXCLUDED.updated_at, tags=EXCLUDED.tags
    """
)

_OCS_MESSAGES_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_messages
        (message_id, session_id, message_index, role, content,
         created_at, metadata, tags)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (message_id) DO UPDATE SET
        role=EXCLUDED.role, content=EXCLUDED.content,
        metadata=EXCLUDED.metadata, tags=EXCLUDED.tags
    """
)

_OCS_PARTICIPANTS_INSERT = psql.SQL(
    """
    INSERT INTO {schema}.raw_participants
        (identifier, platform, remote_id)
    VALUES (%s, %s, %s)
    ON CONFLICT (identifier) DO UPDATE SET
        platform=EXCLUDED.platform, remote_id=EXCLUDED.remote_id
    """
)


def _write_connect_visits(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the visits table and bulk-insert all pages. Returns total row count.

    Column types mirror the Django model + DRF serializer output:
    - ``flag_reason`` is a JSONField, serialized as a dict → JSONB
    - ``deliver_unit``, ``completed_work`` are ForeignKeys, the default
      ModelSerializer renders them as the related PK (int) → BIGINT
    - ``deliver_unit_id``, ``completed_work_id`` are the raw FK columns,
      also int → BIGINT
    - ``form_json``/``images`` remain JSONB
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_visits CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_visits (
            visit_id BIGINT PRIMARY KEY,
            opportunity_id BIGINT,
            username TEXT,
            deliver_unit BIGINT,
            entity_id TEXT,
            entity_name TEXT,
            visit_date TIMESTAMPTZ,
            status TEXT,
            reason TEXT,
            location TEXT,
            flagged BOOLEAN,
            flag_reason JSONB,
            form_json JSONB,
            completed_work BIGINT,
            status_modified_date TIMESTAMPTZ,
            review_status TEXT,
            review_created_on TIMESTAMPTZ,
            justification TEXT,
            date_created TIMESTAMPTZ,
            completed_work_id BIGINT,
            deliver_unit_id BIGINT,
            images JSONB
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CONNECT_VISITS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("visit_id"),
                r.get("opportunity_id"),
                r.get("username", ""),
                r.get("deliver_unit"),
                r.get("entity_id", ""),
                r.get("entity_name", ""),
                r.get("visit_date"),
                r.get("status", ""),
                r.get("reason", ""),
                r.get("location", ""),
                r.get("flagged"),
                _json_or_none(r.get("flag_reason")),
                json.dumps(r.get("form_json") or {}),
                r.get("completed_work"),
                r.get("status_modified_date"),
                r.get("review_status", ""),
                r.get("review_created_on"),
                r.get("justification", ""),
                r.get("date_created"),
                r.get("completed_work_id"),
                r.get("deliver_unit_id"),
                json.dumps(r.get("images") or []),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_connect_users(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the users table and bulk-insert all pages. Returns total row count.

    ``payment_accrued`` is NUMERIC money, ``suspended`` is BOOLEAN, all
    date/datetime fields become TIMESTAMPTZ. ``claim_limits`` is a
    ``SerializerMethodField`` that returns a list of dicts — store as JSONB.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_users CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_users (
            username TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT,
            date_learn_started TIMESTAMPTZ,
            user_invite_status TEXT,
            payment_accrued NUMERIC(14, 2),
            suspended BOOLEAN,
            suspension_date TIMESTAMPTZ,
            suspension_reason TEXT,
            invited_date TIMESTAMPTZ,
            completed_learn_date TIMESTAMPTZ,
            last_active TIMESTAMPTZ,
            date_claimed TIMESTAMPTZ,
            claim_limits JSONB
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CONNECT_USERS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("username", ""),
                r.get("name", ""),
                r.get("phone", ""),
                r.get("date_learn_started"),
                r.get("user_invite_status", ""),
                r.get("payment_accrued"),
                r.get("suspended"),
                r.get("suspension_date"),
                r.get("suspension_reason", ""),
                r.get("invited_date"),
                r.get("completed_learn_date"),
                r.get("last_active"),
                r.get("date_claimed"),
                _json_or_none(r.get("claim_limits")),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_connect_completed_works(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the completed_works table and bulk-insert all pages. Returns total row count.

    Counts become INTEGER, accrued amounts become NUMERIC money, all
    date/datetime fields become TIMESTAMPTZ, opportunity_id becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_completed_works CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_completed_works (
            username TEXT,
            opportunity_id BIGINT,
            payment_unit_id BIGINT,
            status TEXT,
            last_modified TIMESTAMPTZ,
            entity_id TEXT,
            entity_name TEXT,
            reason TEXT,
            status_modified_date TIMESTAMPTZ,
            payment_date TIMESTAMPTZ,
            date_created TIMESTAMPTZ,
            saved_completed_count INTEGER,
            saved_approved_count INTEGER,
            saved_payment_accrued NUMERIC(14, 2),
            saved_payment_accrued_usd NUMERIC(14, 2),
            saved_org_payment_accrued NUMERIC(14, 2),
            saved_org_payment_accrued_usd NUMERIC(14, 2)
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CONNECT_COMPLETED_WORKS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("username", ""),
                r.get("opportunity_id"),
                r.get("payment_unit_id"),
                r.get("status", ""),
                r.get("last_modified"),
                r.get("entity_id", ""),
                r.get("entity_name", ""),
                r.get("reason", ""),
                r.get("status_modified_date"),
                r.get("payment_date"),
                r.get("date_created"),
                r.get("saved_completed_count"),
                r.get("saved_approved_count"),
                r.get("saved_payment_accrued"),
                r.get("saved_payment_accrued_usd"),
                r.get("saved_org_payment_accrued"),
                r.get("saved_org_payment_accrued_usd"),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_connect_payments(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the payments table and bulk-insert all pages. Returns total row count.

    ``amount``/``amount_usd`` become NUMERIC(14,2), ``confirmed`` becomes
    BOOLEAN, all date/datetime fields become TIMESTAMPTZ, ``opportunity_id``
    becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_payments CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_payments (
            username TEXT,
            opportunity_id BIGINT,
            created_at TIMESTAMPTZ,
            amount NUMERIC(14, 2),
            amount_usd NUMERIC(14, 2),
            date_paid TIMESTAMPTZ,
            payment_unit BIGINT,
            confirmed BOOLEAN,
            confirmation_date TIMESTAMPTZ,
            organization TEXT,
            invoice_id BIGINT,
            payment_method TEXT,
            payment_operator TEXT
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CONNECT_PAYMENTS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("username", ""),
                r.get("opportunity_id"),
                r.get("created_at"),
                r.get("amount"),
                r.get("amount_usd"),
                r.get("date_paid"),
                r.get("payment_unit"),
                r.get("confirmed"),
                r.get("confirmation_date"),
                r.get("organization", ""),
                r.get("invoice_id"),
                r.get("payment_method", ""),
                r.get("payment_operator", ""),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_connect_invoices(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the invoices table and bulk-insert all pages. Returns total row count.

    Money fields are NUMERIC(14,2), ``date`` is DATE, ``opportunity_id`` is
    BIGINT. ``service_delivery`` is a BooleanField (not a text label as the
    old TEXT column implied), and ``exchange_rate`` is actually a ForeignKey
    to the ExchangeRate lookup table (the PK, not the rate value) → BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_invoices CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_invoices (
            opportunity_id BIGINT,
            amount NUMERIC(14, 2),
            amount_usd NUMERIC(14, 2),
            date DATE,
            invoice_number TEXT,
            service_delivery BOOLEAN,
            exchange_rate BIGINT
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CONNECT_INVOICES_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("opportunity_id"),
                r.get("amount"),
                r.get("amount_usd"),
                r.get("date"),
                r.get("invoice_number", ""),
                r.get("service_delivery"),
                r.get("exchange_rate"),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_connect_assessments(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the assessments table and bulk-insert all pages. Returns total row count.

    Scores become INTEGER, ``passed`` becomes BOOLEAN, ``date`` becomes
    TIMESTAMPTZ, ``opportunity_id`` becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_assessments CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_assessments (
            username TEXT,
            app BIGINT,
            opportunity_id BIGINT,
            date TIMESTAMPTZ,
            score INTEGER,
            passing_score INTEGER,
            passed BOOLEAN
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CONNECT_ASSESSMENTS_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("username", ""),
                r.get("app"),
                r.get("opportunity_id"),
                r.get("date"),
                r.get("score"),
                r.get("passing_score"),
                r.get("passed"),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


def _write_connect_completed_modules(
    pages: Iterator[list[dict]], schema_name: str, conn: Any
) -> int:
    """Create the completed_modules table and bulk-insert all pages. Returns total row count.

    ``duration`` becomes INTEGER (seconds), ``date`` becomes TIMESTAMPTZ,
    ``opportunity_id`` becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_completed_modules CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_completed_modules (
            username TEXT,
            module BIGINT,
            opportunity_id BIGINT,
            date TIMESTAMPTZ,
            duration TEXT
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _CONNECT_COMPLETED_MODULES_INSERT.format(schema=sid)
    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                r.get("username", ""),
                r.get("module"),
                r.get("opportunity_id"),
                r.get("date"),
                r.get("duration", ""),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)

    return total


# ── Backwards-compatible shim ──────────────────────────────────────────────────


def run_commcare_sync(tenant_membership: Any, credential: dict[str, str]) -> dict:
    """Legacy entry point — delegates to run_pipeline with the default registry."""
    pipeline = get_registry().get("commcare_sync")
    if pipeline is None:
        raise ValueError("commcare_sync pipeline not found in registry")
    return run_pipeline(tenant_membership, credential, pipeline)
