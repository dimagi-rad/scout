"""Real PostgreSQL/dbt build of staging models wider than PostgreSQL allows (#712).

Requires a dedicated MANAGED_DATABASE_URL with schema/role creation privileges,
as in test_dbt_confinement.py. Never point these tests at a development or
provider database.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg.sql
import pytest
from psycopg.types.json import Jsonb

from apps.transformations.models import (
    AssetRunStatus,
    TransformationAsset,
    TransformationRunStatus,
)
from apps.transformations.services.commcare_staging import (
    MAX_STAGING_COLUMNS,
    POSTGRES_MAX_TABLE_COLUMNS,
    upsert_system_assets,
)
from apps.transformations.services.connect_staging import upsert_connect_assets
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

WIDE = 2100
LAST = f"{WIDE - 1:04d}"


def _answers(prefix):
    # Single-character answers keep the row inside PostgreSQL's 8KB heap tuple limit.
    answers = {f"{prefix}{index:04d}": "v" for index in range(WIDE)}
    answers[f"{prefix}0000"] = "first"
    answers[f"{prefix}{LAST}"] = "last"
    return answers


def _questions(prefix, **extra):
    return [{"value": f"{prefix}{index:04d}", "type": "Text", **extra} for index in range(WIDE)]


def _connect_setup(conn, schema):
    form_json = {"data": {**_answers("q"), "items": [_answers("c"), _answers("c")]}}
    target = psycopg.sql.Identifier(schema, "raw_visits")
    with conn.cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(
                "CREATE TABLE {} (visit_id TEXT, opportunity_id BIGINT, username TEXT, "
                "entity_id TEXT, status TEXT, deliver_unit_id TEXT, form_json JSONB)"
            ).format(target)
        )
        cursor.execute(
            psycopg.sql.SQL("INSERT INTO {} VALUES (%s, 1, %s, %s, %s, %s, %s)").format(target),
            ("visit-1", "user", "entity", "approved", "1", Jsonb(form_json)),
        )
    return {
        "form_definitions": {
            "visit": {
                "name": "Visit",
                "questions": _questions("/data/q") + _questions("/data/items/c", repeat=True),
            }
        }
    }


def _commcare_setup(conn, schema):
    target = psycopg.sql.Identifier(schema, "raw_cases")
    with conn.cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(
                "CREATE TABLE {} (case_id TEXT, case_type TEXT, case_name TEXT, owner_id TEXT, "
                "date_opened TIMESTAMPTZ, last_modified TIMESTAMPTZ, closed BOOLEAN, "
                "properties JSONB)"
            ).format(target)
        )
        cursor.execute(
            psycopg.sql.SQL(
                "INSERT INTO {} VALUES (%s, %s, %s, %s, now(), now(), false, %s)"
            ).format(target),
            ("case-1", "patient", "Patient", "owner", Jsonb(_answers("p"))),
        )
    return {
        "case_types": [{"name": "patient"}],
        "app_definitions": [
            {
                "modules": [
                    {
                        "case_type": "patient",
                        "case_properties": [f"p{index:04d}" for index in range(WIDE)],
                    }
                ]
            }
        ],
    }


# provider -> (setup, upsert, [(model, kept column, raw column, folded key path)])
CASES = {
    "commcare_connect": (
        _connect_setup,
        upsert_connect_assets,
        [
            ("stg_visits", "q0000", "form_json", ["data", f"q{LAST}"]),
            ("stg_visits__repeat_items", "c0000", "repeat_data", [f"c{LAST}"]),
        ],
    ),
    "commcare": (
        _commcare_setup,
        upsert_system_assets,
        [("stg_case_patient", "p0000", "properties", [f"p{LAST}"])],
    ),
}


@contextmanager
def _built(provider, setup, upsert):
    tenant = Tenant.objects.create(
        provider=provider,
        external_id=f"wide-dbt-{uuid.uuid4().hex[:12]}",
        canonical_name="Synthetic wide staging",
    )
    manager = SchemaManager()
    conn = get_managed_db_connection()
    schema = None
    try:
        schema = manager.provision(tenant)
        name = schema.schema_name
        metadata = TenantMetadata.objects.create(tenant=tenant, metadata=setup(conn, name))
        upsert(tenant, metadata)

        run = run_transformation_pipeline(tenant=tenant, schema_name=name)

        assert run.status == TransformationRunStatus.COMPLETED, run.error_message
        assert not run.asset_runs.exclude(status=AssetRunStatus.SUCCESS).exists()
        assert run.asset_runs.count() == TransformationAsset.objects.filter(tenant=tenant).count()
        yield conn, name
    finally:
        try:
            if schema is not None:
                manager.teardown(schema)
        finally:
            conn.close()


def _column_count(cursor, schema, model):
    cursor.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, model),
    )
    return cursor.fetchone()[0]


@pytest.mark.parametrize("provider", CASES)
def test_wide_staging_models_build_and_keep_folded_fields(provider, monkeypatch):
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    setup, upsert, models = CASES[provider]
    with _built(provider, setup, upsert) as (conn, name), conn.cursor() as cursor:
        for model, kept, raw, folded_path in models:
            assert _column_count(cursor, name, model) <= MAX_STAGING_COLUMNS
            cursor.execute(
                psycopg.sql.SQL("SELECT {}, {} #>> %s FROM {}").format(
                    psycopg.sql.Identifier(kept),
                    psycopg.sql.Identifier(raw),
                    psycopg.sql.Identifier(name, model),
                ),
                (folded_path,),
            )
            rows = cursor.fetchall()
            assert rows
            assert all(row == ("first", "last") for row in rows)


def test_model_at_the_table_limit_builds_unfolded(monkeypatch):
    # "Nothing that builds today loses a column" rests on CREATE TABLE AS
    # accepting exactly 1600 columns through dbt's table materialization.
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    count = POSTGRES_MAX_TABLE_COLUMNS - 7
    last = f"q{count - 1:04d}"

    def setup(conn, schema):
        metadata = _connect_setup(conn, schema)
        questions = metadata["form_definitions"]["visit"]["questions"]
        metadata["form_definitions"]["visit"]["questions"] = [
            q for q in questions if not q.get("repeat") and q["value"] <= f"/data/{last}"
        ]
        return metadata

    with (
        _built("commcare_connect", setup, upsert_connect_assets) as (conn, name),
        conn.cursor() as cursor,
    ):
        assert _column_count(cursor, name, "stg_visits") == POSTGRES_MAX_TABLE_COLUMNS
        cursor.execute(
            psycopg.sql.SQL("SELECT q0000, {} FROM {}").format(
                psycopg.sql.Identifier(last), psycopg.sql.Identifier(name, "stg_visits")
            )
        )
        assert cursor.fetchall() == [("first", "v")]
