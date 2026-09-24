"""Direct tests for the refresh_tenant_schema task."""

import asyncio
import contextlib
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, User
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services import data_operation
from apps.workspaces.services.data_operation import tenant_data_lock
from apps.workspaces.services.load_candidates import promote_candidate_schema
from apps.workspaces.services.refresh_requests import (
    fail_claimed_refresh_candidate,
)
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.tasks import refresh_tenant_schema
from mcp_server.context import load_tenant_context
from mcp_server.pipeline_registry import PipelineConfig
from tests.pipeline_doubles import completed_refresh_run
from tests.tenant_access import arecord_fresh_proof


@pytest.fixture
def provisioning_schema(db, tenant, workspace, tenant_membership_obj):
    schema = TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_domain_r12345678",
        state=SchemaState.PROVISIONING,
        refresh_workspace_id=workspace.id,
        refresh_actor_user_id=tenant_membership_obj.user_id,
        refresh_membership_id=tenant_membership_obj.id,
    )
    args = {
        "schema_id": str(schema.id),
        "membership_id": str(tenant_membership_obj.id),
        "actor_user_id": str(tenant_membership_obj.user_id),
        "workspace_id": str(workspace.id),
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args) "
            "VALUES (%s, %s, %s::procrastinate_job_status, %s::jsonb) RETURNING id",
            ["default", "apps.workspaces.tasks.refresh_tenant_schema", "doing", json.dumps(args)],
        )
        job_id = cursor.fetchone()[0]
    schema.refresh_job_id = job_id
    schema.save(update_fields=["refresh_job_id"])
    yield schema
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [job_id])


@pytest.fixture
def old_active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_domain",
        state=SchemaState.ACTIVE,
    )


@pytest.fixture
def tenant_membership_obj(db, user, tenant):
    from apps.users.models import TenantMembership

    tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return tm


def _mock_conn():
    conn = MagicMock()
    conn.cursor.return_value = MagicMock()
    return conn


def _mock_registry(provider="commcare"):
    """Return a mock registry whose list() yields one pipeline for the given provider."""
    pipeline = MagicMock()
    pipeline.provider = provider
    pipeline.name = f"{provider}_sync"
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = MagicMock()
    return registry


async def _refresh_auth_kwargs(membership):
    workspace = await Workspace.objects.filter(
        workspace_tenants__tenant_id=membership.tenant_id,
        memberships__user_id=membership.user_id,
    ).afirst()
    assert workspace is not None
    return {"actor_user_id": str(membership.user_id), "workspace_id": str(workspace.id)}


@sync_to_async
def _rebind_refresh(schema, membership, workspace):
    args = {
        "schema_id": str(schema.id),
        "membership_id": str(membership.id),
        "actor_user_id": str(membership.user_id),
        "workspace_id": str(workspace.id),
    }
    with transaction.atomic():
        TenantSchema.objects.filter(id=schema.id).update(
            refresh_workspace_id=workspace.id,
            refresh_actor_user_id=membership.user_id,
            refresh_membership_id=membership.id,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE procrastinate_jobs SET args = %s::jsonb WHERE id = %s",
                [json.dumps(args), schema.refresh_job_id],
            )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_rejects_legacy_job_without_actor_context(
    provisioning_schema, tenant_membership_obj
):
    pipeline = MagicMock()
    with patch("apps.workspaces.tasks.run_pipeline", pipeline):
        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    assert result["status"] == "rejected"
    assert result["error_code"] == ErrorCode.REFRESH_REQUEST_MISMATCH
    assert result["retry_required"] is True
    assert "retry" in result["error"].lower()
    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.PROVISIONING
    pipeline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_denies_read_actor_before_schema_load(
    workspace, provisioning_schema, tenant_membership_obj, read_user
):
    pipeline = MagicMock()
    read_membership = await tenant_membership_obj.__class__.objects.aget(
        user=read_user, tenant=provisioning_schema.tenant
    )
    await _rebind_refresh(provisioning_schema, read_membership, workspace)
    with patch("apps.workspaces.tasks.run_pipeline", pipeline):
        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(read_membership.id),
            actor_user_id=str(read_user.id),
            workspace_id=str(workspace.id),
        )

    assert result["status"] == "denied"
    assert result["retry_required"] is True
    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    pipeline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_denial_never_demotes_a_serving_schema(
    workspace, provisioning_schema, tenant_membership_obj, read_user
):
    read_membership = await tenant_membership_obj.__class__.objects.aget(
        user=read_user, tenant=provisioning_schema.tenant
    )
    # Bind the job to the read-only actor so the claim passes its binding check and
    # only the candidate's ACTIVE state stands between the denial and a settle.
    await _rebind_refresh(provisioning_schema, read_membership, workspace)
    provisioning_schema.state = SchemaState.ACTIVE
    await provisioning_schema.asave(update_fields=["state"])

    with patch("apps.workspaces.tasks.run_pipeline") as pipeline:
        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(read_membership.id),
            actor_user_id=str(read_user.id),
            workspace_id=str(workspace.id),
        )

    assert result == {"status": "ignored"}
    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.ACTIVE
    pipeline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_rejects_schema_outside_authorized_workspace(
    provisioning_schema, tenant_membership_obj, user
):
    other = await Workspace.objects.acreate(name="Other", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=other, user=user, role=WorkspaceRole.MANAGE)
    pipeline = MagicMock()
    with patch("apps.workspaces.tasks.run_pipeline", pipeline):
        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            actor_user_id=str(user.id),
            workspace_id=str(other.id),
        )

    assert result["status"] == "rejected"
    assert result["error_code"] == ErrorCode.REFRESH_REQUEST_MISMATCH
    assert "workspace" in result["error"].lower()
    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.PROVISIONING
    pipeline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_schema_active_on_success(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            new=AsyncMock(return_value={"type": "api_key", "value": "tok"}),
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=completed_refresh_run) as pipeline,
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.ACTIVE
    assert result["status"] == "active"
    assert pipeline.call_args.kwargs["procrastinate_job_id"] == provisioning_schema.refresh_job_id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_real_pipeline_defers_refresh_promotion_to_owned_worker_cas(
    workspace, provisioning_schema, old_active_schema, tenant_membership_obj
):
    pipeline_config = PipelineConfig(
        name="commcare_refresh_test",
        description="",
        version="1.0",
        provider="commcare",
        sources=[],
    )
    registry = MagicMock()
    registry.list.return_value = [pipeline_config]
    registry.get.return_value = pipeline_config
    teardown_deferrer = MagicMock()
    teardown_deferrer.defer_async = AsyncMock(return_value=1)
    state_before_owned_promotion = []

    def observe_owned_promotion(schema_id, **kwargs):
        state_before_owned_promotion.append(TenantSchema.objects.get(id=schema_id).state)
        return promote_candidate_schema(schema_id, **kwargs)

    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            new=AsyncMock(return_value={"type": "api_key", "value": "tok"}),
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=registry),
        patch(
            "apps.workspaces.tasks.promote_candidate_schema",
            side_effect=observe_owned_promotion,
        ),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_semantic_model.defer_async",
            new_callable=AsyncMock,
        ) as semantic_rebuild,
        patch(
            "apps.workspaces.tasks.teardown_schema.configure",
            return_value=teardown_deferrer,
        ),
    ):
        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    await provisioning_schema.arefresh_from_db()
    await old_active_schema.arefresh_from_db()
    assert result["status"] == "active"
    assert state_before_owned_promotion == [SchemaState.PROVISIONING]
    assert provisioning_schema.state == SchemaState.ACTIVE
    assert await MaterializationRun.objects.filter(
        tenant_schema=provisioning_schema,
        state=MaterializationRun.RunState.COMPLETED,
    ).aexists()
    assert old_active_schema.state == SchemaState.TEARDOWN
    semantic_rebuild.assert_awaited_once_with(workspace_id=str(workspace.id))
    teardown_deferrer.defer_async.assert_awaited_once_with(schema_id=str(old_active_schema.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_schedules_old_schema_teardown(
    provisioning_schema, old_active_schema, tenant_membership_obj
):
    """Old ACTIVE schemas are moved to TEARDOWN and a delayed teardown is scheduled."""
    deferrer = MagicMock()
    deferrer.defer_async = AsyncMock(return_value=1)

    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            new=AsyncMock(return_value={"type": "api_key", "value": "tok"}),
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=completed_refresh_run),
        patch(
            "apps.workspaces.tasks.teardown_schema.configure",
            return_value=deferrer,
        ) as mock_configure,
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    await old_active_schema.arefresh_from_db()
    assert old_active_schema.state == SchemaState.TEARDOWN
    mock_configure.assert_called_once_with(schedule_in={"seconds": 30 * 60})
    deferrer.defer_async.assert_awaited_once_with(schema_id=str(old_active_schema.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_schema_creation_error(
    provisioning_schema, tenant_membership_obj
):
    with patch(
        "apps.workspaces.services.schema_manager.get_managed_db_connection",
        side_effect=RuntimeError("Managed DB unreachable"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_refresh_cancellation_drains_writer_then_fails_owned_candidate(
    settings, provisioning_schema, old_active_schema, tenant_membership_obj, cleanup_fails
):
    settings.MANAGED_DATABASE_URL = "postgresql://scout@localhost/scout"
    started = threading.Event()
    release = threading.Event()

    def blocked_create(_manager, _schema):
        started.set()
        assert release.wait(timeout=10)

    with (
        patch("apps.workspaces.tasks.SchemaManager.create_physical_schema", blocked_create),
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
        patch(
            "apps.workspaces.tasks.fail_claimed_refresh_candidate",
            wraps=fail_claimed_refresh_candidate,
            side_effect=DatabaseError("Cleanup database unavailable") if cleanup_fails else None,
        ),
    ):
        work = asyncio.create_task(
            refresh_tenant_schema.func(
                context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
                schema_id=str(provisioning_schema.id),
                membership_id=str(tenant_membership_obj.id),
                **await _refresh_auth_kwargs(tenant_membership_obj),
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 10)
            context = await load_tenant_context(
                provisioning_schema.tenant.external_id,
                provisioning_schema.tenant.provider,
            )
            assert context.schema_name == old_active_schema.schema_name
            work.cancel()
            await asyncio.sleep(0)
            assert not work.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await work
        finally:
            work.cancel()
            release.set()
            await asyncio.gather(work, return_exceptions=True)

    await provisioning_schema.arefresh_from_db()
    if cleanup_fails:
        assert provisioning_schema.state == SchemaState.PROVISIONING
        teardown.assert_not_called()
    else:
        assert provisioning_schema.state == SchemaState.FAILED
        teardown.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_no_credential(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch("apps.workspaces.tasks.aresolve_credential", new=AsyncMock(return_value=None)),
        patch("apps.workspaces.services.schema_manager.SchemaManager.teardown"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_materialization_error(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            new=AsyncMock(return_value={"type": "api_key", "value": "tok"}),
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch(
            "apps.workspaces.tasks.run_pipeline",
            side_effect=RuntimeError("Pipeline exploded"),
        ),
        patch("apps.workspaces.services.schema_manager.SchemaManager.teardown"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_resolves_credential_in_async_context(
    provisioning_schema, tenant_membership_obj
):
    """Regression: the task must resolve credentials via the async ORM path.

    The task runs as an ``async def`` procrastinate job, so it must use
    ``aresolve_credential``. Calling the sync ``resolve_credential`` here ran
    sync ORM queries in an async context and raised ``SynchronousOnlyOperation``
    in production. This test exercises the *real* resolver (no mock) against a
    stored API-key credential to catch that regression.
    """
    membership_tenant = await Tenant.objects.aget(id=tenant_membership_obj.tenant_id)
    conn = await TenantConnection.objects.acreate(
        user=await User.objects.aget(id=tenant_membership_obj.user_id),
        provider=membership_tenant.provider,
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("secret-key"),
    )
    tenant_membership_obj.connection = conn
    await tenant_membership_obj.asave(update_fields=["connection"])
    await arecord_fresh_proof(conn, await Tenant.objects.aget(id=tenant_membership_obj.tenant_id))

    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=completed_refresh_run),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.ACTIVE
    assert result["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_returns_error_for_unknown_schema(tenant_membership_obj):
    result = await refresh_tenant_schema(
        context=MagicMock(job=MagicMock(id=0)),
        schema_id="00000000-0000-0000-0000-000000000000",
        membership_id=str(tenant_membership_obj.id),
        **await _refresh_auth_kwargs(tenant_membership_obj),
    )
    assert result["status"] == "rejected"
    assert result["error_code"] == ErrorCode.REFRESH_REQUEST_MISMATCH
    assert "role" not in result["error"].lower()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_loads_into_new_schema_not_old_active(
    provisioning_schema, old_active_schema, tenant_membership_obj
):
    """Regression: refresh must load the fresh data INTO the new "_r" schema.

    The refresh task mints a new ``_r``-suffixed schema and then runs the
    materialization pipeline. Before the fix, the task called ``run_pipeline``
    without naming a target, so ``run_pipeline`` re-resolved the schema via
    ``SchemaManager().provision()`` — which returns the tenant's OLD active
    *base* schema. The data loaded into the old schema; the task then activated
    the empty new schema and tore down the data-bearing old one, destroying the
    data the refresh had just loaded (and leaving an empty, "successful" schema).

    This test faithfully reproduces the bug by mirroring ``run_pipeline``'s own
    target resolution (an explicit ``target_schema`` wins; otherwise
    ``provision()`` picks the base schema) and asserts the pipeline loads into
    the new ``_r`` schema, not the old active one.
    """
    loaded_schema_ids: list[str] = []

    def fake_run_pipeline(tenant_membership, credential, pipeline, target_schema=None, **kwargs):
        schema = target_schema or SchemaManager().provision(tenant_membership.tenant)
        loaded_schema_ids.append(str(schema.id))
        return {"status": "ok"}

    deferrer = MagicMock()
    deferrer.defer_async = AsyncMock(return_value=1)

    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            new=AsyncMock(return_value={"type": "api_key", "value": "tok"}),
        ),
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_mock_registry(),
        ),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=fake_run_pipeline),
        patch("apps.workspaces.tasks.teardown_schema.configure", return_value=deferrer),
    ):
        await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    assert loaded_schema_ids == [str(provisioning_schema.id)], (
        "Refresh loaded data into the wrong schema: expected the new schema "
        f"{provisioning_schema.schema_name!r}, but the pipeline targeted the old "
        "active base schema (the refresh-data-loss bug)."
    )


# ---------------------------------------------------------------------------
# Dependent multi-tenant view-schema rebuild on refresh (arch #236, finding 00#9)
# ---------------------------------------------------------------------------
#
# A tenant data schema is SHARED across workspaces. refresh_tenant_schema swaps in
# a NEW physical schema, so every multi-tenant WorkspaceViewSchema that includes
# the refreshed tenant still points its namespaced views at the OLD schema (about
# to be torn down). Mirroring materialize_workspace (PR #230), refresh must defer
# a rebuild for each such dependent workspace so the views are recreated against
# the new schema instead of being left to rot (and later falsely marked FAILED by
# teardown).


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_rebuilds_dependent_multitenant_view_schemas(
    provisioning_schema, tenant_membership_obj, user, tenant
):
    """After the swap, refresh defers a rebuild for the multi-tenant workspace B
    (sharing the refreshed tenant + a view schema) and NOT for a single-tenant
    workspace C."""
    # Dependent multi-tenant workspace B: shares `tenant` + an extra tenant, has a view schema.
    extra_tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="refresh-extra", canonical_name="Refresh Extra"
    )
    ws_b = await Workspace.objects.acreate(name="Refresh Sibling B", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=tenant)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=extra_tenant)
    await WorkspaceViewSchema.objects.acreate(
        workspace=ws_b, schema_name="ws_refresh_b", state=SchemaState.ACTIVE
    )

    # Single-tenant workspace C: only `tenant` → must NOT be rebuilt.
    ws_c = await Workspace.objects.acreate(name="Refresh Single C", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws_c, tenant=tenant)

    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            new=AsyncMock(return_value={"type": "api_key", "value": "tok"}),
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry()),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=completed_refresh_run),
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as mock_rebuild,
    ):
        await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    mock_rebuild.assert_awaited_once_with(workspace_id=str(ws_b.id))
    deferred_ids = {c.kwargs["workspace_id"] for c in mock_rebuild.await_args_list}
    assert str(ws_c.id) not in deferred_ids  # single-tenant workspaces excluded


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_claimed_refresh_keeps_query_context_on_previous_active_schema(settings, tenant):
    settings.MANAGED_DATABASE_URL = "postgresql://scout@localhost/scout"
    previous = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="previous_active", state=SchemaState.ACTIVE
    )
    await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="claimed_refresh",
        state=SchemaState.PROVISIONING,
        refresh_claimed_at=timezone.now(),
    )

    context = await load_tenant_context(tenant.external_id, tenant.provider)

    assert context.schema_name == previous.schema_name


def _refresh_patches(**overrides):
    patches = {
        "conn": patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        "credential": patch(
            "apps.workspaces.tasks.aresolve_credential",
            new=AsyncMock(return_value={"type": "api_key", "value": "tok"}),
        ),
        "registry": patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry()),
        "pipeline": patch("apps.workspaces.tasks.run_pipeline", side_effect=completed_refresh_run),
        "retire": patch("apps.workspaces.tasks.teardown_schema.configure"),
    }
    patches.update(overrides)
    return patches


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_publishes_its_generation_so_equivalent_loads_reuse_it(
    provisioning_schema, tenant_membership_obj
):
    tenant_id = provisioning_schema.tenant_id
    patches = _refresh_patches()
    with contextlib.ExitStack() as stack:
        mocks = {name: stack.enter_context(p) for name, p in patches.items()}
        mocks["retire"].return_value.defer_async = AsyncMock(return_value=1)
        result = await refresh_tenant_schema(
            context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
            **await _refresh_auth_kwargs(tenant_membership_obj),
        )

    assert result["status"] == "active"
    ledger = await TenantLoadGeneration.objects.aget(tenant_id=tenant_id)
    assert ledger.published_generation == 1
    assert ledger.published_schema_id == provisioning_schema.id
    assert ledger.loading_generation == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_waits_for_its_tenant_and_fails_visibly_on_timeout(
    provisioning_schema, tenant_membership_obj
):
    """Another writer of the tenant (a workspace load) holds T: the refresh must not
    load alongside it, and a timeout fails the candidate instead of continuing."""
    tenant_id = provisioning_schema.tenant_id
    pipeline = MagicMock()
    holder_ready = asyncio.Event()
    release = asyncio.Event()

    async def hold_tenant():
        async with tenant_data_lock([tenant_id]):
            holder_ready.set()
            await release.wait()

    holder = asyncio.create_task(hold_tenant())
    await holder_ready.wait()
    try:
        with (
            patch.object(data_operation, "_LOCK_TIMEOUT", "300ms"),
            patch("apps.workspaces.tasks.aresolve_credential", new=AsyncMock()),
            patch("apps.workspaces.tasks.run_pipeline", pipeline),
            patch(
                "apps.workspaces.services.schema_manager.get_managed_db_connection",
                return_value=_mock_conn(),
            ),
        ):
            result = await refresh_tenant_schema(
                context=MagicMock(job=MagicMock(id=provisioning_schema.refresh_job_id)),
                schema_id=str(provisioning_schema.id),
                membership_id=str(tenant_membership_obj.id),
                **await _refresh_auth_kwargs(tenant_membership_obj),
            )
    finally:
        release.set()
        await holder

    assert result["retry_required"] is True
    pipeline.assert_not_called()
    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
