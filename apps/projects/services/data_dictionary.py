"""
Data dictionary generator.

Connects to a project's database and introspects the schema to produce
a structured dictionary of tables, columns, types, relationships, and
sample values. The output is stored as JSON on the Project model and
also rendered as a text block for inclusion in the agent's system prompt.

Usage:
    from apps.projects.services.data_dictionary import DataDictionaryGenerator

    generator = DataDictionaryGenerator(project)
    dictionary = generator.generate()  # Returns dict, also saves to project.data_dictionary
    prompt_text = generator.render_for_prompt()  # Returns formatted string for system prompt
"""
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

if TYPE_CHECKING:
    from apps.projects.models import Project


class DataDictionaryGenerator:
    """
    Generates a data dictionary from a PostgreSQL schema.

    The dictionary includes:
    - Table names and row counts (approximate via pg_stat)
    - Column names, types, nullability, defaults
    - Column comments (from pg_description)
    - Primary keys, foreign keys, unique constraints
    - Sample values for non-sensitive columns (first 3 distinct values)
    - Enum types and their allowed values

    The generator respects project.allowed_tables and project.excluded_tables
    to control which tables are documented.
    """

    # SQL Queries used for introspection

    TABLES_QUERY = """
        SELECT
            t.table_name,
            COALESCE(obj_description(
                (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass
            ), '') as table_comment,
            s.n_live_tup as approximate_row_count
        FROM information_schema.tables t
        LEFT JOIN pg_stat_user_tables s
            ON s.schemaname = t.table_schema AND s.relname = t.table_name
        WHERE t.table_schema = %(schema)s
            AND t.table_type = 'BASE TABLE'
        ORDER BY t.table_name;
    """

    COLUMNS_QUERY = """
        SELECT
            c.table_name,
            c.column_name,
            c.data_type,
            c.udt_name,
            c.character_maximum_length,
            c.numeric_precision,
            c.numeric_scale,
            c.is_nullable,
            c.column_default,
            COALESCE(pgd.description, '') as column_comment,
            c.ordinal_position
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_statio_all_tables st
            ON st.schemaname = c.table_schema AND st.relname = c.table_name
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = st.relid AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = %(schema)s
        ORDER BY c.table_name, c.ordinal_position;
    """

    PRIMARY_KEYS_QUERY = """
        SELECT
            tc.table_name,
            kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
            AND tc.table_schema = %(schema)s
        ORDER BY tc.table_name, kcu.ordinal_position;
    """

    FOREIGN_KEYS_QUERY = """
        SELECT
            tc.table_name as from_table,
            kcu.column_name as from_column,
            ccu.table_name as to_table,
            ccu.column_name as to_column,
            tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
            AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = %(schema)s
        ORDER BY tc.table_name;
    """

    ENUM_QUERY = """
        SELECT
            t.typname as enum_name,
            array_agg(e.enumlabel ORDER BY e.enumsortorder) as enum_values
        FROM pg_type t
        JOIN pg_enum e ON t.oid = e.enumtypid
        JOIN pg_namespace n ON t.typnamespace = n.oid
        WHERE n.nspname = %(schema)s
        GROUP BY t.typname
        ORDER BY t.typname;
    """

    INDEXES_QUERY = """
        SELECT
            t.relname as table_name,
            i.relname as index_name,
            array_agg(a.attname ORDER BY array_position(ix.indkey, a.attnum)) as columns,
            ix.indisunique as is_unique
        FROM pg_class t
        JOIN pg_index ix ON t.oid = ix.indrelid
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
        WHERE n.nspname = %(schema)s
            AND NOT ix.indisprimary
        GROUP BY t.relname, i.relname, ix.indisunique
        ORDER BY t.relname, i.relname;
    """

    # Column name patterns that indicate sensitive data
    SENSITIVE_PATTERNS = [
        "password",
        "secret",
        "token",
        "ssn",
        "social_security",
        "credit_card",
        "card_number",
        "cvv",
        "pin",
        "api_key",
        "private_key",
        "auth",
    ]

    # Data types to skip for sample values
    SKIP_SAMPLE_TYPES = ["bytea", "json", "jsonb", "xml", "tsvector"]

    def __init__(self, project: "Project"):
        self.project = project
        self.schema = project.db_schema

    def _get_connection(self):
        """Get a database connection for the project."""
        return psycopg2.connect(**self.project.get_connection_params())

    def _get_visible_tables(self, all_tables: list[str]) -> list[str]:
        """Filter tables based on project allow/exclude lists."""
        if self.project.allowed_tables:
            tables = [t for t in all_tables if t in self.project.allowed_tables]
        else:
            tables = all_tables
        return [t for t in tables if t not in self.project.excluded_tables]

    def _is_sensitive_column(self, column_name: str) -> bool:
        """Check if a column name suggests sensitive data."""
        column_lower = column_name.lower()
        return any(pat in column_lower for pat in self.SENSITIVE_PATTERNS)

    def _get_sample_values(
        self, conn, table_name: str, column_name: str, data_type: str, limit: int = 3
    ) -> list[Any] | None:
        """
        Fetch sample distinct values for a column.
        Skip columns that look sensitive or have types that don't sample well.
        """
        if self._is_sensitive_column(column_name):
            return None

        if data_type in self.SKIP_SAMPLE_TYPES:
            return None

        try:
            with conn.cursor() as cur:
                # Use psycopg2.sql.Identifier for safe identifier quoting
                query = sql.SQL("""
                    SELECT DISTINCT {}::text
                    FROM {}.{}
                    WHERE {} IS NOT NULL
                    LIMIT %s
                """).format(
                    sql.Identifier(column_name),
                    sql.Identifier(self.schema),
                    sql.Identifier(table_name),
                    sql.Identifier(column_name),
                )
                cur.execute(query, (limit,))
                return [row[0] for row in cur.fetchall()]
        except Exception:
            return None

    def generate(self) -> dict:
        """
        Generate the full data dictionary and save it to the project.

        Returns a dict with structure:
        {
            "schema": "project_schema",
            "generated_at": "2025-01-01T00:00:00",
            "tables": {
                "table_name": {
                    "comment": "...",
                    "row_count": 1234,
                    "columns": [...],
                    "primary_key": ["id"],
                    "foreign_keys": [...],
                    "indexes": [...]
                }
            },
            "enums": {...}
        }
        """
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Fetch all metadata
                cur.execute(self.TABLES_QUERY, {"schema": self.schema})
                tables_raw = cur.fetchall()

                cur.execute(self.COLUMNS_QUERY, {"schema": self.schema})
                columns_raw = cur.fetchall()

                cur.execute(self.PRIMARY_KEYS_QUERY, {"schema": self.schema})
                pks_raw = cur.fetchall()

                cur.execute(self.FOREIGN_KEYS_QUERY, {"schema": self.schema})
                fks_raw = cur.fetchall()

                cur.execute(self.ENUM_QUERY, {"schema": self.schema})
                enums_raw = cur.fetchall()

                cur.execute(self.INDEXES_QUERY, {"schema": self.schema})
                indexes_raw = cur.fetchall()

            # Filter visible tables
            visible_table_names = self._get_visible_tables(
                [t["table_name"] for t in tables_raw]
            )

            # Build primary key lookup
            pk_lookup: dict[str, list[str]] = {}
            for pk in pks_raw:
                pk_lookup.setdefault(pk["table_name"], []).append(pk["column_name"])

            # Build foreign key lookup
            fk_lookup: dict[str, list[dict]] = {}
            for fk in fks_raw:
                fk_lookup.setdefault(fk["from_table"], []).append(
                    {
                        "column": fk["from_column"],
                        "references_table": fk["to_table"],
                        "references_column": fk["to_column"],
                    }
                )

            # Build index lookup
            idx_lookup: dict[str, list[dict]] = {}
            for idx in indexes_raw:
                idx_lookup.setdefault(idx["table_name"], []).append(
                    {
                        "name": idx["index_name"],
                        "columns": idx["columns"],
                        "unique": idx["is_unique"],
                    }
                )

            # Build tables dict
            tables: dict[str, dict] = {}
            for table in tables_raw:
                tname = table["table_name"]
                if tname not in visible_table_names:
                    continue

                table_pks = pk_lookup.get(tname, [])
                table_columns = []
                for col in columns_raw:
                    if col["table_name"] != tname:
                        continue

                    # Format type string
                    type_str = col["data_type"]
                    if col["character_maximum_length"]:
                        type_str = f"{col['udt_name']}({col['character_maximum_length']})"
                    elif col["numeric_precision"] and col["data_type"] == "numeric":
                        type_str = f"numeric({col['numeric_precision']},{col['numeric_scale']})"

                    samples = self._get_sample_values(
                        conn, tname, col["column_name"], col["udt_name"]
                    )

                    table_columns.append(
                        {
                            "name": col["column_name"],
                            "type": type_str,
                            "nullable": col["is_nullable"] == "YES",
                            "default": col["column_default"],
                            "comment": col["column_comment"],
                            "is_primary_key": col["column_name"] in table_pks,
                            "sample_values": samples,
                        }
                    )

                tables[tname] = {
                    "comment": table["table_comment"],
                    "row_count": table["approximate_row_count"] or 0,
                    "columns": table_columns,
                    "primary_key": table_pks,
                    "foreign_keys": fk_lookup.get(tname, []),
                    "indexes": idx_lookup.get(tname, []),
                }

            # Build enums dict
            enums = {e["enum_name"]: e["enum_values"] for e in enums_raw}

            dictionary = {
                "schema": self.schema,
                "generated_at": datetime.utcnow().isoformat(),
                "tables": tables,
                "enums": enums,
            }

            # Save to project
            self.project.data_dictionary = dictionary
            self.project.data_dictionary_generated_at = datetime.utcnow()
            self.project.save(
                update_fields=["data_dictionary", "data_dictionary_generated_at"]
            )

            return dictionary

        finally:
            conn.close()

    def render_for_prompt(self, max_tables_inline: int = 15) -> str:
        """
        Render the data dictionary as a text block suitable for a system prompt.

        For schemas with many tables, this uses a two-tier approach:
        - Table listing with descriptions always included
        - Full column detail only for tables up to max_tables_inline
        - For larger schemas, the agent should use a 'describe_table' tool

        Returns a formatted string.
        """
        dd = self.project.data_dictionary
        if not dd:
            return "No data dictionary available. Please generate one first."

        lines = []
        lines.append(f"## Database Schema: {dd['schema']}")
        lines.append(f"Generated: {dd['generated_at']}")
        lines.append("")

        tables = dd["tables"]

        if dd.get("enums"):
            lines.append("### Enum Types")
            for enum_name, values in dd["enums"].items():
                lines.append(f"- **{enum_name}**: {', '.join(values)}")
            lines.append("")

        if len(tables) <= max_tables_inline:
            # Full inline detail
            for tname, tinfo in tables.items():
                lines.append(f"### {tname}")
                if tinfo["comment"]:
                    lines.append(f"_{tinfo['comment']}_")
                lines.append(f"Approximate rows: {tinfo['row_count']:,}")
                lines.append("")
                lines.append(
                    "| Column | Type | Nullable | PK | Description | Sample Values |"
                )
                lines.append(
                    "|--------|------|----------|----|-------------|---------------|"
                )
                for col in tinfo["columns"]:
                    pk = "✓" if col["is_primary_key"] else ""
                    nullable = "✓" if col["nullable"] else ""
                    samples = (
                        ", ".join(col["sample_values"][:3])
                        if col.get("sample_values")
                        else ""
                    )
                    comment = col.get("comment", "")
                    lines.append(
                        f"| {col['name']} | {col['type']} | {nullable} | {pk} | {comment} | {samples} |"
                    )
                lines.append("")

                if tinfo["foreign_keys"]:
                    lines.append("**Relationships:**")
                    for fk in tinfo["foreign_keys"]:
                        lines.append(
                            f"- {fk['column']} → {fk['references_table']}.{fk['references_column']}"
                        )
                    lines.append("")
        else:
            # Table listing only — agent uses describe_table tool for detail
            lines.append(
                "### Tables (use `describe_table` tool for column details)"
            )
            lines.append("")
            for tname, tinfo in tables.items():
                comment = f" — {tinfo['comment']}" if tinfo["comment"] else ""
                col_count = len(tinfo["columns"])
                lines.append(
                    f"- **{tname}** ({tinfo['row_count']:,} rows, {col_count} columns){comment}"
                )
            lines.append("")

        return "\n".join(lines)
