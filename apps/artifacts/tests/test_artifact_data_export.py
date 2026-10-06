"""Tests for the artifact CSV data export (#846)."""

import asyncio
import csv
import io
import logging
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient

from apps.artifacts.models import Artifact, ArtifactType
from apps.artifacts.services import data_export
from apps.artifacts.services.data_export import (
    EXPORT_ROW_LIMIT,
    csv_safe_cell,
    export_error_response,
    export_filename,
    export_slot,
    iter_csv,
)
from apps.semantic.models import SemanticDataset, SemanticField, SemanticModel
from apps.semantic.services import query as query_service
from apps.semantic.services.query import (
    CAPACITY_EXHAUSTED_CATEGORY,
    MAX_SEMANTIC_LIMIT,
    _coerce_limit,
)
from apps.semantic.services.query_outcomes import query_error
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from tests.tenant_access import usable_connection
from tests.test_artifact_date_resolution import CONTEXT, story

RUN = "apps.artifacts.services.data_export.run_semantic_query"


@pytest.fixture(autouse=True)
def query_surface_ready():
    with patch(
        "apps.artifacts.services.recovery.artifact_data_state",
        new=AsyncMock(return_value={"status": "ready", "queryable": True, "message": "ok"}),
    ):
        yield


def _workspace(name):
    tenant = Tenant.objects.create(provider="commcare", external_id=name, canonical_name=name)
    workspace = Workspace.objects.create(name=name)
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    return workspace


def _member(workspace, email, role):
    user = User.objects.create_user(email=email, password="pass")
    tenant = workspace.tenants.get()
    TenantMembership.objects.create(
        user=user, tenant=tenant, connection=usable_connection(user, tenant.provider)
    )
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role=role)
    return user


def _client(user=None):
    client = AsyncClient()
    if user is not None:
        user_logged_in.disconnect(update_last_login)
        try:
            client.force_login(user)
        finally:
            user_logged_in.connect(update_last_login)
    return client


def _artifact(workspace, user, **fields):
    return Artifact.objects.create(
        workspace=workspace,
        created_by=user,
        title="Visit Trends",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="thread-1",
        data={
            "story_doc": {"version": 1, "blocks": []},
            "targets": [{"region": "North", "target": 10}, {"region": "=HYPERLINK()", "target": 5}],
        },
        semantic_queries=[
            {"name": "by_region", "measures": ["visits.count"], "limit": 10},
            {"name": "daily", "measures": ["visits.count"], "time_dimension": "visits.date"},
        ],
        **fields,
    )


@pytest.fixture
def workspace(db):
    return _workspace("export-domain")


@pytest.fixture
def manager(workspace):
    return _member(workspace, "manager@example.com", WorkspaceRole.MANAGE)


@pytest.fixture
def artifact(workspace, manager):
    return _artifact(workspace, manager)


@pytest.fixture
def manager_client(manager):
    return _client(manager)


@pytest.fixture
def role_client(request, workspace):
    return _client(_member(workspace, f"{request.param}@example.com", request.param))


@pytest.fixture
def outsider_client(db):
    return _client(User.objects.create_user(email="outsider@example.com", password="pass"))


@pytest.fixture
def foreign(db):
    other = _workspace("other-domain")
    owner = _member(other, "owner@other.com", WorkspaceRole.MANAGE)
    return other, _artifact(other, owner)


def _csv_url(workspace, artifact, query="?query=by_region"):
    return f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/data-export/csv/{query}"


def _list_url(workspace, artifact):
    return f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/data-export/"


async def _export(client, url, body=None):
    return await client.post(url, body or {}, content_type="application/json")


async def _body(response) -> str:
    chunks = [chunk async for chunk in response.streaming_content]
    return b"".join(chunks).decode("utf-8")


def _rows(text: str) -> list[list[str]]:
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


def _result(rows, *, truncated=False):
    return {
        "columns": ["visits.region", "visits.count"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "semantic_query": {
            "measures": ["visits.count"],
            "filters": [{"field": "visits.region", "operator": "equals", "values": ["North"]}],
            "limit": EXPORT_ROW_LIMIT,
        },
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role_client", "allowed"),
    [
        (WorkspaceRole.READ, False),
        (WorkspaceRole.READ_WRITE, True),
        (WorkspaceRole.MANAGE, True),
    ],
    indirect=["role_client"],
)
async def test_export_requires_read_write(workspace, artifact, role_client, allowed):
    client = role_client
    run = AsyncMock(return_value=_result([["North", "3"]]))
    with patch(RUN, new=run):
        listing = await client.get(_list_url(workspace, artifact))
        response = await _export(client, _csv_url(workspace, artifact))
    if not allowed:
        assert listing.status_code == 403
        assert response.status_code == 403
        run.assert_not_awaited()
        return
    assert listing.status_code == 200
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert _rows(await _body(response)) == [["visits.region", "visits.count"], ["North", "3"]]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_non_member_and_anonymous_cannot_export(workspace, artifact, outsider_client):
    run = AsyncMock(return_value=_result([]))
    with patch(RUN, new=run):
        denied = await _export(outsider_client, _csv_url(workspace, artifact))
        anonymous = await _export(_client(), _csv_url(workspace, artifact))
        anonymous_list = await _client().get(_list_url(workspace, artifact))
    assert denied.status_code == 403
    assert anonymous.status_code == 401
    assert anonymous_list.status_code == 401
    run.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_cannot_export_another_workspaces_artifact(workspace, manager_client, foreign):
    other, foreign = foreign
    client = manager_client
    run = AsyncMock(return_value=_result([["South", "9"]]))
    with patch(RUN, new=run):
        # Own workspace in the URL, foreign artifact id: not found in this workspace.
        via_own = await _export(client, _csv_url(workspace, foreign))
        via_own_static = await _export(client, _csv_url(workspace, foreign, "?static=targets"))
        # The foreign workspace itself: not a member.
        via_foreign = await _export(client, _csv_url(other, foreign))
    assert via_own.status_code == 404
    assert via_own_static.status_code == 404
    assert via_foreign.status_code == 403
    run.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_export_runs_the_query_at_the_export_cap_in_its_workspace(
    workspace, artifact, manager, manager_client
):
    run = AsyncMock(return_value=_result([["North", "3"]]))
    with patch(RUN, new=run):
        response = await _export(manager_client, _csv_url(workspace, artifact))
    assert response.status_code == 200
    await _body(response)
    run.assert_awaited_once()
    (called_workspace, spec), kwargs = run.await_args
    assert called_workspace.id == workspace.id
    # The artifact's own limit=10 sizes the chart; the export takes the data behind it.
    assert spec == {"measures": ["visits.count"], "limit": EXPORT_ROW_LIMIT}
    assert kwargs["max_limit"] == EXPORT_ROW_LIMIT
    assert kwargs["user_id"] == str(manager.id)
    assert response["X-Scout-Export-Truncated"] == "false"
    assert response["X-Scout-Export-Row-Count"] == "1"
    assert response["Content-Disposition"] == 'attachment; filename="visit-trends-by-region.csv"'
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_capped_export_says_so_in_headers_and_filename_not_in_the_data(
    workspace, artifact, manager, manager_client
):
    rows = [["r", str(i)] for i in range(5)]
    with patch(RUN, new=AsyncMock(return_value=_result(rows, truncated=True))):
        response = await _export(manager_client, _csv_url(workspace, artifact))
    assert response.status_code == 200
    assert response["X-Scout-Export-Truncated"] == "true"
    assert response["X-Scout-Export-Row-Limit"] == str(EXPORT_ROW_LIMIT)
    assert response["Content-Disposition"] == (
        f'attachment; filename="visit-trends-by-region-first-{EXPORT_ROW_LIMIT}-rows.csv"'
    )
    parsed = _rows(await _body(response))
    assert parsed[1:] == rows


def test_agent_facing_limit_is_unchanged():
    assert MAX_SEMANTIC_LIMIT == 500
    assert _coerce_limit(1_000_000) == MAX_SEMANTIC_LIMIT
    assert _coerce_limit(1_000_000, EXPORT_ROW_LIMIT) == EXPORT_ROW_LIMIT


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_static_tabular_data_exports_and_is_listed(workspace, artifact, manager_client):
    client = manager_client
    with patch(RUN, new=AsyncMock()) as run:
        listing = await client.get(_list_url(workspace, artifact))
        response = await _export(client, _csv_url(workspace, artifact, "?static=targets"))
        missing = await _export(client, _csv_url(workspace, artifact, "?static=story_doc"))
    run.assert_not_awaited()
    assert listing.json() == {
        "datasets": [
            {"name": "by_region", "source": "query"},
            {"name": "daily", "source": "query"},
            {"name": "targets", "source": "static"},
        ],
        "row_limit": EXPORT_ROW_LIMIT,
    }
    assert response.status_code == 200
    assert _rows(await _body(response)) == [
        ["region", "target"],
        ["North", "10"],
        ["'=HYPERLINK()", "5"],
    ]
    assert missing.status_code == 404


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["", "?query=by_region&static=targets", "?query=nope", "?query=legacy"]
)
async def test_dataset_must_be_named_and_exist(workspace, artifact, manager_client, query):
    artifact.source_queries = [{"name": "legacy", "sql": "SELECT 1"}]
    await artifact.asave(update_fields=["source_queries"])
    with patch(RUN, new=AsyncMock()) as run:
        response = await _export(manager_client, _csv_url(workspace, artifact, query))
    assert response.status_code == (404 if "query=" in query and "static" not in query else 400)
    run.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_unqueryable_surface_is_a_conflict(workspace, artifact, manager_client):
    state = {"status": "needs_semantic_rebuild", "queryable": False, "message": "Rebuilding"}
    with (
        patch(
            "apps.artifacts.services.recovery.artifact_data_state",
            new=AsyncMock(return_value=state),
        ),
        patch(RUN, new=AsyncMock()) as run,
    ):
        response = await _export(manager_client, _csv_url(workspace, artifact))
    assert response.status_code == 409
    run.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_query_failures_are_json_errors(workspace, artifact, manager_client, monkeypatch):
    client = manager_client
    failed = AsyncMock(
        return_value=query_error("VALIDATION_ERROR", "Unknown member", category="invalid_query")
    )
    with patch(RUN, new=failed):
        response = await _export(client, _csv_url(workspace, artifact))
    assert response.status_code == 502
    assert response.json() == {"error": "Unknown member"}

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(data_export, "EXPORT_TIMEOUT_SECONDS", 0.01)
    with patch(RUN, new=slow):
        timed_out = await _export(client, _csv_url(workspace, artifact))
    assert timed_out.status_code == 504


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_export_writes_an_audit_line(workspace, artifact, manager, manager_client, caplog):
    with (
        caplog.at_level(logging.INFO, logger="scout.export.audit"),
        patch(RUN, new=AsyncMock(return_value=_result([["North", "3"]]))),
    ):
        response = await _export(manager_client, _csv_url(workspace, artifact))
    await _body(response)
    [line] = [
        r.getMessage() for r in caplog.records if "Artifact data export started:" in r.getMessage()
    ]
    assert f"user={manager.id}" in line
    assert f"artifact={artifact.id}" in line
    assert "dataset='by_region'" in line
    assert "rows=1" in line
    assert '"measures": ["visits.count"]' in line
    assert "North" not in line


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_post_applies_the_viewers_date_controls(workspace, artifact, manager_client):
    artifact.data = {"story_doc": story()}
    await artifact.asave(update_fields=["data"])
    run = AsyncMock(return_value=_result([]))
    selected = {**CONTEXT, "sources": {"range": {"start": "2026-09-10", "end": "2026-09-16"}}}
    url = _csv_url(workspace, artifact).replace("by_region", "q.sessions")
    with patch(RUN, new=run):
        listing = await manager_client.post(
            _list_url(workspace, artifact), selected, content_type="application/json"
        )
        response = await manager_client.post(url, selected, content_type="application/json")
        invalid = await manager_client.post(
            url, {**CONTEXT, "sources": {"nope": {}}}, content_type="application/json"
        )
    assert listing.json()["datasets"] == [{"name": "q.sessions", "source": "query"}]
    assert response.status_code == 200
    await _body(response)
    spec = run.await_args.args[1]
    assert spec["filters"][0]["values"] == ["2026-09-10", "2026-09-16"]
    assert invalid.status_code == 400


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=SUM(A1)", "'=SUM(A1)"),
        ("+cmd", "'+cmd"),
        ("-2+3+cmd|' /C calc'!A0", "'-2+3+cmd|' /C calc'!A0"),
        ("@SUM(1)", "'@SUM(1)"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ("-5", "-5"),
        ("-1.25e3", "-1.25e3"),
        ("+7", "+7"),
        ("plain", "plain"),
        (" =cmd", "' =cmd"),
        ("\n=cmd", "'\n=cmd"),
        ("\uff1dSUM(1)", "'\uff1dSUM(1)"),
        ("-\u0661\u0662", "'-\u0661\u0662"),
        ({"a": 1}, '{"a": 1}'),
        (["=x"], '["=x"]'),
        (True, "true"),
        (None, ""),
        (-3, -3),
        (2.5, 2.5),
    ],
)
def test_csv_safe_cell(value, expected):
    assert csv_safe_cell(value) == expected


@pytest.mark.asyncio
async def test_iter_csv_is_rfc4180_and_chunked(monkeypatch):
    monkeypatch.setattr(data_export, "CSV_CHUNK_ROWS", 2)
    rows = [['say "hi"', "a,b"], ["line\nbreak", "=1+1"], ["x", None]]
    chunks = [chunk async for chunk in iter_csv(["=col", "b"], rows)]
    assert len(chunks) == 2
    text = b"".join(chunks).decode()
    assert text == ('﻿\'=col,b\r\n"say ""hi""","a,b"\r\n"line\nbreak",\'=1+1\r\nx,\r\n')


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_csv_export_is_post_only_and_csrf_protected(workspace, artifact, manager):
    client = AsyncClient(enforce_csrf_checks=True)
    await asyncio.to_thread(client.force_login, manager)
    with patch(RUN, new=AsyncMock(return_value=_result([]))) as run:
        via_get = await client.get(_csv_url(workspace, artifact))
        no_token = await _export(client, _csv_url(workspace, artifact))
    assert via_get.status_code == 405
    assert no_token.status_code == 403
    run.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_full_cube_pool_or_export_slots_are_busy(workspace, artifact, manager_client):
    full = query_error(
        "CONNECTION_ERROR", "full", category=CAPACITY_EXHAUSTED_CATEGORY, retryable=True
    )
    with patch(RUN, new=AsyncMock(return_value=full)):
        pool_full = await _export(manager_client, _csv_url(workspace, artifact))
    held = [export_slot(), export_slot()]
    try:
        with patch(RUN, new=AsyncMock()) as run:
            slots_full = await _export(manager_client, _csv_url(workspace, artifact))
        run.assert_not_awaited()
    finally:
        for slot in held:
            slot.__exit__(None, None, None)
    assert pool_full.status_code == 503
    assert slots_full.status_code == 503
    assert export_slot() is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_db_connection_is_released_before_streaming(workspace, artifact, manager_client):
    with (
        patch(RUN, new=AsyncMock(return_value=_result([["North", "3"]]))),
        patch("apps.artifacts.views.close_old_connections") as close,
    ):
        response = await _export(manager_client, _csv_url(workspace, artifact))
        close.assert_called_once()
    await _body(response)


@pytest.mark.parametrize(
    ("category", "message", "status"),
    [
        ("transient_runtime_failure", "Cube query timed out after 60s.", 504),
        ("transient_runtime_failure", "connection reset", 503),
        ("data_unavailable", "Data not loaded", 409),
        ("invalid_query", "Unknown member", 502),
    ],
)
def test_export_error_status(category, message, status):
    response = export_error_response({"category": category, "message": message})
    assert response.status_code == status


def test_filename_cannot_inject_headers():
    name = export_filename('a"b\r\nSet-Cookie: x=1', "q.\u00e9", truncated=False)
    assert name == "a-b-set-cookie-x-1-q.csv"
    assert export_filename("\u65e5\u672c", "", truncated=True) == (
        f"artifact-data-first-{EXPORT_ROW_LIMIT}-rows.csv"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("max_limit", "expected"), [(None, MAX_SEMANTIC_LIMIT), (EXPORT_ROW_LIMIT, EXPORT_ROW_LIMIT)]
)
def test_compiled_cube_query_limit(monkeypatch, workspace, max_limit, expected):
    model = SemanticModel.objects.create(workspace=workspace, name="m")
    visits = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="visits",
        label="Visits",
        table_name="raw_visits",
        schema_name="tenant_schema",
    )
    SemanticField.objects.create(
        dataset=visits,
        name="count",
        label="Count",
        field_type=SemanticField.FieldType.MEASURE,
        data_type="integer",
        expression="*",
        measure_type=SemanticField.MeasureType.COUNT,
    )
    monkeypatch.setattr(query_service, "get_active_semantic_model", lambda _workspace: model)
    monkeypatch.setattr(query_service, "get_active_cube_schema", lambda *a, **k: None)
    spec = {"measures": ["visits.count"], "limit": 1_000_000}
    kwargs = {} if max_limit is None else {"max_limit": max_limit}
    compiled = query_service._compile_semantic_query(workspace, spec, **kwargs)
    assert compiled["cube_query"]["limit"] == expected
