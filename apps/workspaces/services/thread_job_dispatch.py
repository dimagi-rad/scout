"""Queue a chat-bound materialization together with the ThreadJob that resumes it."""

import logging

from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from procrastinate.exceptions import AlreadyEnqueued

from apps.chat.models import ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import WorkspaceDataRecovery
from apps.workspaces.services.load_activity import aunserved_tenant_ids, aworkspace_load_pending
from apps.workspaces.services.load_generations import (
    INTENT_RECONCILE_MISSING,
    acapture_workspace_load_intent,
)
from apps.workspaces.services.query_state import (
    included_tenant_snapshot_state,
    semantic_layer_state,
    synced_runs,
    workspace_query_surface,
)
from apps.workspaces.tasks import (
    CHAT_RECOVERY_SOURCE,
    materialize_workspace,
    recover_workspace_data,
)
from mcp_server.pipeline_registry import get_registry

logger = logging.getLogger(__name__)

# The resume can't tell a live turn is still streaming on the same thread, and a
# load refused in preflight settles in about a second. The delay usually lets
# the chat's one-sentence acknowledgement finish first; it narrows the race
# rather than closing it (a slow or tool-calling turn can still overlap).
CHAT_LOAD_START_DELAY_SECONDS = 30


@sync_to_async
def adispatch_thread_materialization(
    *,
    thread_id,
    tool_call_id: str,
    workspace_id: str,
    user_id: str,
    load_intent: dict | None,
    queueing_lock: str | None = None,
    start_in_seconds: int = 0,
    only_unserved: bool = False,
) -> ThreadJob:
    """Defer ``materialize_workspace`` and create its PENDING ThreadJob in one commit.

    Procrastinate's Django connector queues on Django's connection, so no worker
    can see the job before its ThreadJob exists. Queued separately, a fast job
    could finish before the row committed and the resume lookup found nothing,
    leaving the chat waiting on the janitor (#365).
    """
    options = {}
    if queueing_lock:
        options["queueing_lock"] = queueing_lock
    if start_in_seconds:
        options["schedule_in"] = {"seconds": start_in_seconds}
    task = materialize_workspace.configure(**options) if options else materialize_workspace
    # Omitted when False so existing dispatches' job args stay unchanged.
    extra = {"only_unserved": True} if only_unserved else {}
    with transaction.atomic():
        job = task.defer(
            workspace_id=str(workspace_id),
            user_id=str(user_id) if user_id else "",
            load_intent=load_intent,
            **extra,
        )
        return ThreadJob.objects.create(
            thread_id=thread_id,
            job_type=ThreadJob.JobType.MATERIALIZATION,
            procrastinate_job_id=getattr(job, "id", job),
            tool_call_id=tool_call_id,
            state=ThreadJob.State.PENDING,
        )


async def astart_chat_load(*, workspace, user, thread_id) -> ThreadJob | None:
    """Load a workspace that serves no data yet as the chatting user, bound to their chat (#408).

    Only for a caller who may write: the worker refuses anyone else. A workspace
    that serves any source is left to the agent: its prompt says data is ready,
    and a load bound here would make the agent's own ``run_materialization``
    (a refresh the user asked for) report "already running" instead. Nothing is
    queued while a load of the workspace is already queued or running, whoever
    started it; the agent is told it is in progress instead. A chat that already
    had a load gets no second one: its resume reported how that went, and the
    agent decides whether to retry. Never raises: the chat still answers.
    """
    try:
        if not await _chat_load_needed(workspace, thread_id):
            return None
        try:
            load_intent = await acapture_workspace_load_intent(
                workspace.id, INTENT_RECONCILE_MISSING
            )
        except Exception:
            # Not fatal: without it the worker captures intent when the job starts.
            logger.exception("Could not capture load intent for workspace %s", workspace.id)
            load_intent = None
        return await adispatch_thread_materialization(
            thread_id=thread_id,
            tool_call_id="",
            workspace_id=workspace.id,
            user_id=user.id,
            load_intent=load_intent,
            # Two chats opening at once both pass the pending check.
            queueing_lock=f"chat-load:{workspace.id}",
            start_in_seconds=CHAT_LOAD_START_DELAY_SECONDS,
            # A source that starts serving before the job runs is published, not re-fetched.
            only_unserved=True,
        )
    except AlreadyEnqueued:
        return None
    except Exception:
        # The agent can start the load itself.
        logger.exception("Could not start the first load for workspace %s", workspace.id)
        return None


async def _chat_load_needed(workspace, thread_id) -> bool:
    if await ThreadJob.objects.filter(
        thread_id=thread_id, job_type=ThreadJob.JobType.MATERIALIZATION
    ).aexists():
        return False
    unserved = await aunserved_tenant_ids(workspace.id)
    if not unserved or len(unserved) < await workspace.workspace_tenants.acount():
        return False
    providers = {config.provider for config in get_registry().list()}
    if not await Tenant.objects.filter(id__in=unserved, provider__in=providers).aexists():
        # Every such load fails PIPELINE_UNRESOLVED; the prompt explains that instead.
        return False
    return not await aworkspace_load_pending(workspace.id)


async def astart_chat_semantic_rebuild(*, workspace, user) -> WorkspaceDataRecovery | None:
    """Rebuild a loaded workspace's data model when a chat finds it missing or failed (#714).

    Loaded data with no usable catalog needs a semantic rebuild, never a reload, so
    nobody is asked to approve one. The recovery task takes the workspace lock,
    waits out running loads, re-checks the requester's write access and the
    snapshot, and rebuilds only what is still needed. Only for a caller who may
    write. A member whose rebuild failed is not retried until the next sync, so a
    build that fails the same way does not re-run on every message. Never raises:
    the chat still answers.
    """
    try:
        if not await _semantic_rebuild_needed(workspace, user):
            return None
        return await _adispatch_semantic_rebuild(workspace_id=workspace.id, user_id=user.id)
    except IntegrityError:
        # Another recovery became active first; it rebuilds what is still needed.
        return None
    except Exception:
        logger.exception("Could not start the semantic rebuild for workspace %s", workspace.id)
        return None


async def _semantic_rebuild_needed(workspace, user) -> bool:
    # Cheapest first: this runs on every message a writer sends.
    semantic_state, _error = await semantic_layer_state(workspace)
    if semantic_state in {"ready", "deferred"}:
        return False
    if await aworkspace_load_pending(workspace.id):
        # A load, or another recovery, builds the catalog when it finishes.
        return False
    surface = await workspace_query_surface(workspace)
    missing = surface["recovery_action"] == WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD
    failed = surface["status"] == "ready" and surface["semantic_status"] == "stale"
    if not (missing or failed) or surface["in_progress"]:
        return False
    # The surface checks the snapshot only for a missing catalog; a stale one over a
    # failed load would just fail the rebuild again and hold the recovery slot.
    if failed and (
        await included_tenant_snapshot_state(workspace, surface["tenant_coverage"]) != "safe"
    ):
        return False
    last_sync = await (
        synced_runs()
        .filter(tenant_schema__tenant__workspace_tenants__workspace=workspace)
        .values_list("completed_at", flat=True)
        .afirst()
    )
    # Only this member's own failures count: another member's attempt may have
    # failed on their own access, which says nothing about this one's.
    failures = WorkspaceDataRecovery.objects.filter(
        workspace=workspace,
        requested_by=user,
        recovery_type=WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD,
        state=WorkspaceDataRecovery.State.FAILED,
    )
    if last_sync is not None:
        failures = failures.filter(created_at__gt=last_sync)
    return not await failures.aexists()


@sync_to_async
def _adispatch_semantic_rebuild(*, workspace_id, user_id) -> WorkspaceDataRecovery:
    # One commit, as for chat loads: a row left PENDING without its job would
    # hold the one-active-recovery slot and block every later repair.
    with transaction.atomic():
        recovery = WorkspaceDataRecovery.objects.create(
            workspace_id=workspace_id,
            requested_by_id=user_id,
            recovery_type=WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD,
            source_type=CHAT_RECOVERY_SOURCE,
        )
        job = recover_workspace_data.defer(recovery_id=str(recovery.id))
        recovery.procrastinate_job_id = getattr(job, "id", job)
        recovery.save(update_fields=["procrastinate_job_id"])
        return recovery
