"""Three-stage dbt execution pipeline: system → tenant → workspace.

Each stage writes an ephemeral dbt project to a temp directory, runs dbt,
and records per-model results in TransformationAssetRun records.

Transform failures are isolated — they do not fail the overall data load.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings

from apps.transformations.models import (
    AssetRunStatus,
    TransformationAsset,
    TransformationAssetRun,
    TransformationRun,
    TransformationRunStatus,
    TransformationScope,
)
from apps.transformations.services.dbt_project import write_dbt_project
from apps.workspaces.services.schema_manager import dbt_role_name
from mcp_server.services.dbt_runner import generate_profiles_yml, run_dbt, run_dbt_test

logger = logging.getLogger(__name__)


class TransformStageError(RuntimeError):
    """Raised when a dbt stage reports overall failure.

    Surfaced so ``run_transformation_pipeline`` marks the run FAILED instead of
    silently COMPLETED — the dbt connection-level / compilation failures that
    issue #241 (04#4) showed were being swallowed.
    """


def run_transformation_pipeline(
    tenant,
    schema_name: str,
    workspace=None,
    progress_callback=None,
) -> TransformationRun:
    """Execute the three-stage transformation pipeline (system → tenant → workspace)."""
    run = TransformationRun.objects.create(
        tenant=tenant,
        workspace=workspace,
        status=TransformationRunStatus.RUNNING,
    )

    stages = [
        (
            "system",
            TransformationScope.SYSTEM,
            {"tenant": tenant, "scope": TransformationScope.SYSTEM},
        ),
        (
            "tenant",
            TransformationScope.TENANT,
            {"tenant": tenant, "scope": TransformationScope.TENANT},
        ),
    ]
    if workspace:
        stages.append(
            (
                "workspace",
                TransformationScope.WORKSPACE,
                {"workspace": workspace, "scope": TransformationScope.WORKSPACE},
            )
        )

    try:
        for stage_name, _scope, filters in stages:
            assets = list(TransformationAsset.objects.filter(**filters))
            if not assets:
                logger.info("Stage '%s': no assets, skipping", stage_name)
                continue
            if progress_callback:
                progress_callback(f"Running {stage_name} transforms ({len(assets)} models)...")
            _run_stage(run, assets, schema_name, stage_name)

        run.status = TransformationRunStatus.COMPLETED
        run.completed_at = datetime.now(UTC)
        run.save(update_fields=["status", "completed_at"])

    except Exception as e:
        logger.exception("Transformation pipeline failed")
        run.status = TransformationRunStatus.FAILED
        run.completed_at = datetime.now(UTC)
        run.error_message = str(e)
        run.save(update_fields=["status", "completed_at", "error_message"])
        # Don't re-raise — transform failures are isolated from the data load

    return run


def _run_stage(run, assets, schema_name, stage_name):
    asset_runs = {}
    for asset in assets:
        ar = TransformationAssetRun.objects.create(
            run=run,
            asset=asset,
            status=AssetRunStatus.RUNNING,
        )
        asset_runs[asset.name] = ar

    try:
        _execute_stage(asset_runs, assets, schema_name, stage_name)
    except Exception:
        # Mark any asset runs still in RUNNING as FAILED so they don't stay orphaned.
        now = datetime.now(UTC)
        for ar in asset_runs.values():
            if ar.status == AssetRunStatus.RUNNING:
                ar.status = AssetRunStatus.FAILED
                ar.logs = "Stage failed before results were recorded"
                ar.completed_at = now
                ar.save(update_fields=["status", "logs", "completed_at"])
        raise


def _execute_stage(asset_runs, assets, schema_name, stage_name):
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir) / "project"
        profiles_dir = Path(tmpdir) / "profiles"
        profiles_dir.mkdir()

        write_dbt_project(
            output_dir=project_dir,
            project_name=f"scout_{stage_name}",
            assets=assets,
        )

        db_url = getattr(settings, "MANAGED_DATABASE_URL", "")
        if not db_url:
            raise RuntimeError("MANAGED_DATABASE_URL is not configured")
        # Confine dbt to a low-privilege, schema-scoped role and pin its
        # search_path to the target schema (issue #241): user-authored asset SQL
        # must not run as the managed-DB superuser, and unqualified staging
        # SELECTs (FROM raw_cases) must resolve inside the tenant schema.
        generate_profiles_yml(
            output_path=profiles_dir / "profiles.yml",
            schema_name=schema_name,
            db_url=db_url,
            confinement_role=dbt_role_name(schema_name),
        )

        model_names = [a.name for a in assets]
        result = run_dbt(
            dbt_project_dir=str(project_dir),
            profiles_dir=str(profiles_dir),
            models=model_names,
        )

        test_results = {}
        if result.get("success") and any(a.test_yaml for a in assets):
            test_results = run_dbt_test(
                dbt_project_dir=str(project_dir),
                profiles_dir=str(profiles_dir),
                models=model_names,
            )

        now = datetime.now(UTC)
        for asset in assets:
            ar = asset_runs[asset.name]
            ar.logs = ""
            model_status = result.get("models", {}).get(asset.name, "unknown")

            if model_status in ("success", "pass"):
                ar.status = AssetRunStatus.SUCCESS
            elif model_status == "skipped":
                ar.status = AssetRunStatus.SKIPPED
            elif result.get("success") and model_status == "unknown":
                # dbt reported overall success but didn't list this model specifically
                ar.status = AssetRunStatus.SUCCESS
            else:
                ar.status = AssetRunStatus.FAILED
                ar.logs = result.get("error") or f"Model status: {model_status}"

            if asset.name in test_results.get("tests", {}):
                ar.test_results = test_results["tests"][asset.name]

            ar.completed_at = now
            ar.save(update_fields=["status", "logs", "test_results", "completed_at"])

        if not result.get("success"):
            # Surface the failure so the run is marked FAILED rather than silently
            # COMPLETED (issue #241, 04#4). Per-asset results are already recorded
            # above, so the raise only flips the run-level status.
            error = result.get("error") or "dbt run failed"
            logger.warning("Stage '%s' failed: %s", stage_name, error)
            raise TransformStageError(f"Stage '{stage_name}' failed: {error}")
