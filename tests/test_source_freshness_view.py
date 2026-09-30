"""GET /api/workspaces/<id>/freshness/ feeds the chat's stale-data banner (#173)."""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import AsyncClient
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.common.error_codes import ErrorCode
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
)
from apps.workspaces.tasks import materialize_workspace

User = get_user_model()


@pytest.fixture
def queued_jobs():
    """Real queue rows, removed afterwards: procrastinate_jobs is unmanaged, so the
    transactional test flush would leave them behind."""
    before = set(ProcrastinateJob.objects.values_list("id", flat=True))
    yield
    added = set(ProcrastinateJob.objects.values_list("id", flat=True)) - before
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [list(added)])


async def _client(user):
    client = AsyncClient()
    await client.aforce_login(user)
    return client


async def _get(user, workspace):
    client = await _client(user)
    return await client.get(f"/api/workspaces/{workspace.id}/freshness/")


async def _skipped_load(workspace, tenant, loader_id):
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="serving_x", state=SchemaState.ACTIVE
    )
    await WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).aupdate(
        last_load={
            "refresh": "skipped",
            "error_code": ErrorCode.AUTH_TOKEN_EXPIRED,
            "at": timezone.now().isoformat(),
            "by": str(loader_id),
        }
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_exposes_the_threshold_setting(user, workspace, settings):
    settings.STALE_DATA_BANNER_HOURS = 24
    assert (await _get(user, workspace)).json()["stale_data_banner_hours"] == 24

    settings.STALE_DATA_BANNER_HOURS = 6
    assert (await _get(user, workspace)).json()["stale_data_banner_hours"] == 6


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_says_reconnect_when_the_viewers_own_sign_in_expired(user, workspace, tenant):
    await _skipped_load(workspace, tenant, user.id)

    (source,) = (await _get(user, workspace)).json()["sources"]

    assert source["serving"] is True
    assert source["reconnect"] is True
    assert source["provider_label"] == "CommCare HQ"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_offers_refresh_when_another_members_sign_in_expired(user, workspace, tenant):
    other = await User.objects.acreate_user(email="loader@example.com", password="x")
    await _skipped_load(workspace, tenant, other.id)

    (source,) = (await _get(user, workspace)).json()["sources"]

    assert source["reconnect"] is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_source_without_an_active_schema_is_not_serving(user, workspace):
    body = (await _get(user, workspace)).json()

    (source,) = body["sources"]
    assert source["serving"] is False
    assert source["last_fetched_at"] is None
    assert source["reconnect"] is False
    assert body["in_progress"] is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reports_a_running_load(user, workspace, tenant):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="loading_x", state=SchemaState.PROVISIONING
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
    )

    assert (await _get(user, workspace)).json()["in_progress"] is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reports_a_load_queued_but_not_yet_started(user, workspace, queued_jobs):
    # The banner's own Refresh defers the job with no run until a worker picks it up.
    await materialize_workspace.defer_async(
        workspace_id=str(workspace.id), user_id="", notify_thread=False
    )

    assert (await _get(user, workspace)).json()["in_progress"] is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_non_member_is_refused(workspace):
    stranger = await User.objects.acreate_user(email="stranger@example.com", password="x")

    assert (await _get(stranger, workspace)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_only_get_is_allowed(user, workspace):
    client = await _client(user)
    response = await client.post(f"/api/workspaces/{workspace.id}/freshness/")

    assert response.status_code == 405
