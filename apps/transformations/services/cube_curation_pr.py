"""
PR-based curation gate for proposed Cube measures.

Given a list of CubeFile proposals (from measure_proposer.propose_measures), this module:
  1. Writes each file into ``cube/model/...`` (relative to the repo root).
  2. Creates a new git branch.
  3. Commits the proposed files.
  4. Pushes the branch.
  5. Opens a PR via ``gh pr create`` with a descriptive body.

The returned dict contains ``{"branch": str, "pr_url": str}``.

GOVERNANCE: This module NEVER merges or auto-merges. It only opens PRs for human review.

Injectable runner
-----------------
All subprocess calls go through a ``runner`` callable::

    runner(cmd: list[str], *, capture_output: bool = False, cwd: str | None = None,
           check: bool = True) -> subprocess.CompletedProcess

The default runner is a thin wrapper around ``subprocess.run``. Tests inject a
fake runner to record calls without executing real git/gh commands.

Public API
----------
    result = open_curation_pr(
        cube_files,
        workspace_id="some-uuid",
        branch_name="cube/proposed-measures-abc-20240101",
        base_branch="main",
        summary="Closes gap: X; learning: Y",
        runner=None,  # use real subprocess.run
    )
    # result: {"branch": str, "pr_url": str} or {"branch": None, "pr_url": None} if empty
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default subprocess runner
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root: .../scout/


def _default_runner(
    cmd: list[str],
    *,
    capture_output: bool = False,
    cwd: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run for all git/gh calls."""
    return subprocess.run(  # noqa: S603
        cmd,
        capture_output=capture_output,
        cwd=cwd or str(_REPO_ROOT),
        check=check,
        text=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def open_curation_pr(
    cube_files: list[Any],
    *,
    workspace_id: str,
    branch_name: str,
    base_branch: str = "main",
    summary: str = "",
    runner: Any = None,
    repo_root: str | None = None,
) -> dict:
    """Write proposed Cube files, create a branch, commit, push, and open a PR.

    Parameters
    ----------
    cube_files:
        List of CubeFile objects with ``.path`` (relative path) and ``.yaml`` (content).
        May also be plain dicts with ``{"path": ..., "yaml": ...}`` keys.
    workspace_id:
        UUID string of the workspace the proposals come from.
    branch_name:
        Git branch name to create (e.g. ``cube/proposed-measures-<ws>-<ts>``).
    base_branch:
        The base branch for the PR (default ``"main"``).
    summary:
        Human-readable description of what gaps/learnings motivated the proposals.
        Used as the PR body.
    runner:
        Injectable subprocess callable. Signature:
        ``runner(cmd, *, capture_output, cwd, check) -> CompletedProcess``.
        Defaults to a real ``subprocess.run`` wrapper.
    repo_root:
        Path to the git repository root. Defaults to the project repo root.
        Tests inject a temp dir here so no real files are written to the working tree.

    Returns
    -------
    dict
        ``{"branch": branch_name, "pr_url": <url>}`` on success.
        ``{"branch": None, "pr_url": None}`` when ``cube_files`` is empty.

    Raises
    ------
    RuntimeError
        If a required git/gh command fails.

    Notes
    -----
    This function NEVER calls ``git merge``, ``gh pr merge``, or any merge
    operation. The curation gate is a human review of the opened PR.
    """
    if not cube_files:
        logger.info("open_curation_pr: no cube_files provided — skipping branch/PR creation")
        return {"branch": None, "pr_url": None}

    if runner is None:
        runner = _default_runner

    root = Path(repo_root) if repo_root else _REPO_ROOT

    # -----------------------------------------------------------------------
    # 0. Precheck: require a clean working tree before creating a branch.
    #    A dirty tree causes `git checkout -b` to carry over uncommitted changes
    #    and makes the resulting PR diff misleading.  We abort early so the
    #    caller can stash/commit first.
    # -----------------------------------------------------------------------
    status_result = runner(
        ["git", "status", "--porcelain"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    if status_result.stdout.strip():
        raise RuntimeError(
            "working tree must be clean to open a curation PR — "
            f"uncommitted changes detected:\n{status_result.stdout.strip()}"
        )
    logger.debug("Working tree is clean; proceeding with branch creation.")

    # -----------------------------------------------------------------------
    # 1. Write proposed YAML files into the target paths
    # -----------------------------------------------------------------------
    written_paths: list[str] = []
    for cube_file in cube_files:
        # Accept both CubeFile dataclass and plain dict
        if hasattr(cube_file, "path"):
            rel_path = cube_file.path
            content = cube_file.yaml
        else:
            rel_path = cube_file["path"]
            content = cube_file["yaml"]

        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written_paths.append(rel_path)
        logger.debug("Wrote proposed Cube file: %s", rel_path)

    # -----------------------------------------------------------------------
    # 2. Create a new git branch
    # -----------------------------------------------------------------------
    runner(
        ["git", "checkout", "-b", branch_name],
        cwd=str(root),
        capture_output=False,
        check=True,
    )
    logger.info("Created branch: %s", branch_name)

    # -----------------------------------------------------------------------
    # 3. Stage and commit the proposed files
    # -----------------------------------------------------------------------
    runner(
        ["git", "add", "--", *written_paths],
        cwd=str(root),
        capture_output=False,
        check=True,
    )
    commit_message = (
        f"feat(cube): proposed measures for workspace {workspace_id}\n\n"
        f"{summary or 'Proposed by Scout measure proposer.'}"
    )
    runner(
        ["git", "commit", "-m", commit_message],
        cwd=str(root),
        capture_output=False,
        check=True,
    )
    logger.info("Committed %d proposed Cube file(s)", len(written_paths))

    # -----------------------------------------------------------------------
    # 4. Push the branch
    # -----------------------------------------------------------------------
    runner(
        ["git", "push", "--set-upstream", "origin", branch_name],
        cwd=str(root),
        capture_output=False,
        check=True,
    )
    logger.info("Pushed branch: %s", branch_name)

    # -----------------------------------------------------------------------
    # 5. Open a PR via gh (NEVER merges — human review only)
    # -----------------------------------------------------------------------
    pr_title = f"[Scout] Proposed Cube measures for workspace {workspace_id}"
    pr_body = _build_pr_body(workspace_id, written_paths, summary)

    result = runner(
        [
            "gh",
            "pr",
            "create",
            "--title",
            pr_title,
            "--body",
            pr_body,
            "--base",
            base_branch,
            "--head",
            branch_name,
        ],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    pr_url = result.stdout.strip()
    logger.info("Opened PR: %s", pr_url)

    return {"branch": branch_name, "pr_url": pr_url}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pr_body(workspace_id: str, written_paths: list[str], summary: str) -> str:
    """Build a descriptive PR body for human reviewers."""
    lines = [
        "## Scout: Proposed Cube Measures",
        "",
        "This PR was automatically generated by the **Scout measure proposer**.",
        "It proposes new Cube measures and dimensions derived from accumulated model gaps",
        "and agent learnings. **Merge this PR to add these measures to the governed model.**",
        "",
        f"**Workspace:** `{workspace_id}`",
        "",
    ]
    if summary:
        lines += [
            "## Motivation",
            "",
            summary,
            "",
        ]
    lines += [
        "## Proposed files",
        "",
    ]
    for path in written_paths:
        lines.append(f"- `{path}`")
    lines += [
        "",
        "---",
        "_Opened by Scout — review and merge to apply. Never auto-merged._",
    ]
    return "\n".join(lines)
