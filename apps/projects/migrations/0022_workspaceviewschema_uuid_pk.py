import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0021_workspace_view_schema"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE projects_workspaceviewschema ADD COLUMN id_new uuid",
                "UPDATE projects_workspaceviewschema SET id_new = gen_random_uuid()",
                "ALTER TABLE projects_workspaceviewschema ALTER COLUMN id_new SET NOT NULL",
                "ALTER TABLE projects_workspaceviewschema DROP CONSTRAINT projects_workspaceviewschema_pkey",
                "ALTER TABLE projects_workspaceviewschema DROP COLUMN id",
                "ALTER TABLE projects_workspaceviewschema RENAME COLUMN id_new TO id",
                "ALTER TABLE projects_workspaceviewschema ADD PRIMARY KEY (id)",
                "ALTER TABLE projects_workspaceviewschema ALTER COLUMN id SET DEFAULT gen_random_uuid()",
            ],
            reverse_sql=[
                "ALTER TABLE projects_workspaceviewschema DROP CONSTRAINT projects_workspaceviewschema_pkey",
                "ALTER TABLE projects_workspaceviewschema ADD COLUMN id_old bigserial",
                "ALTER TABLE projects_workspaceviewschema DROP COLUMN id",
                "ALTER TABLE projects_workspaceviewschema RENAME COLUMN id_old TO id",
                "ALTER TABLE projects_workspaceviewschema ADD PRIMARY KEY (id)",
            ],
        ),
        migrations.AlterField(
            model_name="workspaceviewschema",
            name="id",
            field=models.UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
    ]
