"""The daily snapshot records tenant schema sizes and platform totals, once per day."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import psycopg
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactType
from apps.chat.models import Thread
from apps.telemetry import snapshots, tasks
from apps.telemetry.models import DailySnapshot, SnapshotMetric
from apps.users.models import Tenant
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
        cursor.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cursor.execute(f"CREATE SCHEMA {SCHEMA}")
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
    assert values[(SnapshotMetric.ARTIFACTS_UPDATED, "")] == 2  # the soft delete counts
    assert values[(SnapshotMetric.WORKSPACES_UPDATED, "")] == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_updates_are_counted_only_on_their_own_day(platform, managed_is_test_db):
    yesterday = timezone.now().date() - timedelta(days=1)

    await snapshots.take_daily_snapshot(yesterday, gauges=True)

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
    assert values[(SnapshotMetric.SCHEMA_BYTES_RETAINED, "")] == 0
    assert values[(SnapshotMetric.SCHEMAS_SKIPPED, "")] == 0


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

    await DailySnapshot.objects.acreate(day=today, metric=SnapshotMetric.SCHEMA_BYTES, value=7)

    with patch.object(snapshots, "get_managed_db_connection", side_effect=OSError("down")):
        await snapshots.take_daily_snapshot(today, gauges=True)

    values = await _values(today)
    assert values[(SnapshotMetric.TENANTS, "")] == 1
    # An earlier run's sizes for the day survive a sizing that could not run.
    assert values[(SnapshotMetric.SCHEMA_BYTES, "")] == 7
    # Both measured schemas went unsized, which the night records.
    assert values[(SnapshotMetric.SCHEMAS_SKIPPED, "")] == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_task_snapshots_the_day_before_its_schedule(managed_is_test_db):
    scheduled = timezone.now() - timedelta(hours=1)

    await tasks.snapshot_daily_metrics.func(timestamp=int(scheduled.timestamp()))

    day = (scheduled - timedelta(days=1)).date()
    assert (await _values(day))[(SnapshotMetric.USERS, "")] == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_run_stuck_in_the_queue_writes_no_gauges(managed_is_test_db):
    scheduled = datetime(2026, 3, 2, 0, 20, tzinfo=UTC)

    await tasks.snapshot_daily_metrics.func(timestamp=int(scheduled.timestamp()))

    values = await _values(date(2026, 3, 1))
    assert (SnapshotMetric.USERS, "") not in values
    assert values[(SnapshotMetric.THREADS_UPDATED, "")] == 0


class _LockedSchema:
    """A managed connection on which sizing one schema hits its lock timeout."""

    def __init__(self, conn, locked):
        self._conn, self._locked = conn, locked

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def execute(self, sql, params=None):
        if params == [self._locked]:
            raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
        return self._conn.execute(sql, params)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_locked_schema_is_skipped_and_totals_are_withheld(sized_schema):
    other_tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="other-size", canonical_name="Other"
    )
    await sync_to_async(_make_schema)("telemetry_other_probe")
    await TenantSchema.objects.acreate(
        tenant=other_tenant, schema_name="telemetry_other_probe", state=SchemaState.ACTIVE
    )
    today = timezone.now().date()
    # An earlier run that day measured the now-locked tenant; it must not linger.
    await DailySnapshot.objects.abulk_create(
        [
            DailySnapshot(
                day=today,
                metric=SnapshotMetric.SCHEMA_BYTES,
                dimension=str(sized_schema.id),
                value=1,
            ),
            DailySnapshot(day=today, metric=SnapshotMetric.SCHEMA_BYTES, value=1),
            DailySnapshot(day=today, metric=SnapshotMetric.SCHEMA_BYTES_RETAINED, value=1),
        ]
    )

    try:
        with patch.object(
            snapshots,
            "get_managed_db_connection",
            lambda: _LockedSchema(_test_db_connection(), SCHEMA),
        ):
            await snapshots.take_daily_snapshot(today, gauges=True)
    finally:
        await sync_to_async(_drop_schema)("telemetry_other_probe")

    values = await _values(today)
    assert values[(SnapshotMetric.SCHEMAS_SKIPPED, "")] == 1
    assert (SnapshotMetric.SCHEMA_BYTES, str(sized_schema.id)) not in values
    assert values[(SnapshotMetric.SCHEMA_BYTES, str(other_tenant.id))] > 0
    assert (SnapshotMetric.SCHEMA_BYTES, "") not in values
    assert (SnapshotMetric.SCHEMA_BYTES_RETAINED, "") not in values


def _make_schema(name):
    with connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        cursor.execute(f"CREATE SCHEMA {name}")
        cursor.execute(f"CREATE TABLE {name}.t AS SELECT 1 AS n")


def _drop_schema(name):
    with connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def test_size_statements_are_bounded():
    executed = []

    class Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            executed.append(sql)
            return type("Result", (), {"fetchone": lambda _self: (0, 0)})()

    with patch.object(snapshots, "get_managed_db_connection", Recorder):
        async_to_sync(snapshots._schema_sizes)(["a"])

    assert executed[:2] == ["SET lock_timeout = 2000", "SET statement_timeout = 30000"]
