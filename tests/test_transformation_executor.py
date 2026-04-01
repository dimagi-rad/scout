"""Tests for the three-stage transformation executor (Milestone 5, Task 5.3)."""

from unittest.mock import MagicMock, patch

import pytest

from apps.transformations.models import (
    AssetRunStatus,
    TransformationAsset,
    TransformationRunStatus,
    TransformationScope,
)
from apps.transformations.services.executor import run_transformation_pipeline

pytestmark = pytest.mark.usefixtures("_managed_db_url")


@pytest.fixture
def _managed_db_url(settings):
    settings.MANAGED_DATABASE_URL = "postgresql://user:pass@localhost:5432/testdb"


def _dbt_success(*model_names):
    """Return a successful run_dbt result for the given model names."""
    return {"success": True, "models": {n: "success" for n in model_names}, "error": None}


def _dbt_failure(error="dbt run failed"):
    return {"success": False, "models": {}, "error": error}


def _dbt_test_success():
    return {"success": True, "tests": {}, "error": None}


@pytest.fixture
def system_assets(tenant):
    """Create 3 system-scoped assets."""
    assets = []
    for i in range(3):
        assets.append(
            TransformationAsset.objects.create(
                name=f"stg_model_{i}",
                scope=TransformationScope.SYSTEM,
                tenant=tenant,
                sql_content=f"SELECT * FROM raw_table_{i}",
            )
        )
    return assets


@pytest.fixture
def tenant_assets(tenant):
    """Create 2 tenant-scoped assets."""
    assets = []
    for i in range(2):
        assets.append(
            TransformationAsset.objects.create(
                name=f"tenant_model_{i}",
                scope=TransformationScope.TENANT,
                tenant=tenant,
                sql_content=f"SELECT * FROM stg_model_{i}",
            )
        )
    return assets


@pytest.fixture
def workspace_assets(workspace):
    """Create 1 workspace-scoped asset."""
    return [
        TransformationAsset.objects.create(
            name="ws_analysis",
            scope=TransformationScope.WORKSPACE,
            workspace=workspace,
            sql_content="SELECT * FROM tenant_model_0",
        )
    ]


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_system_stage_creates_asset_runs(mock_profiles, mock_dbt, tenant, system_assets):
    mock_dbt.return_value = _dbt_success("stg_model_0", "stg_model_1", "stg_model_2")

    run = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")

    assert run.status == TransformationRunStatus.COMPLETED
    assert run.asset_runs.count() == 3
    assert all(ar.status == AssetRunStatus.SUCCESS for ar in run.asset_runs.all())


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_tenant_stage_runs_after_system(
    mock_profiles, mock_dbt, tenant, system_assets, tenant_assets
):
    mock_dbt.return_value = {
        "success": True,
        "models": {a.name: "success" for a in system_assets + tenant_assets},
        "error": None,
    }

    run = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")

    assert run.status == TransformationRunStatus.COMPLETED
    # 3 system + 2 tenant = 5
    assert run.asset_runs.count() == 5
    assert mock_dbt.call_count == 2  # called once per stage


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_workspace_stage_only_runs_with_workspace(
    mock_profiles, mock_dbt, tenant, workspace, workspace_assets
):
    mock_dbt.return_value = _dbt_success("ws_analysis")

    # Without workspace — no workspace stage
    run_no_ws = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")
    assert run_no_ws.asset_runs.count() == 0

    # With workspace — workspace stage runs
    run_with_ws = run_transformation_pipeline(
        tenant=tenant, schema_name="test_schema", workspace=workspace
    )
    assert run_with_ws.asset_runs.count() == 1
    assert run_with_ws.asset_runs.first().asset.name == "ws_analysis"


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_empty_stage_is_skipped(mock_profiles, mock_dbt, tenant):
    """No assets for any scope — no dbt calls, run completes."""
    run = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")

    assert run.status == TransformationRunStatus.COMPLETED
    assert run.asset_runs.count() == 0
    mock_dbt.assert_not_called()


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_partial_dbt_failure(mock_profiles, mock_dbt, tenant, system_assets):
    """One model fails, others succeed — pipeline still completes."""
    mock_dbt.return_value = {
        "success": True,
        "models": {
            "stg_model_0": "success",
            "stg_model_1": "error",
            "stg_model_2": "success",
        },
        "error": None,
    }

    run = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")

    assert run.status == TransformationRunStatus.COMPLETED
    statuses = {ar.asset.name: ar.status for ar in run.asset_runs.all()}
    assert statuses["stg_model_0"] == AssetRunStatus.SUCCESS
    assert statuses["stg_model_1"] == AssetRunStatus.FAILED
    assert statuses["stg_model_2"] == AssetRunStatus.SUCCESS


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_skipped_model_marked_as_skipped(mock_profiles, mock_dbt, tenant, system_assets):
    """A skipped model (e.g. upstream failure) is marked SKIPPED, not FAILED."""
    mock_dbt.return_value = {
        "success": True,
        "models": {
            "stg_model_0": "success",
            "stg_model_1": "skipped",
            "stg_model_2": "success",
        },
        "error": None,
    }

    run = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")

    statuses = {ar.asset.name: ar.status for ar in run.asset_runs.all()}
    assert statuses["stg_model_0"] == AssetRunStatus.SUCCESS
    assert statuses["stg_model_1"] == AssetRunStatus.SKIPPED
    assert statuses["stg_model_2"] == AssetRunStatus.SUCCESS


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_total_dbt_failure(mock_profiles, mock_dbt, tenant, system_assets):
    """dbt crashes entirely — TransformationRun and all AssetRuns are FAILED."""
    mock_dbt.side_effect = RuntimeError("dbt crashed")

    run = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")

    assert run.status == TransformationRunStatus.FAILED
    assert "dbt crashed" in run.error_message
    # Asset runs must not be left in RUNNING — they should be cleaned up to FAILED
    assert run.asset_runs.count() == 3
    assert all(ar.status == AssetRunStatus.FAILED for ar in run.asset_runs.all())


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt_test")
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_test_yaml_triggers_dbt_test(mock_profiles, mock_dbt, mock_test, tenant):
    TransformationAsset.objects.create(
        name="stg_tested",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
        test_yaml="models:\n  - name: stg_tested\n    columns:\n      - name: id\n        tests:\n          - unique\n",
    )
    mock_dbt.return_value = _dbt_success("stg_tested")
    mock_test.return_value = {
        "success": True,
        "tests": {
            "stg_tested": [{"test": "unique_stg_tested_id", "status": "pass", "message": ""}],
        },
        "error": None,
    }

    run = run_transformation_pipeline(tenant=tenant, schema_name="test_schema")

    mock_test.assert_called_once()
    ar = run.asset_runs.first()
    assert ar.test_results == [
        {"test": "unique_stg_tested_id", "status": "pass", "message": ""},
    ]


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_no_test_yaml_skips_dbt_test(mock_profiles, mock_dbt, tenant, system_assets):
    mock_dbt.return_value = _dbt_success("stg_model_0", "stg_model_1", "stg_model_2")

    with patch("apps.transformations.services.executor.run_dbt_test") as mock_test:
        run_transformation_pipeline(tenant=tenant, schema_name="test_schema")
        mock_test.assert_not_called()


@pytest.mark.django_db
@patch("apps.transformations.services.executor.run_dbt")
@patch("apps.transformations.services.executor.generate_profiles_yml")
def test_progress_callback_called(mock_profiles, mock_dbt, tenant, system_assets, tenant_assets):
    mock_dbt.return_value = {
        "success": True,
        "models": {a.name: "success" for a in system_assets + tenant_assets},
        "error": None,
    }
    callback = MagicMock()

    run_transformation_pipeline(
        tenant=tenant, schema_name="test_schema", progress_callback=callback
    )

    assert callback.call_count == 2  # system + tenant stages
    calls = [c.args[0] for c in callback.call_args_list]
    assert "system" in calls[0]
    assert "tenant" in calls[1]
