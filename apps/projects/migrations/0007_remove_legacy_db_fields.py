"""
Remove legacy database connection fields from Project.

Migrates any projects that still use inline credentials to a DatabaseConnection,
then removes the legacy fields.
"""
from django.db import migrations


def migrate_legacy_credentials(apps, schema_editor):
    """
    For any Project with legacy db_host but no database_connection,
    create a DatabaseConnection and link it.
    """
    Project = apps.get_model("projects", "Project")
    DatabaseConnection = apps.get_model("projects", "DatabaseConnection")

    for project in Project.objects.filter(
        database_connection__isnull=True,
    ).exclude(db_host=""):
        conn = DatabaseConnection.objects.create(
            name=f"{project.name} (migrated)",
            db_host=project.db_host,
            db_port=project.db_port,
            db_name=project.db_name,
            _db_user=project._db_user or b"",
            _db_password=project._db_password or b"",
            created_by=project.created_by,
        )
        project.database_connection = conn
        project.save(update_fields=["database_connection"])


class Migration(migrations.Migration):

    # Each operation needs its own transaction because the RunPython data
    # migration creates pending trigger events that block subsequent
    # ALTER TABLE statements on the same table.
    atomic = False

    dependencies = [
        ("projects", "0006_add_database_connection_fk"),
    ]

    operations = [
        # Step 1: Data migration - move legacy credentials to DatabaseConnection
        migrations.RunPython(
            migrate_legacy_credentials,
            migrations.RunPython.noop,
        ),
        # Step 2: Remove legacy fields
        migrations.RemoveField(model_name="project", name="db_host"),
        migrations.RemoveField(model_name="project", name="db_port"),
        migrations.RemoveField(model_name="project", name="db_name"),
        migrations.RemoveField(model_name="project", name="_db_user"),
        migrations.RemoveField(model_name="project", name="_db_password"),
    ]
