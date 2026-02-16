"""
CSV file upload and import into a project's PostgreSQL datasource.
"""

import logging
import re
from urllib.parse import quote_plus

import pandas as pd
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from sqlalchemy import create_engine

from apps.projects.api.data_dictionary import refresh_project_schema
from apps.projects.models import Project, ProjectMembership, ProjectRole

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def clean_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """Clean column names for PostgreSQL compatibility."""
    cleaned_columns = []
    for col in df.columns:
        cleaned = col.lower().replace(" ", "_").replace("-", "_").replace(".", "_").replace("@", "_")
        cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
        if len(cleaned) > 60:
            cleaned = cleaned[:60]
        if not cleaned or cleaned[0].isdigit():
            cleaned = f"col_{cleaned}"
        cleaned_columns.append(cleaned)

    # Handle duplicates by adding suffix
    final_columns = []
    seen: dict[str, int] = {}
    for col in cleaned_columns:
        if col in seen:
            seen[col] += 1
            final_columns.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 0
            final_columns.append(col)

    df.columns = final_columns
    return df


def _build_engine(project: Project):
    """Build a writable SQLAlchemy engine from project connection params."""
    params = project.get_connection_params()
    # The 'options' key contains psycopg2-style options (search_path, statement_timeout)
    # which aren't valid for a SQLAlchemy URL — strip it.
    user = quote_plus(params["user"])
    password = quote_plus(params["password"])
    host = params["host"]
    port = params["port"]
    dbname = params["dbname"]
    url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    return create_engine(url)


@require_POST
def csv_import_view(request):
    """Import an uploaded CSV file into a project's database."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)

    uploaded_file = request.FILES.get("file")
    project_id = request.POST.get("project_id")
    table_name = request.POST.get("table_name")

    if not uploaded_file or not project_id or not table_name:
        return JsonResponse(
            {"error": "Missing required fields: file, project_id, table_name."},
            status=400,
        )

    # Validate file type
    if not uploaded_file.name.lower().endswith(".csv"):
        return JsonResponse({"error": "Only CSV files are supported."}, status=400)

    # Validate file size
    if uploaded_file.size > MAX_FILE_SIZE:
        return JsonResponse(
            {"error": f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)} MB."},
            status=400,
        )

    # Validate table name (alphanumeric + underscores, must start with letter/underscore)
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return JsonResponse(
            {"error": "Invalid table name. Use only letters, numbers, and underscores."},
            status=400,
        )

    # Validate project membership (analyst or admin required for writes)
    try:
        membership = ProjectMembership.objects.select_related("project").get(
            user=request.user,
            project_id=project_id,
        )
    except ProjectMembership.DoesNotExist:
        return JsonResponse({"error": "Project not found or access denied."}, status=403)

    if membership.role not in (ProjectRole.ANALYST, ProjectRole.ADMIN):
        return JsonResponse(
            {"error": "Analyst or admin role required to import data."},
            status=403,
        )

    project = membership.project

    # Read CSV
    try:
        df = pd.read_csv(uploaded_file)
    except Exception as exc:
        logger.warning("CSV parse error: %s", exc)
        return JsonResponse({"error": f"Failed to parse CSV: {exc}"}, status=400)

    if df.empty:
        return JsonResponse({"error": "CSV file is empty."}, status=400)

    df = clean_column_names(df)

    # Import into database
    engine = _build_engine(project)
    try:
        df.to_sql(
            table_name,
            engine,
            schema=project.db_schema,
            if_exists="replace",
            index=False,
            method="multi",
            chunksize=1000,
        )
    except Exception as exc:
        logger.exception("CSV import failed for project %s", project_id)
        return JsonResponse({"error": f"Database import failed: {exc}"}, status=500)
    finally:
        engine.dispose()

    # Refresh data dictionary so the new table is visible
    try:
        refresh_project_schema(project)
    except Exception:
        logger.warning("Schema refresh failed after CSV import for project %s", project_id, exc_info=True)

    columns = [
        {"name": col, "dtype": str(df[col].dtype)}
        for col in df.columns
    ]

    return JsonResponse({
        "table_name": table_name,
        "row_count": len(df),
        "column_count": len(df.columns),
        "columns": columns,
    })
