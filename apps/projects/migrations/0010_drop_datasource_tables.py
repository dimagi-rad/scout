"""
Drop unused datasource tables and clean up django_migrations.

The DatabaseConnection table is preserved (now owned by projects app).
All other datasources tables are dropped.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0008_require_database_connection"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TABLE IF EXISTS datasources_syncjob CASCADE;",
                "DROP TABLE IF EXISTS datasources_materializeddataset CASCADE;",
                "DROP TABLE IF EXISTS datasources_datasourcecredential CASCADE;",
                "DROP TABLE IF EXISTS datasources_projectdatasource CASCADE;",
                "DROP TABLE IF EXISTS datasources_datasource CASCADE;",
                "DELETE FROM django_migrations WHERE app = 'datasources';",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
