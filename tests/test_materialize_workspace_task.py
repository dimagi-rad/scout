"""Tests for the procrastinate-backed materialize_workspace task and the
``/api/workspaces/<id>/materialization/cancel/`` endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import AsyncClient
from django.utils import timezone

from apps.users.models import TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
)


def _mock_pipeline(provider="commcare", name="commcare_sync"):
    p = MagicMock()
    p.provider = provider
    p.name = name
    return p


def _mock_registry(provider="commcare"):
    pipeline = _mock_pipeline(provider=provider, name=f"{provider}_sync")
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline
    return registry


@pytest.fixture
def tenant_membership_obj(db, user, tenant):
    tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return tm


@pytest.fixture
def context_with_job_id():
    """Mock procrastinate JobContext with .job.id."""
    ctx = MagicMock()
    ctx.job.id = 42
    return ctx


# ---------------------------------------------------------------------------
# materialize_workspace task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_dispatches_per_tenant(
    workspace, tenant_membership_obj, context_with_job_id
):
    """The task resolves memberships and runs the pipeline once per tenant."""
    from apps.workspaces.tasks import materialize_workspace

    captured = {"calls": 0}

    def fake_pipeline_run(*args, **kwargs):
        captured["calls"] += 1
        return {"status": "completed", "rows_loaded": 7}

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch("apps.workspaces.tasks._run_pipeline_with_progress", side_effect=fake_pipeline_run),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert captured["calls"] == 1
    assert result["all_succeeded"] is True
    assert result["tenants"][0]["success"] is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_records_failure(
    workspace, tenant_membership_obj, context_with_job_id
):
    from apps.workspaces.tasks import materialize_workspace

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=RuntimeError("upstream API down"),
        ),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert result["all_succeeded"] is False
    assert result["tenants"][0]["success"] is False
    assert "upstream API down" in result["tenants"][0]["error"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_breaks_on_cancel(
    workspace, tenant_membership_obj, context_with_job_id
):
    """When the pipeline raises MaterializationCancelled, processing stops."""
    from apps.workspaces.tasks import materialize_workspace
    from mcp_server.services.materializer import MaterializationCancelled

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=MaterializationCancelled(),
        ),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert result["all_succeeded"] is False
    assert result["tenants"][0]["cancelled"] is True


# ---------------------------------------------------------------------------
# _run_pipeline_with_progress closure
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_run_pipeline_with_progress_writes_progress_and_raises_on_cancel(
    db, tenant, tenant_membership_obj
):
    """The closure mirrors progress to the run row and raises when state==CANCELLED."""
    from apps.workspaces.tasks import _run_pipeline_with_progress
    from mcp_server.services.materializer import MaterializationCancelled

    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name="test_progress", state=SchemaState.ACTIVE
    )
    run = MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=99,
    )

    # Replace run_pipeline with a stub that invokes the updater with our
    # fixture run id, simulating run_pipeline's normal behavior.
    captured: list[dict] = []

    def fake_run_pipeline(*args, progress_updater=None, procrastinate_job_id=None, **kwargs):
        progress_updater(
            {
                "run_id": str(run.id),
                "step": 1,
                "total_steps": 4,
                "source": "cases",
                "message": "Loading...",
                "rows_loaded": 100,
                "rows_total": 1000,
            }
        )
        captured.append({"job_id": procrastinate_job_id})
        # Now flip to cancelled and call again — this should raise.
        MaterializationRun.objects.filter(id=run.id).update(
            state=MaterializationRun.RunState.CANCELLED
        )
        progress_updater(
            {
                "run_id": str(run.id),
                "step": 1,
                "total_steps": 4,
                "source": "cases",
                "message": "Loading...",
                "rows_loaded": 200,
                "rows_total": 1000,
            }
        )
        return {"status": "completed"}

    pipeline = _mock_pipeline()
    with (
        patch("apps.workspaces.tasks.run_pipeline", side_effect=fake_run_pipeline),
        pytest.raises(MaterializationCancelled),
    ):
        _run_pipeline_with_progress(
            tenant_membership_obj,
            {"type": "api_key", "value": "k"},
            pipeline,
            job_id=99,
        )

    # The updater writes progress on every call. The second call writes
    # rows_loaded=200 first, then re-reads state, sees CANCELLED, raises.
    run.refresh_from_db()
    assert run.progress is not None
    assert run.progress["rows_loaded"] == 200
    assert captured[0]["job_id"] == 99


# ---------------------------------------------------------------------------
# materialization cancel endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_endpoint_marks_runs_cancelled(workspace, user, tenant):
    """POSTing cancels every active run for the workspace and aborts the job."""
    from asgiref.sync import sync_to_async

    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant, schema_name="test_cancel", state=SchemaState.ACTIVE
    )
    active_run = await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=123,
    )

    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")

    with patch("apps.workspaces.api.materialization_views.current_app") as mock_app:
        mock_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["runs_cancelled"] == 1
    mock_app.job_manager.cancel_job_by_id_async.assert_awaited_once_with(123, abort=True)

    await active_run.arefresh_from_db()
    assert active_run.state == MaterializationRun.RunState.CANCELLED
    assert active_run.completed_at is not None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_endpoint_returns_no_active_run_when_idle(workspace, user, tenant):
    from asgiref.sync import sync_to_async

    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant, schema_name="test_cancel_idle", state=SchemaState.ACTIVE
    )
    # A completed run shouldn't be touched.
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=timezone.now(),
    )

    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")

    resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_active_run"
    assert body["runs_cancelled"] == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_endpoint_requires_workspace_membership(workspace, other_user):
    from asgiref.sync import sync_to_async

    client = AsyncClient()
    await sync_to_async(client.login)(email=other_user.email, password="otherpass123")

    resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")
    assert resp.status_code == 403
