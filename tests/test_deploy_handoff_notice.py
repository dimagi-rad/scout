"""An interrupted backend handoff must leave actionable, safe operator guidance."""

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _notice(destination):
    filename = "deploy-staging.yml" if destination == "staging" else "deploy.yml"
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / filename).read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    matching = [step for step in steps if step.get("name") == "Report interrupted backend handoff"]
    assert len(matching) == 1, f"{destination} must report an interrupted backend handoff"
    assert (
        next(step for step in steps if step.get("name") == "Drain old workers")["id"]
        == "drain_workers"
    )
    assert (
        next(step for step in steps if step.get("name") == "Deploy Worker")["id"] == "deploy_worker"
    )
    return matching[0]


def _condition_value(expression, *, failed, cancelled, drain, worker):
    """Evaluate only boolean/equality nodes from the actual GitHub condition."""
    expression = expression.strip().removeprefix("${{").removesuffix("}}")
    expression = expression.replace("failure()", repr(failed)).replace(
        "cancelled()", repr(cancelled)
    )
    expression = expression.replace("steps.drain_workers.outcome", repr(drain))
    expression = expression.replace("steps.deploy_worker.outcome", repr(worker))
    expression = re.sub(r"\s+", " ", expression.replace("&&", "and").replace("||", "or")).strip()

    def value(node):
        if isinstance(node, ast.Constant):
            assert type(node.value) in {bool, str}
            return node.value
        if isinstance(node, ast.BoolOp):
            assert isinstance(node.op, (ast.And, ast.Or))
            values = [value(item) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            assert len(node.ops) == len(node.comparators) == 1
            assert isinstance(node.ops[0], (ast.Eq, ast.NotEq))
            equal = value(node.left) == value(node.comparators[0])
            return equal if isinstance(node.ops[0], ast.Eq) else not equal
        raise AssertionError(f"Unexpected condition node: {ast.dump(node)}")

    return value(ast.parse(expression, mode="eval").body)


@pytest.mark.parametrize("destination", ["production", "staging"])
@pytest.mark.parametrize(
    ("failed", "cancelled", "drain", "worker", "expected"),
    [
        (False, False, "success", "success", False),
        (True, False, "skipped", "skipped", False),
        (True, False, "", "", False),
        (False, True, "skipped", "skipped", False),
        (True, False, "success", "success", False),
        (True, False, "success", "skipped", True),
        (True, False, "success", "failure", True),
        (True, False, "failure", "skipped", True),
        (False, True, "cancelled", "skipped", True),
        (False, True, "success", "cancelled", True),
    ],
)
def test_notice_only_runs_for_an_interrupted_handoff(
    destination, failed, cancelled, drain, worker, expected
):
    notice = _notice(destination)
    assert (
        _condition_value(
            notice["if"], failed=failed, cancelled=cancelled, drain=drain, worker=worker
        )
        is expected
    )


@pytest.mark.parametrize("destination", ["production", "staging"])
def test_notice_emits_annotation_and_summary_without_running_remote_commands(destination, tmp_path):
    notice = _notice(destination)
    summary = tmp_path / "summary.md"
    result = subprocess.run(  # noqa: S603 - checked-in notice, no credentials and no external commands in PATH
        ["/bin/bash", "-euo", "pipefail", "-c", notice["run"]],
        env={"PATH": str(tmp_path), "GITHUB_STEP_SUMMARY": os.fspath(summary)},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "::error title=Backend handoff interrupted::" in result.stdout
    guidance = summary.read_text()
    assert destination in guidance
    assert "may be draining or stopped" in guidance
    assert "queued jobs may be paused" in guidance
    assert f".scout-worker-drains-v1/{destination}/" in guidance
    assert "Inspect worker state, in-flight jobs, and pending receipts" in guidance
    assert "roll forward" in guidance
    assert "Do not blindly reboot old workers" in guidance
    assert "delete receipts" in guidance
