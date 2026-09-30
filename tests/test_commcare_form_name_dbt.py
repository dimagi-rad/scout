"""Real PostgreSQL/dbt build of xmlns-keyed duplicate form models (#470).

Requires a dedicated MANAGED_DATABASE_URL with schema/role creation privileges,
as in test_dbt_confinement.py. Never point these tests at a development or
provider database.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import psycopg.sql
import pytest
from psycopg.types.json import Jsonb

from apps.common.identifiers import view_name
from apps.transformations.models import (
    AssetRunStatus,
    TransformationAsset,
    TransformationRunStatus,
)
from apps.transformations.services.commcare_staging import upsert_system_assets
from apps.transformations.services.executor import run_transformation_pipeline
from apps.users.models import Tenant
from apps.workspaces.models import TenantMetadata
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not os.environ.get("MANAGED_DATABASE_URL"), reason="MANAGED_DATABASE_URL not set"
    ),
]

LONG = "Household Registration And Follow Up Visit For Community Health Workers " * 2
XMLNS = [f"urn:synthetic:{'x' * 120}:{index}" for index in range(3)]


def _forms(order):
    return {
        XMLNS[index]: {
            "name": LONG,
            "app_name": LONG,
            "questions": [
                {"value": "/data/answer", "type": "Text"},
                {"value": "/data/items/score", "type": "Int", "repeat": "/data/items"},
            ],
        }
        for index in order
    }


def _load_raw(conn, schema):
    target = psycopg.sql.Identifier(schema, "raw_forms")
    with conn.cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(
                "CREATE TABLE {} (form_id TEXT, xmlns TEXT, received_on TEXT, app_id TEXT, "
                "form_data JSONB)"
            ).format(target)
        )
        for index, xmlns in enumerate(XMLNS):
            data = {"answer": xmlns, "items": [{"score": str(n)} for n in range(index + 1)]}
            cursor.execute(
                psycopg.sql.SQL("INSERT INTO {} VALUES (%s, %s, %s, %s, %s)").format(target),
                (f"form-{index}", xmlns, "2026-09-30T12:00:00Z", "app", Jsonb({"data": data})),
            )


def _relations(cursor, schema):
    cursor.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
    )
    return {row[0] for row in cursor.fetchall()}


def test_duplicate_forms_build_under_stable_fitted_names(monkeypatch):
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    tenant = Tenant.objects.create(
        provider="commcare",
        external_id=f"form-identity-dbt-{uuid.uuid4().hex[:12]}",
        canonical_name=LONG,
    )
    manager = SchemaManager()
    conn = get_managed_db_connection()
    schema = None
    view_schema = f"ws_{uuid.uuid4().hex[:16]}"
    try:
        schema = manager.provision(tenant)
        name = schema.schema_name
        _load_raw(conn, name)
        metadata = TenantMetadata.objects.create(
            tenant=tenant, metadata={"form_definitions": _forms([0, 1, 2])}
        )
        assert upsert_system_assets(tenant, metadata)["created"] == 6

        run = run_transformation_pipeline(tenant=tenant, schema_name=name)
        assert run.status == TransformationRunStatus.COMPLETED, run.error_message
        assert not run.asset_runs.exclude(status=AssetRunStatus.SUCCESS).exists()

        built = dict(
            TransformationAsset.objects.filter(tenant=tenant).values_list("name", "sql_content")
        )
        models = set(built)
        with conn.cursor() as cursor:
            assert models <= _relations(cursor, name)
            for model in models:
                cursor.execute(
                    psycopg.sql.SQL("SELECT count(*) FROM {}").format(
                        psycopg.sql.Identifier(name, model)
                    )
                )
                # Each form model reads exactly its own xmlns; repeats unnest 1..3 items.
                assert cursor.fetchone()[0] in {1, 2, 3}

            # Postgres would silently truncate an overlong view name; creating each
            # fitted name and reading it back proves none collapsed.
            prefix = manager._view_prefix(tenant)
            views = {view_name(prefix, model): model for model in models}
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(view_schema))
            )
            for view, model in views.items():
                cursor.execute(
                    psycopg.sql.SQL("CREATE VIEW {} AS SELECT * FROM {}").format(
                        psycopg.sql.Identifier(view_schema, view),
                        psycopg.sql.Identifier(name, model),
                    )
                )
            assert _relations(cursor, view_schema) == set(views)

        metadata.metadata = {"form_definitions": _forms([2, 0, 1])}
        metadata.save()
        result = upsert_system_assets(tenant, metadata)
        assert (result["created"], result["deleted"]) == (0, 0)
        # Same names alone is not enough: each name must still read the same xmlns.
        assert (
            dict(
                TransformationAsset.objects.filter(tenant=tenant).values_list("name", "sql_content")
            )
            == built
        )
    finally:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(view_schema)
                    )
                )
            if schema is not None:
                manager.teardown(schema)
        finally:
            conn.close()
