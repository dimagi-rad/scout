"""Registered procrastinate names, arguments and schedules are persisted in queue rows.

Jobs already queued (and cron ``periodic_id`` bookkeeping) refer to tasks by name,
so moving task bodies between modules must never rename or re-sign a registered task.
"""

import ast
import inspect
from pathlib import Path

import pytest

from apps.workspaces import tasks as workspace_tasks
from apps.workspaces.services.reconciliation import RESUME_TASK_NAME
from config.procrastinate import app

TASK_PREFIX = f"{workspace_tasks.__name__}."

EXPECTED_SIGNATURES = {
    "drop_abandoned_candidate": (
        "(schema_id: str, attempt: int = 0, last_attempt_at: str = '', "
        "load_job_id: int | None = None, busy_count: int = 0) -> None"
    ),
    "drop_failed_refresh_schema": "(schema_id: str) -> None",
    "expire_inactive_schemas": "(timestamp: int = 0) -> None",
    "expire_stale_thread_jobs": "(timestamp: int = 0) -> dict",
    "expire_stale_workspace_data_recoveries": "(timestamp: int = 0) -> dict",
    "materialize_workspace": (
        "(context, workspace_id: str, user_id: str = '', load_intent: dict | None = None, "
        "only_unserved: bool = False, notify_thread: bool = True) -> dict"
    ),
    "prune_old_procrastinate_jobs": "(timestamp: int = 0) -> dict",
    "rebuild_workspace_semantic_model": "(workspace_id: str) -> dict",
    "rebuild_workspace_view_schema": "(workspace_id: str, revive_retired: bool = False) -> dict",
    "reconcile_refresh_candidates": "(timestamp: int = 0) -> dict",
    "reconcile_stale_materialization_runs": "(timestamp: int = 0) -> dict",
    "recover_workspace_data": "(context, recovery_id: str) -> dict",
    "refresh_tenant_schema": (
        "(context, schema_id: str, membership_id: str, actor_user_id: str = '', "
        "workspace_id: str = '') -> dict"
    ),
    "resume_thread_after_materialization": (
        "(context, thread_job_id: str, busy_attempt: int = 0) -> dict"
    ),
    "sweep_workspace_load_candidates": "(timestamp: int = 0) -> dict",
    "teardown_schema": "(schema_id: str, attempt: int = 0) -> None",
    "teardown_view_schema_task": "(view_schema_id: str) -> None",
}

EXPECTED_CRONS = {
    "expire_inactive_schemas": "*/30 * * * *",
    "expire_stale_thread_jobs": "*/15 * * * *",
    "expire_stale_workspace_data_recoveries": "*/15 * * * *",
    "prune_old_procrastinate_jobs": "17 3 * * *",
    "reconcile_refresh_candidates": "*/15 * * * *",
    "reconcile_stale_materialization_runs": "*/15 * * * *",
    "sweep_workspace_load_candidates": "7,22,37,52 * * * *",
}


def test_registered_task_names_and_signatures_are_pinned():
    registered = {
        name.removeprefix(TASK_PREFIX): task
        for name, task in app.tasks.items()
        if name.startswith(TASK_PREFIX)
    }

    assert {name: str(inspect.signature(task.func)) for name, task in registered.items()} == (
        EXPECTED_SIGNATURES
    )


def test_reconciliation_reaches_the_resume_task_by_its_registered_name():
    assert app.tasks[RESUME_TASK_NAME] is workspace_tasks.resume_thread_after_materialization


def test_periodic_schedules_are_pinned():
    scheduled = {
        name.removeprefix(TASK_PREFIX): periodic.cron
        for (name, _periodic_id), periodic in app.periodic_registry.periodic_tasks.items()
        if name.startswith(TASK_PREFIX)
    }

    assert scheduled == EXPECTED_CRONS


# Modules that own logic on behalf of the task wrappers; a task import here would
# recreate the queue -> service -> queue cycle the wrappers exist to avoid.
TASK_INDEPENDENT_SERVICES = ["data_operation", "data_recovery", "reconciliation"]
SERVICES_DIR = Path(__file__).resolve().parent.parent / "apps" / "workspaces" / "services"


def _imports_tasks(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == workspace_tasks.__name__:
                return True
            if module == "apps.workspaces" and any(alias.name == "tasks" for alias in node.names):
                return True
        elif isinstance(node, ast.Import) and any(
            alias.name == workspace_tasks.__name__ for alias in node.names
        ):
            return True
    return False


@pytest.mark.parametrize("service", TASK_INDEPENDENT_SERVICES)
def test_service_does_not_import_workspace_tasks(service):
    tree = ast.parse((SERVICES_DIR / f"{service}.py").read_text())

    assert not _imports_tasks(tree)
