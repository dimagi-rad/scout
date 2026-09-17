from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0012_workspaceviewschema_view_sources"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantschema",
            name="refresh_actor_user_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenantschema",
            name="refresh_job_id",
            field=models.BigIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="tenantschema",
            name="refresh_membership_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenantschema",
            name="refresh_workspace_id",
            field=models.UUIDField(blank=True, null=True),
        ),
    ]
