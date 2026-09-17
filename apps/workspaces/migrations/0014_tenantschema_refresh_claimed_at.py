from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0013_tenantschema_refresh_request_binding")]

    operations = [
        migrations.AddField(
            model_name="tenantschema",
            name="refresh_claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
