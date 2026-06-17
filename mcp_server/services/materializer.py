"""Three-phase materialization orchestrator: Discover → Load → Transform.

Design notes:
- Each source is loaded inside its own psycopg connection. Non-resumable
  sources commit once at the end inside ``_load_and_commit_source``;
  resumable sources (issue #187) commit per page from inside the writer.
  A failure on source N+1 does NOT roll back rows source N already
  committed.
- A mid-pipeline failure marks the run PARTIAL (if at least one source
  committed) or FAILED (if none did) and short-circuits the remaining
  sources, recording them as ``skipped`` in ``result["sources"]``.
- Loaders expose ``load_pages()`` iterators yielding ``(page, total_count|None)``
  tuples; rows are written page-by-page so the full dataset is never held in
  memory. Inserts use ``executemany`` for efficiency.
- Transform failures are isolated — run is marked COMPLETED; error stored in result.
- ``progress_updater`` is also the cancellation checkpoint. Between pages it
  may raise ``MaterializationCancelled`` (e.g. when the worker observes
  ``MaterializationRun.state == CANCELLED``); the in-flight source's
  transaction rolls back. Sources committed before the cancel survive.
- Phase transitions (DISCOVERING→LOADING, LOADING→TRANSFORMING, TRANSFORMING→
  COMPLETED) are written via conditional UPDATEs that filter on the prior
  state, so a concurrent CANCELLED set by the cancel endpoint is preserved
  (an unconditional ``run.save`` would silently clobber it). The
  TRANSFORMING→COMPLETED CAS also preserves PARTIAL.
- ``result["sources"][name]`` records one of ``completed``, ``failed``,
  ``skipped``, ``cancelled``, or ``in_progress``. Never ``loaded`` — that
  string indicated rows written but un-committed, which was a lie when the
  transaction rolled back.
  ``in_progress`` is the per-page state for a resumable source whose load
  is mid-flight: its physical table may be partially populated, and the
  catalog (``pipeline_list_tables``) must hide that table until the source
  flips to ``completed``.
- For resumable sources, ``cursor_state = {"last_id": int,
  "last_committed_at": iso}`` records the watermark advanced after each
  committed page. A crash leaves a recoverable watermark; the next run
  reads the prior MaterializationRun and continues from ``last_id`` with
  no DROP TABLE. For non-resumable sources, ``cursor_state`` is ``None``.
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
from apps.workspaces.models import MaterializationRun, TenantMetadata, TenantSchema
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

ProgressUpdater = Callable[[dict], None]
OnPage = Callable[[int, int | None], None]
# Called by resumable writers after each per-page commit with
# ``(last_id, rows_committed_so_far)``. The materializer persists last_id
# to ``MaterializationRun.result["sources"][name].cursor_state`` so a
# crash leaves a recoverable watermark for the next run, and rolls
# ``rows_committed_so_far`` into the in-progress source entry so the
# resume-prompt and catalog observers see partial progress.
CursorCallback = Callable[[int, int], None]


class MaterializationCancelled(Exception):
    """Raised by ``progress_updater`` when a cancellation is detected.

    Propagates out through the LOAD phase so the open psycopg transaction
    rolls back (no partial data is written).
    """


def run_pipeline(
    tenant_membership: Any,
    credential: dict[str, str],
    pipeline: PipelineConfig,
    progress_updater: ProgressUpdater | None = None,
    procrastinate_job_id: int | None = None,
    target_schema: TenantSchema | None = None,
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
        progress_updater: Optional callable receiving a progress dict
            ``{step, total_steps, source, message, rows_loaded, rows_total}``.
            The updater also acts as the cancellation checkpoint; it may raise
            ``MaterializationCancelled`` to abort the LOAD phase between pages,
            triggering a transaction rollback.
        procrastinate_job_id: When set, written to ``MaterializationRun`` so
            the cancel endpoint can target the underlying procrastinate job.
        target_schema: When set, the explicit ``TenantSchema`` to load into
            (used by the blue-green refresh path to load into its new "_r"
            schema). When ``None``, the tenant's base schema is resolved via
            ``SchemaManager().provision()`` (the initial-materialization path).

    Returns a summary dict with run_id, status, and per-source row counts.
    """
    # total steps: provision + discover + N sources + transform/skip
    total_steps = 2 + len(pipeline.sources) + 1
    step = 0
    current_source: str | None = None
    # Holds the run id once the MaterializationRun is created so the
    # progress dict carries it. Captured by closures below.
    run_id_holder: dict[str, Any] = {"id": None}

    def report(message: str) -> None:
        nonlocal step
        step += 1
        if progress_updater is not None:
            progress_updater(
                {
                    "run_id": run_id_holder["id"],
                    "step": step,
                    "total_steps": total_steps,
                    "source": current_source,
                    "message": message,
                    "rows_loaded": 0,
                    "rows_total": None,
                }
            )

    def make_on_page(
        source_name: str,
        message: str,
        known_total: int | None = None,
        unit: str = "rows",
    ) -> OnPage:
        def _on_page(rows_loaded: int, rows_total: int | None) -> None:
            if progress_updater is None:
                return
            # Prefer a total the loader itself reports (page envelope ``count``);
            # otherwise fall back to a count discovered up front (e.g. the
            # opportunity's ``visit_count``) so the bar can show a real percent.
            effective_total = rows_total if rows_total is not None else known_total
            progress_updater(
                {
                    "run_id": run_id_holder["id"],
                    "step": step,
                    "total_steps": total_steps,
                    "source": source_name,
                    "message": message,
                    "rows_loaded": rows_loaded,
                    "rows_total": effective_total,
                    # Unit of rows_loaded/rows_total for display. OCS messages
                    # report progress in "sessions" — the API exposes no
                    # message count, but the per-session detail fetches are
                    # the work, so sessions are an honest denominator.
                    "unit": unit,
                }
            )

        return _on_page

    # ── 1. PROVISION ──────────────────────────────────────────────────────────
    # Resolve the target schema and create the run row up-front so
    # progress_updater always has a valid run_id to target.
    #
    # A blue-green refresh passes an explicit ``target_schema``: the freshly
    # minted, not-yet-active "_r" schema it wants this run to load into. We must
    # honor it. Falling back to ``provision()`` here would re-resolve the
    # tenant's OLD active *base* schema and load there; the refresh task would
    # then activate the empty new schema and tear down the data-bearing old one,
    # destroying the data the refresh just loaded. When no target is given
    # (initial materialization), ``provision()`` get-or-creates the base schema.
    if target_schema is not None:
        tenant_schema = target_schema
    else:
        tenant_schema = SchemaManager().provision(tenant_membership.tenant)
    schema_name = tenant_schema.schema_name

    run = MaterializationRun.objects.create(
        tenant_schema=tenant_schema,
        pipeline=pipeline.name,
        state=MaterializationRun.RunState.DISCOVERING,
        procrastinate_job_id=procrastinate_job_id,
    )
    # Stringify the UUID so the dict is JSON-serializable (it is written to
    # ``MaterializationRun.progress``, a JSONField).
    run_id_holder["id"] = str(run.id)

    report(f"Provisioning schema for {tenant_membership.tenant.external_id}...")

    source_results: dict[str, dict] = {}

    try:
        # ── 2. DISCOVER ───────────────────────────────────────────────────────
        report(f"Discovering tenant metadata from {pipeline.provider}...")
        discovered_metadata = _run_discover_phase(tenant_membership, credential, pipeline)

        # The visits export is keyset-paginated and returns no total, but the
        # discovery payload already carries the opportunity's visit_count — use
        # it as the visits progress denominator.
        visit_total: int | None = None
        if pipeline.provider == "commcare_connect":
            visit_total = _connect_visit_total(
                discovered_metadata, int(tenant_membership.tenant.external_id)
            )

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
        # Compare-and-swap: an unconditional save() would silently overwrite a
        # CANCELLED state set by the cancel endpoint while DISCOVER (which has
        # no progress checkpoint) was running, and the run would continue.
        rows = MaterializationRun.objects.filter(
            id=run.id, state=MaterializationRun.RunState.DISCOVERING
        ).update(state=MaterializationRun.RunState.LOADING)
        if not rows:
            raise MaterializationCancelled()
        run.state = MaterializationRun.RunState.LOADING

        # Read the most recent non-terminal-success prior run on this tenant
        # schema so we can resume each resumable source from its last
        # committed cursor (issue #187). PARTIAL or FAILED runs are the only
        # ones that can carry usable ``cursor_state``; COMPLETED runs are
        # whole-history checkpoints (out of scope for this issue).
        prior_cursors = _load_prior_resume_cursors(tenant_schema, exclude_run_id=run.id)

        # The resumable code path (start_cursor + per-page cursor_callback) is
        # currently implemented only for Connect writers. Other providers
        # ignore ``source.resumable`` until their writers are migrated.
        is_resumable_provider = pipeline.provider == "commcare_connect"

        sources_list = list(pipeline.sources)
        for idx, source in enumerate(sources_list):
            current_source = source.name
            load_message = f"Loading {source.name} from {pipeline.provider} API..."
            report(load_message)
            source_is_resumable = is_resumable_provider and source.resumable
            start_cursor = prior_cursors.get(source.name) if source_is_resumable else None
            if source_is_resumable:
                # Surface ``in_progress`` immediately so a fresh crash mid-resume
                # leaves an observable state. The catalog (pipeline_list_tables)
                # treats this as "do not surface this table yet".
                source_results[source.name] = {
                    "state": "in_progress",
                    "rows": 0,
                    "cursor_state": (
                        {
                            "last_id": start_cursor,
                            "last_committed_at": None,
                        }
                        if start_cursor is not None
                        else None
                    ),
                }
                _persist_source_results(run, pipeline, source_results)
            cursor_callback = (
                _make_cursor_callback(run, pipeline, source_results, source.name)
                if source_is_resumable
                else None
            )
            # The discovered visit_count counts *all* visits, so it's only a
            # valid denominator on a fresh load. On resume, rows_loaded counts
            # just this run's new rows, so leave the bar indeterminate.
            source_total = visit_total if source.name == "visits" and start_cursor is None else None
            try:
                rows = _load_and_commit_source(
                    source.name,
                    tenant_membership,
                    credential,
                    schema_name,
                    provider=pipeline.provider,
                    on_page=make_on_page(
                        source.name,
                        load_message,
                        known_total=source_total,
                        unit=source.progress_unit,
                    ),
                    resumable=source_is_resumable,
                    start_cursor=start_cursor,
                    cursor_callback=cursor_callback,
                )
            except MaterializationCancelled:
                # Earlier sources stay committed; this in-flight source rolled
                # back inside _load_and_commit_source. Record per-source state
                # before bubbling the cancellation up.
                prior_cursor = (source_results.get(source.name) or {}).get("cursor_state")
                source_results[source.name] = {
                    "state": "cancelled",
                    "rows": 0,
                    "cursor_state": prior_cursor,
                }
                for remaining in sources_list[idx + 1 :]:
                    source_results[remaining.name] = {
                        "state": "skipped",
                        "rows": 0,
                        "cursor_state": None,
                    }
                raise
            except Exception as e:
                logger.exception(
                    "Source %s failed for schema %s; earlier sources stay committed",
                    source.name,
                    schema_name,
                )
                # Preserve any cursor_state advanced by per-page commits so the
                # next run can resume from the last durable watermark (#187).
                prior_cursor = (source_results.get(source.name) or {}).get("cursor_state")
                source_results[source.name] = {
                    "state": "failed",
                    "rows": (source_results.get(source.name) or {}).get("rows", 0),
                    "error": _summarize_error(e),
                    "attempts": getattr(e, "attempts", 1),
                    "failed_at": datetime.now(UTC).isoformat(),
                    "cursor_state": prior_cursor,
                }
                for remaining in sources_list[idx + 1 :]:
                    source_results[remaining.name] = {
                        "state": "skipped",
                        "rows": 0,
                        "cursor_state": None,
                    }
                # A resumable source whose per-page commits advanced the cursor
                # has committed rows even though the source as a whole failed;
                # treat that as PARTIAL too so the next run resumes from the
                # watermark.
                any_committed = any(
                    s.get("state") == "completed" or _has_committed_cursor(s)
                    for s in source_results.values()
                )
                final_state = (
                    MaterializationRun.RunState.PARTIAL
                    if any_committed
                    else MaterializationRun.RunState.FAILED
                )
                run.state = final_state
                run.completed_at = datetime.now(UTC)
                run.result = {
                    "pipeline": pipeline.name,
                    "sources": source_results,
                }
                run.save(update_fields=["state", "completed_at", "result"])
                raise
            # Preserve the final cursor watermark (resumable sources only) so a
            # future incremental-update feature can read it; non-resumable keep
            # ``None``.
            final_cursor = (source_results.get(source.name) or {}).get("cursor_state")
            source_results[source.name] = {
                "state": "completed",
                "rows": rows,
                "committed_at": datetime.now(UTC).isoformat(),
                "cursor_state": final_cursor if source_is_resumable else None,
            }
            if source_is_resumable:
                _persist_source_results(run, pipeline, source_results)
            logger.info("Loaded %d rows into %s.%s", rows, schema_name, source.name)
        current_source = None

    except MaterializationCancelled:
        # Run state is already CANCELLED (set by the canceller before raising
        # via progress_updater). Stamp completed_at and exit cleanly so the
        # caller can distinguish cancellation from failure. Sources that
        # committed before the cancel survive in source_results as "completed".
        now = datetime.now(UTC)
        MaterializationRun.objects.filter(id=run.id).update(
            completed_at=now,
            result={
                "cancelled": True,
                "pipeline": pipeline.name,
                "sources": source_results,
            },
        )
        logger.info("Run %s cancelled; in-flight source rolled back", run.id)
        raise

    except Exception as e:
        # Pre-loop failures (DISCOVER phase, system-asset generation re-raise,
        # or the DISCOVERING→LOADING CAS) would otherwise escape with the run
        # row stuck in DISCOVERING — no terminal state, no completed_at — which
        # downstream aggregation treats as "partial" and expire_inactive_schemas
        # never cleans up. Stamp a FAILED terminal state here so every
        # non-cancellation failure path produces a terminal run.
        #
        # Per-source load failures are handled inside the loop above (which
        # records FAILED/PARTIAL and re-raises). This handler is idempotent
        # w.r.t. that path: if completed_at was already stamped there, we leave
        # the recorded state untouched and just re-raise.
        if run.completed_at is None:
            logger.exception("Materialization run %s failed before any source committed", run.id)
            now = datetime.now(UTC)
            MaterializationRun.objects.filter(id=run.id, completed_at__isnull=True).update(
                state=MaterializationRun.RunState.FAILED,
                completed_at=now,
                result={
                    "pipeline": pipeline.name,
                    "sources": source_results,
                    "error": _summarize_error(e),
                },
            )
        raise

    # ── 4. TRANSFORM ──────────────────────────────────────────────────────────
    # Transform errors are isolated — failure here does NOT mark the run FAILED.
    # Compare-and-swap, same reasoning as the LOADING transition: a cancel that
    # lands between LOAD commit and here must not be overwritten. On miss,
    # stamp result so the row matches the shape of a LOAD-phase cancel.
    rows = MaterializationRun.objects.filter(
        id=run.id, state=MaterializationRun.RunState.LOADING
    ).update(state=MaterializationRun.RunState.TRANSFORMING)
    if not rows:
        MaterializationRun.objects.filter(id=run.id).update(
            result={"cancelled": True, "sources": source_results},
        )
        logger.info("Run %s cancelled before transform; partial data committed", run.id)
        raise MaterializationCancelled()
    run.state = MaterializationRun.RunState.TRANSFORMING
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
    # state. Preserves a CANCELLED (or FAILED) state written externally during
    # the transform phase.
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
) -> dict | None:
    """Fetch provider metadata, upsert into TenantMetadata, and return it.

    The returned dict lets the LOAD phase read counts that the discovery
    payload already carries (e.g. the opportunity's ``visit_count``) without a
    second API call. Returns ``None`` when the pipeline has no discovery step.
    """
    if not pipeline.has_metadata_discovery:
        return None

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
    return metadata


def _connect_visit_total(metadata: dict | None, opportunity_id: int) -> int | None:
    """Pull the opportunity's all-visits count from discovered Connect metadata.

    ``/export/opp_org_program_list/`` (fetched during DISCOVER) annotates each
    opportunity with ``visit_count``. We use it as the denominator for the
    visits progress bar, since the keyset-paginated visits export returns no
    total of its own. Returns ``None`` when the count is absent or non-positive.
    """
    if not metadata:
        return None
    for opp in metadata.get("all_opportunities", []) or []:
        if isinstance(opp, dict) and opp.get("id") == opportunity_id:
            count = opp.get("visit_count")
            return count if isinstance(count, int) and count > 0 else None
    return None


def _load_prior_resume_cursors(tenant_schema: Any, exclude_run_id: Any) -> dict[str, int]:
    """Return ``{source_name: last_id}`` for the most recent prior run, if any.

    Only PARTIAL or FAILED runs carry a usable per-source ``cursor_state``
    (a COMPLETED run is a whole-history checkpoint, not a resume target).
    A source's cursor is returned only when its prior ``state`` was
    ``in_progress`` or ``failed`` AND ``cursor_state.last_id`` is set,
    matching the resume-eligibility rules in issue #187.

    Crucially, we look at the **most recent** prior run regardless of its
    run-level state, and only use cursors when *that* run is PARTIAL or FAILED.
    This prevents a stale PARTIAL run from being surfaced after an intervening
    COMPLETED run, which would re-insert rows already present in the table and
    cause duplicate-key errors (for tables with a PK) or silent row duplication
    (for tables without one).

    Returns an empty dict if no eligible prior run exists or none of its
    sources have a usable cursor.
    """
    prior = (
        MaterializationRun.objects.filter(tenant_schema=tenant_schema)
        .exclude(id=exclude_run_id)
        .order_by("-started_at")
        .first()
    )
    if prior is None or prior.state not in (
        MaterializationRun.RunState.PARTIAL,
        MaterializationRun.RunState.FAILED,
    ):
        return {}
    if not isinstance(prior.result, dict):
        return {}
    cursors: dict[str, int] = {}
    for name, info in (prior.result.get("sources") or {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("state") not in ("in_progress", "failed"):
            continue
        last_id = (info.get("cursor_state") or {}).get("last_id")
        if isinstance(last_id, int):
            cursors[name] = last_id
    return cursors


def _persist_source_results(run: Any, pipeline: PipelineConfig, source_results: dict) -> None:
    """Write the current ``source_results`` snapshot to ``run.result``.

    The CAS-scoped update preserves a concurrent CANCELLED (set externally
    by the cancel endpoint) from being clobbered by per-page checkpoints.
    Failures here are logged but never raise — a checkpoint write that
    fails must not break the in-flight load.
    """
    try:
        MaterializationRun.objects.filter(
            id=run.id,
            state__in=MaterializationRun.ACTIVE_STATES,
        ).update(
            result={
                "pipeline": pipeline.name,
                "sources": source_results,
            }
        )
    except Exception:
        logger.warning(
            "Failed to persist source_results checkpoint for run %s", run.id, exc_info=True
        )


def _make_cursor_callback(
    run: Any, pipeline: PipelineConfig, source_results: dict, source_name: str
) -> CursorCallback:
    """Return a callback the writer invokes after each per-page commit.

    The callback advances ``source_results[source_name].cursor_state.last_id``
    and persists the snapshot to ``MaterializationRun.result``. A crash
    after this point leaves a recoverable watermark on disk.
    """

    def _on_page_committed(last_id: int, rows_committed: int) -> None:
        entry = source_results.setdefault(
            source_name, {"state": "in_progress", "rows": 0, "cursor_state": None}
        )
        entry["state"] = "in_progress"
        entry["rows"] = rows_committed
        entry["cursor_state"] = {
            "last_id": last_id,
            "last_committed_at": datetime.now(UTC).isoformat(),
        }
        _persist_source_results(run, pipeline, source_results)

    return _on_page_committed


def _has_committed_cursor(entry: dict) -> bool:
    """True when this source has any durably-committed pages (cursor_state set)."""
    cs = entry.get("cursor_state") if isinstance(entry, dict) else None
    return isinstance(cs, dict) and isinstance(cs.get("last_id"), int)


def _summarize_error(exc: BaseException) -> str:
    """Return a short, single-line error description for ``result["sources"][n].error``.

    The string is surfaced to the agent in the resume prompt and (eventually)
    to the end user, so it must be safe to display: no stack trace, no
    sensitive headers, just the exception type and message.
    """
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    if len(msg) > 200:
        msg = msg[:197] + "..."
    return f"{exc.__class__.__name__}: {msg}"


def _load_and_commit_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    provider: str,
    on_page: OnPage | None = None,
    resumable: bool = False,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    """Open a fresh psycopg connection, run the writer, commit, and close.

    Non-resumable path (``resumable=False``): the writer runs inside one
    transaction; this function calls ``conn.commit()`` once at the end.

    Resumable path (``resumable=True``): the writer commits per page
    internally (using the shared ``conn``) and calls ``cursor_callback``
    after each commit. This function does NOT call an outer commit — the
    writer's per-page commits are the source of durability. On a mid-page
    crash, pages already committed are durable; the next run resumes from
    ``cursor_state.last_id`` recorded by the callback.
    """
    conn = get_managed_db_connection()
    conn.autocommit = False
    try:
        rows = _load_source(
            source_name,
            tenant_membership,
            credential,
            schema_name,
            conn,
            provider=provider,
            on_page=on_page,
            start_cursor=start_cursor,
            cursor_callback=cursor_callback,
        )
        if not resumable:
            conn.commit()
        return rows
    except Exception:
        try:
            conn.rollback()
        except Exception:
            logger.exception(
                "Rollback failed for source %s; connection may be in a broken state",
                source_name,
            )
        raise
    finally:
        try:
            conn.close()
        except Exception:
            logger.warning("Failed to close connection for source %s", source_name, exc_info=True)


def _load_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
    provider: str = "commcare",
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    if provider == "commcare_connect":
        return _load_connect_source(
            source_name,
            tenant_membership,
            credential,
            schema_name,
            conn,
            on_page,
            start_cursor=start_cursor,
            cursor_callback=cursor_callback,
        )
    if provider == "ocs":
        return _load_ocs_source(
            source_name, tenant_membership, credential, schema_name, conn, on_page
        )
    # Existing CommCare dispatch
    domain = tenant_membership.tenant.external_id
    if source_name == "cases":
        loader = CommCareCaseLoader(domain=domain, credential=credential)
        return _write_cases(loader.load_pages(), schema_name, conn, on_page=on_page)
    if source_name == "forms":
        loader = CommCareFormLoader(domain=domain, credential=credential)
        return _write_forms(loader.load_pages(), schema_name, conn, on_page=on_page)
    raise ValueError(f"Unknown source '{source_name}'. Known sources: cases, forms")


# Connect sources that the materializer should drive in resumable mode.
# ``users`` is intentionally excluded — its rows are mutable, so a partial
# resume could miss in-place updates behind the cursor (issue #187).
_RESUMABLE_CONNECT_SOURCES = frozenset(
    {"visits", "completed_works", "payments", "invoices", "assessments", "completed_modules"}
)


def _load_connect_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
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
    if source_name in _RESUMABLE_CONNECT_SOURCES:
        pages = loader.load_pages(start_last_id=start_cursor)
        return writer_fn(
            pages,
            schema_name,
            conn,
            on_page=on_page,
            start_cursor=start_cursor,
            cursor_callback=cursor_callback,
        )
    # users: full DROP/CREATE/INSERT, single commit at end (caller-driven).
    return writer_fn(loader.load_pages(), schema_name, conn, on_page=on_page)


def _load_ocs_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
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
    return writer_fn(loader.load_pages(), schema_name, conn, on_page=on_page)


def _write_ocs_experiments(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
) -> int:
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
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_ocs_sessions(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
) -> int:
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
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_ocs_messages(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
) -> int:
    """Create the messages table and bulk-insert all pages.

    The loader yields one ``(rows, total_sessions)`` tuple per session (rows
    may be empty), so progress here is denominated in sessions — the unit the
    N+1 detail fetches actually advance in — not message rows. The return
    value is still the number of message rows written.
    """
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
    sessions_total: int | None = None
    for sessions_done, (page, page_total) in enumerate(pages, start=1):
        if sessions_total is None and page_total is not None:
            sessions_total = page_total
        if page:
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
        if on_page is not None:
            on_page(sessions_done, sessions_total)

    return total


def _write_ocs_participants(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
) -> int:
    """Create the participants table and bulk-insert all pages."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_participants CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.raw_participants (
            participant_id TEXT PRIMARY KEY,
            identifier TEXT,
            name TEXT,
            platform TEXT,
            remote_id TEXT,
            data JSONB
        )
        """
        ).format(schema=sid)
    )

    ins_sql = _OCS_PARTICIPANTS_INSERT.format(schema=sid)
    total = 0
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
        rows = [
            (
                r.get("participant_id", ""),
                r.get("identifier", ""),
                r.get("name", ""),
                r.get("platform", ""),
                r.get("remote_id", ""),
                json.dumps(r.get("data") or []),
            )
            for r in page
        ]
        cur.executemany(ins_sql, rows)
        total += len(page)
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _run_transform_phase(pipeline: PipelineConfig, schema_name: str, tenant=None) -> dict:
    """Run the transformation pipeline's SYSTEM + TENANT stages for this tenant.

    No ``workspace`` is passed because materialization is tenant-scoped: a tenant
    may belong to several workspaces, so there is no single workspace context
    here. The WORKSPACE stage therefore does not run during materialization — it
    is driven separately by the transformations trigger endpoint, which passes an
    explicit ``workspace`` (issue #241, 04#5: the WORKSPACE never-run gap is a
    property of tenant-scoped materialization, not a bug to fix here; running an
    arbitrary workspace's assets against a single tenant's schema would be
    ill-defined). The dbt run itself is now confined to a low-privilege,
    schema-scoped role and a failure surfaces via ``status``/``error`` below.
    """
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


def _write_cases(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
) -> int:
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
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_forms(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
) -> int:
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
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        if on_page is not None:
            on_page(total, rows_total)

    return total


# ── Connect table writers ──────────────────────────────────────────────────────


def _max_id(page: list[dict], field: str) -> int | None:
    """Return the maximum integer value of ``field`` over ``page`` rows.

    Used by resumable Connect writers to advance the per-page cursor
    watermark. Rows with missing or non-integer ids are skipped; if no
    row has a usable id the function returns ``None`` and the caller
    will not record a checkpoint for this page.
    """
    ids = [r.get(field) for r in page]
    valid = [i for i in ids if isinstance(i, int)]
    return max(valid) if valid else None


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
        (participant_id, identifier, name, platform, remote_id, data)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (participant_id) DO UPDATE SET
        identifier=EXCLUDED.identifier, name=EXCLUDED.name,
        platform=EXCLUDED.platform, remote_id=EXCLUDED.remote_id,
        data=EXCLUDED.data
    """
)


def _write_connect_visits(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    """Create the visits table and bulk-insert all pages. Returns total row count.

    When ``start_cursor`` is None this is a clean run (DROP + CREATE +
    INSERT). When ``start_cursor`` is set this is a resume: skip the DROP,
    run an idempotent ``CREATE TABLE IF NOT EXISTS``, and continue
    inserting at the prior watermark. The page-iterator is expected to
    already be positioned past ``start_cursor`` (the caller passes
    ``start_last_id`` to the loader).

    Resumable writers commit per page (rather than once per source) so a
    mid-source crash leaves a partial table the next run can continue.

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

    if start_cursor is None:
        cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_visits CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE IF NOT EXISTS {schema}.raw_visits (
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
    conn.commit()

    ins_sql = _CONNECT_VISITS_INSERT.format(schema=sid)
    total = 0
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        max_id = _max_id(page, "visit_id")
        conn.commit()
        if cursor_callback is not None and max_id is not None:
            cursor_callback(max_id, total)
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_connect_users(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
) -> int:
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
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_connect_completed_works(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    """Create the completed_works table and bulk-insert all pages. Returns total row count.

    Resumable: when ``start_cursor`` is set, skip DROP and use
    ``CREATE TABLE IF NOT EXISTS``. Commits per page; calls
    ``cursor_callback`` with the page's max ``id``.

    Counts become INTEGER, accrued amounts become NUMERIC money, all
    date/datetime fields become TIMESTAMPTZ, opportunity_id becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    if start_cursor is None:
        cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_completed_works CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE IF NOT EXISTS {schema}.raw_completed_works (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    conn.commit()

    ins_sql = _CONNECT_COMPLETED_WORKS_INSERT.format(schema=sid)
    total = 0
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        max_id = _max_id(page, "id")
        conn.commit()
        if cursor_callback is not None and max_id is not None:
            cursor_callback(max_id, total)
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_connect_payments(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    """Create the payments table and bulk-insert all pages. Returns total row count.

    Resumable: when ``start_cursor`` is set, skip DROP and use
    ``CREATE TABLE IF NOT EXISTS``. Commits per page; calls
    ``cursor_callback`` with the page's max ``id``.

    ``amount``/``amount_usd`` become NUMERIC(14,2), ``confirmed`` becomes
    BOOLEAN, all date/datetime fields become TIMESTAMPTZ, ``opportunity_id``
    becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    if start_cursor is None:
        cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_payments CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE IF NOT EXISTS {schema}.raw_payments (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    conn.commit()

    ins_sql = _CONNECT_PAYMENTS_INSERT.format(schema=sid)
    total = 0
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        max_id = _max_id(page, "id")
        conn.commit()
        if cursor_callback is not None and max_id is not None:
            cursor_callback(max_id, total)
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_connect_invoices(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    """Create the invoices table and bulk-insert all pages. Returns total row count.

    Resumable: when ``start_cursor`` is set, skip DROP and use
    ``CREATE TABLE IF NOT EXISTS``. Commits per page; calls
    ``cursor_callback`` with the page's max ``id``.

    Money fields are NUMERIC(14,2), ``date`` is DATE, ``opportunity_id`` is
    BIGINT. ``service_delivery`` is a BooleanField (not a text label as the
    old TEXT column implied), and ``exchange_rate`` is actually a ForeignKey
    to the ExchangeRate lookup table (the PK, not the rate value) → BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    if start_cursor is None:
        cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_invoices CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE IF NOT EXISTS {schema}.raw_invoices (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    conn.commit()

    ins_sql = _CONNECT_INVOICES_INSERT.format(schema=sid)
    total = 0
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        max_id = _max_id(page, "id")
        conn.commit()
        if cursor_callback is not None and max_id is not None:
            cursor_callback(max_id, total)
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_connect_assessments(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    """Create the assessments table and bulk-insert all pages. Returns total row count.

    Resumable: when ``start_cursor`` is set, skip DROP and use
    ``CREATE TABLE IF NOT EXISTS``. Commits per page; calls
    ``cursor_callback`` with the page's max ``id``.

    Scores become INTEGER, ``passed`` becomes BOOLEAN, ``date`` becomes
    TIMESTAMPTZ, ``opportunity_id`` becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    if start_cursor is None:
        cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_assessments CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE IF NOT EXISTS {schema}.raw_assessments (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    conn.commit()

    ins_sql = _CONNECT_ASSESSMENTS_INSERT.format(schema=sid)
    total = 0
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        max_id = _max_id(page, "id")
        conn.commit()
        if cursor_callback is not None and max_id is not None:
            cursor_callback(max_id, total)
        if on_page is not None:
            on_page(total, rows_total)

    return total


def _write_connect_completed_modules(
    pages: Iterator[tuple[list[dict], int | None]],
    schema_name: str,
    conn: Any,
    on_page: OnPage | None = None,
    start_cursor: int | None = None,
    cursor_callback: CursorCallback | None = None,
) -> int:
    """Create the completed_modules table and bulk-insert all pages. Returns total row count.

    Resumable: when ``start_cursor`` is set, skip DROP and use
    ``CREATE TABLE IF NOT EXISTS``. Commits per page; calls
    ``cursor_callback`` with the page's max ``id``.

    ``duration`` becomes INTEGER (seconds), ``date`` becomes TIMESTAMPTZ,
    ``opportunity_id`` becomes BIGINT.
    """
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    if start_cursor is None:
        cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.raw_completed_modules CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE IF NOT EXISTS {schema}.raw_completed_modules (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            username TEXT,
            module BIGINT,
            opportunity_id BIGINT,
            date TIMESTAMPTZ,
            duration TEXT
        )
        """
        ).format(schema=sid)
    )
    conn.commit()

    ins_sql = _CONNECT_COMPLETED_MODULES_INSERT.format(schema=sid)
    total = 0
    rows_total: int | None = None
    for page, page_total in pages:
        if not page:
            continue
        if rows_total is None and page_total is not None:
            rows_total = page_total
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
        max_id = _max_id(page, "id")
        conn.commit()
        if cursor_callback is not None and max_id is not None:
            cursor_callback(max_id, total)
        if on_page is not None:
            on_page(total, rows_total)

    return total


# ── Backwards-compatible shim ──────────────────────────────────────────────────


def run_commcare_sync(tenant_membership: Any, credential: dict[str, str]) -> dict:
    """Legacy entry point — delegates to run_pipeline with the default registry."""
    pipeline = get_registry().get("commcare_sync")
    if pipeline is None:
        raise ValueError("commcare_sync pipeline not found in registry")
    return run_pipeline(tenant_membership, credential, pipeline)
