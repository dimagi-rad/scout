"""A run failed as a zombie keeps its resume cursor, so the resume must be idempotent.

The cursor is persisted to the platform DB only after a page commits in the
managed DB, so it can lag committed rows by a page (a crash between the two, or
a swallowed checkpoint write). The zombie janitor and the orphaned-candidate
sweep flip such runs to FAILED, which makes them resume targets. These tests
drive that sequence against a real managed database.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from asgiref.sync import async_to_sync
from django.conf import settings
from psycopg import sql as psql

from apps.users.models import Tenant
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from apps.workspaces.tasks import _fail_zombie_materialization_run
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
from mcp_server.services.materializer import (
    _write_connect_completed_works,
    _write_connect_visits,
    run_pipeline,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _completed_work(n: int) -> dict:
    # The v2 export sends no ``id``; it is here only so the fake loader can
    # position a resume. The writer ignores it (identity column).
    return {"id": n, "username": f"flw{n}", "opportunity_id": 4242, "status": "approved"}


def _visit(n: int) -> dict:
    return {"visit_id": n, "opportunity_id": 4242, "username": f"flw{n}", "status": "pending"}


@pytest.fixture
def managed_conn():
    if not settings.MANAGED_DATABASE_URL:
        pytest.skip("MANAGED_DATABASE_URL is not configured")
    conn = psycopg.connect(settings.MANAGED_DATABASE_URL, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def candidate(managed_conn):
    tenant = Tenant.objects.create(
        provider="commcare_connect", external_id="4242", canonical_name="Opp 4242"
    )
    schema = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"test_zombie_{uuid.uuid4().hex[:8]}",
        state=SchemaState.PROVISIONING,
    )
    sid = psql.Identifier(schema.schema_name)
    managed_conn.execute(psql.SQL("CREATE SCHEMA {}").format(sid))
    yield schema
    managed_conn.autocommit = True
    managed_conn.execute(psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sid))


def _zombie_run(schema: TenantSchema, source: str, lagging_last_id: int) -> MaterializationRun:
    """A LOADING run whose worker died after committing a page but before its cursor."""
    run = MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="connect_sync",
        state=MaterializationRun.RunState.LOADING,
        result={
            "pipeline": "connect_sync",
            "sources": {
                source: {
                    "state": "in_progress",
                    "rows": 2,
                    "cursor_state": {"last_id": lagging_last_id, "last_committed_at": None},
                }
            },
        },
    )
    assert async_to_sync(_fail_zombie_materialization_run)(run, "worker died")
    run.refresh_from_db()
    assert run.state == MaterializationRun.RunState.FAILED
    # The janitor keeps the cursor: resuming a dead writer's candidate is the design.
    assert run.result["sources"][source]["cursor_state"]["last_id"] == lagging_last_id
    return run


def _pages_after(all_pages):
    def load_pages(start_last_id=None):
        for page in all_pages:
            ids = [r.get("id", r.get("visit_id")) for r in page]
            if start_last_id is None or max(ids) > start_last_id:
                yield page, None

    return load_pages


def _resume(schema: TenantSchema, source: str, loader_attr: str, all_pages):
    # resumable=True is SourceConfig's default, so a source missing its YAML
    # opt-out must not be able to turn a resume into an append.
    pipeline = PipelineConfig(
        name="connect_sync",
        description="",
        version="1.0",
        provider="commcare_connect",
        sources=[SourceConfig(name=source, resumable=True)],
    )
    loader_cls = MagicMock()
    loader_cls.return_value.load_pages.side_effect = _pages_after(all_pages)
    membership = SimpleNamespace(tenant=schema.tenant, tenant_id=schema.tenant_id, connection=None)
    with (
        patch("mcp_server.services.materializer._run_discover_phase", return_value=None),
        patch("mcp_server.services.materializer.get_tenant_metadata", return_value=None),
        patch("mcp_server.services.materializer.pipeline_fingerprint", return_value="fp"),
        patch(f"mcp_server.services.materializer.{loader_attr}", loader_cls),
    ):
        result = run_pipeline(
            membership,
            {"type": "api_key", "value": "x"},
            pipeline,
            target_schema=schema,
            defer_schema_promotion=True,
        )
    return result, loader_cls.return_value.load_pages.call_args


def _count(conn, schema: TenantSchema, table: str) -> int:
    query = psql.SQL("SELECT COUNT(*) FROM {}.{}").format(
        psql.Identifier(schema.schema_name), psql.Identifier(table)
    )
    return conn.execute(query).fetchone()[0]


def test_resuming_a_zombie_run_does_not_duplicate_an_identity_pk_source(managed_conn, candidate):
    page_1 = [_completed_work(1), _completed_work(2)]
    page_2 = [_completed_work(3), _completed_work(4)]
    _write_connect_completed_works(
        iter([(page_1, None), (page_2, None)]), candidate.schema_name, managed_conn
    )
    _zombie_run(candidate, "completed_works", lagging_last_id=2)

    result, load_call = _resume(
        candidate, "completed_works", "ConnectCompletedWorkLoader", [page_1, page_2]
    )

    assert result["status"] == "completed"
    # raw_completed_works has no natural key to upsert on, so a replayed page
    # would append; only a full reload keeps it exact.
    assert _count(managed_conn, candidate, "raw_completed_works") == 4
    assert load_call.kwargs.get("start_last_id") is None


def test_resuming_a_zombie_run_replays_visits_without_duplicating(managed_conn, candidate):
    page_1 = [_visit(1), _visit(2)]
    page_2 = [_visit(3), _visit(4)]
    _write_connect_visits(
        iter([(page_1, None), (page_2, None)]), candidate.schema_name, managed_conn
    )
    _zombie_run(candidate, "visits", lagging_last_id=2)

    result, load_call = _resume(candidate, "visits", "ConnectVisitLoader", [page_1, page_2])

    assert result["status"] == "completed"
    # Visits upserts on visit_id, so it keeps resuming from the zombie's cursor.
    assert load_call.kwargs["start_last_id"] == 2
    assert _count(managed_conn, candidate, "raw_visits") == 4
