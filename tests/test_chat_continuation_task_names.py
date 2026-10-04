"""Chat continuation re-queues workspace tasks by name, since it cannot import them."""

import ast
from pathlib import Path

import pytest

from apps.chat.services.continuation import FLUSH_TASK_NAME, RESUME_TASK_NAME
from apps.workspaces import tasks as workspace_tasks
from config.procrastinate import app

REPO_ROOT = Path(__file__).resolve().parent.parent
# tasks.py imports these, so a task import back would be a cycle.
TASK_INDEPENDENT_MODULES = [
    "apps/chat/services/agent_execution.py",
    "apps/chat/services/continuation.py",
    "apps/workspaces/services/load_outcome.py",
]


def test_continuation_names_the_registered_resume_and_flush_tasks():
    assert app.tasks[RESUME_TASK_NAME] is workspace_tasks.resume_thread_after_materialization
    assert app.tasks[FLUSH_TASK_NAME] is workspace_tasks.flush_pending_requests


@pytest.mark.parametrize("path", TASK_INDEPENDENT_MODULES)
def test_module_does_not_import_workspace_tasks(path):
    imported = set()
    for node in ast.walk(ast.parse((REPO_ROOT / path).read_text())):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert workspace_tasks.__name__ not in imported
