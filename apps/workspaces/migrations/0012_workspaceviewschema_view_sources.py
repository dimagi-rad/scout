from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0011_workspacedatarecovery")]

    operations = [
        migrations.AddField(
            model_name="workspaceviewschema",
            name="view_sources",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Versioned source identities from the last successful physical view publication.",
            ),
        ),
    ]
