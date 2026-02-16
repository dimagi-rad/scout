"""
State-only migration: declare DatabaseConnection as owned by projects app.

The underlying database table (datasources_databaseconnection) is unchanged.
The model class now lives in apps.projects.models with
Meta.db_table = "datasources_databaseconnection".

This migration must come before 0006 which references
projects.databaseconnection.  We insert it into the dependency chain
by making 0006 depend on this migration (via 0005).
"""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0005_remove_savedquery_and_conversationlog"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="DatabaseConnection",
                    fields=[
                        ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ("name", models.CharField(help_text="Display name, e.g. 'Production Analytics DB'", max_length=255)),
                        ("description", models.TextField(blank=True)),
                        ("db_host", models.CharField(max_length=255)),
                        ("db_port", models.IntegerField(default=5432)),
                        ("db_name", models.CharField(max_length=255)),
                        ("_db_user", models.BinaryField(db_column="db_user")),
                        ("_db_password", models.BinaryField(db_column="db_password")),
                        ("is_active", models.BooleanField(default=True)),
                        ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_database_connections", to=settings.AUTH_USER_MODEL)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        "ordering": ["name"],
                        "db_table": "datasources_databaseconnection",
                        "permissions": [("manage_database_connections", "Can create and edit database connections")],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
