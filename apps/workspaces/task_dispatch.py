"""The stable way to enqueue a workspace task: resolved by registered name, never imported.

``apps.workspaces.tasks`` imports most of the application (chat continuation, the
agent graph, pipelines), so a module that imported a task object just to defer it
joined that import graph and, for the agent tools, closed a cycle. Every enqueue
from outside the tasks module goes through here instead.

The names below are persisted queue identities: queued rows and periodic
bookkeeping refer to them, so they must never change (tests/test_workspace_task_registry.py).
Resolution passes ``allow_unknown=False``, so a name that matches no registered task
raises ``TaskNotFound`` instead of queueing a row no worker will ever run.

Each function writes exactly the row its call sites wrote before this module
existed (tests/test_enqueue_call_sites.py): an optional task argument is only sent
when it differs from the task's default, matching what callers always did.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from procrastinate.jobs import JobDeferrer

from config.procrastinate import app

_PREFIX = "apps.workspaces.tasks."

DROP_FAILED_REFRESH_SCHEMA = _PREFIX + "drop_failed_refresh_schema"
FLUSH_PENDING_REQUESTS = _PREFIX + "flush_pending_requests"
MATERIALIZE_WORKSPACE = _PREFIX + "materialize_workspace"
REBUILD_WORKSPACE_SEMANTIC_MODEL = _PREFIX + "rebuild_workspace_semantic_model"
REBUILD_WORKSPACE_VIEW_SCHEMA = _PREFIX + "rebuild_workspace_view_schema"
RECOVER_WORKSPACE_DATA = _PREFIX + "recover_workspace_data"
REFRESH_TENANT_SCHEMA = _PREFIX + "refresh_tenant_schema"
RESUME_THREAD_AFTER_MATERIALIZATION = _PREFIX + "resume_thread_after_materialization"
TEARDOWN_VIEW_SCHEMA = _PREFIX + "teardown_view_schema_task"

DISPATCHED_TASK_NAMES = (
    DROP_FAILED_REFRESH_SCHEMA,
    FLUSH_PENDING_REQUESTS,
    MATERIALIZE_WORKSPACE,
    REBUILD_WORKSPACE_VIEW_SCHEMA,
    RECOVER_WORKSPACE_DATA,
    REFRESH_TENANT_SCHEMA,
    RESUME_THREAD_AFTER_MATERIALIZATION,
    TEARDOWN_VIEW_SCHEMA,
)

# procrastinate_jobs / procrastinate_events grow unbounded otherwise: ~2,000 janitor
# jobs/day plus every materialization/teardown/rebuild/resume. Keep finalized jobs
# for a week (forensics + idempotency headroom) then prune (arch #255, 10#0).
# Refresh settlement relies on it to tell a pruned row from a missing one.
JOB_RETENTION_HOURS = 24 * 7


def _task(
    name: str, *, queueing_lock: str | None = None, schedule_in: dict | None = None
) -> JobDeferrer:
    options: dict[str, Any] = {}
    if queueing_lock:
        options["queueing_lock"] = queueing_lock
    if schedule_in:
        options["schedule_in"] = schedule_in
    return app.configure_task(name, allow_unknown=False, **options)


def _materialize_args(
    workspace_id: str,
    user_id: str,
    load_intent: dict | None,
    only_unserved: bool,
    notify_thread: bool,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "load_intent": load_intent,
    }
    if only_unserved:
        args["only_unserved"] = True
    if not notify_thread:
        args["notify_thread"] = False
    return args


def defer_materialize_workspace(
    *,
    workspace_id: str,
    user_id: str,
    load_intent: dict | None,
    only_unserved: bool = False,
    notify_thread: bool = True,
    queueing_lock: str | None = None,
    schedule_in: dict | None = None,
) -> int:
    return _task(MATERIALIZE_WORKSPACE, queueing_lock=queueing_lock, schedule_in=schedule_in).defer(
        **_materialize_args(workspace_id, user_id, load_intent, only_unserved, notify_thread)
    )


async def adefer_materialize_workspace(
    *,
    workspace_id: str,
    user_id: str,
    load_intent: dict | None,
    only_unserved: bool = False,
    notify_thread: bool = True,
) -> int:
    return await _task(MATERIALIZE_WORKSPACE).defer_async(
        **_materialize_args(workspace_id, user_id, load_intent, only_unserved, notify_thread)
    )


def defer_rebuild_workspace_view_schema(*, workspace_id: str) -> int:
    return _task(REBUILD_WORKSPACE_VIEW_SCHEMA).defer(workspace_id=workspace_id)


def defer_teardown_view_schema(*, view_schema_id: str) -> int:
    return _task(TEARDOWN_VIEW_SCHEMA).defer(view_schema_id=view_schema_id)


def defer_refresh_tenant_schema(
    *, schema_id: str, membership_id: str, actor_user_id: str, workspace_id: str
) -> int:
    return _task(REFRESH_TENANT_SCHEMA).defer(
        schema_id=schema_id,
        membership_id=membership_id,
        actor_user_id=actor_user_id,
        workspace_id=workspace_id,
    )


def defer_drop_failed_refresh_schema(*, schema_id: str) -> int:
    return _task(DROP_FAILED_REFRESH_SCHEMA).defer(schema_id=schema_id)


def defer_recover_workspace_data(*, recovery_id: str) -> int:
    return _task(RECOVER_WORKSPACE_DATA).defer(recovery_id=recovery_id)


async def adefer_recover_workspace_data(*, recovery_id: str) -> int:
    return await _task(RECOVER_WORKSPACE_DATA).defer_async(recovery_id=recovery_id)


async def adefer_resume_thread(
    *,
    thread_job_id: str,
    busy_attempt: int = 0,
    queueing_lock: str | None = None,
    schedule_in: dict | None = None,
) -> int:
    args: dict[str, Any] = {"thread_job_id": thread_job_id}
    if busy_attempt:
        args["busy_attempt"] = busy_attempt
    task = _task(
        RESUME_THREAD_AFTER_MATERIALIZATION, queueing_lock=queueing_lock, schedule_in=schedule_in
    )
    return await task.defer_async(**args)


async def adefer_flush_pending_requests(
    *, workspace_id: str, queueing_lock: str, schedule_in: dict
) -> int:
    task = _task(FLUSH_PENDING_REQUESTS, queueing_lock=queueing_lock, schedule_in=schedule_in)
    return await task.defer_async(workspace_id=workspace_id)


InlineMaterializer = Callable[[str, str, int | None], Awaitable[dict]]
_inline: dict[str, InlineMaterializer] = {}


def register_inline_materializer(run: InlineMaterializer) -> None:
    """Called by the tasks module at import; procrastinate's autodiscovery imports it
    in every process once Django is ready, before any request or job runs."""
    _inline["materialize"] = run


async def materialize_workspace_inline(
    workspace_id: str, user_id: str = "", job_id: int | None = None
) -> dict:
    """Run a blocking materialization in this process (the agent's load tool).

    Not a queue dispatch: the caller waits for the summary. The runner lives with
    the task bodies, so it is reached through registration rather than an import.
    """
    app.perform_import_paths()
    try:
        run = _inline["materialize"]
    except KeyError:
        raise RuntimeError("apps.workspaces.tasks has not registered its materializer") from None
    return await run(workspace_id, user_id, job_id)
