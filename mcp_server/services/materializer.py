"""
Simplified materializer for the vertical slice.

Loads CommCare case data via the Case API v2 and writes it to raw tables
in the tenant's schema.  No DBT transforms — the raw table IS the
queryable table for now.
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

    # 2. Load cases from CommCare (v2 API)
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

        # Create cases table (replace if exists, CASCADE drops dependents)
        cursor.execute(psql.SQL("DROP TABLE IF EXISTS {}.cases CASCADE").format(schema_id))
        cursor.execute(
            psql.SQL(
                """
            CREATE TABLE {schema}.cases (
                case_id TEXT PRIMARY KEY,
                case_type TEXT,
                case_name TEXT,
                external_id TEXT,
                owner_id TEXT,
                date_opened TEXT,
                last_modified TEXT,
                server_last_modified TEXT,
                indexed_on TEXT,
                closed BOOLEAN DEFAULT FALSE,
                date_closed TEXT,
                properties JSONB DEFAULT '{{}}'::jsonb,
                indices JSONB DEFAULT '{{}}'::jsonb
            )
        """
            ).format(schema=schema_id)
        )

        # Insert rows
        insert_sql = psql.SQL(
            """
            INSERT INTO {schema}.cases
                (case_id, case_type, case_name, external_id, owner_id,
                 date_opened, last_modified, server_last_modified, indexed_on,
                 closed, date_closed, properties, indices)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (case_id) DO UPDATE SET
                case_name = EXCLUDED.case_name,
                owner_id = EXCLUDED.owner_id,
                last_modified = EXCLUDED.last_modified,
                server_last_modified = EXCLUDED.server_last_modified,
                indexed_on = EXCLUDED.indexed_on,
                closed = EXCLUDED.closed,
                date_closed = EXCLUDED.date_closed,
                properties = EXCLUDED.properties,
                indices = EXCLUDED.indices
            """
        ).format(schema=schema_id)

        for case in cases:
            cursor.execute(
                insert_sql,
                (
                    case.get("case_id"),
                    case.get("case_type", ""),
                    case.get("case_name", ""),
                    case.get("external_id", ""),
                    case.get("owner_id", ""),
                    case.get("date_opened", ""),
                    case.get("last_modified", ""),
                    case.get("server_last_modified", ""),
                    case.get("indexed_on", ""),
                    case.get("closed", False),
                    case.get("date_closed") or "",
                    json.dumps(case.get("properties", {})),
                    json.dumps(case.get("indices", {})),
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
