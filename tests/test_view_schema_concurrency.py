"""Concurrent workers must publish coverage for the physical plan they built."""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from django.db import connection

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager


@pytest.mark.django_db(transaction=True)
def test_concurrent_builds_publish_coverage_matching_physical_views(workspace, tenant):
    second = Tenant.objects.create(
        provider="commcare", external_id="concurrent-second", canonical_name="Second"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=second)
    TenantSchema.objects.create(
        tenant=tenant, schema_name="concurrency_first", state=SchemaState.ACTIVE
    )
    first_planned = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    worker_context = threading.local()
    physical_sources = set()
    second_backend = []

    def managed_connection():
        if worker_context.name == "first":
            first_planned.set()
            assert release_first.wait(10), "First build was never released"
        conn = MagicMock()
        conn.closed = False
        cursor = conn.cursor.return_value
        cursor.fetchall.return_value = [("raw_cases",)]
        cursor.fetchone.return_value = (1,)

        def execute(statement, *_args):
            statement = statement.as_string() if hasattr(statement, "as_string") else statement
            if statement.startswith("DROP SCHEMA"):
                physical_sources.clear()
            if statement.startswith("CREATE VIEW"):
                source = re.search(r'AS SELECT \* FROM "([^"]+)"', statement)
                assert source is not None
                physical_sources.add(source.group(1))

        cursor.execute.side_effect = execute
        return conn

    def build(name):
        worker_context.name = name
        try:
            if name == "second":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    second_backend.append(cursor.fetchone()[0])
                second_started.set()
            return SchemaManager().build_view_schema(Workspace.objects.get(pk=workspace.pk))
        finally:
            connection.close()

    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            side_effect=managed_connection,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(build, "first")
        try:
            assert first_planned.wait(10)
            assert (
                WorkspaceViewSchema.objects.get(workspace=workspace).state
                == SchemaState.PROVISIONING
            )
            TenantSchema.objects.create(
                tenant=second, schema_name="concurrency_second", state=SchemaState.ACTIVE
            )
            other = executor.submit(build, "second")
            assert second_started.wait(10)
            deadline = time.monotonic() + 10
            while not other.done():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event FROM pg_stat_activity WHERE pid = %s",
                        [second_backend[0]],
                    )
                    row = cursor.fetchone()
                if row and row[0] == "advisory":
                    break
                assert time.monotonic() < deadline, (
                    "Second build neither finished nor waited for serialization"
                )
                time.sleep(0.01)
        finally:
            release_first.set()
        first.result(timeout=10)
        other.result(timeout=10)

    stored = WorkspaceViewSchema.objects.get(workspace=workspace)
    source_ids = {"concurrency_first": str(tenant.id), "concurrency_second": str(second.id)}
    assert stored.state == SchemaState.ACTIVE
    assert {entry["tenant_id"] for entry in stored.tenant_coverage["included_tenants"]} == {
        source_ids[source] for source in physical_sources
    }
    assert physical_sources == set(source_ids)


@pytest.mark.django_db(transaction=True)
def test_failed_build_releases_lock_for_another_worker(workspace):
    def build_again():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '1s'")
            return SchemaManager().build_view_schema(workspace)
        finally:
            connection.close()

    with patch.object(SchemaManager, "_build_view_schema", side_effect=ValueError("DDL failed")):
        with pytest.raises(ValueError, match="DDL failed"):
            SchemaManager().build_view_schema(workspace)

    with patch.object(SchemaManager, "_build_view_schema", return_value="rebuilt"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(build_again).result(timeout=5) == "rebuilt"


@pytest.mark.django_db(transaction=True)
def test_recursive_build_is_rejected_and_next_build_can_proceed(workspace):
    manager = SchemaManager()
    with patch.object(
        manager,
        "_build_view_schema",
        side_effect=lambda current: manager.build_view_schema(current),
    ):
        with pytest.raises(RuntimeError, match="Recursive view build"):
            manager.build_view_schema(workspace)

    with patch.object(manager, "_build_view_schema", return_value="rebuilt"):
        assert manager.build_view_schema(workspace) == "rebuilt"


@pytest.mark.django_db(transaction=True)
def test_other_workspace_build_does_not_wait_for_busy_workspace(workspace):
    first_started = threading.Event()
    release_first = threading.Event()
    other_workspace = MagicMock(id="independent-workspace")

    def build_plan(current):
        if current.id == workspace.id:
            first_started.set()
            assert release_first.wait(5)
        return str(current.id)

    def build(current):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '1s'")
            return SchemaManager().build_view_schema(current)
        finally:
            connection.close()

    with (
        patch.object(SchemaManager, "_build_view_schema", side_effect=build_plan),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(build, workspace)
        try:
            assert first_started.wait(5)
            assert executor.submit(build, other_workspace).result(timeout=3) == str(
                other_workspace.id
            )
        finally:
            release_first.set()
        assert first.result(timeout=5) == str(workspace.id)
