"""
Management command: propose_cube_measures

Reads accumulated ModelGapSignal and AgentLearning rows for a workspace, calls the
LLM-backed measure proposer (Task 13), and opens a GitHub PR with the proposed Cube
measures for human review (Task 14 curation gate).

Usage
-----
    python manage.py propose_cube_measures --workspace <UUID> [--base <branch>]

Prerequisites
-------------
- A clean git working tree (the command creates a branch and commits new files).
- ``gh`` authenticated (``gh auth login`` or ``GH_TOKEN`` env var set).
- The measure proposer and curation PR service must be importable.

This command does NOT run in tests. It shells out to real ``git`` and ``gh``.

Async
-----
``propose_measures`` is an async coroutine. This command drives it via
``asgiref.sync.async_to_sync`` so it runs inside Django's sync management command
context.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from apps.transformations.services.cube_curation_pr import open_curation_pr
from apps.transformations.services.measure_proposer import propose_measures
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[5]  # …/scout/


class Command(BaseCommand):
    help = (
        "Propose new Cube measures from accumulated model gaps and agent learnings, "
        "then open a GitHub PR for human review. Never auto-merges."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            required=True,
            metavar="UUID",
            help="UUID of the workspace to propose measures for.",
        )
        parser.add_argument(
            "--base",
            default="main",
            metavar="BRANCH",
            help="Base git branch for the PR (default: main).",
        )

    def handle(self, *args, **options):
        workspace_id = options["workspace"]
        base_branch = options["base"]

        # -----------------------------------------------------------------------
        # 1. Resolve workspace
        # -----------------------------------------------------------------------
        try:
            workspace = Workspace.objects.get(pk=workspace_id)
        except Workspace.DoesNotExist:
            raise CommandError(f"Workspace {workspace_id!r} not found.") from None

        self.stdout.write(f"Workspace: {workspace.name} ({workspace_id})")

        # -----------------------------------------------------------------------
        # 2. Read existing Cube model YAML (if present)
        # -----------------------------------------------------------------------
        schema_name = str(workspace_id).replace("-", "_")
        model_dir = _REPO_ROOT / "cube" / "model" / schema_name
        existing_model_yaml = ""
        if model_dir.is_dir():
            parts: list[str] = []
            for yml_file in sorted(model_dir.glob("*.yml")):
                parts.append(yml_file.read_text(encoding="utf-8"))
            existing_model_yaml = "\n".join(parts)
            self.stdout.write(
                f"Read existing model from {model_dir} ({len(parts)} file(s))"
            )
        else:
            self.stdout.write(f"No existing model at {model_dir}; starting fresh.")

        # -----------------------------------------------------------------------
        # 3. Propose measures (async → sync bridge)
        # -----------------------------------------------------------------------
        self.stdout.write("Calling measure proposer…")
        cube_files = async_to_sync(propose_measures)(
            workspace,
            existing_model_yaml=existing_model_yaml,
        )

        if not cube_files:
            self.stdout.write(self.style.SUCCESS("No novel measures to propose."))
            return

        self.stdout.write(f"Proposer returned {len(cube_files)} file(s).")

        # -----------------------------------------------------------------------
        # 4. Build branch name + summary
        # -----------------------------------------------------------------------
        short_ts = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d-%H%M%S")
        short_ws = workspace_id.split("-")[0]
        branch_name = f"cube/proposed-measures-{short_ws}-{short_ts}"

        summary = (
            f"Proposed measures for workspace **{workspace.name}** (`{workspace_id}`).\n\n"
            f"These proposals were derived from accumulated ModelGapSignal records "
            f"(questions that fell back to raw SQL) and high-confidence AgentLearning "
            f"entries (aggregation, join_pattern, business_logic categories).\n\n"
            f"Review the diffs and merge to incorporate these measures into the "
            f"governed Cube data model."
        )

        # -----------------------------------------------------------------------
        # 5. Open the curation PR (NEVER auto-merges)
        # -----------------------------------------------------------------------
        self.stdout.write(f"Opening PR on branch {branch_name!r}…")
        result = open_curation_pr(
            cube_files,
            workspace_id=workspace_id,
            branch_name=branch_name,
            base_branch=base_branch,
            summary=summary,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nPR opened: {result['pr_url']}\n"
                f"Branch: {result['branch']}\n"
                f"\nReview and merge the PR to apply the proposed measures."
            )
        )
