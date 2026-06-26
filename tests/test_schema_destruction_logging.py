"""Tests for structured logging of schema destruction (arch #257, finding 08#9).

Schema destruction was silent on success: ``expire_inactive_schemas`` flipped
ACTIVE->TEARDOWN with no logging, and the teardown tasks logged only failures —
so a successful DROP SCHEMA CASCADE of data-bearing schemas emitted zero log
lines. The forensic question of the 2026-06-10 incident ("why did the janitor
drop a fresh schema?") was unanswerable because the decision input
(``last_accessed_at``) is later overwritten and was never logged.

These tests pin explicit log lines at the expire DECISION point (logging
``last_accessed_at`` BEFORE any overwrite) and on successful teardown/drop, plus
the dependent-view-schema failure count.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from django.utils import timezone

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.tasks import expire_inactive_schemas, teardown_schema


@pytest.fixture
def active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="log_test_schema",
        state=SchemaState.ACTIVE,
        last_accessed_at=timezone.now(),
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expire_logs_decision_with_last_accessed_at(active_schema, caplog):
    """The expire decision must be logged WITH last_accessed_at and the cutoff,
    captured BEFORE the row is flipped (that timestamp is the missing forensic
    input — it is overwritten later)."""
    stale_ts = timezone.now() - timedelta(hours=25)
    active_schema.last_accessed_at = stale_ts
    await active_schema.asave(update_fields=["last_accessed_at"])

    with (
        patch("apps.workspaces.tasks.teardown_schema.defer_async", new_callable=AsyncMock),
        caplog.at_level(logging.INFO, logger="apps.workspaces.tasks"),
    ):
        await expire_inactive_schemas()

    msgs = [r.getMessage() for r in caplog.records]
    decision = [m for m in msgs if "log_test_schema" in m and "last_accessed_at" in m]
    assert decision, f"no expire-decision log line with last_accessed_at; saw: {msgs}"
    # The schema id must be present for forensic correlation.
    assert any(str(active_schema.id) in m for m in decision)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_logs_successful_drop(active_schema, caplog):
    """A successful DROP SCHEMA CASCADE must emit a log line (schema id + name)."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        caplog.at_level(logging.INFO, logger="apps.workspaces.tasks"),
    ):
        MockManager.return_value.teardown.return_value = None
        await teardown_schema(schema_id=str(active_schema.id))

    msgs = [r.getMessage() for r in caplog.records]
    dropped = [m for m in msgs if "log_test_schema" in m and "drop" in m.lower()]
    assert dropped, f"successful DROP was not logged; saw: {msgs}"
    assert any(str(active_schema.id) in m for m in dropped)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_logs_dependent_view_schema_failure_count(
    active_schema, tenant, user, caplog
):
    """The count of dependent view schemas flipped to FAILED must be logged, not
    silently discarded."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    extra_tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="logcount-extra", canonical_name="Log Count Extra"
    )
    ws_b = await Workspace.objects.acreate(name="LogCount Sibling B", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=tenant)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=extra_tenant)
    await WorkspaceViewSchema.objects.acreate(
        workspace=ws_b, schema_name="ws_logcount_b", state=SchemaState.ACTIVE
    )

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        caplog.at_level(logging.INFO, logger="apps.workspaces.tasks"),
    ):
        MockManager.return_value.teardown.return_value = None
        await teardown_schema(schema_id=str(active_schema.id))

    msgs = [r.getMessage() for r in caplog.records]
    failed_log = [
        m for m in msgs if ("dependent" in m.lower() or "view schema" in m.lower()) and "1" in m
    ]
    assert failed_log, f"dependent-view-schema failure count not logged; saw: {msgs}"
