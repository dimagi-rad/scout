"""A source the latest load did not refresh is named, with its data age and fix (#715).

An expired CommCare HQ sign-in fails the credential preflight, so that source keeps
serving its last snapshot while a healthy sibling refreshes. The run looked
"completed" and the agent blamed the loader; it must instead say which source is
stale and to reconnect.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.utils import timezone

from apps.agents.graph import base as graph_base
from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant
from apps.users.services.credential_resolver import CredentialResolutionError
from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.source_freshness import (
    REFRESHED,
    REUSED,
    SKIPPED,
    arecord_load_outcomes,
    aworkspace_source_freshness,
    load_outcome,
    remedy,
)
from mcp_server.server import get_schema_status
from tests.pipeline_doubles import completed_pipeline_run
from tests.tenant_access import agrant_tenant_access

RECONNECT_HQ = "reconnect CommCare HQ in Connected Accounts"


@pytest.fixture(autouse=True)
def _no_candidate_ddl(no_candidate_ddl):
    """Shared stub: see tests.pipeline_doubles.no_candidate_ddl."""


def _registry(*providers):
    pipelines = {}
    for provider in providers:
        pipeline = MagicMock()
        pipeline.provider = provider
        pipeline.name = f"{provider}_sync"
        pipelines[pipeline.name] = pipeline
    registry = MagicMock()
    registry.list.return_value = list(pipelines.values())
    registry.get.side_effect = pipelines.__getitem__
    return registry


async def _serving_snapshot(tenant, fetched_at):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"t_{tenant.external_id}_old", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline=f"{tenant.provider}_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=fetched_at,
    )


@pytest.fixture
def month_ago():
    return timezone.now() - timedelta(days=30)


async def _expired_hq_load(workspace, tenant, user, month_ago):
    """Load a workspace whose HQ sign-in expired beside a healthy Connect source."""
    connect = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="7001", canonical_name="Reading Opp"
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=connect)
    await agrant_tenant_access(user, connect)
    await _serving_snapshot(tenant, month_ago)

    async def credential(membership):
        if membership.tenant_id == tenant.id:
            raise CredentialResolutionError(
                ErrorCode.AUTH_TOKEN_EXPIRED, "Your CommCare sign-in has expired."
            )
        return {"type": "api_key", "value": "k"}

    with (
        patch(
            "apps.workspaces.tasks.get_registry",
            return_value=_registry("commcare", "commcare_connect"),
        ),
        patch("apps.workspaces.tasks.aresolve_credential", credential),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=completed_pipeline_run,
        ),
        patch("apps.workspaces.tasks.SchemaManager", return_value=MagicMock()),
        patch("apps.workspaces.tasks.build_and_promote_cube_schema"),
        patch("apps.workspaces.tasks._rebuild_dependent_view_schemas", AsyncMock()),
    ):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name="ws_view", state=SchemaState.ACTIVE
    )
    return result, connect


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_result_says_which_source_was_not_refreshed(workspace, tenant, user, month_ago):
    result, connect = await _expired_hq_load(workspace, tenant, user, month_ago)

    by_tenant = {s["tenant_id"]: s for s in result["source_freshness"]}
    hq = by_tenant[str(tenant.id)]
    assert hq["refresh"] == SKIPPED
    assert hq["error_code"] == ErrorCode.AUTH_TOKEN_EXPIRED
    assert RECONNECT_HQ in hq["remedy"]
    assert hq["last_fetched_at"] == month_ago.isoformat()
    assert by_tenant[str(connect.id)]["refresh"] == REFRESHED
    assert "remedy" not in by_tenant[str(connect.id)]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_schema_status_names_the_stale_source_and_its_fix(workspace, tenant, user, month_ago):
    _result, connect = await _expired_hq_load(workspace, tenant, user, month_ago)

    with patch("mcp_server.server.workspace_list_tables", AsyncMock(return_value=[])):
        response = await get_schema_status(workspace_id=str(workspace.id))

    assert response["success"] is True
    by_tenant = {s["tenant_id"]: s for s in response["data"]["sources"]}
    hq = by_tenant[str(tenant.id)]
    assert hq["not_refreshed"] is True
    assert hq["last_load"] == SKIPPED
    assert hq["error_code"] == ErrorCode.AUTH_TOKEN_EXPIRED
    assert RECONNECT_HQ in hq["remedy"]
    assert hq["last_fetched_at"] == month_ago.isoformat()
    fresh = by_tenant[str(connect.id)]
    assert fresh["not_refreshed"] is False
    assert fresh["last_load"] == REFRESHED
    # The workspace-wide time is the fresh source's, which is why it misled.
    assert response["data"]["last_materialized_at"] == fresh["last_fetched_at"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("write_capable", [True, False])
async def test_prompt_names_the_stale_source_and_says_reconnect(
    workspace, tenant, user, month_ago, write_capable
):
    await _expired_hq_load(workspace, tenant, user, month_ago)
    with patch(
        "apps.agents.graph.base._fetch_semantic_model_context",
        AsyncMock(return_value="Data is loaded and ready."),
    ):
        _stable, volatile = await graph_base._build_system_prompt(
            workspace, user, write_capable=write_capable
        )

    hq_line = next(line for line in volatile.splitlines() if "Test Domain (CommCare HQ)" in line)
    assert "30 days ago" in hq_line
    assert "NOT refreshed" in hq_line
    assert ErrorCode.AUTH_TOKEN_EXPIRED in hq_line
    assert RECONNECT_HQ in hq_line
    connect_line = next(line for line in volatile.splitlines() if "Reading Opp" in line)
    assert "today" in connect_line
    assert "refreshed by the latest load" in connect_line
    assert "NOT refreshed" not in connect_line
    assert "do not tell the user that a refresh cannot help" in volatile
    assert ("read-only" in volatile) is not write_capable


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_later_fetch_clears_the_not_refreshed_flag(workspace, tenant):
    """A workspace sharing the source may fetch it after this workspace's load skipped it."""
    skipped_at = timezone.now() - timedelta(hours=2)
    await WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).aupdate(
        last_load={
            "refresh": SKIPPED,
            "error_code": ErrorCode.AUTH_TOKEN_EXPIRED,
            "at": skipped_at.isoformat(),
        }
    )
    await _serving_snapshot(tenant, skipped_at + timedelta(hours=1))

    (source,) = await aworkspace_source_freshness(workspace.id)

    assert source["not_refreshed"] is False
    assert "remedy" not in source


@pytest.mark.parametrize(
    ("entry", "refresh"),
    [
        ({"success": True, "result": {"status": "completed"}}, REFRESHED),
        ({"success": True, "result": {"status": "completed", "reused": True}}, REUSED),
        ({"success": True, "result": {"status": "already_loaded"}}, REUSED),
        ({"success": False, "error_code": ErrorCode.AUTH_ACCESS_DENIED}, SKIPPED),
        ({"success": False, "cancelled": True}, SKIPPED),
    ],
)
def test_load_outcome_classifies_each_tenant_entry(entry, refresh):
    assert load_outcome(entry)["refresh"] == refresh


def test_an_upstream_denial_is_not_told_to_reconnect():
    """A 403 survives a reconnect (#372); advising one sends the user in a loop."""
    advice = remedy({"error_code": ErrorCode.AUTH_ACCESS_DENIED}, "commcare")

    assert "reconnecting will not restore it" in advice
    assert "admin" in advice


def test_a_removed_source_is_not_sent_into_a_reconnect_loop():
    advice = remedy({"error_code": ErrorCode.WORKSPACE_TENANT_UNREACHABLE}, "commcare")

    assert "reconnecting cannot restore" in advice


def test_another_members_expired_sign_in_is_not_the_viewers_to_fix():
    outcome = {"error_code": ErrorCode.AUTH_TOKEN_EXPIRED}

    assert remedy(outcome, "commcare").startswith("reconnect CommCare HQ in Connected Accounts")
    other = remedy(outcome, "commcare", own_load=False)
    assert "the member who ran the last load" in other
    assert "sign-in works can refresh" in other


async def _skip(workspace, tenant, code, *, by=""):
    await WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).aupdate(
        last_load={
            "refresh": SKIPPED,
            "error_code": code,
            "at": timezone.now().isoformat(),
            "by": by,
        }
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_publishing_an_already_loaded_source_keeps_its_standing_skip(
    workspace, tenant, month_ago
):
    """Adding a source publishes the others without checking their sign-in."""
    await _serving_snapshot(tenant, month_ago)
    await _skip(workspace, tenant, ErrorCode.AUTH_TOKEN_EXPIRED)

    (recorded,) = await arecord_load_outcomes(
        workspace.id,
        [
            {
                "tenant": tenant.external_id,
                "tenant_id": str(tenant.id),
                "provider": tenant.provider,
                "success": True,
                "result": {"status": "already_loaded"},
            }
        ],
    )

    assert recorded["refresh"] == REUSED
    (source,) = await aworkspace_source_freshness(workspace.id)
    assert source["not_refreshed"] is True
    assert source["error_code"] == ErrorCode.AUTH_TOKEN_EXPIRED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_source_the_load_never_reached_is_not_called_refreshed(workspace, tenant):
    await WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).aupdate(
        last_load={"refresh": REFRESHED, "at": timezone.now().isoformat()}
    )

    (recorded,) = await arecord_load_outcomes(workspace.id, [])

    assert recorded["refresh"] == SKIPPED
    (source,) = await aworkspace_source_freshness(workspace.id)
    assert source["not_refreshed"] is True
    assert source["stopped"] is True
    assert "refresh the data again" in source["remedy"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_load_refused_for_an_expired_sign_in_records_the_skip(workspace, tenant, user):
    """With upstream freshness enforced, an expired sign-in is refused before the core runs."""
    entry = {
        "tenant": tenant.external_id,
        "tenant_id": str(tenant.id),
        "provider": tenant.provider,
        "state": "not_run",
        "success": False,
        "error": "Your CommCare sign-in has expired.",
        "error_code": ErrorCode.AUTH_TOKEN_EXPIRED,
    }
    denial = {"status": "denied", "error_code": ErrorCode.AUTH_TOKEN_EXPIRED, "tenants": [entry]}
    with patch(
        "apps.workspaces.tasks._materialization_write_denial", AsyncMock(return_value=denial)
    ):
        result = await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert result["status"] == "denied"
    (source,) = await aworkspace_source_freshness(workspace.id, user.id)
    assert source["not_refreshed"] is True
    assert RECONNECT_HQ in source["remedy"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_role_denial_leaves_the_source_records_alone(workspace, tenant, user):
    denial = {
        "status": "denied",
        "error_code": ErrorCode.WORKSPACE_ROLE_INSUFFICIENT,
        "tenants": [],
    }
    with patch(
        "apps.workspaces.tasks._materialization_write_denial", AsyncMock(return_value=denial)
    ):
        await workspaces_tasks.materialize_workspace_core(str(workspace.id), str(user.id))

    assert (
        await WorkspaceTenant.objects.filter(workspace=workspace, last_load__isnull=False).acount()
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prompt_says_when_a_stale_source_is_left_out_of_the_queryable_data(
    workspace, tenant, user, month_ago
):
    other = await Tenant.objects.acreate(
        provider="commcare", external_id="kept", canonical_name="Kept"
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
    await _serving_snapshot(tenant, month_ago)
    await _serving_snapshot(other, timezone.now())
    await _skip(workspace, tenant, ErrorCode.INTERNAL_ERROR, by=str(user.id))
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="ws_view_excluding",
        state=SchemaState.ACTIVE,
        tenant_coverage={
            "included_tenants": [{"tenant_id": str(other.id)}],
            "excluded_tenants": [{"tenant_id": str(tenant.id)}],
        },
    )
    with patch(
        "apps.agents.graph.base._fetch_semantic_model_context",
        AsyncMock(return_value="Data is loaded and ready."),
    ):
        _stable, volatile = await graph_base._build_system_prompt(
            workspace, user, write_capable=True
        )

    hq_line = next(line for line in volatile.splitlines() if "Test Domain (CommCare HQ)" in line)
    assert "not in what this workspace can query" in hq_line
    assert "report the error" in hq_line
    # Not a sign-in problem, so the agent may say the load itself failed.
    assert "loader is at fault" not in volatile
