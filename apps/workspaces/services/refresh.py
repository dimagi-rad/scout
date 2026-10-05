"""Tenant refreshes: load a claimed refresh candidate, publish it, move its dependents.

The registered ``refresh_tenant_schema`` task is a thin adapter over
``refresh_tenant_schema`` here.
"""

import logging

from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantMembership
from apps.users.services.credential_resolver import (
    CredentialResolutionError,
    aresolve_credential,
)
from apps.workspaces.access import (
    WorkspaceAccess,
    access_denied_body,
    aresolve_workspace_access_ex,
)
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services import publication, retirement
from apps.workspaces.services.access_freshness import (
    FRESHNESS_ERROR_CODES,
    VerificationBudget,
)
from apps.workspaces.services.data_operation import (
    DataLockTimeout,
    run_data_thread,
    tenant_data_lock,
)
from apps.workspaces.services.data_operation import (
    to_thread_fresh_db as _to_thread_fresh_db,
)
from apps.workspaces.services.load_candidates import (
    Promotion,
)
from apps.workspaces.services.load_generations import (
    begin_load_generation,
    end_load_generation,
)
from apps.workspaces.services.materialize import drain
from apps.workspaces.services.pipeline_resolver import no_pipeline_message
from apps.workspaces.services.refresh_requests import (
    DENIED_MEMBERSHIP_MISSING,
    DENIED_ROLE_REQUIRED,
    DENIED_WORKSPACE_UNLINKED,
    claim_refresh_candidate,
)
from apps.workspaces.services.schema_manager import (
    SchemaManager,
)
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import (
    run_pipeline,
)

logger = logging.getLogger(__name__)


async def refresh_tenant_schema(
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
                await drain(
                    retirement.drop_claimed_refresh_schema_and_fail(new_schema, context.job.id),
                    new_schema,
                )
                return _refresh_denial_result(DENIED_MEMBERSHIP_MISSING)
            if not await WorkspaceTenant.objects.filter(
                workspace_id=new_schema.refresh_workspace_id,
                tenant_id=new_schema.tenant_id,
            ).aexists():
                await drain(
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
        await drain(
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
        await drain(retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema)
        return {"error": "Failed to create schema"}
    except BaseException:
        await drain(retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema)
        raise

    generation_result = {}

    def begin_and_remember():
        generation = begin_load_generation(new_schema.tenant_id)
        generation_result["generation"] = generation
        return generation

    try:
        credential = await aresolve_credential(membership)
        if credential is None:
            await drain(
                retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
            )
            return {"error": "No credential available"}

        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}
        pipeline_name = provider_pipeline_map.get(membership.tenant.provider)
        if pipeline_name is None:
            await drain(
                retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema
            )
            return {"error": no_pipeline_message(registry, membership.tenant.provider)}
        pipeline_config = registry.get(pipeline_name)
        generation = await _to_thread_fresh_db(begin_and_remember)
    except CredentialResolutionError as e:
        await drain(retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema)
        return {"error": e.message, "error_code": e.code}
    except Exception:
        logger.exception("Failed to start refresh for schema '%s'", new_schema.schema_name)
        await drain(retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id), new_schema)
        return {"error": "Failed to start the refresh", "retry_required": True}
    except BaseException:
        # An abort may hide the return value after the generation commits.
        cleanup = (
            _end_refresh_load(new_schema, job_id, generation_result["generation"])
            if "generation" in generation_result
            else retirement.drop_claimed_refresh_schema_and_fail(new_schema, job_id)
        )
        await drain(cleanup, new_schema)
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
        await drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
        return {"error": "Materialization failed"}
    except BaseException:
        await drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
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
        await drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
        raise
    if not promotion.promoted:
        try:
            still_ours = await TenantSchema.objects.filter(
                id=new_schema.id,
                state=SchemaState.PROVISIONING,
                refresh_job_id=job_id,
            ).aexists()
        finally:
            await drain(_end_refresh_load(new_schema, job_id, generation), new_schema)
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
    await drain(retirement.drop_claimed_refresh_schema_and_fail(schema, job_id), schema)
    if access.denied_reason in FRESHNESS_ERROR_CODES:
        return _refresh_denial_result(access.denied_reason)
    return _refresh_denial_result(DENIED_ROLE_REQUIRED)


async def _end_refresh_load(schema, job_id: int, generation: int) -> None:
    try:
        await _to_thread_fresh_db(end_load_generation, schema.tenant_id, generation)
    finally:
        # Paired: a failure clearing the marker must not leave the candidate live.
        await retirement.drop_claimed_refresh_schema_and_fail(schema, job_id)


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
