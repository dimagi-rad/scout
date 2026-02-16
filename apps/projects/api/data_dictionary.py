"""
API views for data dictionary management.

Provides endpoints for viewing schema information, refreshing from database,
and managing table annotations via TableKnowledge records.
"""
import asyncio
import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.models import TableKnowledge
from apps.projects.models import Project

from .permissions import ProjectPermissionMixin

logger = logging.getLogger(__name__)


class DataDictionaryView(ProjectPermissionMixin, APIView):
    """
    Get the project's data dictionary merged with TableKnowledge annotations.

    GET /api/projects/{project_id}/data-dictionary/
        Returns the data dictionary JSON with annotations from TableKnowledge.
        Requires project membership.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        """Return data dictionary with merged annotations."""
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        # Get the base data dictionary
        data_dictionary = project.data_dictionary or {}

        # Get all TableKnowledge records for this project
        annotations = TableKnowledge.objects.filter(project=project)
        annotations_by_table = {tk.table_name: tk for tk in annotations}

        # Merge annotations into data dictionary
        tables = data_dictionary.get("tables", {})
        merged_tables = {}

        for table_name, table_info in tables.items():
            merged_info = dict(table_info)

            # Check for annotation using full qualified name
            annotation = annotations_by_table.get(table_name)

            if annotation:
                merged_info["annotation"] = {
                    "description": annotation.description,
                    "use_cases": annotation.use_cases,
                    "data_quality_notes": annotation.data_quality_notes,
                    "refresh_frequency": annotation.refresh_frequency,
                    "owner": annotation.owner,
                    "related_tables": annotation.related_tables,
                    "column_notes": annotation.column_notes,
                    "updated_at": annotation.updated_at.isoformat() if annotation.updated_at else None,
                }

            merged_tables[table_name] = merged_info

        result = {
            "tables": merged_tables,
            "generated_at": (
                project.data_dictionary_generated_at.isoformat()
                if project.data_dictionary_generated_at
                else None
            ),
        }

        return Response(result)


async def fetch_schema(project: Project) -> dict:
    """
    Fetch schema information from the project's database.

    Returns:
        dict with 'tables' key containing table and column information.
    """
    import asyncpg

    params = project.get_connection_params()
    conn = await asyncpg.connect(
        host=params["host"],
        port=params["port"],
        database=params["dbname"],
        user=params["user"],
        password=params["password"],
    )

    try:
        schema = project.db_schema

        # Get all tables in the schema
        tables_rows = await conn.fetch("""
            SELECT
                table_schema,
                table_name,
                table_type
            FROM information_schema.tables
            WHERE table_schema = $1
              AND table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY table_name
        """, schema)

        # Get all columns for tables in the schema
        columns_rows = await conn.fetch("""
            SELECT
                table_schema,
                table_name,
                column_name,
                data_type,
                is_nullable,
                column_default,
                ordinal_position,
                character_maximum_length,
                numeric_precision,
                numeric_scale
            FROM information_schema.columns
            WHERE table_schema = $1
            ORDER BY table_name, ordinal_position
        """, schema)

        # Get primary key information
        pk_rows = await conn.fetch("""
            SELECT
                tc.table_schema,
                tc.table_name,
                kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = $1
              AND tc.constraint_type = 'PRIMARY KEY'
        """, schema)

        # Get foreign key information
        fk_rows = await conn.fetch("""
            SELECT
                tc.table_schema,
                tc.table_name,
                kcu.column_name,
                ccu.table_schema AS foreign_table_schema,
                ccu.table_name AS foreign_table_name,
                ccu.column_name AS foreign_column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name
                AND ccu.table_schema = tc.table_schema
            WHERE tc.table_schema = $1
              AND tc.constraint_type = 'FOREIGN KEY'
        """, schema)

        # Build primary keys lookup
        primary_keys = {}
        for row in pk_rows:
            table_key = f"{row['table_schema']}.{row['table_name']}"
            if table_key not in primary_keys:
                primary_keys[table_key] = []
            primary_keys[table_key].append(row["column_name"])

        # Build foreign keys lookup
        foreign_keys = {}
        for row in fk_rows:
            table_key = f"{row['table_schema']}.{row['table_name']}"
            if table_key not in foreign_keys:
                foreign_keys[table_key] = {}
            foreign_keys[table_key][row["column_name"]] = {
                "references_schema": row["foreign_table_schema"],
                "references_table": row["foreign_table_name"],
                "references_column": row["foreign_column_name"],
            }

        # Build columns lookup by table
        columns_by_table = {}
        for row in columns_rows:
            table_key = f"{row['table_schema']}.{row['table_name']}"
            if table_key not in columns_by_table:
                columns_by_table[table_key] = []

            column_info = {
                "name": row["column_name"],
                "data_type": row["data_type"],
                "nullable": row["is_nullable"] == "YES",
                "default": row["column_default"],
                "ordinal_position": row["ordinal_position"],
            }

            # Add type details for specific types
            if row["character_maximum_length"]:
                column_info["max_length"] = row["character_maximum_length"]
            if row["numeric_precision"]:
                column_info["precision"] = row["numeric_precision"]
            if row["numeric_scale"] is not None:
                column_info["scale"] = row["numeric_scale"]

            # Check if this column is a primary key
            pk_columns = primary_keys.get(table_key, [])
            if row["column_name"] in pk_columns:
                column_info["primary_key"] = True

            # Check if this column is a foreign key
            fk_info = foreign_keys.get(table_key, {}).get(row["column_name"])
            if fk_info:
                column_info["foreign_key"] = fk_info

            columns_by_table[table_key].append(column_info)

        # Build final tables structure
        tables = {}
        for row in tables_rows:
            table_key = f"{row['table_schema']}.{row['table_name']}"
            tables[table_key] = {
                "schema": row["table_schema"],
                "name": row["table_name"],
                "type": row["table_type"],
                "columns": columns_by_table.get(table_key, []),
                "primary_key": primary_keys.get(table_key, []),
            }

        return {"tables": tables}

    finally:
        await conn.close()


def refresh_project_schema(project: Project) -> None:
    """Refresh a project's data dictionary from its database and save."""
    schema_info = asyncio.run(fetch_schema(project))
    project.data_dictionary = schema_info
    project.data_dictionary_generated_at = timezone.now()
    project.save(update_fields=["data_dictionary", "data_dictionary_generated_at"])


class RefreshSchemaView(ProjectPermissionMixin, APIView):
    """
    Refresh the project's data dictionary from the database.

    POST /api/projects/{project_id}/refresh-schema/
        Fetches schema information from the project's database using asyncpg,
        including all tables and columns from information_schema.
        Saves the result to project.data_dictionary.
        Requires admin role.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        """Refresh schema from the project's database."""
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        try:
            refresh_project_schema(project)

            return Response({
                "success": True,
                "tables_count": len(
                    (project.data_dictionary or {}).get("tables", {})
                ),
                "generated_at": project.data_dictionary_generated_at.isoformat(),
            })

        except Exception as e:
            logger.exception("Failed to refresh schema for project %s", project_id)
            return Response(
                {"error": f"Failed to refresh schema: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class TableAnnotationsView(ProjectPermissionMixin, APIView):
    """
    Get or update annotations for a specific table.

    GET /api/projects/{project_id}/data-dictionary/tables/{schema}.{table}/
        Returns table details with columns and annotations.
        Requires project membership.

    PUT /api/projects/{project_id}/data-dictionary/tables/{schema}.{table}/
        Updates or creates TableKnowledge record for the table.
        Requires admin role.
    """

    permission_classes = [IsAuthenticated]

    def _parse_table_path(self, table_path: str) -> tuple[str, str]:
        """
        Parse the table path into schema and table name.

        Args:
            table_path: The table path in format "schema.table"

        Returns:
            tuple of (schema_name, table_name)

        Raises:
            ValueError: If schema or table name contains invalid characters
        """
        import re

        # Valid SQL identifier pattern: starts with letter/underscore, contains only
        # alphanumeric and underscores
        identifier_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

        parts = table_path.split(".", 1)
        if len(parts) == 2:
            schema_name, table_name = parts[0], parts[1]
        else:
            # Default to public schema if not specified
            schema_name, table_name = "public", parts[0]

        # Validate both schema and table names
        if not identifier_pattern.match(schema_name):
            raise ValueError(f"Invalid schema name format: {schema_name}")
        if not identifier_pattern.match(table_name):
            raise ValueError(f"Invalid table name format: {table_name}")

        return schema_name, table_name

    def get(self, request, project_id, table_path):
        """Get table details with annotations."""
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        try:
            schema_name, table_name = self._parse_table_path(table_path)
        except ValueError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qualified_name = f"{schema_name}.{table_name}"

        # Get table info from data dictionary
        data_dictionary = project.data_dictionary or {}
        tables = data_dictionary.get("tables", {})
        table_info = tables.get(qualified_name)

        if not table_info:
            return Response(
                {"error": f"Table '{qualified_name}' not found in data dictionary."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Get annotation if exists
        annotation = TableKnowledge.objects.filter(
            project=project,
            table_name=qualified_name,
        ).first()

        result = {
            "schema": schema_name,
            "name": table_name,
            "qualified_name": qualified_name,
            "type": table_info.get("type"),
            "columns": table_info.get("columns", []),
            "primary_key": table_info.get("primary_key", []),
        }

        if annotation:
            result["annotation"] = {
                "id": str(annotation.id),
                "description": annotation.description,
                "use_cases": annotation.use_cases,
                "data_quality_notes": annotation.data_quality_notes,
                "refresh_frequency": annotation.refresh_frequency,
                "owner": annotation.owner,
                "related_tables": annotation.related_tables,
                "column_notes": annotation.column_notes,
                "updated_at": annotation.updated_at.isoformat() if annotation.updated_at else None,
                "updated_by": str(annotation.updated_by_id) if annotation.updated_by_id else None,
            }

        return Response(result)

    def put(self, request, project_id, table_path):
        """Update or create TableKnowledge annotation for a table."""
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        try:
            schema_name, table_name = self._parse_table_path(table_path)
        except ValueError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qualified_name = f"{schema_name}.{table_name}"

        # Verify table exists in data dictionary
        data_dictionary = project.data_dictionary or {}
        tables = data_dictionary.get("tables", {})

        if qualified_name not in tables:
            return Response(
                {"error": f"Table '{qualified_name}' not found in data dictionary."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Extract annotation fields from request
        allowed_fields = [
            "description",
            "use_cases",
            "data_quality_notes",
            "refresh_frequency",
            "owner",
            "related_tables",
            "column_notes",
        ]

        update_data = {}
        for field in allowed_fields:
            if field in request.data:
                update_data[field] = request.data[field]

        # Require at least description
        if "description" not in update_data:
            return Response(
                {"error": "The 'description' field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Update or create TableKnowledge record
        annotation, created = TableKnowledge.objects.update_or_create(
            project=project,
            table_name=qualified_name,
            defaults={
                **update_data,
                "updated_by": request.user,
            },
        )

        return Response({
            "id": str(annotation.id),
            "table_name": annotation.table_name,
            "description": annotation.description,
            "use_cases": annotation.use_cases,
            "data_quality_notes": annotation.data_quality_notes,
            "refresh_frequency": annotation.refresh_frequency,
            "owner": annotation.owner,
            "related_tables": annotation.related_tables,
            "column_notes": annotation.column_notes,
            "created": created,
            "updated_at": annotation.updated_at.isoformat(),
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
