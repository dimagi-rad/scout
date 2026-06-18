"""
Deterministic tests for the PR-based curation gate (cube_curation_pr).

NO real git/gh commands are executed — all subprocess calls go through an
injected fake runner that records every command called.

The test also uses a temp directory as ``repo_root`` so the function never
reads or writes to the real working tree.

Assertions
----------
1. With 1 CubeFile (clean tree):
   a. A status-check + branch+commit+push+``gh pr create`` sequence was issued
      through the runner (in that order; no merge commands anywhere).
   b. The proposed YAML was written under ``cube/model/…`` inside the temp root.
   c. The returned dict contains ``"branch"`` and ``"pr_url"``.
   d. NO merge command (``git merge`` / ``gh pr merge``) was issued.

2. Empty ``cube_files``:
   - Runner is never called.
   - Returns ``{"branch": None, "pr_url": None}``.

3. Dirty tree:
   - open_curation_pr raises RuntimeError before any branch is created.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.transformations.services.cube_curation_pr import open_curation_pr

# ---------------------------------------------------------------------------
# Fake runner
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    """Mimics subprocess.CompletedProcess for the runner interface."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    """Records all commands passed to it and returns canned results.

    Parameters
    ----------
    gh_pr_url:
        URL returned by the simulated ``gh pr create`` call.
    dirty_tree:
        When True, ``git status --porcelain`` returns non-empty output so that
        the clean-tree precheck raises ``RuntimeError``.  Defaults to False
        (clean tree).
    """

    def __init__(
        self,
        gh_pr_url: str = "https://github.com/org/repo/pull/42",
        *,
        dirty_tree: bool = False,
    ):
        self._gh_pr_url = gh_pr_url
        self._dirty_tree = dirty_tree
        self.calls: list[list[str]] = []

    def __call__(
        self,
        cmd: list[str],
        *,
        capture_output: bool = False,
        cwd: str | None = None,
        check: bool = True,
    ) -> FakeResult:
        self.calls.append(list(cmd))
        # Simulate dirty/clean working tree for ``git status --porcelain``
        if cmd[:2] == ["git", "status"] and "--porcelain" in cmd:
            stdout = " M modified_file.py\n" if self._dirty_tree else ""
            return FakeResult(stdout=stdout)
        # For ``gh pr create`` return a fake PR URL
        if len(cmd) >= 3 and cmd[:3] == ["gh", "pr", "create"]:
            return FakeResult(stdout=self._gh_pr_url)
        return FakeResult()

    def commands(self) -> list[str]:
        """Return the first token of each recorded call for easy assertions."""
        return [" ".join(c) for c in self.calls]

    def has_any_merge(self) -> bool:
        """Return True if any recorded command is a git merge or gh pr merge operation.

        Checks only the command verb and its first subcommand tokens (not body text,
        which may legitimately contain the word "merge" as a user-facing instruction).
        """
        for cmd in self.calls:
            if len(cmd) < 2:
                continue
            # git merge …
            if cmd[0] == "git" and cmd[1] == "merge":
                return True
            # gh pr merge …
            if cmd[0] == "gh" and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "merge":
                return True
        return False


# ---------------------------------------------------------------------------
# Minimal CubeFile-like object (matches real CubeFile dataclass interface)
# ---------------------------------------------------------------------------


@dataclass
class CubeFile:
    path: str
    yaml: str


# ---------------------------------------------------------------------------
# Test 1: happy path — one CubeFile
# ---------------------------------------------------------------------------


def test_open_curation_pr_creates_branch_and_pr(tmp_path: Path) -> None:
    """
    Given one CubeFile, open_curation_pr must:
      (a) issue git checkout -b <branch>, git add, git commit, git push,
          then gh pr create — in that order.
      (b) write the YAML under cube/model/… inside tmp_path.
      (c) return a dict with non-empty "branch" and "pr_url".
      (d) NEVER issue any merge command.
    """
    runner = FakeRunner(gh_pr_url="https://github.com/test/repo/pull/99")

    proposed_yaml = """\
cubes:
  - name: visits
    sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.stg_visits"
    measures:
      - name: total_visits
        type: count
        title: "Total Visits"
"""
    cube_file = CubeFile(
        path="cube/model/proposals/visits.yml",
        yaml=proposed_yaml,
    )

    workspace_id = "aaaabbbb-0000-1111-2222-333333333333"
    branch_name = "cube/proposed-measures-aaaabbbb-20240101-120000"

    result = open_curation_pr(
        [cube_file],
        workspace_id=workspace_id,
        branch_name=branch_name,
        base_branch="main",
        summary="Gap: count visits per FLW.",
        runner=runner,
        repo_root=str(tmp_path),
    )

    # (a) Correct sequence of git/gh commands
    calls = runner.calls
    assert len(calls) == 6, f"Expected 6 calls, got {len(calls)}: {calls}"

    # Step 0: git status --porcelain (clean-tree precheck)
    assert calls[0][0] == "git"
    assert calls[0][1] == "status"
    assert "--porcelain" in calls[0]

    # Step 1: git checkout -b <branch>
    assert calls[1][0] == "git"
    assert calls[1][1] == "checkout"
    assert calls[1][2] == "-b"
    assert calls[1][3] == branch_name

    # Step 2: git add
    assert calls[2][0] == "git"
    assert calls[2][1] == "add"

    # Step 3: git commit
    assert calls[3][0] == "git"
    assert calls[3][1] == "commit"
    assert any(workspace_id in arg for arg in calls[3]), (
        f"commit message should mention workspace_id; got {calls[3]}"
    )

    # Step 4: git push --set-upstream origin <branch>
    assert calls[4][0] == "git"
    assert calls[4][1] == "push"
    assert "--set-upstream" in calls[4]
    assert branch_name in calls[4]

    # Step 5: gh pr create
    assert calls[5][0] == "gh"
    assert calls[5][1] == "pr"
    assert calls[5][2] == "create"
    assert "--base" in calls[5]
    assert "main" in calls[5]

    # (b) YAML was written into tmp_path/cube/model/proposals/visits.yml
    target = tmp_path / "cube" / "model" / "proposals" / "visits.yml"
    assert target.exists(), f"Expected file {target} to exist"
    assert "total_visits" in target.read_text()

    # (c) Return dict has branch and pr_url
    assert result["branch"] == branch_name
    assert result["pr_url"] == "https://github.com/test/repo/pull/99"

    # (d) NEVER issues a merge command
    assert not runner.has_any_merge(), (
        f"A merge command was found in runner calls: {runner.calls}"
    )


# ---------------------------------------------------------------------------
# Test 2: empty cube_files → no side effects
# ---------------------------------------------------------------------------


def test_open_curation_pr_empty_files_returns_early(tmp_path: Path) -> None:
    """
    When cube_files is empty, open_curation_pr must:
    - Never call the runner.
    - Return {"branch": None, "pr_url": None}.
    """
    runner = FakeRunner()

    result = open_curation_pr(
        [],
        workspace_id="00000000-0000-0000-0000-000000000000",
        branch_name="cube/proposed-measures-never",
        runner=runner,
        repo_root=str(tmp_path),
    )

    # No runner calls at all
    assert runner.calls == [], f"Expected no runner calls, got: {runner.calls}"

    # Result is a clear "nothing to propose" shape
    assert result == {"branch": None, "pr_url": None}


# ---------------------------------------------------------------------------
# Test 3: never-merge guarantee — explicit check on all possible call content
# ---------------------------------------------------------------------------


def test_open_curation_pr_never_issues_merge(tmp_path: Path) -> None:
    """
    Regardless of how many files are provided, no call through the runner
    should ever contain the word 'merge'.
    """
    runner = FakeRunner()
    cube_file = CubeFile(
        path="cube/model/proposals/flws.yml",
        yaml="cubes:\n  - name: flws\n    sql_table: \"{COMPILE_CONTEXT.security_context.schema_name}.stg_flws\"\n",
    )
    open_curation_pr(
        [cube_file],
        workspace_id="deadbeef-0000-0000-0000-000000000000",
        branch_name="cube/proposed-measures-deadbeef-20240202",
        runner=runner,
        repo_root=str(tmp_path),
    )

    assert not runner.has_any_merge(), (
        f"Merge command found — curation gate must NEVER auto-merge. "
        f"Commands issued: {runner.calls}"
    )


# ---------------------------------------------------------------------------
# Test 4: dirty working tree → RuntimeError before branch creation
# ---------------------------------------------------------------------------


def test_open_curation_pr_dirty_tree_raises(tmp_path: Path) -> None:
    """
    When the working tree is dirty, open_curation_pr must:
    - Raise RuntimeError with a clear message before creating any branch.
    - Never call git checkout -b or any subsequent command.
    """
    runner = FakeRunner(dirty_tree=True)
    cube_file = CubeFile(
        path="cube/model/proposals/visits.yml",
        yaml="cubes:\n  - name: visits\n    sql_table: x\n",
    )

    with pytest.raises(RuntimeError, match="working tree must be clean"):
        open_curation_pr(
            [cube_file],
            workspace_id="aaaabbbb-0000-1111-2222-333333333333",
            branch_name="cube/proposed-measures-aaaabbbb-20240101-120000",
            runner=runner,
            repo_root=str(tmp_path),
        )

    # Only the status precheck call should have been made; no branch was created.
    assert len(runner.calls) == 1, (
        f"Expected only 1 runner call (git status), got {len(runner.calls)}: {runner.calls}"
    )
    assert runner.calls[0][:2] == ["git", "status"]
