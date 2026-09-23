from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0012_workspaceviewschema_view_sources"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspaceviewschema",
            name="physical_build_token",
            # db_default keeps inserts from not-yet-upgraded writers valid during a
            # rolling deploy (same rule as 0012).
            field=models.CharField(blank=True, db_default="", default="", max_length=32),
        ),
    ]
