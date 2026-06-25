"""Tests for the /health/ readiness check (arch #257, finding 08#7).

The old /health/ returned a static 200 with no DB or queue check, so a
crash-looping container with a dead DB connection or unreachable queue still
reported healthy. The readiness check must verify the platform DB AND the
Procrastinate queue (both are PostgreSQL) and return a non-200 status when
either is unreachable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import AsyncClient


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_health_ok_when_db_reachable():
    client = AsyncClient()
    resp = await client.get("/health/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["queue"] == "ok"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_health_503_when_db_unreachable():
    """A failing DB probe must surface as a non-200 readiness failure."""
    with patch(
        "apps.workspaces.views._check_database",
        side_effect=Exception("connection refused"),
    ):
        client = AsyncClient()
        resp = await client.get("/health/")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["database"] != "ok"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_health_503_when_queue_unreachable():
    """A failing queue probe must surface as a non-200 readiness failure."""
    with patch(
        "apps.workspaces.views._check_queue",
        side_effect=Exception("queue table missing"),
    ):
        client = AsyncClient()
        resp = await client.get("/health/")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["queue"] != "ok"
