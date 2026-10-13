"""A failed rebuild must never demote the schema Cube is serving."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import psycopg
import pytest
from django.db import OperationalError, connection, connections

from apps.common.errors import ExpectedStateError
from apps.semantic.models import CubeSchema, SemanticDataset, SemanticField, SemanticModel
from apps.semantic.services import cube_client, cube_schema
from apps.semantic.services.cube_schema import CubeSchemaBuildError, build_and_promote_cube_schema


@pytest.fixture
def model(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Serving", status="active")
    dataset = SemanticDataset.objects.create(
        workspace=workspace,
        semantic_model=model,
        name="visits",
        table_name="raw_visits",
        primary_key="id",
    )
    SemanticField.objects.create(
        dataset=dataset, name="id", expression="id", field_type="dimension"
    )
    return model


@pytest.fixture
def validator(monkeypatch):
    validate = AsyncMock(return_value={"valid": True})
    monkeypatch.setattr(cube_schema.CubeClient, "validate_schema", validate)
    monkeypatch.setattr(cube_schema.CubeClient, "invalidate_schema_cache", AsyncMock())
    monkeypatch.setattr(
        cube_schema,
        "load_workspace_context",
        AsyncMock(return_value=SimpleNamespace(schema_name="fixture", readonly_role="role")),
    )
    return validate


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("validation_required", [True, False])
def test_failed_identical_rebuild_keeps_the_serving_schema_active(
    workspace, model, validator, settings, validation_required
):
    settings.CUBE_SCHEMA_VALIDATION_REQUIRED = validation_required
    serving = build_and_promote_cube_schema(workspace, model=model)

    validator.return_value = {"valid": False, "errors": ["Validator unavailable"]}
    with pytest.raises(CubeSchemaBuildError):
        build_and_promote_cube_schema(workspace, model=model)

    serving.refresh_from_db()
    model.refresh_from_db()
    assert serving.status == CubeSchema.Status.ACTIVE
    assert CubeSchema.objects.filter(semantic_model=model).count() == 1
    assert model.status == SemanticModel.Status.ACTIVE
    assert model.metadata["last_build"]["ok"] is False


@pytest.mark.django_db(transaction=True)
def test_failed_changed_rebuild_records_an_error_row_beside_the_serving_schema(
    workspace, model, validator
):
    serving = build_and_promote_cube_schema(workspace, model=model)
    SemanticDataset.objects.filter(name="visits").update(description="Changed")

    validator.return_value = {"valid": False, "errors": ["Cube compiler failure"]}
    with pytest.raises(CubeSchemaBuildError):
        build_and_promote_cube_schema(workspace, model=model)

    serving.refresh_from_db()
    assert serving.status == CubeSchema.Status.ACTIVE
    failed = CubeSchema.objects.get(semantic_model=model, status=CubeSchema.Status.ERROR)
    assert failed.diagnostics[-1]["message"] == "Cube compiler failure"


@pytest.fixture
def cube_http(monkeypatch, settings):
    """Route CubeClient's real HTTP code through a scripted transport."""
    settings.CUBE_API_URL = "http://cube.test"
    settings.CUBE_VALIDATOR_URL = "http://validator.test"
    settings.CUBEJS_API_SECRET = "s" * 32
    monkeypatch.setattr(cube_client, "SCHEMA_RETRY_BASE_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        cube_schema,
        "load_workspace_context",
        AsyncMock(return_value=SimpleNamespace(schema_name="fixture", readonly_role="role")),
    )
    handlers = {"validate": None, "meta": None}
    calls = {"validate": 0, "meta": 0}

    def handler(request):
        kind = "validate" if request.url.host == "validator.test" else "meta"
        calls[kind] += 1
        scripted = handlers[kind]
        if scripted is not None:
            return scripted(request)
        return httpx.Response(200, json={"valid": True, "errors": []})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        cube_client.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    return SimpleNamespace(handlers=handlers, calls=calls)


def _read_timeout(request):
    raise httpx.ReadTimeout("timed out", request=request)


def _warnings_and_errors(caplog):
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.django_db(transaction=True)
def test_unreachable_validator_keeps_serving_and_is_an_expected_state(workspace, model, cube_http):
    serving = build_and_promote_cube_schema(workspace, model=model)
    SemanticDataset.objects.filter(name="visits").update(description="Changed")
    cube_http.handlers["validate"] = _read_timeout

    with pytest.raises(CubeSchemaBuildError) as raised:
        build_and_promote_cube_schema(workspace, model=model)

    assert isinstance(raised.value, ExpectedStateError)
    assert cube_http.calls["validate"] == 1 + 3
    serving.refresh_from_db()
    model.refresh_from_db()
    assert serving.status == CubeSchema.Status.ACTIVE
    assert model.status == SemanticModel.Status.ACTIVE
    assert model.metadata["last_build"]["ok"] is False


@pytest.mark.django_db(transaction=True)
def test_validator_hiccup_that_recovers_promotes_without_a_warning(
    workspace, model, cube_http, caplog
):
    failed_once = []

    def flaky(request):
        if not failed_once:
            failed_once.append(True)
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(200, json={"valid": True, "errors": []})

    cube_http.handlers["validate"] = flaky
    with caplog.at_level(logging.INFO):
        promoted = build_and_promote_cube_schema(workspace, model=model)

    assert promoted.status == CubeSchema.Status.ACTIVE
    assert _warnings_and_errors(caplog) == []


@pytest.mark.django_db(transaction=True)
def test_failed_cache_warmup_logs_one_warning_without_a_traceback(
    workspace, model, cube_http, caplog
):
    cube_http.handlers["meta"] = _read_timeout

    with caplog.at_level(logging.INFO):
        promoted = build_and_promote_cube_schema(workspace, model=model)

    assert promoted.status == CubeSchema.Status.ACTIVE
    assert cube_http.calls["meta"] == 1
    records = _warnings_and_errors(caplog)
    assert [record.levelno for record in records] == [logging.WARNING]
    assert records[0].exc_info is None
    assert str(workspace.id) in records[0].getMessage()


@pytest.fixture
def other_connection():
    other = connections.create_connection("default")
    yield other
    try:
        # Postgres drops session locks only once the backend exits, after close() returns.
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock_all()")
    finally:
        other.close()


def _hold_validator_slots(conn, slots):
    with conn.cursor() as cursor:
        for slot in slots:
            cursor.execute(
                "SELECT pg_advisory_lock(%s, %s)", [cube_schema.VALIDATOR_LOCK_CLASS, slot]
            )


@pytest.fixture
def keep_build_session(monkeypatch):
    """Keep the build's session open, or closing it would free its locks for it."""
    monkeypatch.setattr(cube_schema, "close_old_connections", lambda: None)


@pytest.mark.django_db(transaction=True)
def test_build_fails_over_when_every_validator_slot_stays_busy(
    workspace, model, cube_http, other_connection
):
    serving = build_and_promote_cube_schema(workspace, model=model)
    SemanticDataset.objects.filter(name="visits").update(description="Changed")
    _hold_validator_slots(other_connection, range(cube_schema.VALIDATOR_CONCURRENCY))

    with pytest.raises(cube_schema.CubeValidatorBusyError) as raised:
        build_and_promote_cube_schema(workspace, model=model, slot_wait_seconds=0)

    assert isinstance(raised.value, ExpectedStateError)
    assert cube_http.calls["validate"] == 1
    serving.refresh_from_db()
    model.refresh_from_db()
    assert serving.status == CubeSchema.Status.ACTIVE
    assert model.metadata["last_build"]["ok"] is False

    with other_connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock_all()")
    build_and_promote_cube_schema(workspace, model=model, slot_wait_seconds=0)
    assert cube_http.calls["validate"] == 2


@pytest.mark.django_db(transaction=True)
def test_validation_skips_a_busy_slot_and_releases_the_one_it_took(
    workspace, model, cube_http, other_connection, keep_build_session, monkeypatch
):
    monkeypatch.setattr(cube_schema, "VALIDATOR_CONCURRENCY", 2)
    _hold_validator_slots(other_connection, [0])

    build_and_promote_cube_schema(workspace, model=model)

    assert cube_http.calls["validate"] == 1
    with other_connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            [cube_schema.VALIDATOR_LOCK_CLASS, cube_schema.VALIDATOR_CONCURRENCY - 1],
        )
        assert cursor.fetchone()[0] is True


@pytest.mark.django_db(transaction=True)
def test_failed_validation_releases_its_slot(
    workspace, model, cube_http, other_connection, keep_build_session
):
    cube_http.handlers["validate"] = _read_timeout
    with pytest.raises(CubeSchemaBuildError):
        build_and_promote_cube_schema(workspace, model=model)

    with other_connection.cursor() as cursor:
        for slot in range(cube_schema.VALIDATOR_CONCURRENCY):
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [cube_schema.VALIDATOR_LOCK_CLASS, slot]
            )
            assert cursor.fetchone()[0] is True


def _free_slots(conn):
    free = []
    with conn.cursor() as cursor:
        for slot in range(cube_schema.VALIDATOR_CONCURRENCY):
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [cube_schema.VALIDATOR_LOCK_CLASS, slot]
            )
            if cursor.fetchone()[0]:
                free.append(slot)
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", [cube_schema.VALIDATOR_LOCK_CLASS, slot]
                )
    return free


@pytest.mark.django_db(transaction=True)
def test_slot_is_released_before_promotion_and_warmup(
    workspace, model, cube_http, keep_build_session
):
    settings_dict = connection.settings_dict
    # The scripted handlers run on async_to_sync's loop thread, which Django's
    # thread-bound connections refuse; a plain psycopg session can observe from there.
    observer = psycopg.connect(
        dbname=settings_dict["NAME"],
        user=settings_dict["USER"],
        password=settings_dict["PASSWORD"],
        host=settings_dict["HOST"],
        port=settings_dict["PORT"] or None,
        autocommit=True,
    )
    seen = {}

    def validate(request):
        seen["validate"] = _free_slots(observer)
        return httpx.Response(200, json={"valid": True, "errors": []})

    def meta(request):
        seen["meta"] = _free_slots(observer)
        return httpx.Response(200, json={})

    cube_http.handlers["validate"] = validate
    cube_http.handlers["meta"] = meta
    try:
        build_and_promote_cube_schema(workspace, model=model)
    finally:
        observer.close()

    assert len(seen["validate"]) == cube_schema.VALIDATOR_CONCURRENCY - 1
    assert seen["meta"] == list(range(cube_schema.VALIDATOR_CONCURRENCY))


@pytest.mark.django_db(transaction=True)
def test_releasing_a_slot_the_session_no_longer_holds_warns(caplog):
    with caplog.at_level(logging.WARNING, logger=cube_schema.__name__):
        cube_schema._ValidatorSlot(0).release()

    assert "already released" in caplog.text


@pytest.mark.parametrize(("in_atomic", "closed"), [(True, False), (False, True)])
def test_failed_release_closes_the_session_only_outside_a_transaction(
    monkeypatch, in_atomic, closed
):
    fake = SimpleNamespace(
        cursor=Mock(side_effect=RuntimeError("session gone")),
        in_atomic_block=in_atomic,
        close=Mock(),
    )
    monkeypatch.setattr(cube_schema, "connection", fake)

    cube_schema._ValidatorSlot(0).release()

    assert fake.close.called is closed


@pytest.mark.django_db(transaction=True)
def test_explicit_model_build_generates_without_holding_a_validator_slot(
    workspace, model, cube_http, other_connection, monkeypatch
):
    real_generate = cube_schema.generate_cube_schema
    seen = {}

    def observing_generate(semantic_model):
        seen["generate"] = _free_slots(other_connection)
        return real_generate(semantic_model)

    monkeypatch.setattr(cube_schema, "generate_cube_schema", observing_generate)
    build_and_promote_cube_schema(workspace, model=model)

    assert seen["generate"] == list(range(cube_schema.VALIDATOR_CONCURRENCY))


@pytest.fixture
def raw_holder():
    """A psycopg session outside Django that holds slots for the build to contend with."""
    settings_dict = connection.settings_dict
    holder = psycopg.connect(
        dbname=settings_dict["NAME"],
        user=settings_dict["USER"],
        password=settings_dict["PASSWORD"],
        host=settings_dict["HOST"],
        port=settings_dict["PORT"] or None,
        autocommit=True,
    )
    yield holder
    holder.close()


@pytest.fixture
def refreshed_model(workspace, model, cube_http, monkeypatch):
    """Drive the refreshed-model path (model=None) with an ACTIVE schema to protect."""
    refreshes = []

    def ensure(ws):
        refreshes.append(connection.in_atomic_block)
        return model

    monkeypatch.setattr(cube_schema, "ensure_semantic_model", ensure)
    build_and_promote_cube_schema(workspace)
    refreshes.clear()
    SemanticDataset.objects.filter(name="visits").update(description="Changed")
    return SimpleNamespace(refreshes=refreshes)


def _record_slot_attempts(monkeypatch):
    attempts = []
    real_try = cube_schema._try_acquire_validator_slot

    def recording_try():
        attempts.append(connection.in_atomic_block)
        return real_try()

    monkeypatch.setattr(cube_schema, "_try_acquire_validator_slot", recording_try)
    return attempts


@pytest.mark.django_db(transaction=True)
def test_refresh_under_contention_waits_outside_its_transaction_then_redoes_it(
    workspace, refreshed_model, cube_http, raw_holder, monkeypatch
):
    monkeypatch.setattr(cube_schema, "VALIDATOR_SLOT_POLL_SECONDS", 0)
    raw_holder.execute("SELECT pg_advisory_lock(%s, 0)", [cube_schema.VALIDATOR_LOCK_CLASS])
    attempts = []
    real_try = cube_schema._try_acquire_validator_slot

    def try_then_free_on_the_second_outside_poll():
        attempts.append(connection.in_atomic_block)
        if len(attempts) == 3:
            raw_holder.execute(
                "SELECT pg_advisory_unlock(%s, 0)", [cube_schema.VALIDATOR_LOCK_CLASS]
            )
        return real_try()

    monkeypatch.setattr(
        cube_schema, "_try_acquire_validator_slot", try_then_free_on_the_second_outside_poll
    )

    promoted = build_and_promote_cube_schema(workspace, slot_wait_seconds=10)

    assert promoted.status == CubeSchema.Status.ACTIVE
    # One optimistic try inside the refresh transaction, then every wait outside it.
    assert attempts == [True, False, False]
    assert refreshed_model.refreshes == [True, True]


@pytest.mark.django_db(transaction=True)
def test_uncontended_refresh_takes_the_slot_once_inside_its_transaction(
    workspace, refreshed_model, cube_http, monkeypatch
):
    attempts = _record_slot_attempts(monkeypatch)

    build_and_promote_cube_schema(workspace)

    assert attempts == [True]
    assert refreshed_model.refreshes == [True]


@pytest.mark.django_db(transaction=True)
def test_refresh_that_never_gets_a_slot_keeps_serving(
    workspace, model, refreshed_model, cube_http, raw_holder
):
    raw_holder.execute("SELECT pg_advisory_lock(%s, 0)", [cube_schema.VALIDATOR_LOCK_CLASS])

    with pytest.raises(cube_schema.CubeValidatorBusyError):
        build_and_promote_cube_schema(workspace, slot_wait_seconds=0)

    serving = CubeSchema.objects.get(semantic_model=model, status=CubeSchema.Status.ACTIVE)
    assert serving is not None
    model.refresh_from_db()
    assert model.metadata["last_build"]["ok"] is False
    assert refreshed_model.refreshes == [True]


def _deadlock():
    try:
        raise psycopg.errors.DeadlockDetected("deadlock detected")
    except psycopg.Error as exc:
        raise OperationalError("deadlock detected") from exc


@pytest.mark.django_db(transaction=True)
def test_refresh_chosen_as_deadlock_victim_is_retried_once(
    workspace, model, refreshed_model, cube_http, monkeypatch
):
    """SCOUT-DJANGO-43: a deadlock rolls the refresh back whole, so redo it."""
    real_ensure = cube_schema.ensure_semantic_model
    calls = []

    def deadlock_first(ws):
        calls.append(ws)
        if len(calls) == 1:
            _deadlock()
        return real_ensure(ws)

    monkeypatch.setattr(cube_schema, "ensure_semantic_model", deadlock_first)

    promoted = build_and_promote_cube_schema(workspace)

    assert promoted.status == CubeSchema.Status.ACTIVE
    assert len(calls) == 2
    model.refresh_from_db()
    assert model.metadata["last_build"]["ok"] is True


@pytest.mark.django_db(transaction=True)
def test_refresh_deadlocked_at_commit_is_retried(
    workspace, model, refreshed_model, cube_http, monkeypatch
):
    """Where SCOUT-DJANGO-43 struck: the deferred FK checks at COMMIT."""
    real_commit = connection.commit
    commits = []

    def deadlock_first_commit():
        commits.append(connection.in_atomic_block)
        if len(commits) == 1:
            _deadlock()
        real_commit()

    monkeypatch.setattr(connection, "commit", deadlock_first_commit)

    promoted = build_and_promote_cube_schema(workspace)

    assert promoted.status == CubeSchema.Status.ACTIVE
    assert len(commits) == 2
    assert refreshed_model.refreshes == [True, True]
    model.refresh_from_db()
    assert model.metadata["last_build"]["ok"] is True


@pytest.mark.django_db(transaction=True)
def test_refresh_that_fails_other_than_by_deadlock_is_not_retried(
    workspace, model, refreshed_model, cube_http, monkeypatch
):
    calls = []

    def fail(ws):
        calls.append(ws)
        raise OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(cube_schema, "ensure_semantic_model", fail)

    with pytest.raises(OperationalError):
        build_and_promote_cube_schema(workspace)

    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_refresh_deadlocked_by_a_workspace_delete_gives_up_without_retrying(
    workspace, refreshed_model, monkeypatch
):
    calls = []

    def deleted_then_deadlocked(ws, slot_wait_seconds):
        calls.append(ws)
        type(ws).objects.filter(pk=ws.pk).delete()
        _deadlock()

    monkeypatch.setattr(cube_schema, "_refresh_and_promote", deleted_then_deadlocked)

    with pytest.raises(CubeSchemaBuildError, match="deleted"):
        build_and_promote_cube_schema(workspace)

    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_refresh_deadlocked_twice_fails_and_keeps_serving(
    workspace, model, refreshed_model, monkeypatch
):
    calls = []

    def deadlocked(ws, slot_wait_seconds):
        calls.append(ws)
        _deadlock()

    monkeypatch.setattr(cube_schema, "_refresh_and_promote", deadlocked)

    with pytest.raises(OperationalError):
        build_and_promote_cube_schema(workspace)

    assert len(calls) == 2
    assert CubeSchema.objects.filter(semantic_model=model, status=CubeSchema.Status.ACTIVE).exists()
    model.refresh_from_db()
    assert model.metadata["last_build"]["ok"] is False
