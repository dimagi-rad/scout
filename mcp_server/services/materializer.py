"""
Simplified materializer for the vertical slice.

Loads CommCare case data and writes it to raw tables in the tenant's schema.
No DBT transforms — the raw table IS the queryable table for now.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from psycopg2 import sql as psql

from apps.projects.services.schema_manager import SchemaManager, get_managed_db_connection
from mcp_server.loaders.commcare_cases import CommCareCaseLoader

logger = logging.getLogger(__name__)


def run_commcare_sync(tenant_membership, access_token: str) -> dict:
    """Load CommCare cases into the tenant's schema.

    Returns a summary dict with row counts and status.
    """
    # 1. Provision schema
    mgr = SchemaManager()
    tenant_schema = mgr.provision(tenant_membership)
    schema_name = tenant_schema.schema_name

    # 2. Load cases from CommCare
    loader = CommCareCaseLoader(
        domain=tenant_membership.tenant_id,
        access_token=access_token,
    )
    cases = loader.load()

    if not cases:
        return {"status": "completed", "rows_loaded": 0, "schema": schema_name}

    # 3. Write to managed DB
    conn = get_managed_db_connection()
    try:
        cursor = conn.cursor()
        schema_id = psql.Identifier(schema_name)

        # Create cases table (replace if exists)
        cursor.execute(psql.SQL("DROP TABLE IF EXISTS {}.cases").format(schema_id))
        cursor.execute(
            psql.SQL(
                """
            CREATE TABLE {schema}.cases (
                case_id TEXT PRIMARY KEY,
                case_type TEXT,
                owner_id TEXT,
                date_opened TEXT,
                date_modified TEXT,
                closed BOOLEAN DEFAULT FALSE,
                properties JSONB DEFAULT '{{}}'::jsonb
            )
        """
            ).format(schema=schema_id)
        )

        # Insert rows
        for case in cases:
            props = case.get("properties", {})
            cursor.execute(
                psql.SQL(
                    """
                    INSERT INTO {schema}.cases
                        (case_id, case_type, owner_id, date_opened, date_modified, closed, properties)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (case_id) DO UPDATE SET
                        properties = EXCLUDED.properties,
                        date_modified = EXCLUDED.date_modified,
                        closed = EXCLUDED.closed
                """
                ).format(schema=schema_id),
                (
                    case.get("case_id"),
                    case.get("case_type", ""),
                    case.get("owner_id", ""),
                    case.get("date_opened", ""),
                    case.get("date_modified", ""),
                    case.get("closed", False),
                    json.dumps(props),
                ),
            )

        cursor.close()
    finally:
        conn.close()

    # 4. Update materialization record
    from apps.projects.models import MaterializationRun

    run = MaterializationRun.objects.create(
        tenant_schema=tenant_schema,
        pipeline="commcare_sync",
        state="completed",
        completed_at=datetime.now(UTC),
        result={"rows_loaded": len(cases), "table": "cases"},
    )

    tenant_schema.state = "active"
    tenant_schema.save(update_fields=["state", "last_accessed_at"])

    logger.info("Materialized %d cases into schema '%s'", len(cases), schema_name)

    return {
        "status": "completed",
        "run_id": str(run.id),
        "rows_loaded": len(cases),
        "schema": schema_name,
        "table": "cases",
    }
