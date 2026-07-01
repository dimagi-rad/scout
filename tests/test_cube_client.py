"""Tests for CubeClient's /v1/load handling (Continue wait long-polling)."""

import json

import httpx
import pytest

from apps.semantic.services import cube_client as cube_client_module
from apps.semantic.services.cube_client import CubeClient


def _patched_async_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        cube_client_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )


@pytest.mark.asyncio
async def test_execute_query_retries_on_continue_wait(monkeypatch):
    calls = {"count": 0, "bodies": []}

    def handler(request):
        calls["count"] += 1
        calls["bodies"].append(json.loads(request.read()))
        if calls["count"] < 3:
            return httpx.Response(200, json={"error": "Continue wait"})
        return httpx.Response(
            200,
            json={
                "data": [{"visits.count": 3}],
                "annotation": {"measures": {"visits.count": {}}},
            },
        )

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "CONTINUE_WAIT_POLL_DELAY_SECONDS", 0)
    client = CubeClient(base_url="http://cube.test", api_secret="secret")

    result = await client.execute_query(
        {"measures": ["visits.count"]},
        security_context={"workspaceId": "w1"},
    )

    assert result == {"columns": ["visits.count"], "rows": [[3]], "row_count": 1}
    assert calls["count"] == 3
    # The same query is re-POSTed verbatim on every poll.
    assert all(body == {"query": {"measures": ["visits.count"]}} for body in calls["bodies"])


@pytest.mark.asyncio
async def test_execute_query_times_out_when_continue_wait_persists(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"error": "Continue wait"})

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "CONTINUE_WAIT_POLL_DELAY_SECONDS", 0)
    monkeypatch.setattr(cube_client_module, "QUERY_TOTAL_TIMEOUT_SECONDS", 0.0)
    client = CubeClient(base_url="http://cube.test", api_secret="secret")

    with pytest.raises(RuntimeError, match="timed out"):
        await client.execute_query(
            {"measures": ["visits.count"]},
            security_context={"workspaceId": "w1"},
        )


@pytest.mark.asyncio
async def test_execute_query_raises_on_real_cube_error(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"error": "Member not found: visits.bogus"})

    _patched_async_client(monkeypatch, handler)
    client = CubeClient(base_url="http://cube.test", api_secret="secret")

    with pytest.raises(RuntimeError, match="Member not found"):
        await client.execute_query(
            {"measures": ["visits.bogus"]},
            security_context={"workspaceId": "w1"},
        )
