"""Real-DB confinement tests for the dbt transform path (issue #241).

These prove the security boundary 04#3 (dbt must NOT run user-authored asset SQL
as the managed-DB superuser) and the correctness fix 04#4 (a generated staging
model's unqualified ``FROM raw_cases`` must resolve inside the tenant schema, and
a dbt failure must surface as a FAILED run rather than a swallowed COMPLETED).

They require a real managed database (``MANAGED_DATABASE_URL``) AND a managed-DB
role that can ``CREATE ROLE`` / ``GRANT`` / ``SET ROLE`` (the production role and
the local ``platform`` role both can; CI is the authoritative gate). Schema/role
names are uniquely suffixed so sibling test runs sharing the managed DB do not
collide.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import psycopg.errors
import psycopg.sql
import pytest

from apps.transformations.models import (
    TransformationAsset,
    TransformationRunStatus,
    TransformationScope,
)
from apps.transformations.services.executor import run_transformation_pipeline
from apps.users.models import Tenant
from apps.workspaces.services.schema_manager import (
    SchemaManager,
    dbt_role_name,
    get_managed_db_connection,
    readonly_role_name,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"),
    reason="MANAGED_DATABASE_URL not set",
)


@pytest.fixture
def managed_conn():
    conn = get_managed_db_connection()
    yield conn
    if not conn.closed:
        conn.close()


@pytest.fixture
def confinement_schemas(db, managed_conn):
    """Provision an attacker tenant schema + a victim schema with a secret table.

    Yields ``(attacker_schema, victim_schema)``. Both physical schemas and the
    derived roles are dropped on teardown.
    """
    suffix = uuid.uuid4().hex[:8]
    attacker_schema = f"conf241_attacker_{suffix}"
    victim_schema = f"conf241_victim_{suffix}"

    attacker_tenant = Tenant.objects.create(
        provider="commcare",
        external_id=f"conf241-attacker-{suffix}",
        canonical_name=f"attacker_{suffix}",
    )

    mgr = SchemaManager()
    cur = managed_conn.cursor()
    # Provision the attacker schema the real way so the {schema}_dbt role and its
    # grants are created exactly as production would create them.
    cur.execute(
        psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
            psycopg.sql.Identifier(attacker_schema)
        )
    )
    mgr._create_readonly_role(cur, attacker_schema)  # also creates {schema}_dbt
    # The attacker schema has its own raw_cases (what a staging model reads).
    cur.execute(
        psycopg.sql.SQL("CREATE TABLE {}.raw_cases (case_id TEXT, case_type TEXT)").format(
            psycopg.sql.Identifier(attacker_schema)
        )
    )
    cur.execute(
        psycopg.sql.SQL("INSERT INTO {}.raw_cases VALUES ('c1', 'patient')").format(
            psycopg.sql.Identifier(attacker_schema)
        )
    )
    # The victim schema holds a secret the attacker must NOT be able to read.
    cur.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(victim_schema)))
    cur.execute(
        psycopg.sql.SQL("CREATE TABLE {}.secret (ssn TEXT)").format(
            psycopg.sql.Identifier(victim_schema)
        )
    )
    cur.execute(
        psycopg.sql.SQL("INSERT INTO {}.secret VALUES ('111-22-3333')").format(
            psycopg.sql.Identifier(victim_schema)
        )
    )

    yield attacker_schema, victim_schema, attacker_tenant

    for schema in (attacker_schema, victim_schema):
        cur.execute(
            psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(schema)
            )
        )
    cur.execute(
        psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
            psycopg.sql.Identifier(dbt_role_name(attacker_schema))
        )
    )
    cur.execute(
        psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
            psycopg.sql.Identifier(f"{attacker_schema}_ro")
        )
    )


@pytest.mark.django_db(transaction=True)
def test_dbt_role_cannot_read_other_tenant_schema(confinement_schemas, managed_conn):
    """The low-privilege dbt role can read its OWN schema but is blocked from a
    fully-qualified cross-tenant read — this is the boundary dbt's profile
    ``role`` (SET ROLE) enforces (issue #241, 04#3)."""
    attacker_schema, victim_schema, _ = confinement_schemas
    role = dbt_role_name(attacker_schema)

    cur = managed_conn.cursor()
    cur.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
    try:
        # Own schema is readable.
        cur.execute(
            psycopg.sql.SQL("SELECT count(*) FROM {}.raw_cases").format(
                psycopg.sql.Identifier(attacker_schema)
            )
        )
        assert cur.fetchone()[0] == 1

        # Cross-tenant read is blocked by missing USAGE — not allowed as superuser.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                psycopg.sql.SQL("SELECT ssn FROM {}.secret").format(
                    psycopg.sql.Identifier(victim_schema)
                )
            )
    finally:
        managed_conn.rollback()
        cur.execute("RESET ROLE")


@pytest.mark.django_db(transaction=True)
def test_generated_staging_model_resolves_raw_cases(confinement_schemas, settings):
    """A SYSTEM asset with the generated shape (unqualified ``FROM raw_cases``)
    materializes successfully — the profile's schema-scoped search_path resolves
    raw_cases inside the tenant schema (issue #241, 04#4)."""
    attacker_schema, _, attacker_tenant = confinement_schemas

    TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=attacker_tenant,
        sql_content="SELECT case_id, case_type FROM raw_cases WHERE case_type = 'patient'",
    )

    run = run_transformation_pipeline(tenant=attacker_tenant, schema_name=attacker_schema)

    assert run.status == TransformationRunStatus.COMPLETED, run.error_message
    ar = run.asset_runs.get(asset__name="stg_case_patient")
    assert ar.status == "success", ar.logs

    conn = get_managed_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            psycopg.sql.SQL("SET ROLE {}").format(
                psycopg.sql.Identifier(readonly_role_name(attacker_schema))
            )
        )
        cur.execute(
            psycopg.sql.SQL("SET search_path TO {}").format(
                psycopg.sql.Identifier(attacker_schema)
            )
        )
        cur.execute("SELECT count(*) FROM stg_case_patient")
        assert cur.fetchone()[0] == 1
    finally:
        conn.rollback()
        cur.execute("RESET ROLE")
        cur.close()
        conn.close()


@pytest.mark.django_db(transaction=True)
def test_readonly_role_can_read_dbt_materialized_table(confinement_schemas, managed_conn):
    """The ``_ro`` query role can SELECT a table dbt materialized under the ``_dbt``
    confinement role.

    Regression for the prod ``permission denied for table stg_visits`` error: since
    issue #241 dbt owns the tables it creates, so the CURRENT_USER default-privilege
    grant never reached them and the ``_ro`` role (used by the MCP query path via
    ``SET ROLE``) could not read freshly materialized staging tables.
    """
    attacker_schema, _, attacker_tenant = confinement_schemas

    TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=attacker_tenant,
        sql_content="SELECT case_id, case_type FROM raw_cases WHERE case_type = 'patient'",
    )
    run = run_transformation_pipeline(tenant=attacker_tenant, schema_name=attacker_schema)
    assert run.status == TransformationRunStatus.COMPLETED, run.error_message

    ro_role = f"{attacker_schema}_ro"
    cur = managed_conn.cursor()
    cur.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(ro_role)))
    try:
        cur.execute(
            psycopg.sql.SQL("SELECT count(*) FROM {}.stg_case_patient").format(
                psycopg.sql.Identifier(attacker_schema)
            )
        )
        assert cur.fetchone()[0] == 1
    finally:
        managed_conn.rollback()
        cur.execute("RESET ROLE")


@pytest.mark.django_db(transaction=True)
def test_cross_tenant_asset_cannot_materialize(confinement_schemas):
    """A SYSTEM asset whose free-text SQL reaches into ANOTHER tenant's schema
    cannot materialize: dbt runs it under the confinement role, which lacks USAGE
    on the victim schema, so the run is FAILED (issue #241, 04#3). Before the fix
    dbt ran this as superuser and happily copied the secret into the attacker's
    schema."""
    attacker_schema, victim_schema, attacker_tenant = confinement_schemas

    TransformationAsset.objects.create(
        name="stg_exfil",
        scope=TransformationScope.SYSTEM,
        tenant=attacker_tenant,
        sql_content=f"SELECT ssn FROM {victim_schema}.secret",
    )

    run = run_transformation_pipeline(tenant=attacker_tenant, schema_name=attacker_schema)

    assert run.status == TransformationRunStatus.FAILED
