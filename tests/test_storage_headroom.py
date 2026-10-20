import logging
from types import SimpleNamespace
from unittest import mock

import pytest
from django.db import connection

from apps.telemetry import storage, tasks

GIB = 2**30


def _usage(free_gib: float, total_gib: float = 50):
    return SimpleNamespace(total=int(total_gib * GIB), free=int(free_gib * GIB))


@pytest.fixture
def sentry():
    with mock.patch("apps.telemetry.storage.sentry_sdk") as sentry_mock:
        yield sentry_mock


@pytest.fixture
def disk():
    with mock.patch("apps.telemetry.storage.shutil.disk_usage") as usage_mock:
        yield usage_mock


def _scope(sentry):
    return sentry.new_scope.return_value.__enter__.return_value


@pytest.mark.parametrize(
    ("free_gib", "expected"),
    [(25, None), (10, None), (9.9, storage.WARNING), (5, storage.WARNING), (4.9, storage.CRITICAL)],
)
def test_severity_follows_the_thresholds(free_gib, expected):
    reading = storage.DiskReading("/", total_bytes=50 * GIB, free_bytes=int(free_gib * GIB))
    assert storage.disk_severity(reading) == expected


def test_thresholds_are_settings(settings):
    settings.STORAGE_DISK_WARNING_FREE_PERCENT = 50
    settings.STORAGE_DISK_CRITICAL_FREE_PERCENT = 30
    reading = storage.DiskReading("/", total_bytes=100, free_bytes=40)
    assert storage.disk_severity(reading) == storage.WARNING


def test_an_empty_filesystem_reading_is_critical_not_a_crash():
    assert storage.disk_severity(storage.DiskReading("/", 0, 0)) == storage.CRITICAL


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_plenty_of_disk_sends_nothing_and_logs_both_readings(sentry, disk, caplog):
    disk.return_value = _usage(free_gib=30)
    with caplog.at_level(logging.INFO, logger="apps.telemetry.storage"):
        result = await storage.check_storage_headroom()

    sentry.capture_message.assert_not_called()
    assert result["disk_severity"] is None
    assert result["disk_free_percent"] == 60.0
    assert result["db_bytes"] > 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("host disk 30.0 GiB free of 50.0 GiB (60.0%)" in m for m in messages)
    assert any("platform DB" in m and "largest tables:" in m for m in messages)
    assert all(r.levelno == logging.INFO for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_low_disk_is_a_sentry_warning_with_numbers(sentry, disk):
    disk.return_value = _usage(free_gib=7.5)
    await storage.check_storage_headroom()

    sentry.capture_message.assert_called_once()
    message = sentry.capture_message.call_args.args[0]
    assert message == "Host disk low: 15.0% free (7.5 GiB of 50.0 GiB), below 20%"
    assert sentry.capture_message.call_args.kwargs["level"] == "warning"
    scope = _scope(sentry)
    assert scope.fingerprint == ["storage-headroom", "host-disk", "warning"]
    context = scope.set_context.call_args.args
    assert context[0] == "storage"
    assert context[1]["free_bytes"] == int(7.5 * GIB)
    assert context[1]["total_bytes"] == 50 * GIB
    assert context[1]["threshold_percent"] == 20.0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_critical_disk_is_a_sentry_error_in_its_own_issue(sentry, disk):
    disk.return_value = _usage(free_gib=2)
    await storage.check_storage_headroom()

    assert sentry.capture_message.call_args.kwargs["level"] == "error"
    assert _scope(sentry).fingerprint == ["storage-headroom", "host-disk", "critical"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_fingerprint_does_not_change_with_the_numbers(sentry, disk):
    fingerprints = []
    for free_gib in (9, 8, 6.2):
        disk.return_value = _usage(free_gib=free_gib)
        await storage.check_storage_headroom()
        fingerprints.append(list(_scope(sentry).fingerprint))
    assert fingerprints == [["storage-headroom", "host-disk", "warning"]] * 3


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_configured_path_is_checked(sentry, disk, settings):
    settings.STORAGE_DISK_PATH = "/var/lib/docker"
    disk.return_value = _usage(free_gib=30)
    await storage.check_storage_headroom()
    disk.assert_called_once_with("/var/lib/docker")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_failed_db_read_still_sends_the_disk_alert(sentry, disk):
    disk.return_value = _usage(free_gib=1)
    with mock.patch.object(storage, "_platform_db_sizes", side_effect=RuntimeError("db down")):
        result = await storage.check_storage_headroom()

    sentry.capture_message.assert_called_once()
    assert "db_bytes" not in result


@pytest.mark.django_db
def test_db_size_matches_postgres():
    total, largest = storage._platform_db_sizes()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        assert total == pytest.approx(cursor.fetchone()[0], rel=0.05)
    assert 0 < len(largest) <= 5
    sizes = [size for _, size in largest]
    assert sizes == sorted(sizes, reverse=True)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_periodic_task_runs_the_check(sentry, disk):
    disk.return_value = _usage(free_gib=30)
    result = await tasks.storage_headroom.func()
    assert result["disk_severity"] is None
    assert result["db_bytes"] > 0
