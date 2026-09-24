"""Execution receipts hash the exact config and assets consumed by one load."""

import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.executor import run_transformation_pipeline
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from apps.workspaces.services.load_generations import pipeline_fingerprint
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
from mcp_server.services.materializer import run_pipeline

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("mutate_registry", [False, True])
def test_pipeline_uses_snapshot_across_registry_and_asset_edits(tenant, mutate_registry):
    pipeline = PipelineConfig(
        name="snapshot",
        description="",
        version="1",
        provider="ocs",
        sources=[SourceConfig(name="sessions")],
    )
    original_config = deepcopy(pipeline)
    asset = TransformationAsset.objects.create(
        tenant=tenant,
        name="model",
        scope=TransformationScope.TENANT,
        sql_content="select 1",
    )
    system_asset = TransformationAsset.objects.create(
        tenant=tenant,
        name="system_model",
        scope=TransformationScope.SYSTEM,
        sql_content="select 10",
    )
    candidate = TenantSchema.objects.create(
        tenant=tenant, schema_name="snapshot_candidate", state=SchemaState.PROVISIONING
    )
    versions = []
    loaded_sql = []
    expected = []
    source_names = []

    def discover(_membership, _credential, config):
        versions.append(config.version)
        TransformationAsset.objects.filter(pk=asset.pk).update(sql_content="select 2")
        TransformationAsset.objects.filter(pk=system_asset.pk).update(sql_content="select 20")
        expected.append(pipeline_fingerprint(original_config, tenant))
        return {}

    def progress(value):
        if mutate_registry and value["message"].startswith("Provisioning"):
            pipeline.version = "mutated"
            pipeline.sources[0].name = "mutated_source"
        if value["message"].startswith("Running transforms"):
            TransformationAsset.objects.filter(pk=asset.pk).update(sql_content="select 3")

    def stage(_run, assets, _schema, _stage):
        loaded_sql.extend(a.sql_content for a in assets)
        TransformationAsset.objects.filter(pk=asset.pk).update(sql_content="select 4")
        return []

    def source(name, *_args, **_kwargs):
        source_names.append(name)
        return 0

    with (
        patch("mcp_server.services.materializer._run_discover_phase", side_effect=discover),
        patch("apps.transformations.services.executor._run_stage", side_effect=stage),
        patch("mcp_server.services.materializer._load_and_commit_source", side_effect=source),
    ):
        result = run_pipeline(
            SimpleNamespace(tenant=tenant, tenant_id=tenant.id, connection=None),
            {"type": "api_key", "value": "not-in-receipt"},
            pipeline,
            progress_updater=progress,
            target_schema=candidate,
            defer_schema_promotion=True,
        )
    assert versions == ["1"]
    assert source_names == ["sessions"]
    assert loaded_sql == ["select 20", "select 2"]
    assert result["load_fingerprint"] == expected[0]
    run = MaterializationRun.objects.get(pk=result["run_id"])
    assert run.result["load_fingerprint"] == expected[0]
    assert pipeline_fingerprint(pipeline, tenant) != expected[0]
    assert "not-in-receipt" not in str(run.result)


@pytest.mark.parametrize("wrong_owner", ["tenant", "workspace"])
def test_transform_snapshot_rejects_other_container_before_execution(
    tenant, workspace, wrong_owner
):
    asset = TransformationAsset(
        name="other_container",
        scope=TransformationScope.TENANT,
        # The workspace case keeps the right tenant so only the workspace check can fire.
        tenant_id=uuid.uuid4() if wrong_owner == "tenant" else tenant.pk,
        workspace=workspace if wrong_owner == "workspace" else None,
        sql_content="select 1",
    )
    with pytest.raises(ValueError, match="tenant snapshot"):
        run_transformation_pipeline(tenant, "candidate", asset_snapshot=[asset])


def test_a_workspace_run_cannot_take_a_snapshot(tenant, workspace):
    with pytest.raises(ValueError, match="only supported for tenant-scoped runs"):
        run_transformation_pipeline(tenant, "candidate", workspace=workspace, asset_snapshot=[])


@pytest.mark.parametrize(
    ("sources", "expected"),
    [([], MaterializationRun.RunState.FAILED), (["sessions"], MaterializationRun.RunState.PARTIAL)],
)
def test_a_receipt_failure_ends_the_run_terminal_not_transforming(tenant, sources, expected):
    """Hashing the implementation can raise; the run must still end terminal, or
    it stays in an ACTIVE state and the workspace looks mid-refresh forever. With
    sources committed it is PARTIAL, not FAILED as if nothing had loaded."""
    pipeline = PipelineConfig(
        name="receipt",
        description="",
        version="1",
        provider="ocs",
        sources=[SourceConfig(name=name) for name in sources],
    )
    candidate = TenantSchema.objects.create(
        tenant=tenant, schema_name="receipt_candidate", state=SchemaState.PROVISIONING
    )
    with (
        patch("mcp_server.services.materializer._run_discover_phase", return_value={}),
        patch("mcp_server.services.materializer._load_and_commit_source", return_value=0),
        patch(
            "mcp_server.services.materializer.pipeline_fingerprint",
            side_effect=RuntimeError("implementation path missing"),
        ),
        pytest.raises(RuntimeError),
    ):
        run_pipeline(
            SimpleNamespace(tenant=tenant, tenant_id=tenant.id, connection=None),
            {"type": "api_key", "value": "x"},
            pipeline,
            target_schema=candidate,
            defer_schema_promotion=True,
        )

    run = MaterializationRun.objects.get(tenant_schema=candidate)
    assert run.state == expected
    assert run.completed_at is not None
