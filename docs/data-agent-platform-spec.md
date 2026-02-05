# Data Agent Platform — Design Specification

## Overview

A self-hosted platform for deploying AI agents that can query project-specific PostgreSQL databases. Each project gets an isolated agent with its own system prompt, database access scope, and auto-generated data dictionary. Users are invited to projects and interact with agents through a web UI that supports rich artifact display (tables, charts, SQL).

## Architecture Summary

```
┌─────────────────────────────────────────────────────────┐
│                    Chainlit Frontend                      │
│  (Auth, Project Selection, Chat, Artifact Rendering)     │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│                  Django Backend (API)                     │
│  (Project CRUD, User Management, Agent Config, Auth)     │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│               LangGraph Agent Runtime                    │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Base Agent Graph                                │    │
│  │  - Memory (conversation history via checkpointer)│    │
│  │  - Tool routing                                  │    │
│  │  - Common system prompt                          │    │
│  │  - Error handling & retries                      │    │
│  └──────────┬──────────────────────────────────────┘    │
│             │                                            │
│  ┌──────────▼──────────────────────────────────────┐    │
│  │  Project Layer (injected at runtime)             │    │
│  │  - Project system prompt                         │    │
│  │  - Data dictionary context                       │    │
│  │  - Scoped SQL tool (read-only, schema-locked)    │    │
│  │  - Visualization tool                            │    │
│  └─────────────────────────────────────────────────┘    │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│          PostgreSQL (per-project isolation)               │
│  - Schema-based separation OR                            │
│  - Separate database per project                         │
│  - Read-only roles per project                           │
│  - Connection pooling via pgbouncer or Django pools      │
└─────────────────────────────────────────────────────────┘
```

---

## 1. Project Structure

```
data-agent-platform/
├── manage.py
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── .env.example
│
├── config/                        # Django project config
│   ├── settings/
│   │   ├── base.py
│   │   ├── development.py
│   │   └── production.py
│   ├── urls.py
│   └── wsgi.py
│
├── apps/
│   ├── projects/                  # Project & membership management
│   │   ├── models.py
│   │   ├── admin.py
│   │   ├── api/
│   │   │   ├── serializers.py
│   │   │   └── views.py
│   │   ├── services/
│   │   │   ├── data_dictionary.py  # Schema introspection
│   │   │   └── db_manager.py       # Connection management
│   │   └── migrations/
│   │
│   ├── agents/                    # Agent configuration & runtime
│   │   ├── models.py
│   │   ├── graph/
│   │   │   ├── base.py            # Base LangGraph agent
│   │   │   ├── nodes.py           # Agent graph nodes
│   │   │   └── state.py           # Agent state definition
│   │   ├── tools/
│   │   │   ├── sql_tool.py        # Scoped SQL execution
│   │   │   ├── visualization.py   # Chart generation
│   │   │   └── registry.py        # Tool registration per project
│   │   ├── prompts/
│   │   │   ├── base_system.py     # Common system prompt
│   │   │   └── templates.py       # Prompt assembly
│   │   └── memory/
│   │       └── checkpointer.py    # LangGraph memory backend
│   │
│   └── users/                     # Auth & user management
│       ├── models.py
│       ├── api/
│       └── auth.py
│
├── chainlit_app/                  # Chainlit frontend
│   ├── app.py                     # Main Chainlit entrypoint
│   ├── auth.py                    # Chainlit auth hooks
│   ├── handlers.py                # Message handlers
│   ├── artifacts.py               # Artifact rendering helpers
│   └── .chainlit/
│       └── config.toml
│
├── scripts/
│   ├── generate_data_dictionary.py
│   └── setup_project_db.py
│
└── tests/
    ├── test_sql_tool.py
    ├── test_data_dictionary.py
    ├── test_agent.py
    └── test_auth.py
```

---

## 2. Django Models

### `apps/projects/models.py`

```python
import uuid
from django.db import models
from django.conf import settings
from cryptography.fernet import Fernet


class Project(models.Model):
    """
    Represents a data project with its own database scope and agent configuration.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)

    # Database connection (encrypted at rest)
    db_host = models.CharField(max_length=255)
    db_port = models.IntegerField(default=5432)
    db_name = models.CharField(max_length=255)
    db_schema = models.CharField(max_length=255, default="public")
    # Encrypted fields - store the encrypted connection credentials
    _db_user = models.BinaryField(db_column="db_user")
    _db_password = models.BinaryField(db_column="db_password")

    # Optional: restrict which tables the agent can see
    # Empty list = all tables in schema are visible
    allowed_tables = models.JSONField(default=list, blank=True)
    # Tables to explicitly exclude (useful when allowed_tables is empty/all)
    excluded_tables = models.JSONField(default=list, blank=True)

    # Agent configuration
    system_prompt = models.TextField(
        blank=True,
        help_text="Project-specific system prompt. Merged with the base agent prompt."
    )
    max_rows_per_query = models.IntegerField(
        default=500,
        help_text="Maximum rows the SQL tool will return per query."
    )
    max_query_timeout_seconds = models.IntegerField(
        default=30,
        help_text="Query execution timeout."
    )
    llm_model = models.CharField(
        max_length=100,
        default="claude-sonnet-4-5-20250929",
        help_text="LLM model identifier for the agent."
    )

    # Data dictionary (cached, regenerated on demand)
    data_dictionary = models.JSONField(
        null=True, blank=True,
        help_text="Auto-generated schema documentation. Regenerated via management command."
    )
    data_dictionary_generated_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="created_projects"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def db_user(self):
        f = Fernet(settings.DB_CREDENTIAL_KEY)
        return f.decrypt(bytes(self._db_user)).decode()

    @db_user.setter
    def db_user(self, value):
        f = Fernet(settings.DB_CREDENTIAL_KEY)
        self._db_user = f.encrypt(value.encode())

    @property
    def db_password(self):
        f = Fernet(settings.DB_CREDENTIAL_KEY)
        return f.decrypt(bytes(self._db_password)).decode()

    @db_password.setter
    def db_password(self, value):
        f = Fernet(settings.DB_CREDENTIAL_KEY)
        self._db_password = f.encrypt(value.encode())

    def get_connection_params(self) -> dict:
        """Return connection params for psycopg2/SQLAlchemy."""
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
            "options": f"-c search_path={self.db_schema},public -c statement_timeout={self.max_query_timeout_seconds * 1000}",
        }


class ProjectRole(models.TextChoices):
    VIEWER = "viewer", "Viewer"        # Can chat with agent, view results
    ANALYST = "analyst", "Analyst"      # Can chat, export data, create saved queries
    ADMIN = "admin", "Admin"            # Full project config access


class ProjectMembership(models.Model):
    """
    Links users to projects with role-based access.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="project_memberships"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE,
        related_name="memberships"
    )
    role = models.CharField(max_length=20, choices=ProjectRole.choices, default=ProjectRole.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "project"]

    def __str__(self):
        return f"{self.user} - {self.project} ({self.role})"


class SavedQuery(models.Model):
    """
    Queries that users can save and re-run.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="saved_queries")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    sql = models.TextField()
    is_shared = models.BooleanField(default=False, help_text="Visible to all project members")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class ConversationLog(models.Model):
    """
    Stores conversation history for audit and memory.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="conversations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    thread_id = models.CharField(max_length=255, db_index=True)
    messages = models.JSONField(default=list)
    # Track which queries were executed in this conversation
    queries_executed = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["project", "user", "-created_at"]),
        ]
```

---

## 3. Data Dictionary Generator

### `apps/projects/services/data_dictionary.py`

This service introspects a project's database schema and generates a structured data dictionary that gets injected into the agent's system prompt.

```python
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
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from typing import Optional


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

    def __init__(self, project):
        self.project = project
        self.schema = project.db_schema

    def _get_connection(self):
        return psycopg2.connect(**self.project.get_connection_params())

    def _get_visible_tables(self, all_tables: list[str]) -> list[str]:
        """Filter tables based on project allow/exclude lists."""
        if self.project.allowed_tables:
            tables = [t for t in all_tables if t in self.project.allowed_tables]
        else:
            tables = all_tables
        return [t for t in tables if t not in self.project.excluded_tables]

    def _get_sample_values(self, conn, table_name: str, column_name: str,
                           data_type: str, limit: int = 3) -> Optional[list]:
        """
        Fetch sample distinct values for a column.
        Skip columns that look sensitive (password, secret, token, ssn, etc.).
        """
        sensitive_patterns = ['password', 'secret', 'token', 'ssn', 'social_security',
                              'credit_card', 'card_number', 'cvv', 'pin', 'api_key',
                              'private_key', 'auth']
        if any(pat in column_name.lower() for pat in sensitive_patterns):
            return None

        # Skip binary/json types for sample values
        skip_types = ['bytea', 'json', 'jsonb', 'xml', 'tsvector']
        if data_type in skip_types:
            return None

        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT {column_name}::text FROM {self.schema}.{table_name} "
                    f"WHERE {column_name} IS NOT NULL LIMIT {limit}"
                )
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
                    "columns": [
                        {
                            "name": "col_name",
                            "type": "varchar(255)",
                            "nullable": true,
                            "default": null,
                            "comment": "...",
                            "is_primary_key": false,
                            "sample_values": ["a", "b", "c"]
                        }
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {"column": "user_id", "references_table": "users", "references_column": "id"}
                    ],
                    "indexes": [
                        {"name": "idx_users_email", "columns": ["email"], "unique": true}
                    ]
                }
            },
            "enums": {
                "status_type": ["active", "inactive", "pending"]
            },
            "relationships_summary": "..."
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
            pk_lookup = {}
            for pk in pks_raw:
                pk_lookup.setdefault(pk["table_name"], []).append(pk["column_name"])

            # Build foreign key lookup
            fk_lookup = {}
            for fk in fks_raw:
                fk_lookup.setdefault(fk["from_table"], []).append({
                    "column": fk["from_column"],
                    "references_table": fk["to_table"],
                    "references_column": fk["to_column"],
                })

            # Build index lookup
            idx_lookup = {}
            for idx in indexes_raw:
                idx_lookup.setdefault(idx["table_name"], []).append({
                    "name": idx["index_name"],
                    "columns": idx["columns"],
                    "unique": idx["is_unique"],
                })

            # Build tables dict
            tables = {}
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

                    table_columns.append({
                        "name": col["column_name"],
                        "type": type_str,
                        "nullable": col["is_nullable"] == "YES",
                        "default": col["column_default"],
                        "comment": col["column_comment"],
                        "is_primary_key": col["column_name"] in table_pks,
                        "sample_values": samples,
                    })

                tables[tname] = {
                    "comment": table["table_comment"],
                    "row_count": table["approximate_row_count"],
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
            self.project.save(update_fields=["data_dictionary", "data_dictionary_generated_at"])

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
                lines.append("| Column | Type | Nullable | PK | Description | Sample Values |")
                lines.append("|--------|------|----------|----|-------------|---------------|")
                for col in tinfo["columns"]:
                    pk = "✓" if col["is_primary_key"] else ""
                    nullable = "✓" if col["nullable"] else ""
                    samples = ", ".join(col["sample_values"][:3]) if col.get("sample_values") else ""
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
            lines.append("### Tables (use `describe_table` tool for column details)")
            lines.append("")
            for tname, tinfo in tables.items():
                comment = f" — {tinfo['comment']}" if tinfo["comment"] else ""
                col_count = len(tinfo["columns"])
                lines.append(
                    f"- **{tname}** ({tinfo['row_count']:,} rows, {col_count} columns){comment}"
                )
            lines.append("")

        return "\n".join(lines)
```

---

## 4. SQL Tool with Validation

### `apps/agents/tools/sql_tool.py`

```python
"""
Scoped SQL execution tool for LangGraph agents.

This tool:
1. Validates that the query is read-only (SELECT/WITH only)
2. Validates that only allowed tables/schemas are referenced
3. Executes against the project's database with a read-only role
4. Enforces row limits and query timeouts
5. Returns results as structured data (list of dicts)

Security layers:
- SQL parsing via sqlglot to reject non-SELECT statements
- Schema/table allowlist enforcement
- PostgreSQL read-only role (defense in depth)
- Statement timeout set at connection level
- Row limit via LIMIT injection if not present

Dependencies:
    pip install sqlglot psycopg2-binary

Usage in LangGraph:
    sql_tool = create_sql_tool(project)
    # Returns a LangChain-compatible tool
"""
import sqlglot
from sqlglot import exp
from langchain_core.tools import tool
from typing import Any
import psycopg2
from psycopg2.extras import RealDictCursor


class SQLValidationError(Exception):
    """Raised when SQL fails validation checks."""
    pass


class SQLValidator:
    """
    Validates SQL queries against project-specific rules.

    Rejects:
    - Any non-SELECT statement (INSERT, UPDATE, DELETE, DROP, ALTER, etc.)
    - References to schemas/tables outside the project's scope
    - Multiple statements (injection prevention)
    - COPY, EXECUTE, SET, and other dangerous commands
    """

    BLOCKED_STATEMENT_TYPES = {
        exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
        exp.AlterTable, exp.TruncateTable, exp.Command,
    }

    def __init__(self, allowed_schema: str, allowed_tables: list[str] = None,
                 excluded_tables: list[str] = None):
        self.allowed_schema = allowed_schema
        self.allowed_tables = set(allowed_tables) if allowed_tables else None
        self.excluded_tables = set(excluded_tables) if excluded_tables else set()

    def validate(self, sql: str) -> str:
        """
        Validate and return the cleaned SQL.
        Raises SQLValidationError if validation fails.
        """
        # Check for multiple statements
        statements = sqlglot.parse(sql, dialect="postgres")
        if len(statements) != 1:
            raise SQLValidationError(
                "Only single SELECT statements are allowed. "
                "Multiple statements detected."
            )

        statement = statements[0]

        # Check statement type
        if not isinstance(statement, (exp.Select, exp.Union)):
            # Allow WITH (CTE) that results in SELECT
            if isinstance(statement, exp.With):
                # The final expression of a CTE should be a SELECT
                pass
            else:
                raise SQLValidationError(
                    f"Only SELECT queries are allowed. "
                    f"Got: {type(statement).__name__}"
                )

        # Check for blocked expressions anywhere in the tree
        for node in statement.walk():
            if type(node) in self.BLOCKED_STATEMENT_TYPES:
                raise SQLValidationError(
                    f"Statement contains blocked operation: {type(node).__name__}"
                )

            # Check for function calls that could be dangerous
            if isinstance(node, exp.Anonymous):
                func_name = node.name.lower()
                blocked_funcs = [
                    'pg_read_file', 'pg_read_binary_file', 'pg_ls_dir',
                    'lo_import', 'lo_export', 'dblink', 'dblink_exec',
                    'copy', 'pg_execute_server_program'
                ]
                if func_name in blocked_funcs:
                    raise SQLValidationError(
                        f"Function '{func_name}' is not allowed."
                    )

        # Check table references
        for table in statement.find_all(exp.Table):
            table_name = table.name
            schema_name = table.db if table.db else self.allowed_schema

            if schema_name != self.allowed_schema:
                raise SQLValidationError(
                    f"Access to schema '{schema_name}' is not allowed. "
                    f"Only '{self.allowed_schema}' is accessible."
                )

            if table_name in self.excluded_tables:
                raise SQLValidationError(
                    f"Table '{table_name}' is not accessible in this project."
                )

            if self.allowed_tables and table_name not in self.allowed_tables:
                raise SQLValidationError(
                    f"Table '{table_name}' is not accessible in this project. "
                    f"Available tables: {', '.join(sorted(self.allowed_tables))}"
                )

        return sql

    def inject_limit(self, sql: str, max_rows: int) -> str:
        """
        If the query doesn't have a LIMIT clause, add one.
        If it has a LIMIT higher than max_rows, cap it.
        """
        statements = sqlglot.parse(sql, dialect="postgres")
        statement = statements[0]

        existing_limit = statement.find(exp.Limit)
        if existing_limit:
            # Check if existing limit exceeds max
            limit_val = existing_limit.expression
            if isinstance(limit_val, exp.Literal) and limit_val.is_int:
                if int(limit_val.this) > max_rows:
                    existing_limit.set(
                        "expression",
                        exp.Literal.number(max_rows)
                    )
        else:
            statement = statement.limit(max_rows)

        return statement.sql(dialect="postgres")


def create_sql_tool(project) -> callable:
    """
    Factory function that creates a project-scoped SQL tool.

    Returns a LangChain-compatible tool function that can be used
    in a LangGraph agent.
    """
    validator = SQLValidator(
        allowed_schema=project.db_schema,
        allowed_tables=project.allowed_tables or None,
        excluded_tables=project.excluded_tables or [],
    )

    @tool
    def execute_sql(query: str) -> dict[str, Any]:
        """Execute a read-only SQL query against the project database.

        Args:
            query: A SELECT SQL query. Only SELECT statements are allowed.
                   The query will be validated before execution.

        Returns:
            A dict with:
            - "columns": list of column names
            - "rows": list of dicts (column_name: value)
            - "row_count": number of rows returned
            - "truncated": whether results were truncated by row limit
        """
        # Validate
        try:
            validated_sql = validator.validate(query)
            validated_sql = validator.inject_limit(validated_sql, project.max_rows_per_query)
        except SQLValidationError as e:
            return {"error": str(e)}

        # Execute
        try:
            conn = psycopg2.connect(**project.get_connection_params())
            conn.set_session(readonly=True, autocommit=True)
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(validated_sql)
                    rows = cur.fetchall()
                    columns = [desc.name for desc in cur.description] if cur.description else []

                    return {
                        "columns": columns,
                        "rows": [dict(row) for row in rows],
                        "row_count": len(rows),
                        "truncated": len(rows) >= project.max_rows_per_query,
                        "sql_executed": validated_sql,
                    }
            finally:
                conn.close()

        except psycopg2.errors.QueryCanceled:
            return {"error": f"Query timed out (limit: {project.max_query_timeout_seconds}s). Try a more specific query."}
        except psycopg2.Error as e:
            return {"error": f"Database error: {str(e)}"}

    return execute_sql
```

---

## 5. LangGraph Agent

### `apps/agents/graph/state.py`

```python
"""
Agent state definition.

The state flows through the LangGraph nodes and accumulates
messages, tool results, and metadata.
"""
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """State that flows through the agent graph."""
    messages: Annotated[list[BaseMessage], add_messages]
    # Project context injected at runtime
    project_id: str
    project_name: str
    user_id: str
    user_role: str
```

### `apps/agents/graph/base.py`

```python
"""
Base LangGraph agent definition.

Defines the core agent graph structure:

    START → agent → should_continue? → tools → agent → ...
                  └→ END

The agent node calls the LLM with the current messages and available tools.
The should_continue edge checks if the LLM wants to call a tool or end.
The tools node executes any tool calls and returns results.

Configuration is injected at runtime via LangGraph's `configurable` dict,
which carries project-specific settings (prompt, tools, DB config).
"""
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.postgres import PostgresSaver
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage

from apps.agents.graph.state import AgentState
from apps.agents.tools.sql_tool import create_sql_tool
from apps.agents.tools.visualization import create_viz_tool
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from apps.projects.services.data_dictionary import DataDictionaryGenerator


def build_agent_graph(project, checkpointer=None):
    """
    Build a LangGraph agent for a specific project.

    Args:
        project: Project model instance
        checkpointer: LangGraph checkpointer for conversation persistence.
                      If None, conversations won't persist between sessions.

    Returns:
        A compiled LangGraph that can be invoked with:
            graph.invoke(
                {"messages": [HumanMessage(content="...")]},
                config={"configurable": {"thread_id": "..."}}
            )
    """
    # --- Build tools ---
    sql_tool = create_sql_tool(project)
    viz_tool = create_viz_tool()

    tools = [sql_tool, viz_tool]

    # If schema is large, add a describe_table tool
    if (project.data_dictionary and
            len(project.data_dictionary.get("tables", {})) > 15):
        tools.append(create_describe_table_tool(project))

    tool_node = ToolNode(tools)

    # --- Build LLM ---
    llm = ChatAnthropic(
        model=project.llm_model,
        max_tokens=4096,
        temperature=0,
    ).bind_tools(tools)

    # --- Build system prompt ---
    dd_generator = DataDictionaryGenerator(project)
    data_dict_text = dd_generator.render_for_prompt()

    system_prompt = f"""{BASE_SYSTEM_PROMPT}

## Project Context
{project.system_prompt}

## Available Database Schema
{data_dict_text}

## Query Guidelines
- Always use the execute_sql tool to run queries. Never fabricate data.
- Maximum {project.max_rows_per_query} rows per query result.
- Query timeout: {project.max_query_timeout_seconds} seconds.
- When results are truncated, suggest more specific WHERE clauses or aggregations.
- For large result sets, prefer aggregations (COUNT, SUM, AVG, GROUP BY) first,
  then drill into details.
- Always explain what the query does before executing it.
- If a query fails, explain the error and suggest a fix.
"""

    # --- Define graph nodes ---
    def agent_node(state: AgentState):
        messages = [SystemMessage(content=system_prompt)] + state["messages"]
        response = llm.invoke(messages)
        return {"messages": [response]}

    def should_continue(state: AgentState):
        last_message = state["messages"][-1]
        if last_message.tool_calls:
            return "tools"
        return END

    # --- Build graph ---
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)


def create_describe_table_tool(project):
    """
    For large schemas, provide a tool that returns detailed column info
    for a specific table. This keeps the system prompt small while still
    giving the agent access to full schema details on demand.
    """
    from langchain_core.tools import tool

    @tool
    def describe_table(table_name: str) -> str:
        """Get detailed column information for a specific table.

        Use this when you need to know the exact columns, types, and
        relationships for a table before writing a query.

        Args:
            table_name: Name of the table to describe.
        """
        dd = project.data_dictionary
        if not dd or table_name not in dd.get("tables", {}):
            available = list(dd["tables"].keys()) if dd else []
            return f"Table '{table_name}' not found. Available: {', '.join(available)}"

        tinfo = dd["tables"][table_name]
        lines = [f"## {table_name}"]
        if tinfo["comment"]:
            lines.append(f"Description: {tinfo['comment']}")
        lines.append(f"Approximate rows: {tinfo['row_count']:,}")
        lines.append("")
        lines.append("| Column | Type | Nullable | PK | Description |")
        lines.append("|--------|------|----------|----|-------------|")
        for col in tinfo["columns"]:
            pk = "✓" if col["is_primary_key"] else ""
            nullable = "✓" if col["nullable"] else ""
            lines.append(
                f"| {col['name']} | {col['type']} | {nullable} | {pk} | {col.get('comment', '')} |"
            )

        if tinfo["foreign_keys"]:
            lines.append("")
            lines.append("Relationships:")
            for fk in tinfo["foreign_keys"]:
                lines.append(f"- {fk['column']} → {fk['references_table']}.{fk['references_column']}")

        return "\n".join(lines)

    return describe_table
```

### `apps/agents/prompts/base_system.py`

```python
"""
Base system prompt shared across all project agents.

This prompt defines the agent's core behavior, tool usage patterns,
and response formatting. Project-specific context and the data dictionary
are appended to this prompt at runtime.
"""

BASE_SYSTEM_PROMPT = """You are a data analyst assistant with access to a PostgreSQL database.
Your role is to help users explore, understand, and analyze data by writing and executing SQL queries.

## Core Behavior
- Be precise and data-driven. Always back claims with query results.
- Explain your reasoning and the queries you write in plain language.
- When you're uncertain about the data structure, use the available schema
  information or describe_table tool before guessing.
- If a query returns unexpected results, investigate rather than speculate.

## Response Format
- For tabular data: present results as markdown tables when there are <= 20 rows.
  For larger results, summarize key findings and offer to show details.
- For numeric analysis: offer to create visualizations when appropriate.
- Always show the SQL you executed so users can learn and verify.
- Use clear section headers when providing multi-part analysis.

## Error Handling
- If a query fails, explain the error in plain language and suggest a corrected query.
- If the user asks about data that doesn't exist in the schema, say so clearly
  and suggest what related data is available.
- Never fabricate data or results. If you don't know, say so.

## Security
- You can only execute SELECT queries. You cannot modify data.
- You can only access tables within your project's schema.
- Do not attempt to access system tables or other schemas.
"""
```

---

## 6. Visualization Tool

### `apps/agents/tools/visualization.py`

```python
"""
Visualization tool for generating charts from query results.

Uses matplotlib/plotly to generate charts and returns them as
base64-encoded images or Plotly JSON specs that Chainlit can render.

The agent decides when a visualization would be helpful and calls
this tool with the data and chart configuration.

Dependencies:
    pip install matplotlib plotly pandas
"""
import json
import base64
import io
from langchain_core.tools import tool


@tool
def create_visualization(
    data: list[dict],
    chart_type: str,
    x_column: str,
    y_column: str,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    color_column: str = None,
    group_column: str = None,
) -> dict:
    """Create a chart visualization from data.

    Args:
        data: List of row dicts (e.g. from execute_sql results).
        chart_type: One of: bar, line, scatter, pie, histogram, heatmap.
        x_column: Column name for x-axis.
        y_column: Column name for y-axis.
        title: Chart title.
        x_label: X-axis label (defaults to column name).
        y_label: Y-axis label (defaults to column name).
        color_column: Optional column for color grouping.
        group_column: Optional column for series grouping.

    Returns:
        A dict with:
        - "plotly_json": Plotly figure spec as JSON string (for interactive rendering)
        - "image_base64": PNG fallback as base64 string
    """
    import pandas as pd
    import plotly.express as px
    import plotly.io as pio

    df = pd.DataFrame(data)

    chart_builders = {
        "bar": lambda: px.bar(df, x=x_column, y=y_column, color=color_column,
                               title=title, labels={x_column: x_label or x_column,
                                                     y_column: y_label or y_column}),
        "line": lambda: px.line(df, x=x_column, y=y_column, color=color_column,
                                 title=title),
        "scatter": lambda: px.scatter(df, x=x_column, y=y_column, color=color_column,
                                       title=title),
        "pie": lambda: px.pie(df, names=x_column, values=y_column, title=title),
        "histogram": lambda: px.histogram(df, x=x_column, title=title),
        "heatmap": lambda: px.density_heatmap(df, x=x_column, y=y_column, title=title),
    }

    if chart_type not in chart_builders:
        return {"error": f"Unknown chart type '{chart_type}'. "
                f"Supported: {', '.join(chart_builders.keys())}"}

    try:
        fig = chart_builders[chart_type]()
        fig.update_layout(template="plotly_white")

        # Generate both formats
        plotly_json = pio.to_json(fig)

        # PNG fallback
        img_bytes = pio.to_image(fig, format="png", width=800, height=500)
        image_base64 = base64.b64encode(img_bytes).decode()

        return {
            "plotly_json": plotly_json,
            "image_base64": image_base64,
        }
    except Exception as e:
        return {"error": f"Visualization error: {str(e)}"}
```

---

## 7. Chainlit Frontend

### `chainlit_app/app.py`

```python
"""
Chainlit application entry point.

Handles:
- OAuth authentication (delegates to Django backend for user lookup)
- Project selection on session start
- Message routing to the LangGraph agent
- Artifact rendering (tables, charts, SQL)

Run with:
    chainlit run chainlit_app/app.py --port 8501

Dependencies:
    pip install chainlit langchain-anthropic langgraph
"""
import chainlit as cl
from chainlit.input_widget import Select
import json
import os

# Import Django setup
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
import django
django.setup()

from apps.projects.models import Project, ProjectMembership
from apps.agents.graph.base import build_agent_graph
from langchain_core.messages import HumanMessage


# --- Authentication ---
# Configure in .chainlit/config.toml:
# [auth]
# provider = "header"  # or "oauth" for Google/GitHub/etc.

@cl.oauth_callback
def oauth_callback(provider_id, token, raw_user_data, default_user):
    """
    Handle OAuth callback. Look up or create user in Django.
    Returns a cl.User if authentication succeeds.
    """
    from apps.users.auth import get_or_create_user_from_oauth
    user = get_or_create_user_from_oauth(provider_id, raw_user_data)
    if user:
        return cl.User(
            identifier=user.email,
            metadata={"user_id": str(user.id), "email": user.email}
        )
    return None


@cl.on_chat_start
async def on_chat_start():
    """
    Called when a new chat session starts.
    Shows project selection and initializes the agent.
    """
    user = cl.user_session.get("user")
    user_id = user.metadata["user_id"]

    # Get user's projects
    memberships = ProjectMembership.objects.filter(
        user_id=user_id
    ).select_related("project")

    if not memberships.exists():
        await cl.Message(content="You don't have access to any projects. "
                         "Contact an admin to get added.").send()
        return

    projects = {str(m.project.id): m for m in memberships}

    # Project selection
    settings = await cl.ChatSettings(
        [
            Select(
                id="project",
                label="Project",
                values=[
                    cl.input_widget.SelectOption(
                        value=str(m.project.id),
                        label=f"{m.project.name} ({m.role})"
                    )
                    for m in memberships
                ],
                initial_value=str(memberships.first().project.id),
            )
        ]
    ).send()

    # Initialize with first project
    await setup_agent(settings["project"], projects)


async def setup_agent(project_id: str, memberships: dict):
    """Initialize the LangGraph agent for the selected project."""
    membership = memberships[project_id]
    project = membership.project

    # Build agent graph
    # In production, use PostgresSaver for persistent memory:
    # from langgraph.checkpoint.postgres import PostgresSaver
    # checkpointer = PostgresSaver.from_conn_string(settings.DATABASE_URL)
    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()

    graph = build_agent_graph(project, checkpointer=checkpointer)

    # Store in session
    cl.user_session.set("graph", graph)
    cl.user_session.set("project", project)
    cl.user_session.set("user_role", membership.role)
    cl.user_session.set("thread_id", cl.context.session.id)

    await cl.Message(
        content=f"Connected to **{project.name}**. "
        f"I have access to the database schema and can help you explore the data. "
        f"What would you like to know?"
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    """
    Handle incoming user messages.
    Routes to the LangGraph agent and renders responses.
    """
    graph = cl.user_session.get("graph")
    if not graph:
        await cl.Message(content="Please select a project first.").send()
        return

    thread_id = cl.user_session.get("thread_id")

    # Stream the agent response
    response_msg = cl.Message(content="")
    await response_msg.send()

    config = {"configurable": {"thread_id": thread_id}}

    # Invoke the graph
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=message.content)]},
        config=config,
    )

    # Process the final response
    final_message = result["messages"][-1]

    # Check for tool results that need special rendering
    for msg in result["messages"]:
        if hasattr(msg, "content") and isinstance(msg.content, str):
            try:
                tool_result = json.loads(msg.content)
                await render_artifact(tool_result)
            except (json.JSONDecodeError, TypeError):
                pass

    # Send the final text response
    response_msg.content = final_message.content
    await response_msg.update()


async def render_artifact(data: dict):
    """
    Render tool results as rich artifacts in the Chainlit UI.
    """
    # SQL results as table
    if "columns" in data and "rows" in data:
        if data["rows"]:
            # Render as markdown table
            cols = data["columns"]
            header = "| " + " | ".join(cols) + " |"
            separator = "| " + " | ".join(["---"] * len(cols)) + " |"
            rows = []
            for row in data["rows"][:50]:  # Cap display at 50 rows
                row_str = "| " + " | ".join(str(row.get(c, "")) for c in cols) + " |"
                rows.append(row_str)

            table_md = "\n".join([header, separator] + rows)
            if data.get("truncated"):
                table_md += f"\n\n*Results truncated. Showing {len(data['rows'])} rows.*"

            elements = [cl.Text(name="Query Results", content=table_md, display="inline")]
            await cl.Message(content="", elements=elements).send()

    # Plotly chart
    if "plotly_json" in data:
        fig_json = json.loads(data["plotly_json"])
        elements = [cl.Plotly(name="Chart", figure=fig_json, display="inline")]
        await cl.Message(content="", elements=elements).send()

    # Error
    if "error" in data and len(data) == 1:
        await cl.Message(content=f"⚠️ {data['error']}").send()
```

---

## 8. Database Setup & Security

### PostgreSQL Role Setup Script

Create a management command or setup script that configures the read-only database role for each project:

### `scripts/setup_project_db.py`

```python
"""
Setup script for project database access.

Creates a read-only PostgreSQL role for agent access to a project's schema.
This ensures defense-in-depth: even if the SQL validator is bypassed,
the database role prevents writes.

Usage:
    python scripts/setup_project_db.py \
        --host localhost \
        --port 5432 \
        --admin-db postgres \
        --admin-user postgres \
        --project-db myproject \
        --project-schema analytics \
        --agent-role agent_readonly_analytics

This script requires a PostgreSQL superuser/admin connection to create roles.
"""
import psycopg2
import argparse


def setup_readonly_role(admin_conn_params: dict, project_db: str,
                        schema: str, role_name: str, role_password: str):
    """
    Create a read-only role scoped to a specific schema.

    The role:
    - Cannot create, modify, or delete any data
    - Can only access the specified schema
    - Has SELECT on all current and future tables in the schema
    - Has a connection limit to prevent resource exhaustion
    """
    conn = psycopg2.connect(**admin_conn_params)
    conn.autocommit = True

    try:
        with conn.cursor() as cur:
            # Create role if not exists
            cur.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role_name}') THEN
                        CREATE ROLE {role_name} WITH
                            LOGIN
                            PASSWORD '{role_password}'
                            NOSUPERUSER
                            NOCREATEDB
                            NOCREATEROLE
                            CONNECTION LIMIT 10;
                    END IF;
                END
                $$;
            """)

            # Grant connect to the database
            cur.execute(f"GRANT CONNECT ON DATABASE {project_db} TO {role_name};")

            # Grant usage on schema
            cur.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role_name};")

            # Grant SELECT on all existing tables
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role_name};")

            # Grant SELECT on future tables (default privileges)
            cur.execute(f"""
                ALTER DEFAULT PRIVILEGES IN SCHEMA {schema}
                GRANT SELECT ON TABLES TO {role_name};
            """)

            # Grant usage on sequences (needed for some queries)
            cur.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO {role_name};")

            # Revoke everything from public schema (defense in depth)
            cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_name};")

            print(f"✓ Role '{role_name}' configured for schema '{schema}' "
                  f"on database '{project_db}'")

    finally:
        conn.close()
```

---

## 9. Django Management Commands

### `apps/projects/management/commands/generate_data_dictionary.py`

```python
"""
Management command to generate/refresh data dictionaries.

Usage:
    # Generate for a specific project
    python manage.py generate_data_dictionary --project-slug my-project

    # Generate for all projects
    python manage.py generate_data_dictionary --all

    # Dry run (print to stdout, don't save)
    python manage.py generate_data_dictionary --project-slug my-project --dry-run
"""
from django.core.management.base import BaseCommand
from apps.projects.models import Project
from apps.projects.services.data_dictionary import DataDictionaryGenerator


class Command(BaseCommand):
    help = "Generate data dictionary from project database schema"

    def add_arguments(self, parser):
        parser.add_argument("--project-slug", type=str, help="Project slug to generate for")
        parser.add_argument("--all", action="store_true", help="Generate for all projects")
        parser.add_argument("--dry-run", action="store_true", help="Print to stdout, don't save")

    def handle(self, *args, **options):
        if options["all"]:
            projects = Project.objects.all()
        elif options["project_slug"]:
            projects = Project.objects.filter(slug=options["project_slug"])
        else:
            self.stderr.write("Specify --project-slug or --all")
            return

        for project in projects:
            self.stdout.write(f"Generating data dictionary for: {project.name}")
            generator = DataDictionaryGenerator(project)

            if options["dry_run"]:
                dictionary = generator.generate()
                self.stdout.write(generator.render_for_prompt())
            else:
                generator.generate()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ Generated: {len(project.data_dictionary['tables'])} tables"
                    )
                )
```

---

## 10. Docker Compose Setup

### `docker-compose.yml`

```yaml
version: "3.9"

services:
  # Platform database (Django models, user data, project config)
  platform-db:
    image: postgres:16
    environment:
      POSTGRES_DB: agent_platform
      POSTGRES_USER: platform
      POSTGRES_PASSWORD: ${PLATFORM_DB_PASSWORD}
    volumes:
      - platform_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  # Django API server
  api:
    build: .
    command: >
      sh -c "python manage.py migrate &&
             python manage.py runserver 0.0.0.0:8000"
    environment:
      - DATABASE_URL=postgresql://platform:${PLATFORM_DB_PASSWORD}@platform-db:5432/agent_platform
      - DJANGO_SETTINGS_MODULE=config.settings.development
      - DB_CREDENTIAL_KEY=${DB_CREDENTIAL_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    volumes:
      - .:/app
    ports:
      - "8000:8000"
    depends_on:
      - platform-db

  # Chainlit frontend
  chainlit:
    build: .
    command: chainlit run chainlit_app/app.py --host 0.0.0.0 --port 8501
    environment:
      - DATABASE_URL=postgresql://platform:${PLATFORM_DB_PASSWORD}@platform-db:5432/agent_platform
      - DJANGO_SETTINGS_MODULE=config.settings.development
      - DB_CREDENTIAL_KEY=${DB_CREDENTIAL_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - CHAINLIT_AUTH_SECRET=${CHAINLIT_AUTH_SECRET}
    volumes:
      - .:/app
    ports:
      - "8501:8501"
    depends_on:
      - api
      - platform-db

volumes:
  platform_data:
```

### `.env.example`

```bash
# Platform database
PLATFORM_DB_PASSWORD=change-me-platform-password

# Encryption key for project DB credentials (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
DB_CREDENTIAL_KEY=

# LLM provider
ANTHROPIC_API_KEY=

# Chainlit
CHAINLIT_AUTH_SECRET=change-me-random-secret
```

---

## 11. Dependencies

### `pyproject.toml` (key dependencies)

```toml
[project]
name = "data-agent-platform"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    # Django
    "django>=5.0",
    "djangorestframework>=3.15",
    "django-environ>=0.11",
    "psycopg2-binary>=2.9",

    # Encryption
    "cryptography>=42.0",

    # LangGraph / LangChain
    "langgraph>=0.2",
    "langchain-anthropic>=0.2",
    "langchain-core>=0.3",
    "langgraph-checkpoint-postgres>=2.0",

    # SQL validation
    "sqlglot>=25.0",

    # Visualization
    "plotly>=5.0",
    "pandas>=2.0",
    "matplotlib>=3.8",
    "kaleido>=0.2",  # for plotly image export

    # Frontend
    "chainlit>=1.3",

    # Utilities
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-django>=4.8",
    "pytest-asyncio>=0.23",
    "factory-boy>=3.3",
    "ruff>=0.5",
]
```

---

## 12. Implementation Order

Build in this sequence. Each phase produces a working, testable increment.

### Phase 1: Foundation (Week 1)
1. Django project scaffold with settings, User model, and Project/Membership models
2. Project admin interface (Django admin is fine for MVP)
3. Database credential encryption
4. Data dictionary generator service
5. `generate_data_dictionary` management command
6. Tests for data dictionary generation against a test PostgreSQL schema

### Phase 2: Agent Core (Week 2)
7. SQL validator with sqlglot (write comprehensive tests first)
8. SQL tool with connection management, validation, and execution
9. Base LangGraph agent graph with system prompt assembly
10. In-memory checkpointer for conversation history
11. Tests: SQL validation (injection attempts, blocked statements, schema enforcement)
12. Tests: Agent end-to-end with a test database

### Phase 3: Frontend (Week 3)
13. Chainlit app with basic auth (start with header auth for dev)
14. Project selection UI
15. Message routing to LangGraph agent
16. Artifact rendering: markdown tables, SQL display
17. Plotly chart rendering via visualization tool
18. Basic error handling and loading states

### Phase 4: Polish & Production (Week 4)
19. OAuth authentication (Google/GitHub)
20. PostgreSQL-backed checkpointer for persistent conversations
21. Connection pooling for project databases
22. Setup script for read-only PostgreSQL roles
23. Docker Compose for full stack
24. Saved queries feature
25. Conversation logging for audit

### Future Enhancements (Backlog)
- Row-level security as an alternative to schema separation
- Agent tool extensibility (custom tools per project)
- Streaming responses in Chainlit
- Export results to CSV/Excel
- Scheduled report generation
- Multi-LLM support (switch between Claude/GPT per project)
- Query cost estimation before execution
- Rate limiting per user/project
- Webhook notifications for long-running queries

---

## 13. Key Design Decisions & Rationale

### Why schema-based isolation over RLS?
Schema separation provides stronger default isolation — a misconfigured RLS policy could leak data between projects. Schemas are simpler to reason about, easier to set up, and the PostgreSQL role system provides an additional enforcement layer. RLS can be added as an option for projects that share a schema but need row-level boundaries.

### Why Chainlit over Streamlit/Gradio?
Chainlit is purpose-built for chat UIs with tool-calling agents. It has native support for streaming, tool call visualization, Plotly rendering, file handling, and authentication — all things that require significant custom work in Streamlit. The `cl.Step` system also gives clean visibility into what the agent is doing.

### Why LangGraph over vanilla LangChain agents?
LangGraph gives explicit control over the agent loop, makes state management predictable, and the checkpointer system handles conversation persistence cleanly. It's also much easier to add custom routing logic (e.g., "if the user is a viewer, don't allow export" or "route complex queries through a validation step first").

### Why sqlglot for validation?
Regex-based SQL validation is fragile and bypassable. sqlglot parses SQL into an AST, making it possible to reliably detect statement types, table references, and dangerous functions regardless of formatting tricks, comments, or encoding. Combined with the read-only PostgreSQL role, this provides defense in depth.

### Why encrypt DB credentials in the Django model?
Project database credentials need to be stored somewhere. Fernet symmetric encryption with a key from the environment means credentials are encrypted at rest in the platform database. This is a pragmatic middle ground — a secrets manager (Vault, AWS Secrets Manager) would be better for production but adds operational complexity.
