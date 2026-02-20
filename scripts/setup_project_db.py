#!/usr/bin/env python
"""
Database role setup script for project databases.

Creates read-only PostgreSQL roles for projects with appropriate permissions.
This script is idempotent and safe to re-run.

Usage:
    # Setup role for a single project
    python scripts/setup_project_db.py --project-slug myproject

    # Setup roles for all projects
    python scripts/setup_project_db.py --all

    # Dry run (show what would be done)
    python scripts/setup_project_db.py --project-slug myproject --dry-run
"""

import argparse
import os
import secrets
import sys

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

import django

django.setup()

import psycopg2

from apps.projects.models import Project


def generate_secure_password() -> str:
    """Generate a cryptographically secure random password."""
    return secrets.token_urlsafe(32)


def generate_role_name(project: Project) -> str:
    """Generate a role name for a project."""
    # Clean the slug to make it a valid PostgreSQL identifier
    clean_slug = project.slug.replace("-", "_").replace(".", "_")[:50]
    return f"scout_readonly_{clean_slug}"


def setup_project_role(project: Project, dry_run: bool = False) -> dict:
    """
    Create a read-only PostgreSQL role for a project.

    This function:
    1. Creates a role if it doesn't exist
    2. Grants CONNECT to the database
    3. Grants USAGE on the project schema
    4. Grants SELECT on all current and future tables
    5. Revokes everything on public schema
    6. Sets a connection limit

    Args:
        project: The Project instance
        dry_run: If True, just print what would be done

    Returns:
        Dict with status and role_name
    """
    conn_params = project.get_connection_params()
    role_name = generate_role_name(project)
    schema = project.db_schema or "public"
    db_name = conn_params.get("dbname", conn_params.get("database"))

    # Generate a secure random password for the role
    role_password = generate_secure_password()

    # SQL statements to execute
    statements = [
        # Create role if not exists (with a secure random password)
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{role_name}') THEN
                CREATE ROLE {role_name} WITH
                    LOGIN
                    PASSWORD '{role_password}'
                    NOSUPERUSER
                    NOCREATEDB
                    NOCREATEROLE
                    INHERIT
                    NOREPLICATION
                    CONNECTION LIMIT 5;
            END IF;
        END
        $$;
        """,
        # Grant CONNECT to database
        f"GRANT CONNECT ON DATABASE {db_name} TO {role_name};",
        # Grant USAGE on schema
        f"GRANT USAGE ON SCHEMA {schema} TO {role_name};",
        # Grant SELECT on all existing tables
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role_name};",
        # Grant SELECT on all future tables
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role_name};",
        # Grant SELECT on all sequences (for serial columns)
        f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role_name};",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON SEQUENCES TO {role_name};",
    ]

    # If schema is not public, revoke public schema access
    if schema != "public":
        statements.extend(
            [
                f"REVOKE ALL ON SCHEMA public FROM {role_name};",
                f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_name};",
            ]
        )

    results = {
        "project": project.slug,
        "role_name": role_name,
        "schema": schema,
        "statements": statements,
        "success": False,
        "error": None,
    }

    if dry_run:
        print(f"\n[DRY RUN] Would execute for project '{project.slug}':")
        for stmt in statements:
            # Clean up whitespace for display
            clean_stmt = " ".join(stmt.split())
            print(f"  {clean_stmt[:100]}...")
        results["success"] = True
        return results

    # Execute statements
    conn = None
    try:
        # Connect as admin to create roles and grant permissions
        conn = psycopg2.connect(**conn_params)
        conn.autocommit = True
        cursor = conn.cursor()

        for stmt in statements:
            try:
                cursor.execute(stmt)
            except psycopg2.Error as e:
                # Some errors are expected (e.g., granting to non-existent tables)
                if "does not exist" not in str(e):
                    raise

        cursor.close()
        results["success"] = True

        # Update project with role name if not already set
        if project.readonly_role != role_name:
            project.readonly_role = role_name
            project.save(update_fields=["readonly_role"])

        print(f"[OK] Created/updated role '{role_name}' for project '{project.slug}'")

    except psycopg2.Error as e:
        results["error"] = str(e)
        print(f"[ERROR] Failed to setup role for project '{project.slug}': {e}")

    finally:
        if conn:
            conn.close()

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Setup read-only PostgreSQL roles for Scout projects"
    )
    parser.add_argument(
        "--project-slug",
        type=str,
        help="Project slug to setup role for",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Setup roles for all projects",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )

    args = parser.parse_args()

    if not args.project_slug and not args.all:
        parser.error("Either --project-slug or --all is required")

    # Get projects to process
    if args.all:
        projects = Project.objects.filter(is_active=True)
        print(f"Setting up roles for {projects.count()} active projects...")
    else:
        try:
            projects = [Project.objects.get(slug=args.project_slug)]
        except Project.DoesNotExist:
            print(f"Error: Project '{args.project_slug}' not found")
            sys.exit(1)

    # Process each project
    results = []
    for project in projects:
        result = setup_project_role(project, dry_run=args.dry_run)
        results.append(result)

    # Summary
    print("\n" + "=" * 60)
    successful = sum(1 for r in results if r["success"])
    failed = sum(1 for r in results if not r["success"])
    print(f"Summary: {successful} successful, {failed} failed")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
