"""The daily snapshot records tenant schema sizes and platform totals, once per day."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactType
from apps.chat.models import Thread
from apps.telemetry import snapshots, tasks
from apps.telemetry.models import DailySnapshot, SnapshotMetric
from apps.workspaces.models import SchemaState, TenantSchema

SCHEMA = "telemetry_size_probe"


async def _values(day):
    return {(s.metric, s.dimension): s.value async for s in DailySnapshot.objects.filter(day=day)}


def _test_db_connection():
    params = connection.get_connection_params()
    return psycopg.connect(
        dbname=params.get("dbname"),
        user=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=params.get("port"),
        autocommit=True,
    )


@pytest.fixture
def managed_is_test_db():
    with patch.object(snapshots, "get_managed_db_connection", _test_db_connection):
        yield


@pytest.fixture
def sized_schema(transactional_db, tenant):
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        cursor.execute(f"CREATE TABLE {SCHEMA}.cases AS SELECT generate_series(1, 5000) AS n")
    TenantSchema.objects.create(tenant=tenant, schema_name=SCHEMA, state=SchemaState.ACTIVE)
    # Never measured: dropped already.
    TenantSchema.objects.create(
        tenant=tenant, schema_name="telemetry_gone_probe", state=SchemaState.EXPIRED
    )
    # Measured but absent on disk, as after an external drop: no bytes, no error.
    TenantSchema.objects.create(
        tenant=tenant, schema_name="telemetry_missing_probe", state=SchemaState.ACTIVE
    )
    yield tenant
    with connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@pytest.fixture
def platform(transactional_db, user, workspace):
    Thread.objects.create(workspace=workspace, user=user)
    Artifact.objects.create(
        workspace=workspace, created_by=user, title="t", artifact_type=ArtifactType.REACT, code=""
    )
    deleted = Artifact.objects.create(
        workspace=workspace, created_by=user, title="d", artifact_type=ArtifactType.REACT, code=""
    )
    Artifact.all_objects.filter(pk=deleted.pk).update(is_deleted=True)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_snapshot_counts_the_platform(platform, managed_is_test_db):
    today = timezone.now().date()

    await snapshots.take_daily_snapshot(today, gauges=True)

    values = await _values(today)
    assert values[(SnapshotMetric.THREADS, "")] == 1
    assert values[(SnapshotMetric.ARTIFACTS, "")] == 1
    assert values[(SnapshotMetric.WORKSPACES, "")] == 1
    assert values[(SnapshotMetric.TENANTS, "")] == 1
    assert values[(SnapshotMetric.USERS, "")] == 1
    assert values[(SnapshotMetric.THREADS_UPDATED, "")] == 1
    assert values[(SnapshotMetric.ARTIFACTS_UPDATED, "")] == 1
    assert values[(SnapshotMetric.WORKSPACES_UPDATED, "")] == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_updates_are_counted_only_on_their_own_day(platform, managed_is_test_db):
    yesterday = timezone.now().date() - timedelta(days=1)

    await snapshots.take_daily_snapshot(yesterday)

    values = await _values(yesterday)
    assert values[(SnapshotMetric.THREADS, "")] == 1
    assert values[(SnapshotMetric.THREADS_UPDATED, "")] == 0
    assert values[(SnapshotMetric.TENANTS_UPDATED, "")] == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_backfill_never_overwrites_a_past_days_totals(platform, managed_is_test_db):
    past = timezone.now().date() - timedelta(days=5)
    await DailySnapshot.objects.acreate(day=past, metric=SnapshotMetric.THREADS, value=42)

    await snapshots.take_daily_snapshot(past)

    values = await _values(past)
    assert values[(SnapshotMetric.THREADS, "")] == 42
    assert values[(SnapshotMetric.THREADS_UPDATED, "")] == 0
    assert not any(metric == SnapshotMetric.SCHEMA_BYTES for metric, _ in values)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_schemas_kept_on_disk_but_not_serving_are_counted(sized_schema, managed_is_test_db):
    await TenantSchema.objects.filter(schema_name=SCHEMA).aupdate(state=SchemaState.FAILED)
    today = timezone.now().date()

    await snapshots.take_daily_snapshot(today, gauges=True)

    values = await _values(today)
    total = values[(SnapshotMetric.SCHEMA_BYTES, "")]
    assert total > 100_000
    assert values[(SnapshotMetric.SCHEMA_BYTES_RETAINED, "")] == total


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_schema_sizes_are_recorded_per_tenant_and_in_total(sized_schema, managed_is_test_db):
    today = timezone.now().date()

    await snapshots.take_daily_snapshot(today, gauges=True)

    values = await _values(today)
    tenant_bytes = values[(SnapshotMetric.SCHEMA_BYTES, str(sized_schema.id))]
    assert tenant_bytes > 100_000
    assert values[(SnapshotMetric.SCHEMA_BYTES, "")] == tenant_bytes


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rerunning_a_day_replaces_its_values(platform, managed_is_test_db, user, workspace):
    today = timezone.now().date()
    await snapshots.take_daily_snapshot(today, gauges=True)
    rows_before = await DailySnapshot.objects.acount()
    await Thread.objects.acreate(workspace=workspace, user=user)

    await snapshots.take_daily_snapshot(today, gauges=True)

    assert await DailySnapshot.objects.acount() == rows_before
    assert (await _values(today))[(SnapshotMetric.THREADS, "")] == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_failed_size_query_still_records_the_counts(sized_schema):
    today = timezone.now().date()

    with patch.object(snapshots, "get_managed_db_connection", side_effect=OSError("down")):
        await snapshots.take_daily_snapshot(today, gauges=True)

    values = await _values(today)
    assert values[(SnapshotMetric.TENANTS, "")] == 1
    assert not any(metric == SnapshotMetric.SCHEMA_BYTES for metric, _ in values)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_task_snapshots_the_day_before_its_schedule(managed_is_test_db):
    scheduled = datetime(2026, 3, 2, 0, 20, tzinfo=UTC)

    await tasks.snapshot_daily_metrics.func(timestamp=int(scheduled.timestamp()))

    assert (await _values(date(2026, 3, 1)))[(SnapshotMetric.USERS, "")] == 0
