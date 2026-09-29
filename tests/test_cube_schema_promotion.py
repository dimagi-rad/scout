"""A failed rebuild must never demote the schema Cube is serving."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

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
    settings.CUBEJS_API_SECRET = "secret"
    monkeypatch.setattr(cube_client, "SCHEMA_RETRY_BASE_DELAY_SECONDS", 0, raising=False)
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
    assert cube_http.calls["meta"] == 3
    records = _warnings_and_errors(caplog)
    assert [record.levelno for record in records] == [logging.WARNING]
    assert records[0].exc_info is None
    assert str(workspace.id) in records[0].getMessage()
