"""Tests for TransformationAsset, TransformationRun, TransformationAssetRun models (Milestone 3)."""

import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError

from apps.transformations.models import (
    AssetRunStatus,
    TransformationAsset,
    TransformationAssetRun,
    TransformationRun,
    TransformationRunStatus,
    TransformationScope,
)

# ---------------------------------------------------------------------------
# TransformationAsset
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_system_scoped_asset_with_tenant_succeeds(tenant):
    asset = TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
    )
    assert asset.pk is not None
    assert asset.tenant == tenant
    assert asset.workspace is None


@pytest.mark.django_db
def test_tenant_scoped_asset_with_tenant_succeeds(tenant):
    asset = TransformationAsset.objects.create(
        name="stg_cases_custom",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('stg_cases') }}",
    )
    assert asset.pk is not None


@pytest.mark.django_db
def test_workspace_scoped_asset_with_workspace_succeeds(workspace):
    asset = TransformationAsset.objects.create(
        name="my_analysis",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        sql_content="SELECT * FROM stg_cases",
    )
    assert asset.pk is not None
    assert asset.workspace == workspace
    assert asset.tenant is None


@pytest.mark.django_db
def test_asset_with_both_tenant_and_workspace_fails(tenant, workspace):
    with pytest.raises(IntegrityError):
        TransformationAsset.objects.create(
            name="bad_asset",
            scope=TransformationScope.SYSTEM,
            tenant=tenant,
            workspace=workspace,
            sql_content="SELECT 1",
        )


@pytest.mark.django_db
def test_asset_with_neither_tenant_nor_workspace_fails():
    with pytest.raises(IntegrityError):
        TransformationAsset.objects.create(
            name="no_container",
            scope=TransformationScope.SYSTEM,
            sql_content="SELECT 1",
        )


@pytest.mark.django_db
def test_clean_system_scope_with_workspace_raises(workspace):
    asset = TransformationAsset(
        name="bad",
        scope=TransformationScope.SYSTEM,
        workspace=workspace,
        sql_content="SELECT 1",
    )
    with pytest.raises(ValidationError, match="require a tenant"):
        asset.clean()


@pytest.mark.django_db
def test_clean_workspace_scope_with_tenant_raises(tenant, workspace):
    asset = TransformationAsset(
        name="bad",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    with pytest.raises(ValidationError, match="must not have a tenant"):
        asset.clean()


@pytest.mark.django_db
def test_clean_workspace_scope_without_workspace_raises():
    asset = TransformationAsset(
        name="bad",
        scope=TransformationScope.WORKSPACE,
        sql_content="SELECT 1",
    )
    with pytest.raises(ValidationError, match="require a workspace"):
        asset.clean()


@pytest.mark.django_db
def test_unique_constraint_same_name_scope_tenant(tenant):
    TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    with pytest.raises(IntegrityError):
        TransformationAsset.objects.create(
            name="stg_cases",
            scope=TransformationScope.SYSTEM,
            tenant=tenant,
            sql_content="SELECT 2",
        )


@pytest.mark.django_db
def test_same_name_different_scopes_on_same_tenant_succeeds(tenant):
    TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    asset2 = TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 2",
    )
    assert asset2.pk is not None


@pytest.mark.django_db
def test_replaces_fk_and_reverse_relation(tenant):
    original = TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    replacement = TransformationAsset.objects.create(
        name="stg_cases_v2",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 2",
        replaces=original,
    )
    assert replacement.replaces == original
    assert original.replaced_by.first() == replacement


@pytest.mark.django_db
def test_asset_str(tenant):
    asset = TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    assert "system:stg_cases" in str(asset)


# ---------------------------------------------------------------------------
# TransformationRun
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_transformation_run_status_transitions(tenant):
    run = TransformationRun.objects.create(tenant=tenant)
    assert run.status == TransformationRunStatus.PENDING

    run.status = TransformationRunStatus.RUNNING
    run.save()
    run.refresh_from_db()
    assert run.status == TransformationRunStatus.RUNNING

    run.status = TransformationRunStatus.COMPLETED
    run.save()
    run.refresh_from_db()
    assert run.status == TransformationRunStatus.COMPLETED


@pytest.mark.django_db
def test_transformation_run_str(tenant):
    run = TransformationRun.objects.create(tenant=tenant)
    assert "TransformationRun" in str(run)
    assert "pending" in str(run)


# ---------------------------------------------------------------------------
# TransformationAssetRun
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_asset_run_links_to_run_and_asset(tenant):
    asset = TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    run = TransformationRun.objects.create(tenant=tenant)
    asset_run = TransformationAssetRun.objects.create(run=run, asset=asset)

    assert asset_run.run == run
    assert asset_run.asset == asset
    assert asset_run.status == AssetRunStatus.PENDING
    assert run.asset_runs.first() == asset_run
    assert asset.runs.first() == asset_run


@pytest.mark.django_db
def test_asset_run_test_results_json_roundtrip(tenant):
    asset = TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    run = TransformationRun.objects.create(tenant=tenant)
    payload = {"passed": 10, "failed": 2, "errors": ["null check failed"]}
    asset_run = TransformationAssetRun.objects.create(run=run, asset=asset, test_results=payload)
    asset_run.refresh_from_db()
    assert asset_run.test_results == payload


@pytest.mark.django_db
def test_asset_run_str(tenant):
    asset = TransformationAsset.objects.create(
        name="stg_cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    run = TransformationRun.objects.create(tenant=tenant)
    asset_run = TransformationAssetRun.objects.create(run=run, asset=asset)
    assert "stg_cases" in str(asset_run)
    assert "pending" in str(asset_run)
