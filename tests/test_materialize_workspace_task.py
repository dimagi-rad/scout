"""Tests for the procrastinate-backed materialize_workspace task and the
``/api/workspaces/<id>/materialization/cancel/`` endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import AsyncClient
from django.utils import timezone

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant, TenantMembership
from apps.users.services.credential_resolver import CredentialResolutionError
from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.tasks import _run_pipeline_with_progress, materialize_workspace
from mcp_server.envelope import AUTH_TOKEN_EXPIRED
from mcp_server.services.materializer import MaterializationCancelled


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
async def test_materialize_workspace_surfaces_team_mismatch_distinctly(
    workspace, tenant_membership_obj, context_with_job_id
):
    """A team-mismatch credential failure must surface a distinct, actionable
    re-authorize message — NOT the generic "No credential configured" — so a
    user logged into the wrong OCS team is told to re-connect (finding 07#3)."""
    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
    ):
        mock_cred.side_effect = CredentialResolutionError(
            AUTH_TOKEN_EXPIRED,
            "Your sign-in is scoped to a different OCS team than this chatbot "
            "(team-a). Please re-connect to team team-a.",
        )
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert result["all_succeeded"] is False
    tenant_result = result["tenants"][0]
    assert tenant_result["success"] is False
    assert "No credential configured" not in tenant_result["error"]
    assert "re-connect" in tenant_result["error"]
    assert tenant_result["error_code"] == AUTH_TOKEN_EXPIRED


@pytest.fixture
def multi_tenant_workspace(db, workspace, user):
    """Augment the single-tenant `workspace` fixture with a second tenant +
    membership, so workspace_tenants.acount() > 1."""
    second_tenant = Tenant.objects.create(
        provider="commcare", external_id="test-domain-2", canonical_name="Test Domain 2"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=second_tenant)
    TenantMembership.objects.create(user=user, tenant=second_tenant)
    return workspace


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_rebuilds_view_schema_when_multi_tenant_succeeds(
    multi_tenant_workspace, tenant_membership_obj, context_with_job_id
):
    """After all tenants materialize, the workspace view schema is rebuilt
    so the agent's next list_tables call sees the namespaced views."""
    mock_manager = MagicMock()

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            return_value={"status": "completed"},
        ),
        patch("apps.workspaces.tasks.SchemaManager", return_value=mock_manager),
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(multi_tenant_workspace.id),
            user_id="",
        )

    assert result["all_succeeded"] is True
    mock_manager.build_view_schema.assert_called_once_with(multi_tenant_workspace)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_skips_view_rebuild_for_single_tenant(
    workspace, tenant_membership_obj, context_with_job_id
):
    """Single-tenant workspaces don't use a view schema, so we must not
    attempt to build one (build_view_schema would raise for tenant_count==1)."""
    mock_manager = MagicMock()

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            return_value={"status": "completed"},
        ),
        patch("apps.workspaces.tasks.SchemaManager", return_value=mock_manager),
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    mock_manager.build_view_schema.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_skips_view_rebuild_when_any_tenant_failed(
    multi_tenant_workspace, tenant_membership_obj, context_with_job_id
):
    """When even one tenant pipeline fails, the view rebuild would itself
    fail (it requires every tenant to have an ACTIVE schema), so we skip
    it rather than burning a noisy traceback."""
    mock_manager = MagicMock()

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=RuntimeError("upstream API down"),
        ),
        patch("apps.workspaces.tasks.SchemaManager", return_value=mock_manager),
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(multi_tenant_workspace.id),
            user_id="",
        )

    assert result["all_succeeded"] is False
    mock_manager.build_view_schema.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_view_rebuild_failure_does_not_block_resume(
    multi_tenant_workspace, tenant_membership_obj, context_with_job_id
):
    """If build_view_schema raises (e.g. DB write failure), the materialize
    task must still defer the resume task so the user is not left with a
    silent phantom spinner."""
    mock_manager = MagicMock()
    mock_manager.build_view_schema.side_effect = RuntimeError("DDL failed")
    defer_mock = AsyncMock()

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            return_value={"status": "completed"},
        ),
        patch("apps.workspaces.tasks.SchemaManager", return_value=mock_manager),
        patch("apps.workspaces.tasks._defer_resume_for_job", defer_mock),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(multi_tenant_workspace.id),
            user_id="",
        )

    # The task returns successfully (tenants succeeded), the view rebuild
    # exception is swallowed, and the resume task is still deferred.
    assert result["all_succeeded"] is True
    mock_manager.build_view_schema.assert_called_once()
    defer_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_breaks_on_cancel(
    workspace, tenant_membership_obj, context_with_job_id
):
    """When the pipeline raises MaterializationCancelled, processing stops."""
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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_core_runs_without_deferring_resume(
    workspace, tenant_membership_obj
):
    """materialize_workspace_core does the tenant loop + view rebuild but does
    NOT defer any chat-resume task — headless callers (recipes) invoke it
    directly and block on the return value. The fire-and-resume deferral lives
    only in the materialize_workspace task wrapper."""
    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            return_value={"status": "completed"},
        ),
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock) as defer_mock,
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await workspaces_tasks.materialize_workspace_core(
            str(workspace.id), user_id="", job_id=None
        )

    assert result["all_succeeded"] is True
    assert result["tenants"][0]["success"] is True
    defer_mock.assert_not_awaited()  # core must never defer the resume task


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_blocking_runs_immediately_when_idle(
    workspace, tenant, tenant_membership_obj, monkeypatch
):
    """With no in-progress materialization, the headless blocking entrypoint
    runs the core immediately without waiting."""
    core = {"n": 0}

    async def fake_core(wid, uid="", jid=None):
        core["n"] += 1
        return {"all_succeeded": True, "tenants": [], "view_schema": None}

    slept = {"n": 0}

    async def fake_sleep(_delay):
        slept["n"] += 1

    monkeypatch.setattr(workspaces_tasks, "materialize_workspace_core", fake_core)
    monkeypatch.setattr("apps.workspaces.tasks.asyncio.sleep", fake_sleep)

    result = await workspaces_tasks.materialize_workspace_blocking(str(workspace.id))

    assert slept["n"] == 0  # nothing in progress → no waiting
    assert core["n"] == 1
    assert result["all_succeeded"] is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_blocking_waits_out_in_progress_run(
    workspace, tenant, tenant_membership_obj, monkeypatch
):
    """If a materialization is already ACTIVE for one of the workspace's
    tenants, the blocking entrypoint WAITS for it to clear before starting its
    own — never running two in parallel against the same tenant schema."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="t_wait", state=SchemaState.MATERIALIZING
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
    )

    core = {"n": 0, "when_slept": None}

    async def fake_core(wid, uid="", jid=None):
        core["n"] += 1
        core["when_slept"] = slept["n"]
        return {"all_succeeded": True, "tenants": [], "view_schema": None}

    slept = {"n": 0}

    async def fake_sleep(_delay):
        # Clear the in-progress run on the first poll so the wait loop exits.
        slept["n"] += 1
        await MaterializationRun.objects.filter(id=run.id).aupdate(
            state=MaterializationRun.RunState.COMPLETED
        )

    monkeypatch.setattr(workspaces_tasks, "materialize_workspace_core", fake_core)
    monkeypatch.setattr("apps.workspaces.tasks.asyncio.sleep", fake_sleep)

    result = await workspaces_tasks.materialize_workspace_blocking(str(workspace.id))

    assert slept["n"] >= 1  # it waited for the in-progress run
    assert core["n"] == 1  # then ran its own
    assert core["when_slept"] >= 1  # core ran AFTER the wait, not before
    assert result["all_succeeded"] is True


# ---------------------------------------------------------------------------
# _run_pipeline_with_progress closure
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_run_pipeline_with_progress_writes_progress_and_raises_on_cancel(
    db, tenant, tenant_membership_obj
):
    """The closure mirrors progress to the run row and raises when state==CANCELLED."""
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
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_cancel", state=SchemaState.ACTIVE
    )
    active_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=123,
    )
    # The refactored view routes through cancel_thread_job, which looks up
    # ThreadJobs by procrastinate_job_id. Create one so the run is cancelled.
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=123,
        tool_call_id="tc_legacy",
        state=ThreadJob.State.RUNNING,
    )

    client = AsyncClient()
    await client.alogin(email=user.email, password="testpass123")

    with patch("apps.workspaces.api.jobs_cancel.current_app") as mock_app:
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
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_cancel_idle", state=SchemaState.ACTIVE
    )
    # A completed run shouldn't be touched.
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=timezone.now(),
    )

    client = AsyncClient()
    await client.alogin(email=user.email, password="testpass123")

    resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_active_run"
    assert body["runs_cancelled"] == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_endpoint_requires_workspace_membership(workspace, other_user):
    client = AsyncClient()
    await client.alogin(email=other_user.email, password="otherpass123")

    resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_defers_resume_on_no_memberships_early_return(
    workspace,
    user,
    context_with_job_id,
):
    """Finding #4: the early-return path (no memberships) must still defer
    the resume task. Otherwise the user is left with a phantom spinner —
    the chat layer is waiting on a chained resume that never fires."""
    # Workspace exists, but the workspace has no tenants → no memberships.
    # Build a fresh workspace with no tenants/memberships.
    bare_ws = await Workspace.objects.acreate(
        name="bare-no-memberships",
        created_by=user,
    )
    # Create a ThreadJob bound to context_with_job_id.job.id so the resume
    # finally-block can locate it.
    thread = await Thread.objects.acreate(workspace=bare_ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=context_with_job_id.job.id,
        tool_call_id="tc-early-return",
    )

    with patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume_mock:
        resume_mock.defer_async = AsyncMock(return_value=MagicMock(id=42424))
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(bare_ws.id),
            user_id="",
        )

    # Early-return error envelope returned to the worker.
    assert result == {"error": "No tenant memberships found", "tenants": []}
    # But the resume task IS still deferred (in the finally block).
    resume_mock.defer_async.assert_awaited_once_with(thread_job_id=str(tj.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_defers_resume_on_workspace_not_found(
    workspace,
    user,
    context_with_job_id,
):
    """Even when the workspace lookup fails, the resume must be deferred so
    the user is not stuck with a spinner. The _defer_resume_for_job helper
    looks up the ThreadJob by procrastinate_job_id, independent of the
    workspace_id passed in."""
    # ThreadJob bound to the context's job id, on an unrelated workspace.
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=context_with_job_id.job.id,
        tool_call_id="tc-no-ws",
    )

    with patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume_mock:
        resume_mock.defer_async = AsyncMock(return_value=MagicMock(id=51515))
        # Pass a non-existent workspace_id to trigger the early-return branch.
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id="00000000-0000-0000-0000-000000000000",
            user_id="",
        )

    assert result == {"error": "Workspace not found"}
    resume_mock.defer_async.assert_awaited_once_with(thread_job_id=str(tj.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_defer_resume_for_job_retries_when_threadjob_not_yet_committed(
    workspace,
    user,
):
    """Finding #11: MCP commits the ThreadJob after defer_async returns the
    procrastinate job id; under load the worker can finish before the row
    is visible. The bounded retry loop in _defer_resume_for_job hedges
    against that race — it must still locate the ThreadJob on a later tick.

    We test the helper in isolation: insert the ThreadJob *after* the first
    polling attempt by spying on asyncio.sleep."""
    job_id = 70707
    insert_state = {"inserted": False, "tj_id": None}

    async def fake_sleep(_delay):
        # On the first sleep, commit the ThreadJob so the *next* afirst sees it.
        if not insert_state["inserted"]:
            thread = await Thread.objects.acreate(workspace=workspace, user=user)
            tj = await ThreadJob.objects.acreate(
                thread=thread,
                job_type="materialization",
                procrastinate_job_id=job_id,
                tool_call_id="tc-retry",
            )
            insert_state["inserted"] = True
            insert_state["tj_id"] = str(tj.id)

    with (
        patch("apps.workspaces.tasks.asyncio.sleep", side_effect=fake_sleep),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume_mock,
    ):
        resume_mock.defer_async = AsyncMock(return_value=MagicMock(id=99))
        await workspaces_tasks._defer_resume_for_job(job_id)

    assert insert_state["inserted"], "fake_sleep should have inserted the ThreadJob"
    resume_mock.defer_async.assert_awaited_once_with(
        thread_job_id=insert_state["tj_id"],
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_chains_resume_task(
    workspace, user, tenant_membership_obj, context_with_job_id
):
    """When materialize_workspace finishes, it defers
    resume_thread_after_materialization for the matching ThreadJob."""
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=context_with_job_id.job.id,
        tool_call_id="tc-chain",
    )

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch("apps.workspaces.tasks._run_pipeline_with_progress", return_value={"status": "ok"}),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume_mock,
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        resume_mock.defer_async = AsyncMock(return_value=MagicMock(id=9999))
        await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    resume_mock.defer_async.assert_awaited_once()
    kwargs = resume_mock.defer_async.await_args.kwargs
    assert kwargs["thread_job_id"] == str(tj.id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_legacy_cancel_handles_mixed_tracked_and_orphan_runs(workspace, user, tenant):
    """Mixed case: workspace has one tracked materialization (chat-triggered,
    with ThreadJob) and one orphan (e.g., /refresh/-triggered, no ThreadJob).
    The cancel endpoint must cancel BOTH runs."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_mixed_cancel", state=SchemaState.ACTIVE
    )

    # Tracked run: has a ThreadJob
    tracked_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=555,
    )
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=555,
        tool_call_id="tc-tracked",
        state=ThreadJob.State.RUNNING,
    )

    # Orphan run: no ThreadJob (e.g., /refresh/-initiated)
    orphan_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=666,
    )

    client = AsyncClient()
    await client.alogin(email=user.email, password="testpass123")

    with (
        patch("apps.workspaces.api.jobs_cancel.current_app") as mock_tracked_app,
        patch("apps.workspaces.api.materialization_views.current_app") as mock_orphan_app,
    ):
        mock_tracked_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        mock_orphan_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    # Both runs must be cancelled
    assert body["runs_cancelled"] == 2

    await tracked_run.arefresh_from_db()
    assert tracked_run.state == MaterializationRun.RunState.CANCELLED

    await orphan_run.arefresh_from_db()
    assert orphan_run.state == MaterializationRun.RunState.CANCELLED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_legacy_cancel_does_not_cancel_other_users_threadjob(
    workspace,
    user,
    other_user,
    tenant,
):
    """A workspace member must NOT be able to cancel another member's
    chat-driven materialization via the legacy
    /api/workspaces/<id>/materialization/cancel/ endpoint.

    The legacy endpoint resolves the workspace by membership, so a peer could
    previously sweep up ThreadJobs owned by another user — which then triggers
    a resume-task message in the victim's chat. The fix adds a thread__user
    filter so only the caller's own ThreadJobs are cancelled."""
    # Make other_user a member of the workspace (peer with access).
    await WorkspaceMembership.objects.acreate(
        workspace=workspace,
        user=other_user,
        role=WorkspaceRole.READ_WRITE,
    )
    await TenantMembership.objects.acreate(user=other_user, tenant=tenant)  # peer's live access
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="test_xuser_cancel",
        state=SchemaState.ACTIVE,
    )
    # Active run owned by `user`'s thread.
    active_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=777,
    )
    owner_thread = await Thread.objects.acreate(workspace=workspace, user=user)
    owner_tj = await ThreadJob.objects.acreate(
        thread=owner_thread,
        job_type="materialization",
        procrastinate_job_id=777,
        tool_call_id="tc-owner",
        state=ThreadJob.State.RUNNING,
    )

    # other_user (a workspace peer) attempts to cancel via the legacy endpoint.
    client = AsyncClient()
    await client.alogin(email=other_user.email, password="otherpass123")
    with (
        patch("apps.workspaces.api.jobs_cancel.current_app") as mock_tracked_app,
        patch("apps.workspaces.api.materialization_views.current_app") as mock_orphan_app,
    ):
        mock_tracked_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        mock_orphan_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    # The endpoint must report no_active_run: the peer owns no ThreadJobs of
    # their own, and the only active run belongs to another user (so it is
    # NOT an orphan and must not be swept by the orphan-fallback either).
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_active_run"
    assert body["runs_cancelled"] == 0

    await owner_tj.arefresh_from_db()
    assert owner_tj.state == ThreadJob.State.RUNNING, (
        "Peer must not be able to cancel another user's chat-driven ThreadJob"
    )
    # The owner's MaterializationRun must remain untouched. Previously the
    # orphan-fallback used a user-scoped tracked set, so the peer's call
    # incorrectly classified the owner's run as "orphan" and cancelled it.
    await active_run.arefresh_from_db()
    assert active_run.state == MaterializationRun.RunState.LOADING, (
        "Peer must not be able to cancel another user's MaterializationRun "
        "via the orphan-fallback path"
    )
    # The orphan-cancel branch must NOT have been invoked for this job id.
    mock_orphan_app.job_manager.cancel_job_by_id_async.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_legacy_cancel_orphan_path_skips_other_users_runs(
    workspace,
    user,
    other_user,
    tenant,
):
    """When user A calls the legacy cancel endpoint and user B has a
    chat-driven materialization (with ThreadJob) in the same workspace, only
    the genuinely-orphan run (no ThreadJob anywhere) is cancelled. User B's
    MaterializationRun and ThreadJob must remain untouched.

    Regression test for the orphan-detection bug: ``orphan_job_ids`` was
    computed against the caller-scoped tracked set, so other users' jobs
    leaked into the orphan branch and were aborted."""
    await WorkspaceMembership.objects.acreate(
        workspace=workspace,
        user=other_user,
        role=WorkspaceRole.READ_WRITE,
    )
    await TenantMembership.objects.acreate(user=other_user, tenant=tenant)  # peer's live access
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="test_orphan_skip_other",
        state=SchemaState.ACTIVE,
    )

    # Run #1: belongs to user B's chat (has a ThreadJob).
    b_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=1001,
    )
    b_thread = await Thread.objects.acreate(workspace=workspace, user=user)
    b_tj = await ThreadJob.objects.acreate(
        thread=b_thread,
        job_type="materialization",
        procrastinate_job_id=1001,
        tool_call_id="tc-b-owner",
        state=ThreadJob.State.RUNNING,
    )

    # Run #2: genuine orphan (e.g., /refresh/ path — no ThreadJob anywhere).
    orphan_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=1002,
    )

    # User A (other_user) calls cancel.
    client = AsyncClient()
    await client.alogin(email=other_user.email, password="otherpass123")
    with (
        patch("apps.workspaces.api.jobs_cancel.current_app") as mock_tracked_app,
        patch("apps.workspaces.api.materialization_views.current_app") as mock_orphan_app,
    ):
        mock_tracked_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        mock_orphan_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    # Only the orphan run is reported cancelled.
    assert body["status"] == "cancelled"
    assert body["runs_cancelled"] == 1

    # B's run + ThreadJob untouched.
    await b_run.arefresh_from_db()
    assert b_run.state == MaterializationRun.RunState.LOADING
    await b_tj.arefresh_from_db()
    assert b_tj.state == ThreadJob.State.RUNNING

    # Orphan run is cancelled and procrastinate was signalled exactly once
    # (for the orphan job id, not B's job id).
    await orphan_run.arefresh_from_db()
    assert orphan_run.state == MaterializationRun.RunState.CANCELLED
    assert orphan_run.completed_at is not None
    mock_orphan_app.job_manager.cancel_job_by_id_async.assert_awaited_once_with(
        1002,
        abort=True,
    )


# ---------------------------------------------------------------------------
# Sibling view-schema consistency on re-materialization
# ---------------------------------------------------------------------------
#
# Tenant data schemas (t_<id>) are SHARED across workspaces. Re-materializing a
# tenant from workspace A cascade-drops the namespaced views inside every OTHER
# multi-tenant workspace that shares that tenant, so materialize_workspace must
# defer a rebuild for each such sibling.


async def _make_sibling_multitenant_workspace(user, shared_tenant, *, with_view_schema, suffix):
    """Build a multi-tenant workspace that shares ``shared_tenant`` and has a
    distinct extra tenant, optionally with a WorkspaceViewSchema row."""
    extra_tenant = await Tenant.objects.acreate(
        provider="commcare",
        external_id=f"sibling-extra-{suffix}",
        canonical_name=f"Sibling Extra {suffix}",
    )
    ws = await Workspace.objects.acreate(name=f"Sibling WS {suffix}", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=shared_tenant)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=extra_tenant)
    if with_view_schema:
        await WorkspaceViewSchema.objects.acreate(
            workspace=ws,
            schema_name=f"ws_sibling_{suffix}",
            state=SchemaState.ACTIVE,
        )
    return ws


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_defers_rebuild_for_sibling_view_schemas(
    workspace, tenant, tenant_membership_obj, user, context_with_job_id
):
    """Materializing workspace A (sharing tenant T) defers a rebuild for the
    multi-tenant sibling B that shares T and has a WorkspaceViewSchema — and
    does NOT defer one for A itself nor for a single-tenant sibling."""
    # Sibling B: multi-tenant, shares T, has a view schema → must be rebuilt.
    sibling_b = await _make_sibling_multitenant_workspace(
        user, tenant, with_view_schema=True, suffix="b"
    )
    # Sibling C: single-tenant (only T), no view schema → must NOT be rebuilt.
    sibling_c = await Workspace.objects.acreate(name="Sibling C single", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=sibling_c, tenant=tenant)

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch("apps.workspaces.tasks._run_pipeline_with_progress", return_value={"status": "ok"}),
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as mock_rebuild,
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    mock_rebuild.assert_awaited_once_with(workspace_id=str(sibling_b.id))
    deferred_ids = {c.kwargs["workspace_id"] for c in mock_rebuild.await_args_list}
    assert str(workspace.id) not in deferred_ids  # never the current workspace
    assert str(sibling_c.id) not in deferred_ids  # single-tenant siblings excluded


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_dedupes_sibling_rebuild(
    workspace, tenant, tenant_membership_obj, user, context_with_job_id
):
    """A sibling B that shares the materialized tenant must be deferred exactly
    once even though the dedupe path could otherwise enqueue duplicates."""
    sibling_b = await _make_sibling_multitenant_workspace(
        user, tenant, with_view_schema=True, suffix="dedup"
    )

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch("apps.workspaces.tasks._run_pipeline_with_progress", return_value={"status": "ok"}),
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as mock_rebuild,
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert mock_rebuild.await_count == 1
    mock_rebuild.assert_awaited_once_with(workspace_id=str(sibling_b.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_no_sibling_rebuild_when_none_qualify(
    workspace, tenant_membership_obj, context_with_job_id
):
    """Regression: with no qualifying sibling (no other multi-tenant workspace
    sharing the tenant + view schema), no rebuild is deferred."""
    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch("apps.workspaces.tasks._run_pipeline_with_progress", return_value={"status": "ok"}),
        patch("apps.workspaces.tasks._defer_resume_for_job", new_callable=AsyncMock),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as mock_rebuild,
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    mock_rebuild.assert_not_awaited()
