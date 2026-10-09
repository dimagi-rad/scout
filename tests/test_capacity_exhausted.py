"""Running out of connections is a calm "busy" retry for the user and one alert for us.

Every service draws from one small RDS instance; in July it filled and users saw a red
"server unreachable" bar. Every layer that can hit the limit must answer with the
same retryable 503 (or the chat stream's busy event), and escalate to Sentry once
per window under a stable fingerprint.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import OperationalError
from django.test import AsyncClient, RequestFactory
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from apps.chat import stream
from apps.chat.checkpointer import CheckpointerPool, CheckpointerPoolExhausted
from apps.common import capacity
from apps.common.capacity import (
    ALERT_MESSAGE,
    BUSY_MESSAGE,
    CapacityExhausted,
    CapacityResource,
    classify_capacity_error,
    report_capacity_exhausted,
)
from apps.semantic.services import query
from apps.semantic.services.cube_client import CubeConnectionError, CubeServiceUnavailable
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from config.middleware.capacity import CapacityExhaustedMiddleware
from tests.tenant_access import ausable_connection

User = get_user_model()

BUSY_BODY = {"error": "busy", "code": "CAPACITY_EXHAUSTED", "message": BUSY_MESSAGE}


def _django_wrapped(message: str) -> OperationalError:
    """Mirror Django's wrapping: a django.db error caused by the psycopg one."""
    try:
        try:
            raise psycopg.OperationalError(message)
        except psycopg.OperationalError as inner:
            raise OperationalError(message) from inner
    except OperationalError as outer:
        return outer


EXHAUSTION_SIGNALS = {
    "too_many_clients": (
        _django_wrapped("connection failed: FATAL:  sorry, too many clients already"),
        CapacityResource.DATABASE,
    ),
    "reserved_slots": (
        _django_wrapped(
            "FATAL:  remaining connection slots are reserved for roles with the SUPERUSER attribute"
        ),
        CapacityResource.DATABASE,
    ),
    "sqlstate_53300": (
        psycopg.errors.TooManyConnections("too many connections for role"),
        CapacityResource.DATABASE,
    ),
    "checkpointer_pool_timeout": (
        CheckpointerPoolExhausted("couldn't get a connection after 30.00 sec"),
        CapacityResource.CHECKPOINTER_POOL,
    ),
    "cube_slot_wait": (
        CubeConnectionError("Cube is temporarily unavailable.", capacity_exhausted=True),
        CapacityResource.CUBE,
    ),
    "cube_service_unavailable": (
        CubeServiceUnavailable("schema validation failed: HTTP 503", capacity_exhausted=True),
        CapacityResource.CUBE,
    ),
}


@pytest.fixture(autouse=True)
def _fresh_alert_window():
    cache.clear()
    capacity._local_alert_deadlines.clear()
    yield
    cache.clear()
    capacity._local_alert_deadlines.clear()


@pytest.fixture
def sentry():
    with patch("apps.common.capacity.sentry_sdk") as mock_sentry:
        yield mock_sentry


def _assert_busy_response(response):
    assert response.status_code == 503
    assert response["Retry-After"] == str(capacity.RETRY_AFTER_SECONDS)
    assert json.loads(response.content) == BUSY_BODY


@pytest.mark.parametrize(("exc", "resource"), EXHAUSTION_SIGNALS.values(), ids=EXHAUSTION_SIGNALS)
def test_each_exhaustion_signal_is_classified(exc, resource):
    found = classify_capacity_error(exc)
    assert isinstance(found, CapacityExhausted)
    assert found.resource == resource


@pytest.mark.parametrize(
    "exc",
    [
        OperationalError("server closed the connection unexpectedly"),
        psycopg.OperationalError("connection refused"),
        CubeConnectionError("Cube is temporarily unavailable."),
        CubeServiceUnavailable("schema validation failed: HTTP 503"),
        ValueError("too many connections"),
        # An untagged open timeout stays a down database; the pool tags a refusal itself.
        PoolTimeout("pool initialization incomplete after 10.0 sec"),
        # Scout's own per-process managed-pool cap is not a connection limit.
        PoolTimeout("all 4 managed-DB pool slots are in use"),
    ],
)
def test_other_failures_are_not_capacity(exc):
    assert classify_capacity_error(exc) is None


def test_a_bug_raised_while_handling_capacity_stays_a_bug():
    try:
        try:
            raise _django_wrapped("FATAL:  sorry, too many clients already")
        except OperationalError:
            raise KeyError("recovery bug")  # noqa: B904 -- the implicit context is the point
    except KeyError as bug:
        assert classify_capacity_error(bug) is None


def _checkpointer_pool(pool_size):
    pool = CheckpointerPool("dbname=unused", max_size=4, open=False)
    pool.get_stats = lambda: {"pool_size": pool_size}
    return pool


@pytest.mark.asyncio
async def test_the_checkpointer_pool_tags_a_full_pool_checkout_timeout_as_capacity():
    pool = _checkpointer_pool(pool_size=4)
    with patch.object(
        AsyncConnectionPool,
        "getconn",
        AsyncMock(side_effect=PoolTimeout("couldn't get a connection")),
    ):
        with pytest.raises(CheckpointerPoolExhausted) as raised:
            await pool.getconn(timeout=0.1)
    assert classify_capacity_error(raised.value).resource == CapacityResource.CHECKPOINTER_POOL


@pytest.mark.asyncio
async def test_the_checkpointer_pool_tags_a_timeout_after_a_slot_refusal_as_capacity():
    pool = _checkpointer_pool(pool_size=1)
    refusal = psycopg.OperationalError("FATAL:  sorry, too many clients already")
    with (
        patch.object(AsyncConnectionPool, "_connect", AsyncMock(side_effect=refusal)),
        pytest.raises(psycopg.OperationalError),
    ):
        await pool._connect()
    with (
        patch.object(
            AsyncConnectionPool, "getconn", AsyncMock(side_effect=PoolTimeout("timed out"))
        ),
        pytest.raises(CheckpointerPoolExhausted),
    ):
        await pool.getconn(timeout=0.1)


@pytest.mark.asyncio
async def test_the_checkpointer_pool_leaves_a_timeout_on_an_unreachable_database_untagged():
    pool = _checkpointer_pool(pool_size=1)
    down = psycopg.OperationalError("connection refused")
    with (
        patch.object(AsyncConnectionPool, "_connect", AsyncMock(side_effect=down)),
        pytest.raises(psycopg.OperationalError),
    ):
        await pool._connect()
    with (
        patch.object(
            AsyncConnectionPool, "getconn", AsyncMock(side_effect=PoolTimeout("timed out"))
        ),
        pytest.raises(PoolTimeout) as raised,
    ):
        await pool.getconn(timeout=0.1)
    assert not isinstance(raised.value, CheckpointerPoolExhausted)
    assert classify_capacity_error(raised.value) is None


@pytest.mark.asyncio
async def test_a_checkpointer_cold_open_timeout_after_a_slot_refusal_is_capacity():
    pool = _checkpointer_pool(pool_size=0)
    refusal = psycopg.OperationalError("FATAL:  sorry, too many clients already")
    with (
        patch.object(AsyncConnectionPool, "_connect", AsyncMock(side_effect=refusal)),
        pytest.raises(psycopg.OperationalError),
    ):
        await pool._connect()
    with (
        patch.object(AsyncConnectionPool, "open", AsyncMock(side_effect=PoolTimeout("timed out"))),
        pytest.raises(CheckpointerPoolExhausted),
    ):
        await pool.open(wait=True, timeout=1)


@pytest.mark.parametrize(("exc", "resource"), EXHAUSTION_SIGNALS.values(), ids=EXHAUSTION_SIGNALS)
def test_each_exhaustion_signal_becomes_a_busy_503(sentry, exc, resource):
    middleware = CapacityExhaustedMiddleware(lambda request: None)
    response = middleware.process_exception(RequestFactory().get("/api/anything/"), exc)
    _assert_busy_response(response)
    assert sentry.new_scope.return_value.__enter__.return_value.fingerprint == [
        "capacity-exhausted",
        str(resource),
    ]


def test_an_ordinary_error_is_left_to_django():
    middleware = CapacityExhaustedMiddleware(lambda request: None)
    assert middleware.process_exception(RequestFactory().get("/"), ValueError("boom")) is None


def test_alert_fires_once_per_window_with_a_stable_fingerprint(sentry):
    for _ in range(5):
        report_capacity_exhausted(CapacityResource.CHECKPOINTER_POOL)

    sentry.capture_message.assert_called_once()
    message = sentry.capture_message.call_args.args[0]
    assert ALERT_MESSAGE in message
    assert sentry.capture_message.call_args.kwargs["level"] == "error"
    scope = sentry.new_scope.return_value.__enter__.return_value
    assert scope.fingerprint == ["capacity-exhausted", "checkpointer_pool"]
    scope.set_tag.assert_called_with("capacity_resource", "checkpointer_pool")


def test_each_resource_has_its_own_window(sentry):
    report_capacity_exhausted(CapacityResource.DATABASE)
    report_capacity_exhausted(CapacityResource.CUBE)
    report_capacity_exhausted(CapacityResource.DATABASE)
    assert sentry.capture_message.call_count == 2


def test_alert_fires_again_once_the_window_expires(sentry):
    report_capacity_exhausted(CapacityResource.CUBE)
    cache.clear()
    report_capacity_exhausted(CapacityResource.CUBE)
    assert sentry.capture_message.call_count == 2


def test_the_window_claim_uses_the_shared_cache(sentry):
    with patch("apps.common.capacity.cache") as mock_cache:
        mock_cache.add.side_effect = [True, False]
        report_capacity_exhausted(CapacityResource.CUBE)
        report_capacity_exhausted(CapacityResource.CUBE)
    assert mock_cache.add.call_args.args[2] == capacity.ALERT_WINDOW_SECONDS
    sentry.capture_message.assert_called_once()


def test_an_unreachable_cache_still_rate_limits_per_process(sentry):
    with patch("apps.common.capacity.cache") as mock_cache:
        mock_cache.add.side_effect = ConnectionError("redis down")
        for _ in range(3):
            report_capacity_exhausted(CapacityResource.CUBE)
    sentry.capture_message.assert_called_once()


@pytest.mark.django_db
def test_alert_includes_connection_usage_when_the_database_answers(sentry):
    report_capacity_exhausted(CapacityResource.CHECKPOINTER_POOL)
    scope = sentry.new_scope.return_value.__enter__.return_value
    name, details = scope.set_context.call_args.args
    assert name == "capacity"
    assert details["pg_stat_activity_count"] >= 1
    assert details["max_connections"] > 0


def test_alert_never_opens_a_connection_to_read_usage(sentry):
    with patch("apps.common.capacity.connection") as conn:
        conn.connection = None
        report_capacity_exhausted(CapacityResource.DATABASE, "FATAL: too many clients already")
    conn.cursor.assert_not_called()
    details = sentry.new_scope.return_value.__enter__.return_value.set_context.call_args.args[1]
    assert details == {
        "resource": "db",
        "detail": "FATAL: too many clients already",
        "usage": "unavailable",
    }


def test_an_unreachable_cache_is_logged(sentry, caplog):
    with patch("apps.common.capacity.cache") as mock_cache:
        mock_cache.add.side_effect = ConnectionError("redis down")
        report_capacity_exhausted(CapacityResource.CUBE)
    assert "per-process window" in caplog.text


class _RaisingAgent:
    def __init__(self, exc):
        self._exc = exc

    def astream_events(self, input_state, *, config, version):
        exc = self._exc

        async def _gen():
            if False:
                yield
            raise exc

        return _gen()


@pytest.mark.asyncio
async def test_the_chat_stream_sends_a_busy_event_instead_of_an_error(sentry):
    agent = _RaisingAgent(CheckpointerPoolExhausted("couldn't get a connection after 30.00 sec"))
    chunks = [c async for c in stream.langgraph_to_ui_stream(agent, {}, {"configurable": {}})]
    events = [json.loads(c.removeprefix("data: ").strip()) for c in chunks if c.startswith("data:")]

    busy = [e for e in events if e.get("type") == "data-chat-status"]
    assert busy == [
        {
            "type": "data-chat-status",
            "data": {
                "kind": "retryable-error",
                "reason": "busy",
                "message": BUSY_MESSAGE,
                "retryAfter": capacity.RETRY_AFTER_SECONDS,
            },
            "transient": True,
        }
    ]
    assert not any(e.get("type") == "error" for e in events)
    assert events[-1] == {"type": "finish", "finishReason": "stop"}
    sentry.capture_message.assert_called_once()


async def _chat_member(slug):
    owner = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=owner)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name=f"{slug} Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=owner, role=WorkspaceRole.READ)
    await TenantMembership.objects.acreate(
        user=owner, tenant=tenant, connection=await ausable_connection(owner, tenant.provider)
    )
    client = AsyncClient(raise_request_exception=False)
    await client.alogin(email=f"{slug}@b.c", password="x")
    return ws, client


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_full_checkpointer_pool_answers_chat_with_busy_not_500(sentry):
    ws, client = await _chat_member("pool-full")
    ensure = AsyncMock(
        side_effect=CheckpointerPoolExhausted("couldn't get a connection after 30.00 sec")
    )
    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", ensure),
    ):
        response = await client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "workspaceId": str(ws.id),
                    "threadId": str(uuid.uuid4()),
                }
            ),
            content_type="application/json",
        )

    _assert_busy_response(response)
    # A full pool stays full; rebuilding it would just wait out a second timeout.
    ensure.assert_awaited_once()
    sentry.capture_message.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_full_database_answers_an_async_view_with_busy(sentry):
    ws, client = await _chat_member("db-full")
    with patch(
        "apps.chat.views._upsert_thread",
        side_effect=_django_wrapped("FATAL:  sorry, too many clients already"),
    ):
        response = await client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "workspaceId": str(ws.id),
                    "threadId": str(uuid.uuid4()),
                }
            ),
            content_type="application/json",
        )
    _assert_busy_response(response)


@pytest.mark.django_db
def test_a_full_cube_pool_answers_the_semantic_query_api_with_busy(sentry, client):
    user = User.objects.create_user(email="cube-full@b.c", password="x")
    ws = Workspace.objects.create(name="W-cube-full", created_by=user)
    client.force_login(user)
    capacity_result = query.query_error(
        "CONNECTION_ERROR",
        "Cube is at its database connection limit.",
        category=query.CAPACITY_EXHAUSTED_CATEGORY,
        retryable=True,
    )
    with (
        patch("apps.semantic.api.views.resolve_workspace", return_value=(ws, None, None)),
        patch("apps.semantic.api.views.run_semantic_query_sync", return_value=capacity_result),
    ):
        response = client.post(
            f"/api/workspaces/{ws.id}/semantic-query/",
            data={"measures": ["visits.count"]},
            content_type="application/json",
        )
    _assert_busy_response(response)


@pytest.mark.asyncio
async def test_a_full_cube_pool_is_a_retryable_capacity_result(sentry, monkeypatch):
    monkeypatch.setattr(
        query,
        "_compile_semantic_query_for_async",
        lambda *args: {"cube_query": {}, "model": None, "cube_schema": None},
    )
    monkeypatch.setattr(query, "load_workspace_context", AsyncMock(return_value=None))
    monkeypatch.setattr(query, "build_cube_security_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        query.CubeClient,
        "execute_query",
        AsyncMock(side_effect=CubeConnectionError("full", capacity_exhausted=True)),
    )
    result = await query.run_semantic_query(SimpleNamespace(id="workspace"), {})
    assert query.is_capacity_exhausted(result)
    assert result["error"]["retryable"] is True
    sentry.capture_message.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_health_reports_busy_not_unreachable_when_connections_run_out(sentry):
    with patch(
        "apps.workspaces.views._check_database",
        side_effect=_django_wrapped("FATAL:  sorry, too many clients already"),
    ):
        response = await AsyncClient().get("/health/")
    assert response.status_code == 503
    assert response["Retry-After"] == str(capacity.RETRY_AFTER_SECONDS)
    assert response.json()["status"] == "busy"
    assert response.json()["checks"]["database"] == "busy"
    sentry.capture_message.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_health_stays_unhealthy_when_a_probe_really_fails(sentry):
    with (
        patch(
            "apps.workspaces.views._check_database",
            side_effect=_django_wrapped("FATAL:  sorry, too many clients already"),
        ),
        patch("apps.workspaces.views._check_queue", side_effect=Exception("table missing")),
    ):
        response = await AsyncClient().get("/health/")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert "Retry-After" not in response


def test_capacity_exhausted_is_an_expected_state_so_its_raw_exception_never_pages():
    assert issubclass(CapacityExhausted, capacity.ExpectedStateError)
