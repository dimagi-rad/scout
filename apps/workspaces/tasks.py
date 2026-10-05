"""Background tasks for schema lifecycle management."""

import asyncio
import logging
from collections.abc import Iterable
from functools import wraps

import sentry_sdk
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.utils import timezone

from apps.chat import resume_stream
from apps.chat.models import ThreadJob
from apps.chat.services import continuation
from apps.chat.services.continuation import defer_pending_flush as _defer_pending_flush
from apps.common.capacity import classify_capacity_error
from apps.common.error_codes import ErrorCode, code_of
from apps.semantic.services.cube_schema import (
    CubeSchemaBuildError,
    build_and_promote_cube_schema,
    record_cube_schema_build_failure,
)
from apps.users.models import TenantMembership, User
from apps.users.services.credential_resolver import (
    CredentialResolutionError,
    aresolve_credential,
)
from apps.workspaces.access import (
    NO_SOURCES,
    NO_SOURCES_MESSAGE,
    TENANT_ACCESS_LOST,
    WorkspaceAccess,
    access_denied_body,
    aresolve_workspace_access_ex,
)
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceDataRecovery,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services import publication, retirement
from apps.workspaces.services.access_freshness import (
    FRESHNESS_ERROR_CODES,
    VerificationBudget,
)
from apps.workspaces.services.credential_coverage import CoverageRecovery, MissingTenant
from apps.workspaces.services.data_operation import (
    DataLockTimeout,
    LockOrderError,
    run_data_thread,
    tenant_data_lock,
    workspace_data_lock,
)
from apps.workspaces.services.data_operation import (
    to_thread_fresh_db as _to_thread_fresh_db,
)
from apps.workspaces.services.data_recovery import (
    CAPACITY_REFUSED_KEY,
    CHAT_RECOVERY_SOURCE,
    recovery_query_surface,
)
from apps.workspaces.services.data_recovery import (
    ROLE_DENIED_MESSAGE as _ROLE_DENIED_MESSAGE,
)
from apps.workspaces.services.data_recovery import (
    workspace_recovery_error as _workspace_recovery_error,
)
from apps.workspaces.services.failure_guidance import credential_guidance
from apps.workspaces.services.failure_guidance import summary_failures as _summary_failures
from apps.workspaces.services.load_activity import (
    active_runs_for_workspaces,
)
from apps.workspaces.services.load_candidates import (
    Promotion,
    fail_workspace_candidate,
    load_owner_token,
    open_workspace_candidate,
    settle_orphaned_workspace_candidates,
)
from apps.workspaces.services.load_generations import (
    INTENT_FULL_REFRESH,
    INTENT_RECONCILE_MISSING,
    begin_load_generation,
    capture_load_intent,
    end_load_generation,
    parse_load_intent,
    pipeline_fingerprint,
    raw_load_fingerprint,
    reusable_generation,
)
from apps.workspaces.services.load_outcome import TENANT_NOT_RUN
from apps.workspaces.services.load_outcome import (
    set_tenant_display_names as _set_tenant_display_names,
)
from apps.workspaces.services.load_outcome import (
    unreachable_tenant_error as _unreachable_tenant_error,
)
from apps.workspaces.services.pipeline_resolver import no_pipeline_message
from apps.workspaces.services.query_state import (
    included_tenant_snapshot_state as _included_tenant_snapshot_state,
)
from apps.workspaces.services.reconciliation import (
    sweep_stale_materialization_runs,
    sweep_stale_thread_jobs,
    sweep_stale_workspace_data_recoveries,
)
from apps.workspaces.services.refresh_requests import (
    DENIED_MEMBERSHIP_MISSING,
    DENIED_ROLE_REQUIRED,
    DENIED_WORKSPACE_UNLINKED,
    claim_refresh_candidate,
)
from apps.workspaces.services.schema_manager import (
    NoActiveTenantSchema,
    SchemaManager,
    ViewSchemaRetired,
    aview_schema_buildable,
)
from apps.workspaces.services.source_freshness import REQUESTER_CODES, arecord_load_outcomes
from apps.workspaces.services.tenant_coverage import parse_coverage
from apps.workspaces.task_dispatch import JOB_RETENTION_HOURS, register_inline_materializer
from config.procrastinate import app
from mcp_server.loaders.connect_base import ConnectExportError
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import (
    MaterializationCancelled,
    run_pipeline,
)

logger = logging.getLogger(__name__)


@app.task(pass_context=True)
async def refresh_tenant_schema(
    context,
    schema_id: str,
    membership_id: str,
    actor_user_id: str = "",
    workspace_id: str = "",
) -> dict:
    """See ``_refresh_tenant_schema``; a held request waiting on it is flushed after."""
    try:
        return await _refresh_tenant_schema(
            context,
            schema_id,
            membership_id,
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
        )
    finally:
        await _defer_pending_flush(workspace_id)


async def _refresh_tenant_schema(
    context,
    schema_id: str,
    membership_id: str,
    actor_user_id: str = "",
    workspace_id: str = "",
) -> dict:
    """Provision a new schema and run the materialization pipeline.

    On success: marks state=ACTIVE, schedules teardown of old active schemas.
    On failure: drops the new schema, marks state=FAILED.
    """
    if not actor_user_id or not workspace_id:
        return {
            "status": "rejected",
            "error_code": ErrorCode.REFRESH_REQUEST_MISMATCH,
            "error": (
                "This queued refresh is missing acting-user/workspace authorization context. "
                "Retry the refresh from the workspace."
            ),
            "retry_required": True,
        }

    claim = await _to_thread_fresh_db(
        claim_refresh_candidate,
        schema_id=schema_id,
        membership_id=membership_id,
        actor_user_id=actor_user_id,
        workspace_id=workspace_id,
        job_id=context.job.id,
    )
    if claim.status == "ignored":
        return {"status": "ignored"}
    if claim.status == "rejected":
        return {
            "status": "rejected",
            "error_code": ErrorCode.REFRESH_REQUEST_MISMATCH,
            "error": (
                "This queued job does not match the refresh request recorded for this "
                "workspace, so nothing was run. Retry the refresh from the workspace."
            ),
            "retry_required": True,
        }
    if claim.status != "claimed":
        return _refresh_denial_result(claim.reason)

    new_schema = claim.schema
    membership = claim.membership
    if new_schema is None or membership is None:
        logger.error(
            "refresh_tenant_schema: claim for schema %s, job %s returned no schema or membership",
            schema_id,
            context.job.id,
        )
        return {
            "status": "rejected",
            "error_code": ErrorCode.INTERNAL_ERROR,
            "error": "The refresh could not be started. Retry the refresh from the workspace.",
            "retry_required": True,
        }

    # Upstream freshness is checked here, after the claim's transaction closed, so
    # no row lock is held across a provider call.
    denial = await _refresh_access_denial(membership, workspace_id, new_schema, context.job.id)
    if denial is not None:
        return denial

    # T serializes this refresh with every other writer of the tenant (workspace
    # loads, retirement). Sibling work happens only after T is released: never
    # wait on another workspace's lock while holding a tenant lock.
    try:
        async with tenant_data_lock([new_schema.tenant_id]):
            membership = (
                await TenantMembership.objects.select_related("tenant", "user", "connection")
                .filter(
                    id=new_schema.refresh_membership_id,
                    user_id=new_schema.refresh_actor_user_id,
                    tenant_id=new_schema.tenant_id,
                )
                .afirst()
            )
            if membership is None:
                await _drain(
                    retirement.drop_claimed_refresh_schema_and_fail(new_schema, context.job.id),
                    new_schema,
                )
                return _refresh_denial_result(DENIED_MEMBERSHIP_MISSING)
            if not await WorkspaceTenant.objects.filter(
                workspace_id=new_schema.refresh_workspace_id,
                tenant_id=new_schema.tenant_id,
            ).aexists():
                await _drain(
                    retirement.drop_claimed_refresh_schema_and_fail(new_schema, context.job.id),
                    new_schema,
                )
                return _refresh_denial_result(DENIED_WORKSPACE_UNLINKED)
            # The wait for T can outlast the proof (up to the lock timeout), and
            # the fetch must not run on stale authority: check again under T.
            outcome = await _refresh_access_denial(
                membership, workspace_id, new_schema, context.job.id
            ) or await _run_claimed_refresh(context, new_schema, membership)
    except DataLockTimeout:
        logger.warning("Refresh of '%s' timed out waiting for its tenant", new_schema.schema_name)
        await _drain(
            retirement.drop_claimed_refresh_schema_and_fail(new_schema, context.job.id), new_schema
        )
        return {
            "error": "Another load of this source is still running. Retry the refresh later.",
            "retry_required": True,
        }
    if outcome.get("status") != "active":
        return outcome

    # The tenant data schema is SHARED across workspaces; this refresh swapped in a
    # NEW physical schema. Dependent multi-tenant view schemas still point at the OLD
    # schema, so rebuild them against the new ACTIVE one (the old schema is retired
    # only once nothing reads it).
    await publication.rebuild_dependent_view_schemas([new_schema.tenant_id])

    # Single-tenant workspaces query the tenant schema directly (no view schema),
    # so the sibling rebuild above skips them. The generated Cube YAML is
    # schema-agnostic, but refreshed data may add or remove columns, which only a
    # semantic-model rebuild picks up.
    await publication.rebuild_single_tenant_semantic_models([new_schema.tenant_id])

    logger.info("Refresh complete: schema '%s' is now active", new_schema.schema_name)
    return outcome


async def _run_claimed_refresh(context, new_schema, membership) -> dict:
    """Load and publish one claimed refresh candidate; the caller holds its T."""
    job_id = context.job.id
    manager = SchemaManager()
    try:
        await run_data_thread(manager.create_physical_schema, new_schema)
    except Exception:
        logger.exception("Failed to create schema '%s'", new_schema.schema_name)
        await _drain(
            retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
        )
        return {"error": "Failed to create schema"}
    except BaseException:
        await _drain(
            retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
        )
        raise

    generation_result = {}

    def begin_and_remember():
        generation = begin_load_generation(new_schema.tenant_id)
        generation_result["generation"] = generation
        return generation

    try:
        credential = await aresolve_credential(membership)
        if credential is None:
            await _drain(
                retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
            )
            return {"error": "No credential available"}

        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}
        pipeline_name = provider_pipeline_map.get(membership.tenant.provider)
        if pipeline_name is None:
            await _drain(
                retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
            )
            return {"error": no_pipeline_message(registry, membership.tenant.provider)}
        pipeline_config = registry.get(pipeline_name)
        generation = await _to_thread_fresh_db(begin_and_remember)
    except CredentialResolutionError as e:
        await _drain(
            retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
        )
        return {"error": e.message, "error_code": e.code}
    except Exception:
        logger.exception("Failed to start refresh for schema '%s'", new_schema.schema_name)
        await _drain(
            retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
        )
        return {"error": "Failed to start the refresh", "retry_required": True}
    except BaseException:
        # An abort may hide the return value after the generation commits.
        cleanup = (
            _end_refresh_load(new_schema, job_id, generation_result["generation"])
            if "generation" in generation_result
            else retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id)
        )
        await _drain(cleanup, new_schema)
        raise

    try:
        # target_schema forces the load into the new "_r" schema; without it
        # run_pipeline re-resolves the old active base schema and data lands there.
        result = await _to_thread_fresh_db(
            run_pipeline,
            membership,
            credential,
            pipeline_config,
            target_schema=new_schema,
            procrastinate_job_id=job_id,
            defer_schema_promotion=True,
        )
    except Exception:
        logger.exception("Materialization failed for schema '%s'", new_schema.schema_name)
        await _drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
        return {"error": "Materialization failed"}
    except BaseException:
        await _drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
        raise

    # Reset last_accessed_at so the fresh schema starts with a clean inactivity
    # TTL; otherwise expire_inactive_schemas could drop it before first use.
    try:
        promotion = await _to_thread_fresh_db(
            retirement.promote_and_queue_retirement,
            new_schema.id,
            accessed_at=timezone.now(),
            refresh_job_id=job_id,
            loading_generation=generation,
            run_id=result.get("run_id") if isinstance(result, dict) else None,
            fingerprint=result.get("load_fingerprint", "") if isinstance(result, dict) else "",
        )
    except Exception:
        # It rolled back, so the candidate is still ours and handled as unpublished.
        logger.exception("Publishing refresh schema '%s' failed", new_schema.schema_name)
        promotion = Promotion(promoted=False)
    except BaseException:
        # A no-op if the promotion committed: both steps CAS on the unpublished state.
        await _drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
        raise
    if not promotion.promoted:
        try:
            still_ours = await TenantSchema.objects.filter(
                id=new_schema.id,
                state=SchemaState.PROVISIONING,
                refresh_job_id=job_id,
            ).aexists()
        finally:
            await _drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
        if still_ours:
            return {
                "error": (
                    "The refresh finished without a complete result to publish; the "
                    "previous data is still being served."
                ),
                "retry_required": True,
            }
        return {"status": "ignored"}
    return {"status": "active", "schema_id": str(new_schema.id)}


async def _refresh_access_denial(membership, workspace_id, schema, job_id) -> dict | None:
    """Fail the candidate and return the denial if the actor's authority lapsed.

    A denial fails the candidate like any other refresh failure, with its own
    code (an outage says retry).
    """
    access = await aresolve_workspace_access_ex(
        membership.user,
        workspace_id,
        minimum_role=WorkspaceRole.READ_WRITE,
        verification=VerificationBudget.BACKGROUND,
    )
    if access.granted:
        return None
    await _drain(retirement.drop_claimed_refresh_schema_and_fail(schema, job_id), schema)
    if access.denied_reason in FRESHNESS_ERROR_CODES:
        return _refresh_denial_result(access.denied_reason)
    return _refresh_denial_result(DENIED_ROLE_REQUIRED)


async def _end_refresh_load(schema, job_id: int, generation: int) -> None:
    try:
        await _to_thread_fresh_db(end_load_generation, schema.tenant_id, generation)
    finally:
        # Paired: a failure clearing the marker must not leave the candidate live.
        await retirement.drop_claimed_refresh_schema_and_fail(schema, job_id)


def _preflight_failure(tenant, error: str, code: str = "") -> dict:
    return {
        "tenant": tenant.external_id,
        "tenant_id": str(tenant.id),
        "provider": tenant.provider,
        "state": TENANT_NOT_RUN,
        "success": False,
        "error": error,
        **({"error_code": str(code)} if code else {}),
    }


def _unreachable_tenant_results(tenants: Iterable) -> list[dict]:
    results = [
        _preflight_failure(
            tenant, _unreachable_tenant_error(tenant), ErrorCode.WORKSPACE_TENANT_UNREACHABLE
        )
        for tenant in tenants
    ]
    _set_tenant_display_names(results)
    return results


def _no_reachable_tenants_result(
    tenant_results: list[dict], error: str = "No tenant memberships found"
) -> dict:
    return {
        "error": error,
        "tenants": tenant_results,
        "all_succeeded": False,
        "guidance": credential_guidance(_summary_failures(tenant_results)),
    }


# The code picks the guidance, so it must match what the member has to do: only
# a member with no usable membership is told to connect the account (A4).
_RECOVERY_ERROR_CODES = {
    CoverageRecovery.CONNECT_SOURCE: ErrorCode.WORKSPACE_TENANT_UNREACHABLE,
    CoverageRecovery.ACCESS_REMOVED: ErrorCode.WORKSPACE_TENANT_UNREACHABLE,
    CoverageRecovery.RECONNECT: ErrorCode.AUTH_CREDENTIAL_MISSING,
    CoverageRecovery.CONNECT_TEAM: ErrorCode.AUTH_CREDENTIAL_MISSING,
    CoverageRecovery.LEGACY_TEAM_UNKNOWN: ErrorCode.AUTH_CREDENTIAL_MISSING,
}


def _missing_tenant_failure(tenant, missing: MissingTenant) -> dict:
    """Describe why the acting user can't use ``tenant``; the advice comes from the code."""
    code = _RECOVERY_ERROR_CODES[missing.recovery]
    if code == ErrorCode.WORKSPACE_TENANT_UNREACHABLE:
        return _preflight_failure(tenant, _unreachable_tenant_error(tenant), code)
    who = f"the acting user's {tenant.provider}"
    if missing.recovery == CoverageRecovery.RECONNECT:
        problem = f"{who} sign-in for '{tenant.external_id}' can't be used"
    elif missing.recovery == CoverageRecovery.LEGACY_TEAM_UNKNOWN:
        problem = f"{who} membership in '{tenant.external_id}' records no team"
    else:
        team = missing.team_name or missing.team_slug
        problem = f"{who} credential is not for the team that owns '{tenant.external_id}'" + (
            f" ('{team}')" if team else ""
        )
    error = f"{problem}, so this tenant was not attempted"
    return _preflight_failure(tenant, error, code)


def _covered_tenant_skipped(tenant) -> dict:
    # Its own code: the missing tenants' guidance is not about this one.
    return _preflight_failure(
        tenant,
        "not attempted: the requesting user can't use every data source of this workspace",
        ErrorCode.WORKSPACE_TENANT_SKIPPED,
    )


def _missing_tenant_results(tenants: Iterable, missing_tenants: Iterable[MissingTenant]) -> list:
    missing = {t.tenant_id: t for t in missing_tenants}
    results = [
        _missing_tenant_failure(tenant, missing[str(tenant.pk)])
        if str(tenant.pk) in missing
        else _covered_tenant_skipped(tenant)
        for tenant in tenants
    ]
    _set_tenant_display_names(results)
    return results


async def _materialization_write_denial(workspace_id: str, user_id: str) -> dict | None:
    """Return ``None`` if ``user_id`` may load the workspace, else a denied summary.

    Every denial has one shape: ``status: "denied"``, a str ``error``, a registry
    ``error_code`` saying why, and every workspace tenant as a not-run failure, so
    the resume path records per-tenant codes the same way for either reason.
    """
    access = None
    if user_id:
        try:
            user = await User.objects.filter(id=user_id).afirst()
        except (TypeError, ValueError, ValidationError):
            user = None
        if user is not None:
            access = await aresolve_workspace_access_ex(
                user,
                workspace_id,
                minimum_role=WorkspaceRole.READ_WRITE,
                verification=VerificationBudget.BACKGROUND,
            )
            if access.granted:
                return None
    tenants = [
        wt.tenant
        async for wt in WorkspaceTenant.objects.filter(workspace_id=workspace_id).select_related(
            "tenant"
        )
    ]
    if access is not None and access.denied_reason == TENANT_ACCESS_LOST:
        # Even a MANAGE member cannot fix this by changing roles. Every tenant gets
        # a recorded not-run entry (the resume path reads one per tenant), and the
        # remedy comes from each entry's code's guidance.
        results = _missing_tenant_results(tenants, access.missing_tenants)
        missing_codes = {
            r["error_code"]
            for r in results
            if r["error_code"] != ErrorCode.WORKSPACE_TENANT_SKIPPED
        }
        code = (
            ErrorCode(missing_codes.pop())
            if len(missing_codes) == 1
            else ErrorCode.WORKSPACE_TENANT_UNREACHABLE
        )
        error = "The requesting user can't use these data sources: " + (
            ", ".join(access.lost_tenant_names) or "one or more of this workspace's sources"
        )
    elif access is not None and access.denied_reason == NO_SOURCES:
        # No role change fixes this; there is nothing to load.
        code = ErrorCode.WORKSPACE_TENANT_UNREACHABLE
        error = NO_SOURCES_MESSAGE
        results = []
    elif access is not None and access.denied_reason in FRESHNESS_ERROR_CODES:
        code = FRESHNESS_ERROR_CODES[access.denied_reason]
        error = access_denied_body(access)["error"]
        missing = {t.tenant_id for t in access.missing_tenants}
        results = [
            _preflight_failure(tenant, error, code)
            if not missing or str(tenant.pk) in missing
            else _covered_tenant_skipped(tenant)
            for tenant in tenants
        ]
        _set_tenant_display_names(results)
    else:
        code = ErrorCode.WORKSPACE_ROLE_INSUFFICIENT
        results = [_preflight_failure(tenant, _ROLE_DENIED_MESSAGE, code) for tenant in tenants]
        _set_tenant_display_names(results)
        error = _ROLE_DENIED_MESSAGE
    return {
        "status": "denied",
        "error_code": str(code),
        **_no_reachable_tenants_result(results, error),
    }


async def _workspace_tenant_ids(workspace_id) -> list:
    return [
        tenant_id
        async for tenant_id in WorkspaceTenant.objects.filter(
            workspace_id=workspace_id
        ).values_list("tenant_id", flat=True)
    ]


async def _recorded_denial(workspace_id, user_id, denial: dict) -> dict:
    """A load refused before it started still left some sources unrefreshed (#715).

    Only a source the requester's own sign-in or membership failed for is
    recorded. A role denial, a source skipped for another one, or an
    inconclusive check says nothing about the source, so its record stands.
    """
    failed = [
        entry for entry in denial.get("tenants") or [] if entry.get("error_code") in REQUESTER_CODES
    ]
    if failed:
        await arecord_load_outcomes(workspace_id, failed, user_id, partial=True)
    return denial


def serialized_workspace_materialization(function):
    """Capture load intent, then take W and the sorted tenant locks T*.

    Authority is rechecked after every wait. The intent is captured before the W
    wait (or arrives from the queue), so two requests queued behind one lock join
    the same load. T* covers every tenant of the workspace at once: a tenant added
    after the locks are taken is reported, never loaded outside T.
    """

    @wraps(function)
    async def wrapped(
        workspace_id,
        user_id="",
        *args,
        load_intent=None,
        intent_kind: str = INTENT_FULL_REFRESH,
        **kwargs,
    ):
        denial = await _materialization_write_denial(workspace_id, user_id)
        if denial is not None:
            return await _recorded_denial(workspace_id, user_id, denial)
        intent = parse_load_intent(load_intent)
        if intent is None:
            tenant_ids = await _workspace_tenant_ids(workspace_id)
            intent = await _to_thread_fresh_db(capture_load_intent, tenant_ids, intent_kind)
        async with workspace_data_lock(workspace_id):
            denial = await _materialization_write_denial(workspace_id, user_id)
            if denial is not None:
                return await _recorded_denial(workspace_id, user_id, denial)
            tenant_ids = await _workspace_tenant_ids(workspace_id)
            async with tenant_data_lock(tenant_ids):
                denial = await _materialization_write_denial(workspace_id, user_id)
                if denial is not None:
                    return await _recorded_denial(workspace_id, user_id, denial)
                return await function(
                    workspace_id,
                    user_id,
                    *args,
                    load_intent=intent,
                    locked_tenant_ids=frozenset(str(t) for t in tenant_ids),
                    **kwargs,
                )

    return wrapped


# The view build re-reads the workspace's sources and cannot expand the T set
# this load holds, so a source added mid-run surfaces as a LockOrderError.
_SOURCE_ADDED_DURING_LOAD = (
    "A source was added while this load was running, so this run did not republish "
    "the workspace's views; the follow-up queued when the source was added does."
)


_DENIAL_CODES_SAFE_TO_SKIP = frozenset(
    {ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE, ErrorCode.WORKSPACE_TENANT_SKIPPED}
)


async def _skippable_despite_denial(tm, denied_by_tenant, locked_tenant_ids) -> bool:
    """Whether a new-source load may pass over this source after a mid-run denial.

    Only when the loop would have reached its served check (within the held
    locks, membership still live) and the denial says nothing is wrong with this
    source: a transient verification outage, or SKIPPED because a different
    source was lost. A denial naming a real access problem with this source is
    what the run must report, so it is never upgraded to success.
    """
    if locked_tenant_ids is not None and str(tm.tenant_id) not in locked_tenant_ids:
        return False
    entry = denied_by_tenant.get(str(tm.tenant_id)) or {}
    if entry.get("error_code") not in _DENIAL_CODES_SAFE_TO_SKIP:
        return False
    return await TenantMembership.objects.filter(id=tm.id, archived_at__isnull=True).aexists()


async def _served_schema(tm):
    return await TenantSchema.objects.filter(
        tenant_id=tm.tenant_id, state=SchemaState.ACTIVE
    ).afirst()


async def _already_loaded(tm, served) -> dict:
    # The views about to be published read this schema, so it counts as used;
    # otherwise the inactivity sweep could drop it from under them.
    await served.atouch()
    result = {"status": "already_loaded"}
    # Named so a chat resumed by this job reports the run that loaded it, not
    # "the run recorded nothing" (it produced no run of its own).
    run_id = await (
        MaterializationRun.objects.filter(
            tenant_schema=served,
            state=MaterializationRun.RunState.COMPLETED,
            # Postgres sorts NULLs first under DESC; a timestampless run would shadow real ones.
            completed_at__isnull=False,
        )
        .order_by("-completed_at")
        .values_list("id", flat=True)
        .afirst()
    )
    if run_id is not None:
        result["run_id"] = str(run_id)
    return {
        "tenant": tm.tenant.external_id,
        "tenant_id": str(tm.tenant_id),
        "provider": tm.tenant.provider,
        "success": True,
        "result": result,
    }


@serialized_workspace_materialization
async def materialize_workspace_core(
    workspace_id: str,
    user_id: str = "",
    job_id: int | None = None,
    *,
    load_intent: dict[str, int] | None = None,
    locked_tenant_ids: frozenset[str] | None = None,
    only_unserved: bool = False,
) -> dict:
    """Run materialization for all tenants in a workspace and rebuild view schemas.

    Returns a per-tenant summary. Does NOT defer any chat-resume task — the
    interactive chat path uses the ``materialize_workspace`` Procrastinate task
    (which wraps this and defers ``resume_thread_after_materialization``);
    headless callers (e.g. the recipe runner's blocking materialize tool) call
    this directly and block on the return value.

    Writes progress to ``MaterializationRun.progress`` (keyed by ``job_id``)
    after each page so the MCP polling loop can surface real-time status. The
    ``progress_updater`` closure also acts as the cancellation checkpoint: it
    re-reads ``MaterializationRun.state`` and raises ``MaterializationCancelled``
    when the run has been marked CANCELLED, triggering a transaction rollback.
    """
    tenant_results: list[dict] = []
    attempted_tenant_ids: set[str] = set()
    successful_attempted_tenant_ids: set[str] = set()
    loaded_tenant_ids: set[str] = set()

    try:
        workspace = await Workspace.objects.aget(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.exception("materialize_workspace: workspace %s not found", workspace_id)
        return {"error": "Workspace not found"}

    workspace_tenants = {
        wt.tenant_id: wt.tenant
        async for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant")
    }

    qs = TenantMembership.objects.select_related("user", "tenant", "connection").filter(
        archived_at__isnull=True,
        tenant_id__in=list(workspace_tenants),
        user_id=user_id,
    )

    memberships = [tm async for tm in qs]

    # A workspace tenant the acting user cannot reach is not ours to quietly
    # drop: it never entered tenant_results, so `all(...)` was vacuous over it
    # and the run reported success having loaded a subset of the workspace (#364).
    #
    # The all-of gate (#380) refuses a requester who lacks a tenant, so this is
    # reached only while its rollout switch is off or when access is lost after
    # the gate passed. Report it without borrowing a teammate's credentials.
    reachable = {tm.tenant_id for tm in memberships}
    unreachable_results = _unreachable_tenant_results(
        tenant for tenant_id, tenant in workspace_tenants.items() if tenant_id not in reachable
    )
    for entry in unreachable_results:
        logger.warning(
            "materialize_workspace: workspace %s includes tenant %s, which the "
            "acting user cannot reach; this run does not cover it (#364)",
            workspace_id,
            entry["tenant"],
        )

    if not memberships:
        logger.warning("materialize_workspace: no memberships for workspace %s", workspace_id)
        await arecord_load_outcomes(workspace.id, unreachable_results, user_id)
        return _no_reachable_tenants_result(unreachable_results)

    registry = get_registry()
    provider_pipeline_map = {p.provider: p.name for p in registry.list()}
    load_intent = load_intent or {}
    # A mid-run denial for a real access problem, even one whose sources were all
    # passed over below: the run must never report it as a clean success.
    determinate_denial: dict | None = None

    for index, tm in enumerate(memberships):
        # The wrapper checked before the first tenant. A long load can outlive a
        # five-minute proof, so each later tenant re-checks before protected work.
        if index:
            denial = await _materialization_write_denial(workspace_id, user_id)
            if denial is not None:
                if denial["error_code"] != ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE:
                    determinate_denial = denial
                denied_by_tenant = {entry.get("tenant_id"): entry for entry in denial["tenants"]}
                pending = []
                for later in memberships[index:]:
                    served = (
                        await _served_schema(later)
                        if only_unserved
                        and await _skippable_despite_denial(
                            later, denied_by_tenant, locked_tenant_ids
                        )
                        else None
                    )
                    if served is not None:
                        tenant_results.append(await _already_loaded(later, served))
                    else:
                        pending.append(later)
                attempted_tenant_ids.update(str(later.tenant_id) for later in pending)
                tenant_results.extend(
                    denied_by_tenant.get(str(later.tenant_id))
                    or _preflight_failure(later.tenant, denial["error"], denial["error_code"])
                    for later in pending
                )
                # Only source loads stop here. The derived view and Cube rebuilds below
                # read already-published tenant data and keep other members' views
                # consistent; the Cube gate treats the pending (denied) tenants as
                # failed, while serving siblings passed over above count as served.
                break
            # The workspace can stay accessible through another tenant after this
            # recheck archived this one, so the membership itself must still be live.
            if not await TenantMembership.objects.filter(id=tm.id).aexists():
                attempted_tenant_ids.add(str(tm.tenant_id))
                tenant_results.append(
                    _preflight_failure(
                        tm.tenant,
                        "Access to this source was removed upstream during the run.",
                        ErrorCode.AUTH_ACCESS_DENIED,
                    )
                )
                continue
        tenant_id = tm.tenant.external_id
        if locked_tenant_ids is not None and str(tm.tenant_id) not in locked_tenant_ids:
            # Added after the tenant locks were taken; loading it now would run
            # outside T. Report it truthfully and let a re-run cover it.
            tenant_results.append(
                # Its own code: the access codes carry advice (reconnect an
                # account) that would contradict "run it again", and no code at
                # all reads as INTERNAL_ERROR on resume.
                _preflight_failure(
                    tm.tenant,
                    "This source was added while the load was starting and was not "
                    "loaded. Run the load again to include it.",
                    ErrorCode.WORKSPACE_SOURCES_CHANGED,
                )
            )
            continue
        served = await _served_schema(tm) if only_unserved else None
        if served is not None:
            # A source added to the workspace loads before publication; sources
            # already serving data are only published, never reloaded for it.
            tenant_results.append(await _already_loaded(tm, served))
            continue
        attempted_tenant_ids.add(str(tm.tenant_id))
        pipeline_name = provider_pipeline_map.get(tm.tenant.provider)
        if pipeline_name is None:
            tenant_results.append(
                _preflight_failure(
                    tm.tenant,
                    no_pipeline_message(registry, tm.tenant.provider),
                    ErrorCode.PIPELINE_UNRESOLVED,
                )
            )
            continue

        try:
            credential = await aresolve_credential(tm)
        except CredentialResolutionError as e:
            # Actionable failure (e.g. token scoped to a different team) —
            # surface a distinct message + code so the user knows to
            # re-connect, not the generic "No usable credential could be resolved"
            # (arch #245 finding 07#3).
            tenant_results.append(_preflight_failure(tm.tenant, e.message, e.code))
            continue
        if credential is None:
            tenant_results.append(
                _preflight_failure(
                    tm.tenant,
                    "No usable credential could be resolved",
                    ErrorCode.AUTH_CREDENTIAL_MISSING,
                )
            )
            continue

        pipeline_config = registry.get(pipeline_name)
        required = load_intent.get(str(tm.tenant_id))
        evidence = None
        if required is not None:
            fingerprint = await _to_thread_fresh_db(
                pipeline_fingerprint, pipeline_config, tm.tenant
            )
            evidence = await _to_thread_fresh_db(
                reusable_generation, tm.tenant_id, required, fingerprint
            )
        if evidence is not None:
            # An equivalent load completed while this request waited. The requester
            # authorized and resolved its own credential above, and still publishes
            # its own views and Cube below; its status says what really happened.
            # Reuse counts as use: without the touch, the inactivity sweep could
            # retire the schema this run just reported ready.
            await evidence.schema.atouch()
            successful_attempted_tenant_ids.add(str(tm.tenant_id))
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "tenant_id": str(tm.tenant_id),
                    "provider": tm.tenant.provider,
                    "success": True,
                    "reused_generation": evidence.generation,
                    "result": {
                        "status": "completed",
                        "reused": True,
                        "run_id": str(evidence.run.id),
                        "schema": evidence.schema.schema_name,
                        "pipeline": pipeline_config.name,
                    },
                }
            )
            continue
        loaded_tenant_ids.add(str(tm.tenant_id))
        try:
            result = await _load_workspace_candidate(
                workspace, tm, credential, pipeline_config, job_id
            )
            successful_attempted_tenant_ids.add(str(tm.tenant_id))
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "tenant_id": str(tm.tenant_id),
                    "provider": tm.tenant.provider,
                    "success": True,
                    "result": result,
                }
            )
        except MaterializationCancelled:
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "tenant_id": str(tm.tenant_id),
                    "provider": tm.tenant.provider,
                    "success": False,
                    "cancelled": True,
                }
            )
            break
        except ConnectExportError as e:
            # Capture the sentry-trace header so support can correlate with
            # Connect's Sentry in one hop. set_tag is a no-op without a DSN.
            logger.exception(
                "Materialization failed for tenant %s on pipeline %s: "
                "connect status=%s after %d attempts (last_id=%s, sentry-trace=%s)",
                tenant_id,
                pipeline_name,
                e.status,
                e.attempts,
                e.last_id,
                e.sentry_trace,
            )
            sentry_sdk.set_tag("connect.upstream_sentry_trace", e.sentry_trace or "")
            sentry_sdk.set_tag("connect.pipeline", pipeline_name or "")
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "tenant_id": str(tm.tenant_id),
                    "provider": tm.tenant.provider,
                    "success": False,
                    "error": str(e),
                    "error_code": code_of(e),
                }
            )
        except Exception as e:
            logger.exception("Materialization failed for tenant %s", tenant_id)
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "tenant_id": str(tm.tenant_id),
                    "provider": tm.tenant.provider,
                    "success": False,
                    "error": str(e),
                    "error_code": code_of(e),
                }
            )

    # Two questions, so two flags. `attempted_succeeded` asks whether every
    # attempted load succeeded; the coverage-aware Cube gate below can refine it
    # for a failed tenant omitted from a degraded view. `all_succeeded` is the
    # honesty flag callers read, and an uncovered workspace is not a success.
    attempted_succeeded = all(r.get("success") for r in tenant_results)
    failed_attempted_tenant_ids = attempted_tenant_ids - successful_attempted_tenant_ids
    all_succeeded = attempted_succeeded and not unreachable_results and determinate_denial is None

    # A partial/cancelled multi-tenant run DROP-CASCADEs some namespaced views,
    # leaving the workspace's own view schema ACTIVE-but-missing. Rebuild it
    # unconditionally (not only on full success) before the resume fires (arch #255 03#1).
    view_schema_outcome: dict | None = None
    workspace_tenant_count = await workspace.workspace_tenants.acount()
    if workspace_tenant_count > 1:
        try:
            view_schema = await _to_thread_fresh_db(SchemaManager().build_view_schema, workspace)
            tenant_coverage = (
                view_schema.tenant_coverage if isinstance(view_schema.tenant_coverage, dict) else {}
            )
            view_schema_outcome = {
                "ok": True,
                "error": None,
                "tenant_coverage": tenant_coverage,
            }
        except ViewSchemaRetired as exc:
            # Dropped to one source during the load: its tenant schema serves now.
            logger.info("Workspace %s needs no view schema: %s", workspace_id, exc)
        except Exception as exc:
            # Don't re-raise — the resume task must still fire. The failure is
            # recorded on the WorkspaceViewSchema row (state=FAILED, last_error),
            # which the resume task reads directly.
            if isinstance(exc, NoActiveTenantSchema):
                logger.warning(
                    "Post-materialization view schema rebuild skipped for workspace %s: %s",
                    workspace_id,
                    exc,
                )
            else:
                logger.exception(
                    "Post-materialization view schema rebuild failed for workspace %s",
                    workspace_id,
                )
            failed_view_schema = await WorkspaceViewSchema.objects.filter(
                workspace=workspace
            ).afirst()
            tenant_coverage = (
                failed_view_schema.tenant_coverage
                if failed_view_schema is not None
                and isinstance(failed_view_schema.tenant_coverage, dict)
                else {}
            )
            view_schema_outcome = {
                "ok": False,
                "error": (
                    _SOURCE_ADDED_DURING_LOAD if isinstance(exc, LockOrderError) else str(exc)[:500]
                ),
                "tenant_coverage": tenant_coverage,
            }

    # A failed source can be safe only when the built view explicitly excludes
    # it. An included old ACTIVE schema must not conceal a failed refresh.
    cube_input_is_safe = attempted_succeeded
    if view_schema_outcome is not None and view_schema_outcome.get("ok"):
        tenant_coverage = parse_coverage(view_schema_outcome.get("tenant_coverage")) or {}
        excluded_tenant_ids = {
            str(entry.get("tenant_id"))
            for entry in tenant_coverage.get("excluded_tenants", [])
            if isinstance(entry, dict) and entry.get("tenant_id")
        }
        included_tenant_ids = {
            str(entry.get("tenant_id"))
            for entry in tenant_coverage.get("included_tenants", [])
            if isinstance(entry, dict) and entry.get("tenant_id")
        }
        cube_input_is_safe = failed_attempted_tenant_ids.issubset(
            excluded_tenant_ids
        ) and failed_attempted_tenant_ids.isdisjoint(included_tenant_ids)

    snapshot_state = "safe" if cube_input_is_safe else "unsafe"
    if cube_input_is_safe and view_schema_outcome and view_schema_outcome.get("ok"):
        snapshot_state = await _included_tenant_snapshot_state(
            workspace, view_schema_outcome["tenant_coverage"]
        )
        cube_input_is_safe = snapshot_state == "safe"

    cube_schema_outcome: dict | None = None
    cube_build_allowed = cube_input_is_safe and (
        workspace_tenant_count <= 1
        or (view_schema_outcome is not None and view_schema_outcome.get("ok"))
    )
    if cube_build_allowed:
        try:
            cube_schema = await run_data_thread(build_and_promote_cube_schema, workspace)
            cube_schema_outcome = {
                "ok": True,
                "id": str(cube_schema.id),
                "content_hash": cube_schema.content_hash,
                "error": None,
            }
        except CubeSchemaBuildError as exc:
            logger.warning(
                "Semantic Cube schema build failed for workspace %s: %s",
                workspace_id,
                exc,
            )
            cube_schema_outcome = {"ok": False, "error": str(exc)[:500]}
        except Exception as exc:
            logger.exception("Semantic Cube schema build failed for workspace %s", workspace_id)
            cube_schema_outcome = {"ok": False, "error": str(exc)[:500]}
    else:
        if view_schema_outcome is not None and not view_schema_outcome.get("ok"):
            skip_reason = (
                "Semantic Cube schema build skipped because the workspace view schema build failed."
            )
        else:
            skip_reason = (
                "Semantic Cube schema build skipped because materialization did not produce "
                "a safe tenant snapshot."
            )
        if snapshot_state == "in_progress":
            cube_schema_outcome = await publication.defer_cube_promotion(workspace)
        else:
            await _to_thread_fresh_db(record_cube_schema_build_failure, workspace, skip_reason)
            cube_schema_outcome = {"ok": False, "error": skip_reason}

    # Tenant data schemas (t_<id>) are SHARED. Re-materializing drops & recreates
    # raw_* tables, cascade-dropping the namespaced views in every OTHER workspace's
    # view schema (leaving them ACTIVE but empty). Rebuild each sibling multi-tenant
    # workspace's views against the new tables.
    # A new-source load leaves already-serving sources untouched, so only the
    # sources it actually loaded can have invalidated sibling views.
    await publication.rebuild_dependent_view_schemas(
        [
            tm.tenant_id
            for tm in memberships
            if not only_unserved or str(tm.tenant_id) in loaded_tenant_ids
        ],
        exclude_workspace_id=str(workspace.id),
    )

    all_results = tenant_results + unreachable_results
    _set_tenant_display_names(all_results)
    guidance_sources = all_results
    denied_mid_run = None
    if determinate_denial is not None:
        # The lost source may have been handled before the recheck, so it shows
        # as a success above; its guidance comes from the denial. Sources already
        # reported as failed carry that same entry, so they are not added twice.
        reported_failed = {e.get("tenant_id") for e in all_results if not e.get("success")}
        guidance_sources = all_results + [
            entry
            for entry in determinate_denial["tenants"]
            if entry.get("tenant_id") not in reported_failed
        ]
        denied_mid_run = {
            "error": determinate_denial["error"],
            "error_code": determinate_denial["error_code"],
        }
    return {
        "tenants": all_results,
        "all_succeeded": all_succeeded,
        "view_schema": view_schema_outcome,
        "cube_schema": cube_schema_outcome,
        "guidance": credential_guidance(_summary_failures(guidance_sources)),
        "denied_mid_run": denied_mid_run,
        "source_freshness": await arecord_load_outcomes(
            workspace.id, all_results, user_id, partial=only_unserved
        ),
    }


async def _await_in_progress_materializations(
    workspace_id: str, *, poll_interval: float = 2.0, max_wait_seconds: float = 1800.0
) -> None:
    """Block until no materialization is ACTIVE for this workspace's tenants.

    Headless callers (recipes) call this before starting their own run so they
    do not execute a parallel materialization against the same tenant schemas —
    the pipeline drops & recreates ``raw_*`` tables, so concurrent runs corrupt
    each other. Best-effort: on timeout, log and return so the caller proceeds.
    """
    # Poll cross-process MaterializationRun state (another worker owns the run,
    # so no in-process Event to await). Bounded to keep a ceiling on the wait.
    max_polls = max(1, int(max_wait_seconds / poll_interval))
    for _ in range(max_polls):
        if not await active_runs_for_workspaces([workspace_id]).aexists():
            return
        await asyncio.sleep(poll_interval)
    logger.warning(
        "materialize_workspace_blocking: still waiting on an in-progress materialization "
        "of workspace %s after ~%.0fs; proceeding",
        workspace_id,
        max_wait_seconds,
    )


async def materialize_workspace_blocking(
    workspace_id: str, user_id: str = "", job_id: int | None = None
) -> dict:
    """Ensure the workspace is materialized, blocking until done.

    Unlike the bare ``materialize_workspace_core``, this first WAITS for any
    materialization already in progress for the workspace's tenants to finish,
    then runs a fresh one — so a headless recipe never starts a second, parallel
    materialization against the same tenant schema (which the interactive path
    avoids by telling the agent not to). Returns the core summary shape.
    """
    denial = await _materialization_write_denial(workspace_id, user_id)
    if denial is not None:
        return await _recorded_denial(workspace_id, user_id, denial)
    await _await_in_progress_materializations(workspace_id)
    return await materialize_workspace_core(workspace_id, user_id, job_id)


register_inline_materializer(materialize_workspace_blocking)


@app.task(pass_context=True)
async def materialize_workspace(
    context,
    workspace_id: str,
    user_id: str = "",
    load_intent: dict | None = None,
    only_unserved: bool = False,
    notify_thread: bool = True,
) -> dict:
    """Procrastinate task: run materialization for a workspace, then ALWAYS
    defer the chat-resume task so an interactive user is never left with a
    phantom spinner — even on early-return paths (workspace missing, no
    memberships) where the per-tenant loop never executed.

    The actual work lives in ``materialize_workspace_core`` so headless callers
    (recipes) can reuse it without the fire-and-resume machinery.
    ``notify_thread=False`` is for dispatches no chat thread waits on (adding a
    source, a retry sent without a thread), which have no ThreadJob to resume.
    ``only_unserved`` loads every workspace source that serves nothing (typically
    the one just added) and republishes the views; if the run stops before publishing, a plain view
    rebuild is queued instead so the views reflect the sources that do serve, when there is
    something for it to build (``aview_schema_buildable``).
    """
    job_id = context.job.id
    preflight_failures = None
    result = None
    try:
        result = await materialize_workspace_core(
            workspace_id,
            user_id,
            job_id,
            load_intent=load_intent,
            only_unserved=only_unserved,
        )
        preflight_failures = _resume_records(result)
        return result
    finally:
        reported_publication = isinstance(result, dict) and "view_schema" in result
        outcome = result.get("view_schema") if reported_publication else None
        # None means a single-source workspace needed no view publication.
        published = reported_publication and (outcome is None or outcome.get("ok"))
        if only_unserved and not published:
            try:
                if await aview_schema_buildable(workspace_id):
                    await rebuild_workspace_view_schema.defer_async(workspace_id=str(workspace_id))
            except Exception:
                logger.exception("Could not queue the view rebuild for workspace %s", workspace_id)
        if notify_thread:
            await _defer_resume_for_job(job_id, preflight_failures)
        await _defer_pending_flush(workspace_id)


def _resume_records(result: dict) -> list[dict]:
    """What the chat resume needs beyond this job's run rows.

    Preflight failures explain tenants that never produced a run. A reused
    tenant has no run under this job either; its entry names the reused run so
    the resume reports what was served instead of "the run recorded nothing".
    """
    tenants = result.get("tenants", [])
    return [
        {
            "tenant_id": entry["tenant_id"],
            "provider": entry["provider"],
            "error": str(entry["error"])[:1000],
            "error_code": str(entry.get("error_code") or ""),
        }
        for entry in tenants
        if entry.get("state") == TENANT_NOT_RUN
    ] + [
        {
            "tenant_id": entry["tenant_id"],
            "provider": entry["provider"],
            "reused_run_id": str(entry["result"]["run_id"]),
        }
        for entry in tenants
        if entry.get("tenant_id")
        and (
            entry.get("reused_generation")
            or (
                (entry.get("result") or {}).get("status") == "already_loaded"
                and entry["result"].get("run_id")
            )
        )
    ]


async def _defer_resume_for_job(job_id: int, preflight_failures: list[dict] | None = None) -> None:
    """Find the ThreadJob bound to ``job_id`` and defer the resume task.

    Chat dispatches commit the job and its ThreadJob together
    (``adispatch_thread_materialization``, #365), so the row is visible before any
    worker can start. The bounded backoff (~3.75s) only covers jobs queued by an
    older deploy that committed them separately; after that, the janitor catches up.
    """
    try:
        tj = None
        for delay in (0, 0.25, 0.5, 1.0, 2.0):
            if delay:
                await asyncio.sleep(delay)
            tj = await ThreadJob.objects.filter(procrastinate_job_id=job_id).afirst()
            if tj is not None:
                break
        if tj is None:
            logger.warning(
                "materialize_workspace: no ThreadJob found for job_id %s after retries; "
                "janitor will catch up if MCP eventually commits one",
                job_id,
            )
            return
        if preflight_failures is not None:
            # Commit before enqueue: a failed enqueue can be recovered by the janitor.
            await ThreadJob.objects.filter(id=tj.id).aupdate(
                materialization_preflight_failures=preflight_failures
            )
        await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))
    except Exception:
        logger.exception("Failed to defer resume task for job %s", job_id)


def _run_pipeline_with_progress(
    tenant_membership,
    credential: dict,
    pipeline_config,
    job_id: int,
    target_schema=None,
) -> dict:
    """Synchronous entry point invoked under ``asyncio.to_thread``.

    Builds the ``progress_updater`` closure (mirrors progress to the DB and
    surfaces cancellation), then runs the pipeline.
    """
    # Pool thread's connection is unreachable by the worker's async-ORM cleanup
    # and may have died since the last job here — close it so the first use reopens.
    close_old_connections()

    def updater(progress: dict) -> None:
        run_id = progress.get("run_id")
        if run_id is None:
            return
        MaterializationRun.objects.filter(id=run_id).update(progress=progress)
        current_state = (
            MaterializationRun.objects.filter(id=run_id).values_list("state", flat=True).first()
        )
        if current_state == MaterializationRun.RunState.CANCELLED:
            raise MaterializationCancelled()

    return run_pipeline(
        tenant_membership,
        credential,
        pipeline_config,
        progress_updater=updater,
        procrastinate_job_id=job_id,
        target_schema=target_schema,
        defer_schema_promotion=target_schema is not None,
    )


async def _load_workspace_candidate(
    workspace, tm, credential: dict, pipeline_config, job_id: int | None
) -> dict:
    """Load one tenant into a candidate and promote it; the caller holds W and T.

    The serving schema is never written, so a failed or cancelled load leaves
    last-good data readable for every workspace sharing the tenant. A failed
    candidate keeps its data: the next load of the same pending generation with
    the same raw-load configuration resumes it, and any other failed candidate
    is dropped by a bounded-retry cleanup.
    """
    # The candidate owner; a job-less load (the agent's blocking tool) gets a token.
    owner = load_owner_token(job_id)
    # Off the loop: the first call hashes the implementation source tree.
    config = await asyncio.to_thread(raw_load_fingerprint, pipeline_config)
    await _to_thread_fresh_db(settle_orphaned_workspace_candidates, tm.tenant_id)
    generation_result = {}

    def begin_and_remember():
        generation = begin_load_generation(tm.tenant_id)
        generation_result["generation"] = generation
        return generation

    try:
        generation = await _to_thread_fresh_db(begin_and_remember)
    except BaseException:
        # Cancellation can hide the return value after the transaction commits.
        # Capture it in the worker so the marker can still be cleared.
        if "generation" in generation_result:
            await _drain(
                _end_load(tm.tenant_id, generation_result["generation"]),
                f"tenant {tm.tenant_id}",
            )
        raise
    opened_result = {}

    def open_and_remember():
        opened = open_workspace_candidate(
            tm.tenant,
            workspace_id=workspace.id,
            job_id=owner,
            generation=generation,
            config_fingerprint=config,
        )
        opened_result["opened"] = opened
        return opened

    try:
        opened = await _to_thread_fresh_db(open_and_remember)
    except BaseException:
        # Cancellation may arrive after a candidate commits but before its return
        # reaches this task. Fail that candidate under the caller's T lock so it
        # remains resumable; otherwise only the loading marker needs clearing.
        opened = opened_result.get("opened")
        if opened is None:
            cleanup = _end_load(tm.tenant_id, generation)
        else:
            cleanup = _fail_workspace_candidate(opened.schema, workspace.id, owner, generation)
        await _drain(cleanup, opened.schema if opened is not None else f"tenant {tm.tenant_id}")
        raise
    candidate = opened.schema
    if opened.resumed:
        logger.info(
            "Resuming failed candidate '%s' for tenant %s (generation %d)",
            candidate.schema_name,
            tm.tenant_id,
            generation,
        )
    try:
        try:
            await retirement.defer_abandoned_candidate_drops(tm.tenant_id, keep_id=candidate.id)
        except Exception:
            # Best effort; an abort still reaches the guard below and settles the candidate.
            logger.exception("Could not queue cleanup of abandoned candidates for %s", tm.tenant_id)
        await run_data_thread(SchemaManager().create_physical_schema, candidate)
        result = await run_data_thread(
            _run_pipeline_with_progress, tm, credential, pipeline_config, job_id, candidate
        )
        promotion = await _to_thread_fresh_db(
            retirement.promote_and_queue_retirement,
            candidate.id,
            accessed_at=timezone.now(),
            workspace_id=workspace.id,
            workspace_job_id=owner,
            loading_generation=generation,
            run_id=result.get("run_id") if isinstance(result, dict) else None,
            fingerprint=result.get("load_fingerprint", "") if isinstance(result, dict) else "",
        )
        if not promotion.promoted:
            raise RuntimeError(
                "The load finished without a complete, owned result to publish; "
                "the previous data is still being served. Run the load again."
            )
    except BaseException:
        # Includes a promotion that raised: it rolled back, so the candidate is
        # still PROVISIONING and the generation still marked loading. Drained, so
        # an abort landing during this cleanup cannot strand either.
        await _drain(
            _fail_workspace_candidate(candidate, workspace.id, owner, generation), candidate
        )
        raise
    if isinstance(result, dict) and opened.resumed:
        result = {**result, "resumed": True}
    return result


async def _end_load(tenant_id, generation: int) -> None:
    await _to_thread_fresh_db(end_load_generation, tenant_id, generation)


async def _fail_workspace_candidate(candidate, workspace_id, job_id, generation: int) -> None:
    # The physical schema is kept: it is this generation's resume point, and the
    # generation stays pending (not loading) so a retry joins and resumes it.
    try:
        await _to_thread_fresh_db(fail_workspace_candidate, candidate.id, workspace_id, job_id)
    finally:
        # Even if the CAS failed (often the same outage that failed the load),
        # a stuck marker would stop retries joining and resuming this generation.
        await _to_thread_fresh_db(end_load_generation, candidate.tenant_id, generation)


async def _drain(operation, subject) -> None:
    """Finish cleanup before propagating worker cancellation, even if aborted again.

    An abort that lands during cleanup is re-raised once cleanup is done, so a
    caller handling an ordinary error still stops instead of moving on.
    """
    cleanup = asyncio.create_task(operation)
    aborted = False
    while True:
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            aborted = True
            if cleanup.cancelled():
                break
            continue
        except Exception:
            logger.exception(
                "Cancellation cleanup failed for '%s'", getattr(subject, "schema_name", subject)
            )
        break
    if aborted:
        raise asyncio.CancelledError


async def _recovery_intent(workspace) -> str:
    """What a data-restore repair must ask of the tenants' loads.

    Missing data is reconciled: whatever is already published satisfies it. But
    the same repair is offered when data is present and its latest load did not
    complete, and reconciling would reuse that same published generation and
    change nothing, so that case asks for a fresh load.
    """
    view = await WorkspaceViewSchema.objects.filter(
        workspace=workspace, state=SchemaState.ACTIVE
    ).afirst()
    coverage = view.tenant_coverage if view is not None else None
    if await _included_tenant_snapshot_state(workspace, coverage) == "unsafe":
        return INTENT_FULL_REFRESH
    return INTENT_RECONCILE_MISSING


@app.task
async def drop_abandoned_candidate(
    schema_id: str,
    attempt: int = 0,
    last_attempt_at: str = "",
    load_job_id: int | None = None,
    busy_count: int = 0,
) -> None:
    """Drop the partial data of a failed candidate no load will resume."""
    await retirement.drop_abandoned_candidate(
        schema_id,
        attempt=attempt,
        last_attempt_at=last_attempt_at,
        load_job_id=load_job_id,
        busy_count=busy_count,
    )


@app.periodic(cron="7,22,37,52 * * * *")
@app.task
async def sweep_workspace_load_candidates(timestamp: int = 0) -> dict:
    """Reclaim workspace-load candidates whose writer died or no load will resume."""
    return await retirement.sweep_workspace_load_candidates()


def _refresh_denial_result(reason: str) -> dict:
    if reason in FRESHNESS_ERROR_CODES:
        error_code = FRESHNESS_ERROR_CODES[reason]
        error = access_denied_body(WorkspaceAccess(denied_reason=reason))["error"]
    elif reason == DENIED_MEMBERSHIP_MISSING:
        error_code = ErrorCode.WORKSPACE_TENANT_UNREACHABLE
        error = "The requesting user no longer has access to this tenant, so nothing was run."
    elif reason == DENIED_WORKSPACE_UNLINKED:
        error_code = ErrorCode.WORKSPACE_TENANT_UNREACHABLE
        error = "This tenant is no longer part of the requesting workspace, so nothing was run."
    else:
        error_code = ErrorCode.WORKSPACE_ROLE_INSUFFICIENT
        error = "Read-write or manage role required to refresh this workspace."
    return {"status": "denied", "error_code": error_code, "error": error, "retry_required": True}


@app.task
async def drop_failed_refresh_schema(schema_id: str) -> None:
    """Drop the physical schema of a refresh candidate settled as FAILED."""
    await retirement.drop_failed_refresh_schema(schema_id)


@app.periodic(cron="*/15 * * * *")
@app.task
async def reconcile_refresh_candidates(timestamp: int = 0) -> dict:
    """Settle refresh candidates whose queue job finished or whose worker died."""
    return await retirement.reconcile_refresh_candidates()


@app.periodic(cron="*/30 * * * *")
@app.task
async def expire_inactive_schemas(timestamp: int = 0) -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    `timestamp` is supplied by the procrastinate periodic deferrer; the default
    lets tests invoke this task directly.
    """
    await retirement.expire_inactive_schemas()


@app.task
async def rebuild_workspace_view_schema(workspace_id: str, revive_retired: bool = False) -> dict:
    """Build (or rebuild) the UNION ALL view schema for a multi-tenant workspace."""
    return await publication.rebuild_workspace_view_schema(
        workspace_id, revive_retired=revive_retired
    )


@app.task
async def rebuild_workspace_semantic_model(workspace_id: str) -> dict:
    """Rebuild the semantic model + Cube schema after workspace data changed shape."""
    return await publication.rebuild_workspace_semantic_model_core(workspace_id)


@app.task(pass_context=True)
async def recover_workspace_data(context, recovery_id: str) -> dict:
    """See ``_recover_workspace_data``; a held request waiting on it is flushed after."""
    try:
        return await _recover_workspace_data(context, recovery_id)
    finally:
        await _defer_flush_after_recovery(recovery_id)


async def _defer_flush_after_recovery(recovery_id: str) -> None:
    try:
        workspace_id = (
            await WorkspaceDataRecovery.objects.filter(id=recovery_id)
            .values_list("workspace_id", flat=True)
            .afirst()
        )
    except Exception:
        # From the task's finally: never replace its outcome; the sweep is the backstop.
        logger.exception("Could not find the workspace of recovery %s", recovery_id)
        return
    await _defer_pending_flush(workspace_id)


async def _recover_workspace_data(context, recovery_id: str) -> dict:
    """Repair the least healthy layer of a workspace's artifact query surface.

    The requested recovery type captures why the job was created. The task
    reassesses after waiting for any in-flight materialization, because another
    session may have repaired the physical layer while this job was queued. It
    then runs only the remaining repair and records a durable terminal state.
    """
    try:
        recovery = await WorkspaceDataRecovery.objects.select_related("workspace").aget(
            id=recovery_id
        )
    except WorkspaceDataRecovery.DoesNotExist:
        logger.warning("recover_workspace_data: recovery %s not found", recovery_id)
        return {"status": "missing"}

    now = timezone.now()
    claimed = await WorkspaceDataRecovery.objects.filter(
        id=recovery.id,
        state=WorkspaceDataRecovery.State.PENDING,
    ).aupdate(
        state=WorkspaceDataRecovery.State.RUNNING,
        procrastinate_job_id=context.job.id,
        started_at=now,
        error="",
    )
    if not claimed:
        return {"status": recovery.state}

    if recovery.requested_by_id is None:
        error = "The user who requested recovery no longer exists. Ask a workspace member to retry."
        await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
            state=WorkspaceDataRecovery.State.FAILED,
            error=error,
            completed_at=timezone.now(),
        )
        return {"status": "failed", "error": error}

    result: dict = {}
    try:
        async with workspace_data_lock(str(recovery.workspace_id)):
            await _await_in_progress_materializations(str(recovery.workspace_id))

            requester = await User.objects.filter(id=recovery.requested_by_id).afirst()
            access = (
                await aresolve_workspace_access_ex(
                    requester,
                    recovery.workspace_id,
                    minimum_role=WorkspaceRole.READ_WRITE,
                    verification=VerificationBudget.BACKGROUND,
                )
                if requester is not None
                else None
            )
            if access is None or not access.granted:
                raise ValueError(_recovery_requester_denied_message(access))

            try:
                reconciliation = await _to_thread_fresh_db(
                    SchemaManager().reconcile_view_publication, recovery.workspace
                )
                if reconciliation.get("status") == "republish_failed":
                    logger.warning(
                        "Recovery %s: view publication diverges and could not be republished: %s",
                        recovery_id,
                        reconciliation.get("error"),
                    )
            except Exception:
                # A republish can fail for the very reason this recovery exists
                # (e.g. no loaded source yet); the repair below must still run.
                logger.exception(
                    "Reconciling the view publication for recovery %s failed", recovery_id
                )
            surface = await recovery_query_surface(recovery)
            action = surface.get("recovery_action")
            if (
                recovery.source_type == CHAT_RECOVERY_SOURCE
                and action == WorkspaceDataRecovery.RecoveryType.MATERIALIZATION
            ):
                # Nobody approved a reload: a chat only asks for the data model (#714).
                result = {"error": _CHAT_RECOVERY_NEEDS_RELOAD}
            elif action == WorkspaceDataRecovery.RecoveryType.MATERIALIZATION:
                result = await materialize_workspace_core(
                    str(recovery.workspace_id),
                    str(recovery.requested_by_id),
                    context.job.id,
                    intent_kind=await _recovery_intent(recovery.workspace),
                )
            elif action == WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD:
                result = await publication.rebuild_workspace_semantic_model_core(
                    str(recovery.workspace_id)
                )
            elif action == WorkspaceDataRecovery.RecoveryType.VIEW_REBUILD:
                result = await publication.rebuild_workspace_view_schema(
                    str(recovery.workspace_id), revive_retired=True
                )
            elif (
                recovery.recovery_type == WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD
                and surface["status"] == "ready"
                and surface.get("semantic_status") == "stale"
            ):
                # The catalog still serves but its latest build failed; chat asks for this (#714).
                result = await publication.rebuild_workspace_semantic_model_core(
                    str(recovery.workspace_id)
                )
            elif surface["status"] == "ready":
                result = {"status": "already_recovered"}
            else:
                result = {"error": surface["message"]}

            final_surface = await recovery_query_surface(recovery)
            # A previously promoted Cube schema can remain readable after a
            # failed rebuild. Serving that fallback is safe, but it must not
            # turn an unsuccessful recovery attempt into a reported success.
            cube_result = result.get("cube_schema") or {}
            if cube_result.get(CAPACITY_REFUSED_KEY):
                result = {**result, CAPACITY_REFUSED_KEY: True}
            if (
                final_surface["status"] != "ready"
                or final_surface.get("recovery_action") is not None
                or result.get("error")
                or cube_result.get("ok") is False
            ):
                error = _workspace_recovery_error(result, final_surface)
                await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
                    state=WorkspaceDataRecovery.State.FAILED,
                    result=result,
                    error=error,
                    completed_at=timezone.now(),
                )
                return {"status": "failed", "error": error, "result": result}

            await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
                state=WorkspaceDataRecovery.State.COMPLETED,
                result=result,
                error="",
                completed_at=timezone.now(),
            )
            return {"status": "completed", "result": result}
    except Exception as exc:
        logger.exception("Workspace data recovery %s failed", recovery.id)
        if classify_capacity_error(exc) is not None:
            result = {**result, CAPACITY_REFUSED_KEY: True}
        error = str(exc)[:1000] or "Scout could not restore this artifact's data."
        await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
            state=WorkspaceDataRecovery.State.FAILED,
            result=result,
            error=error,
            completed_at=timezone.now(),
        )
        return {"status": "failed", "error": error, "result": result}


_CHAT_RECOVERY_NEEDS_RELOAD = (
    "The data model can't be rebuilt from the loaded data: it needs a data refresh first."
)


def _recovery_requester_denied_message(access: WorkspaceAccess | None) -> str:
    if access is not None and access.denied_reason == TENANT_ACCESS_LOST:
        projects = ", ".join(access.lost_tenant_names) or "this workspace's data sources"
        # Both causes stay named: a lost membership can't tell a disconnect from
        # upstream removal, and a different member may be reading this card.
        return (
            f"The requesting user no longer has access to: {projects} through a connected "
            "account. If they disconnected it, they should reconnect it in Settings → "
            "Connections; if their access was removed in the provider, an admin there "
            "must restore it."
        )
    if access is not None and access.denied_reason == NO_SOURCES:
        return NO_SOURCES_MESSAGE
    if access is not None and access.denied_reason in FRESHNESS_ERROR_CODES:
        return f"The requesting user's access could not be confirmed: {access_denied_body(access)['error']}"
    return _ROLE_DENIED_MESSAGE


@app.task
async def teardown_view_schema_task(view_schema_id: str) -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema and mark EXPIRED."""
    await retirement.teardown_view_schema(view_schema_id)


@app.task
async def teardown_schema(schema_id: str, attempt: int = 0) -> None:
    """Retire a tenant schema once nothing outside it depends on it, then mark EXPIRED."""
    await retirement.teardown_schema(schema_id, attempt)


@app.periodic(cron="*/15 * * * *")
@app.task
async def expire_stale_thread_jobs(timestamp: int = 0) -> dict:
    """Flip ThreadJobs that have been active too long and whose procrastinate
    job is no longer running. Fires the resume task so the user is not stuck
    with a phantom spinner.
    """
    return await sweep_stale_thread_jobs()


@app.periodic(cron="*/15 * * * *")
@app.task
async def expire_stale_workspace_data_recoveries(timestamp: int = 0) -> dict:
    """Release artifact recoveries stranded by a stopped background worker."""
    return await sweep_stale_workspace_data_recoveries()


@app.periodic(cron="*/15 * * * *")
@app.task
async def reconcile_stale_materialization_runs(timestamp: int = 0) -> dict:
    """Fail MaterializationRuns stuck ACTIVE after a hard worker death, then settle
    view schemas whose build will never finish."""
    return await sweep_stale_materialization_runs()


@app.periodic(cron="17 3 * * *")
@app.task
async def prune_old_procrastinate_jobs(timestamp: int = 0) -> dict:
    """Delete old finalized procrastinate jobs (and their events) so the queue
    tables don't grow without bound.

    Only 'succeeded' jobs are pruned (delete_old_jobs' default — failed/cancelled/
    aborted are retained): the reconciler treats an unknown job id as "can't tell,
    don't touch", so pruning a job still referenced by an active ThreadJob/
    MaterializationRun would strand it. The 7-day horizon is far longer than the
    15-minute stale-job janitor's window, so any active row referencing a succeeded
    job has long since been reconciled before its job becomes prunable (arch #255,
    10#0, reconciler↔retention coupling).
    """
    try:
        await app.job_manager.delete_old_jobs(nb_hours=JOB_RETENTION_HOURS)
    except Exception:
        logger.warning("prune_old_procrastinate_jobs: delete_old_jobs failed", exc_info=True)
        return {"pruned": False}
    logger.info(
        "prune_old_procrastinate_jobs: pruned succeeded jobs older than %sh", JOB_RETENTION_HOURS
    )
    return {"pruned": True}


# The scout-worker-silent alarm (infra/scout-stack.yml) fires on 15 minutes without a
# worker log line, and an idle worker's janitors log nothing, so it flapped all day.
# The lock stops ticks piling up behind a long job: one queued tick is enough.
@app.periodic(cron="*/5 * * * *")
@app.task(queueing_lock="log_worker_keepalive")
async def log_worker_keepalive(timestamp: int = 0) -> None:
    """Log one line, so a worker that is running its jobs is never silent."""
    logger.info("worker keepalive: periodic jobs are running")


@app.task(pass_context=True)
async def resume_thread_after_materialization(
    context, thread_job_id: str, busy_attempt: int = 0
) -> dict:
    """Inject a system-framed message into the LangGraph conversation and
    re-invoke the agent so it can respond to the original request with the
    now-loaded data.

    Runs only while holding the thread's turn lease, so it never writes the
    checkpoint while a live chat turn is streaming on the same thread; a busy
    thread re-queues it with backoff (arch review R08).
    """
    return await continuation.resume_thread_job(thread_job_id, busy_attempt)


@app.task
async def flush_pending_requests(workspace_id: str) -> dict:
    """Send the workspace's held requests that no load of their own will send.

    Runs when a load of the workspace ends, and from the minute sweep. A load
    still under way flushes them itself when it ends.
    """
    return await continuation.flush_workspace_requests(workspace_id)


@app.periodic(cron="* * * * *")
@app.task
async def sweep_pending_requests(timestamp: int = 0) -> dict:
    """Backstop for a flush that never ran: a lost defer, or a hold racing a load's end.

    Queues a flush per workspace rather than running them, so one slow answer
    never holds a worker across workspaces; the queueing lock dedupes them.
    """
    return await continuation.sweep_pending_requests()


@app.periodic(cron="*/15 * * * *")
@app.task
async def prune_resume_streams(timestamp: int = 0) -> dict:
    """Drop streamed resume text old enough that no chat still tails it."""
    return {"deleted": await resume_stream.aprune()}
