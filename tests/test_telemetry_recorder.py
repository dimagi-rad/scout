"""The telemetry recorder never fails its caller, and the retention prune keeps recent events."""

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.telemetry import recorder, tasks
from apps.telemetry.models import EventKind, TelemetryEvent
from apps.users.models import User


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_arecord_writes_one_event():
    user_id = uuid.uuid4()
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
        TelemetryEvent.objects, "abulk_create", side_effect=RuntimeError("db gone")
    ):
        await recorder.arecord(EventKind.CHAT_TURN)

    assert await TelemetryEvent.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_arecord_ignores_an_unparseable_id():
    await recorder.arecord(EventKind.CHAT_TURN, user_id="not-a-uuid")

    event = await TelemetryEvent.objects.aget()
    assert event.user_id is None


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
            "prompt": "x" * (recorder.MAX_ATTR_STRING + 1),
            "nested": {"text": "hello"},
            "items": ["a"],
        },
    )

    assert event.attrs == {"tokens": 10, "ratio": 0.5, "cached": True, "model": "claude-x"}


def test_negative_duration_is_clamped():
    assert recorder.build_event(EventKind.CHAT_TURN, duration_ms=-5).duration_ms == 0


@pytest.mark.django_db
def test_record_failure_leaves_the_callers_transaction_usable():
    with transaction.atomic():
        with mock.patch.object(TelemetryEvent, "save", side_effect=IntegrityError("boom")):
            recorder.record(EventKind.ARTIFACT_VIEW)
        # The caller's transaction still accepts writes after the failed insert.
        User.objects.create_user(email="still-ok@example.com", password="pass")

    assert User.objects.filter(email="still-ok@example.com").exists()
    assert TelemetryEvent.objects.count() == 0


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
async def test_prune_task_is_scheduled_daily():
    assert await tasks.prune_telemetry.func() == {"deleted": 0}
    assert any(
        task.task is tasks.prune_telemetry
        for task in tasks.app.periodic_registry.periodic_tasks.values()
    )
