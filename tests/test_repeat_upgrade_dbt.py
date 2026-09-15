"""Real PostgreSQL/dbt checks for existing repeat consumers during rematerialization.

Requires a dedicated MANAGED_DATABASE_URL with schema/role creation privileges,
as in test_dbt_confinement.py. Locally, use the disposable database test runner;
never point these tests at a development or provider database.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import psycopg
import psycopg.sql
import pytest
from psycopg.types.json import Jsonb

from apps.transformations.models import (
    AssetRunStatus,
    TransformationAsset,
    TransformationRunStatus,
    TransformationScope,
)
from apps.transformations.services.commcare_staging import upsert_system_assets
from apps.transformations.services.connect_staging import upsert_connect_assets
from apps.transformations.services.executor import run_transformation_pipeline
from apps.users.models import Tenant
from apps.workspaces.models import TenantMetadata, TenantSchema
from apps.workspaces.services.schema_manager import (
    SchemaManager,
    get_managed_db_connection,
    readonly_role_name,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not os.environ.get("MANAGED_DATABASE_URL"), reason="MANAGED_DATABASE_URL not set"
    ),
]

XMLNS = "urn:synthetic:repeat-upgrade"
RIGHT_BEFORE = ["right-before-1", "right-before-2"]
RIGHT_AFTER = ["right-after-1", "right-after-2", "right-after-3"]
LEFT_BEFORE = ["wrong-left-before"]
LEFT_AFTER = ["wrong-left-after-1", "wrong-left-after-2"]


@dataclass
class RepeatUpgrade:
    tenant: Tenant
    metadata: TenantMetadata
    parent: TransformationAsset
    legacy: TransformationAsset
    consumer: TransformationAsset
    schema: TenantSchema
    schemas: list[TenantSchema]
    manager: SchemaManager
    conn: psycopg.Connection


def _metadata(provider):
    return {
        "form_definitions": {
            XMLNS: {
                "name": "Registration",
                "questions": [
                    {
                        "value": f"{path}/answer",
                        "type": "Text",
                        "repeat": path if provider == "commcare" else True,
                    }
                    for path in ("/data/left/a", "/data/right/a")
                ],
            }
        }
    }


def _legacy_sql(provider):
    """Freeze the pre-disambiguation SQL; the current generator is not the oracle."""
    if provider == "commcare":
        parent = "stg_form_registration"
        item_id, json_column = "form_id", "form_data"
        parent_sql = (
            "SELECT\n    form_id,\n    xmlns,\n"
            '    received_on::timestamp AS "received_on",\n    app_id,\n    form_data\n'
            f"FROM raw_forms\nWHERE xmlns = '{XMLNS}'"
        )
    else:
        parent = "stg_visits"
        item_id, json_column = "visit_id", "form_json"
        parent_sql = (
            "SELECT\n    visit_id,\n    opportunity_id,\n    username,\n"
            "    entity_id,\n    status,\n    deliver_unit_id,\n    form_json\nFROM raw_visits"
        )
    # Before repeat-name disambiguation, the second /a overwrote the first /a.
    path = "ARRAY['data','right','a']::text[]"
    repeat_sql = (
        f"SELECT\n    f.{item_id},\n"
        f"    row_number() OVER (PARTITION BY f.{item_id} ORDER BY elem.ordinality) "
        'AS "repeat_index",\n'
        "    elem.value->>'answer' AS \"answer\"\n"
        f"FROM {{{{ ref('{parent}') }}}} f,\n"
        "LATERAL jsonb_array_elements(\n"
        f"    f.{json_column} #> {path}\n"
        ") WITH ORDINALITY AS elem(value, ordinality)\n"
        f"WHERE f.{json_column} #> {path} IS NOT NULL"
    )
    return parent, parent_sql, repeat_sql


def _load_raw(conn, provider, schema, *, create, right, left):
    data = Jsonb(
        {
            "data": {
                "left": {"a": [{"answer": answer} for answer in left]},
                "right": {"a": [{"answer": answer} for answer in right]},
            }
        }
    )
    if provider == "commcare":
        table = "raw_forms"
        columns = "form_id TEXT, xmlns TEXT, received_on TEXT, app_id TEXT, form_data JSONB"
        values = ("synthetic-form", XMLNS, "2026-09-15T12:00:00Z", "synthetic-app", data)
    else:
        table = "raw_visits"
        columns = (
            "visit_id TEXT, opportunity_id BIGINT, username TEXT, entity_id TEXT, "
            "status TEXT, deliver_unit_id TEXT, form_json JSONB"
        )
        values = ("synthetic-visit", 1, "synthetic-user", "synthetic-entity", "complete", "1", data)
    target = psycopg.sql.Identifier(schema, table)
    with conn.cursor() as cursor:
        if create:
            cursor.execute(
                psycopg.sql.SQL("CREATE TABLE {} ({})").format(target, psycopg.sql.SQL(columns))
            )
        else:
            cursor.execute(psycopg.sql.SQL("TRUNCATE {}").format(target))
        cursor.execute(
            psycopg.sql.SQL("INSERT INTO {} VALUES ({})").format(
                target, psycopg.sql.SQL(", ").join(psycopg.sql.Placeholder() for _ in values)
            ),
            values,
        )


def _tables(conn, schema):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (schema,),
        )
        return {row[0] for row in cursor.fetchall()}


def _answers(conn, schema, model):
    with conn.cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL("SET ROLE {}").format(
                psycopg.sql.Identifier(readonly_role_name(schema))
            )
        )
        try:
            cursor.execute(
                psycopg.sql.SQL("SELECT repeat_index, answer FROM {} ORDER BY repeat_index").format(
                    psycopg.sql.Identifier(schema, model)
                )
            )
            return cursor.fetchall()
        finally:
            cursor.execute("RESET ROLE")


def _assert_build(tenant, schema):
    run = run_transformation_pipeline(tenant=tenant, schema_name=schema)
    assert run.status == TransformationRunStatus.COMPLETED, run.error_message
    assert not run.asset_runs.exclude(status=AssetRunStatus.SUCCESS).exists()
    assert run.asset_runs.count() == TransformationAsset.objects.filter(tenant=tenant).count()
    return run


@pytest.fixture(params=["commcare", "commcare_connect"])
def repeat_upgrade(request, monkeypatch):
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    tenant = Tenant.objects.create(
        provider=request.param,
        external_id=f"repeat-dbt-{uuid.uuid4().hex[:12]}",
        canonical_name="Synthetic repeat upgrade",
    )
    manager = SchemaManager()
    schemas = []
    conn = get_managed_db_connection()
    try:
        schema = manager.provision(tenant)
        schemas.append(schema)
        _load_raw(
            conn,
            tenant.provider,
            schema.schema_name,
            create=True,
            right=RIGHT_BEFORE,
            left=LEFT_BEFORE,
        )
        parent_name, parent_sql, repeat_sql = _legacy_sql(tenant.provider)
        parent = TransformationAsset.objects.create(
            tenant=tenant,
            scope=TransformationScope.SYSTEM,
            name=parent_name,
            sql_content=parent_sql,
        )
        legacy = TransformationAsset.objects.create(
            tenant=tenant,
            scope=TransformationScope.SYSTEM,
            name=f"{parent_name}__repeat_a",
            sql_content=repeat_sql,
        )
        consumer = TransformationAsset.objects.create(
            tenant=tenant,
            scope=TransformationScope.TENANT,
            name="custom_right_answers",
            # Cross-stage references are physical table names, not same-project dbt refs.
            sql_content=f'SELECT repeat_index, answer FROM "{legacy.name}"',
            replaces=legacy,
        )
        metadata = TenantMetadata.objects.create(tenant=tenant, metadata=_metadata(tenant.provider))
        yield RepeatUpgrade(
            tenant, metadata, parent, legacy, consumer, schema, schemas, manager, conn
        )
    finally:
        cleanup_errors = []
        try:
            for owned_schema in reversed(schemas):
                try:
                    manager.teardown(owned_schema)
                except Exception as exc:
                    cleanup_errors.append(exc)
        finally:
            conn.close()
        if cleanup_errors:
            raise ExceptionGroup("Owned repeat schemas could not all be removed", cleanup_errors)


@pytest.mark.parametrize("mode", ["empty_refresh_schema", "in_place_reload"])
def test_repeat_consumer_survives_real_dbt_rematerialization(repeat_upgrade, mode):
    case = repeat_upgrade
    initial_schema = case.schema.schema_name
    legacy_id, legacy_name = case.legacy.id, case.legacy.name
    parent_id, consumer_id = case.parent.id, case.consumer.id
    consumer_sql = case.consumer.sql_content
    before = list(enumerate(RIGHT_BEFORE, start=1))
    after = list(enumerate(RIGHT_AFTER, start=1))

    _assert_build(case.tenant, initial_schema)
    assert _answers(case.conn, initial_schema, legacy_name) == before
    assert _answers(case.conn, initial_schema, case.consumer.name) == before

    if mode == "empty_refresh_schema":
        refresh = case.manager.create_refresh_schema(case.tenant)
        case.schemas.append(refresh)
        case.manager.create_physical_schema(refresh)
        target_schema = refresh.schema_name
        assert target_schema != initial_schema
        assert _tables(case.conn, target_schema) == set()
    else:
        target_schema = initial_schema

    _load_raw(
        case.conn,
        case.tenant.provider,
        target_schema,
        create=mode == "empty_refresh_schema",
        right=RIGHT_AFTER,
        left=LEFT_AFTER,
    )
    if mode == "empty_refresh_schema":
        assert _tables(case.conn, target_schema) == {
            "raw_forms" if case.tenant.provider == "commcare" else "raw_visits"
        }
    else:
        # A successful run against an old table could look green but return stale answers.
        assert _answers(case.conn, target_schema, case.consumer.name) == before

    upsert = upsert_system_assets if case.tenant.provider == "commcare" else upsert_connect_assets
    upsert(case.tenant, case.metadata)
    _assert_build(case.tenant, target_schema)
    assert _answers(case.conn, target_schema, case.consumer.name) == after
    assert _answers(case.conn, target_schema, legacy_name) == after

    case.legacy.refresh_from_db()
    case.parent.refresh_from_db()
    case.consumer.refresh_from_db()
    assert (case.legacy.id, case.legacy.name) == (legacy_id, legacy_name)
    assert (case.parent.id, case.consumer.id) == (parent_id, consumer_id)
    assert case.consumer.replaces_id == legacy_id
    assert case.consumer.sql_content == consumer_sql
    assert case.metadata.metadata == _metadata(case.tenant.provider)

    siblings = list(
        TransformationAsset.objects.filter(tenant=case.tenant, scope=TransformationScope.SYSTEM)
        .exclude(pk__in=[parent_id, legacy_id])
        .values_list("name", flat=True)
    )
    assert len(siblings) == 1
    assert _answers(case.conn, target_schema, siblings[0]) == list(enumerate(LEFT_AFTER, start=1))
    if mode == "empty_refresh_schema":
        assert _answers(case.conn, initial_schema, case.consumer.name) == before


@pytest.mark.parametrize("repeat_upgrade", ["commcare_connect"], indirect=True)
def test_historical_connect_parent_repairs_real_dbt_without_retargeting_consumers(repeat_upgrade):
    case = repeat_upgrade
    case.parent.sql_content = case.parent.sql_content.replace("    username,", "    user_id,")
    case.parent.save(update_fields=["sql_content"])
    legacy_id, legacy_name = case.legacy.id, case.legacy.name
    consumer_sql = case.consumer.sql_content

    # The historical generator persisted assets before dbt found its bad column.
    broken = run_transformation_pipeline(tenant=case.tenant, schema_name=case.schema.schema_name)
    assert broken.status == TransformationRunStatus.FAILED
    assert "user_id" in broken.error_message

    _load_raw(
        case.conn,
        case.tenant.provider,
        case.schema.schema_name,
        create=False,
        right=RIGHT_AFTER,
        left=LEFT_AFTER,
    )
    upsert_connect_assets(case.tenant, case.metadata)
    _assert_build(case.tenant, case.schema.schema_name)
    assert _answers(case.conn, case.schema.schema_name, case.consumer.name) == list(
        enumerate(RIGHT_AFTER, start=1)
    )
    case.legacy.refresh_from_db()
    case.parent.refresh_from_db()
    case.consumer.refresh_from_db()
    assert (case.legacy.id, case.legacy.name) == (legacy_id, legacy_name)
    assert case.consumer.replaces_id == legacy_id
    assert case.consumer.sql_content == consumer_sql
    assert "    username," in case.parent.sql_content
    assert "    user_id," not in case.parent.sql_content
