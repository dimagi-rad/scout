"""
Make database_connection FK required (non-nullable).

Split from 0007 because PostgreSQL cannot ALTER a table
that has pending trigger events from the same transaction.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0007_remove_legacy_db_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="database_connection",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="projects",
                to="projects.databaseconnection",
            ),
        ),
    ]
