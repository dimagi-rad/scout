"""The telemetry recorder never fails its caller, and the retention prune keeps recent events."""

import uuid
from datetime import datetime, timedelta
from unittest import mock

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.telemetry import admin as admin_module
from apps.telemetry import recorder, tasks
from apps.telemetry.admin import EstimatedCountPaginator
from apps.telemetry.models import EventKind, TelemetryEvent
from apps.users.models import User


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_arecord_writes_one_event():
    user_id = 42
    await recorder.arecord(
        EventKind.TOOL_CALL,
        user_id=user_id,
        workspace_id=str(uuid.uuid4()),
        name="execute_sql",
        outcome="ok",
        duration_ms=12.6,
        attrs={"rows": 3},
    )

    event = await TelemetryEvent.objects.aget()
    assert event.kind == EventKind.TOOL_CALL
    assert event.user_id == user_id
    assert event.duration_ms == 13
    assert event.attrs == {"rows": 3}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_arecord_swallows_a_failed_insert():
    with mock.patch.object(
        TelemetryEvent.objects, "bulk_create", side_effect=RuntimeError("db gone")
    ):
        await recorder.arecord(EventKind.CHAT_TURN)

    assert await TelemetryEvent.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_arecord_ignores_unparseable_ids():
    await recorder.arecord(EventKind.CHAT_TURN, user_id="not-an-id", workspace_id="nope")

    event = await TelemetryEvent.objects.aget()
    assert event.user_id is None
    assert event.workspace_id is None


@pytest.mark.django_db
def test_event_keeps_a_real_users_id():
    user = User.objects.create_user(email="ids@example.com", password="pass")

    recorder.record(EventKind.CHAT_TURN, user_id=user.id)

    assert TelemetryEvent.objects.get().user_id == user.id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_values_postgres_rejects_never_reach_the_insert():
    await recorder.arecord_events(
        [
            recorder.build_event(
                EventKind.CHAT_TURN,
                duration_ms=float("inf"),
                attrs={"ratio": float("nan"), "big": float("-inf"), "nul": "a\x00b"},
            ),
            recorder.build_event(EventKind.TOOL_CALL, duration_ms=10**12),
        ]
    )

    events = {e.kind: e async for e in TelemetryEvent.objects.all()}
    assert events[EventKind.CHAT_TURN].duration_ms is None
    assert events[EventKind.CHAT_TURN].attrs == {}
    assert events[EventKind.TOOL_CALL].duration_ms == recorder.MAX_DURATION_MS


def test_build_event_never_raises_on_odd_input():
    event = recorder.build_event(
        EventKind.CHAT_TURN, name=123, outcome=None, attrs="not a dict", duration_ms="5"
    )

    assert event is not None
    assert (event.name, event.outcome, event.attrs, event.duration_ms) == ("", "", {}, None)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_arecord_writes_nothing_when_disabled(settings):
    settings.TELEMETRY_ENABLED = False

    await recorder.arecord(EventKind.CHAT_TURN)
    await recorder.arecord_events([recorder.build_event(EventKind.CHAT_TURN)])

    assert await TelemetryEvent.objects.acount() == 0


def test_attrs_keep_only_scalars_and_short_labels():
    event = recorder.build_event(
        EventKind.CHAT_TURN,
        attrs={
            "tokens": 10,
            "ratio": 0.5,
            "cached": True,
            "model": "claude-x",
            "reply": "yes",
            "username": "jdoe",
            "huge": 2**64,
            "prompt": "x" * 65,
            "question": "how many visits?",
            "email": "someone@example.com",
            "sql": "select * from t",
            "nested": {"text": "hello"},
            "items": ["a"],
            "Free Text Key": 1,
            "k" * 41: 1,
        },
    )

    assert event.attrs == {"tokens": 10, "ratio": 0.5, "cached": True, "model": "claude-x"}


def test_a_trailing_newline_is_not_a_label():
    assert not recorder.is_label("a" * 64 + "\n")
    assert recorder.build_event(EventKind.TOOL_CALL, name="sql\n").name == ""


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_unknown_keyword_never_raises():
    await recorder.arecord(EventKind.TOOL_CALL, tool="execute_sql")
    recorder_sync = sync_to_async(recorder.record)
    await recorder_sync(EventKind.TOOL_CALL, tool="execute_sql")

    assert await TelemetryEvent.objects.acount() == 0


def test_name_must_be_a_label():
    assert recorder.build_event(EventKind.TOOL_CALL, name="execute_sql").name == "execute_sql"
    assert recorder.build_event(EventKind.TOOL_CALL, name="what is this?").name == ""


def test_negative_duration_is_clamped():
    assert recorder.build_event(EventKind.CHAT_TURN, duration_ms=-5).duration_ms == 0


@pytest.mark.django_db
def test_record_failure_leaves_the_callers_transaction_usable():
    # duration_ms=-1 fails the column's CHECK constraint inside Postgres, which
    # aborts the transaction unless the insert ran in its own savepoint.
    invalid = TelemetryEvent(kind=EventKind.ARTIFACT_VIEW, duration_ms=-1)
    with transaction.atomic():
        with mock.patch.object(recorder, "build_event", return_value=invalid):
            recorder.record(EventKind.ARTIFACT_VIEW)
        User.objects.create_user(email="still-ok@example.com", password="pass")

    assert User.objects.filter(email="still-ok@example.com").exists()
    assert TelemetryEvent.objects.count() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_failure_leaves_an_open_transaction_usable():
    invalid = TelemetryEvent(kind=EventKind.ARTIFACT_VIEW, duration_ms=-1)

    def write_inside_a_transaction():
        with transaction.atomic():
            async_to_sync(recorder.arecord_events)([invalid])
            User.objects.create_user(email="async-ok@example.com", password="pass")

    await sync_to_async(write_inside_a_transaction)()

    assert await User.objects.filter(email="async-ok@example.com").aexists()
    assert await TelemetryEvent.objects.acount() == 0


def test_out_of_range_user_ids_and_naive_times_are_dropped():
    event = recorder.build_event(
        EventKind.CHAT_TURN, user_id=2**63, occurred_at=datetime(2026, 1, 1)
    )

    assert event.user_id is None
    assert event.occurred_at.tzinfo is not None


@pytest.mark.django_db
def test_admin_count_is_bounded_when_the_estimate_is_unknown(monkeypatch):
    # A fresh table has no planner estimate yet; the count must still be real so
    # the changelist keeps paginating instead of loading every row.
    monkeypatch.setattr(admin_module, "EXACT_COUNT_BOUND", 3)
    TelemetryEvent.objects.bulk_create([TelemetryEvent(kind=EventKind.CHAT_TURN) for _ in range(5)])
    paginator = EstimatedCountPaginator(TelemetryEvent.objects.order_by("-occurred_at"), 2)

    with CaptureQueriesContext(connection) as queries:
        assert paginator.count == 3

    assert any("LIMIT 3" in q["sql"] for q in queries.captured_queries)


@pytest.mark.django_db
def test_a_filtered_admin_list_gets_a_real_count(monkeypatch):
    # With the bound at 1 the unfiltered estimate (2) would be used; a filter must not.
    monkeypatch.setattr(admin_module, "EXACT_COUNT_BOUND", 1)
    TelemetryEvent.objects.bulk_create(
        [TelemetryEvent(kind=EventKind.CHAT_TURN), TelemetryEvent(kind=EventKind.TOOL_CALL)]
    )
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE telemetry_telemetryevent")
    filtered = TelemetryEvent.objects.filter(kind=EventKind.TOOL_CALL).order_by("-occurred_at")

    assert EstimatedCountPaginator(filtered, 100).count == 1


@pytest.mark.django_db
def test_record_writes_from_sync_code():
    recorder.record(EventKind.ARTIFACT_VIEW, name="dashboard")

    assert TelemetryEvent.objects.get().name == "dashboard"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prune_deletes_only_events_past_retention(monkeypatch):
    monkeypatch.setattr(tasks, "PRUNE_BATCH_SIZE", 2)
    now = timezone.now()
    old = now - timedelta(days=tasks.RETENTION_DAYS + 1)
    recent = now - timedelta(days=tasks.RETENTION_DAYS - 1)
    await TelemetryEvent.objects.abulk_create(
        [TelemetryEvent(kind=EventKind.CHAT_TURN, occurred_at=old) for _ in range(5)]
        + [TelemetryEvent(kind=EventKind.CHAT_TURN, occurred_at=recent)]
    )

    deleted = await tasks.prune_telemetry_events(now=now)

    assert deleted == 5
    remaining = [e async for e in TelemetryEvent.objects.all()]
    assert [e.occurred_at for e in remaining] == [recent]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prune_stops_after_its_batch_budget(monkeypatch):
    monkeypatch.setattr(tasks, "PRUNE_BATCH_SIZE", 1)
    monkeypatch.setattr(tasks, "PRUNE_MAX_BATCHES", 2)
    old = timezone.now() - timedelta(days=tasks.RETENTION_DAYS + 1)
    await TelemetryEvent.objects.abulk_create(
        [TelemetryEvent(kind=EventKind.CHAT_TURN, occurred_at=old) for _ in range(3)]
    )

    assert await tasks.prune_telemetry_events() == 2
    assert await TelemetryEvent.objects.acount() == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_exactly_drained_backlog_does_not_warn(monkeypatch, caplog):
    monkeypatch.setattr(tasks, "PRUNE_BATCH_SIZE", 1)
    monkeypatch.setattr(tasks, "PRUNE_MAX_BATCHES", 2)
    old = timezone.now() - timedelta(days=tasks.RETENTION_DAYS + 1)
    await TelemetryEvent.objects.abulk_create(
        [TelemetryEvent(kind=EventKind.CHAT_TURN, occurred_at=old) for _ in range(2)]
    )

    assert await tasks.prune_telemetry_events() == 2
    assert "batch budget" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prune_task_runs_the_prune():
    assert await tasks.prune_telemetry.func() == {"deleted": 0}


def test_prune_schedule_and_name_are_pinned():
    # Procrastinate keys periodic bookkeeping by task name, so a rename re-schedules.
    scheduled = {
        name: periodic.cron
        for (name, _), periodic in tasks.app.periodic_registry.periodic_tasks.items()
        if name.startswith("apps.telemetry.")
    }

    assert scheduled == {
        "apps.telemetry.tasks.prune_telemetry": "23 3 * * *",
        "apps.telemetry.tasks.snapshot_daily_metrics": "20 0 * * *",
    }
