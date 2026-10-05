"""Workspace tasks are enqueued by registered name through task_dispatch, never imported.

No production module in apps/, config/ or mcp_server/ (management commands included)
may import apps.workspaces.tasks; procrastinate's Django autodiscovery loads it by
name in every process. Only tests are exempt. A caller that needs a task enqueued
adds a function to task_dispatch instead.
"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from procrastinate.exceptions import TaskNotFound

from apps.workspaces import task_dispatch
from apps.workspaces import tasks as workspace_tasks
from config.procrastinate import app

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_ROOTS = ("apps", "config", "mcp_server")
TASKS_MODULE = workspace_tasks.__name__
TASKS_FILE = Path(*TASKS_MODULE.split(".")).with_suffix(".py")


def _is_test_file(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_") or path.name == "conftest.py"


def _production_modules() -> list[Path]:
    return sorted(
        path.relative_to(REPO_ROOT)
        for root in PRODUCTION_ROOTS
        for path in (REPO_ROOT / root).rglob("*.py")
        if not _is_test_file(path.relative_to(REPO_ROOT))
    )


def _absolute(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _imports_tasks_module(tree: ast.AST, package: str = "") -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == TASKS_MODULE for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            if module == TASKS_MODULE:
                return True
            if any(f"{module}.{alias.name}" == TASKS_MODULE for alias in node.names):
                return True
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == TASKS_MODULE
        ):
            # importlib.import_module("apps.workspaces.tasks") and the like.
            return True
    return False


def _package(path: Path) -> str:
    return ".".join(path.with_suffix("").parts[:-1])


def test_the_guard_sees_the_production_tree():
    modules = _production_modules()
    assert TASKS_FILE in modules
    assert Path("apps/workspaces/task_dispatch.py") in modules
    assert not any(_is_test_file(path) for path in modules)


@pytest.mark.parametrize(
    "source",
    [
        "import apps.workspaces.tasks",
        "from apps.workspaces.tasks import materialize_workspace",
        "from apps.workspaces import tasks",
        "def f():\n    from apps.workspaces.tasks import materialize_workspace",
    ],
)
def test_the_guard_recognizes_each_import_form(source):
    assert _imports_tasks_module(ast.parse(source))


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("from . import tasks", "apps.workspaces"),
        ("from .tasks import materialize_workspace", "apps.workspaces"),
        ("from ..tasks import materialize_workspace", "apps.workspaces.services"),
        ("from ... import workspaces", "apps.workspaces.api"),
    ],
)
def test_the_guard_resolves_relative_imports(source, package):
    expected = source != "from ... import workspaces"
    assert _imports_tasks_module(ast.parse(source), package) is expected


def test_the_guard_flags_a_dynamic_import_by_name():
    assert _imports_tasks_module(
        ast.parse('importlib.import_module("apps.workspaces.tasks")'), "apps.common"
    )


def test_no_production_module_imports_workspace_tasks():
    offenders = [
        str(path)
        for path in _production_modules()
        if path != TASKS_FILE
        and _imports_tasks_module(ast.parse((REPO_ROOT / path).read_text()), _package(path))
    ]
    assert offenders == []


@pytest.mark.parametrize("name", task_dispatch.DISPATCHED_TASK_NAMES)
def test_each_dispatched_name_resolves_to_its_registered_task(name):
    assert name.startswith(f"{TASKS_MODULE}.")
    assert app.tasks[name] is getattr(workspace_tasks, name.removeprefix(f"{TASKS_MODULE}."))


def test_an_unknown_name_raises_instead_of_queueing_an_orphan_row():
    with pytest.raises(TaskNotFound):
        task_dispatch._task(f"{TASKS_MODULE}.no_such_task")


def test_the_tasks_module_registers_its_blocking_materializer():
    assert task_dispatch._inline["materialize"] is workspace_tasks.materialize_workspace_blocking


@pytest.mark.asyncio
async def test_inline_materialization_delegates_to_the_registered_runner(monkeypatch):
    run = AsyncMock(return_value={"all_succeeded": True})
    monkeypatch.setitem(task_dispatch._inline, "materialize", run)

    result = await task_dispatch.materialize_workspace_inline("ws-1", "user-1", 7)

    assert result == {"all_succeeded": True}
    run.assert_awaited_once_with("ws-1", "user-1", 7)
