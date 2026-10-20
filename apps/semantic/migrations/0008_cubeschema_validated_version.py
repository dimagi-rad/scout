from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("semantic", "0007_canvas_pending_confirmations"),
    ]

    operations = [
        migrations.AddField(
            model_name="cubeschema",
            name="validated_version",
            # db_default keeps inserts from not-yet-upgraded writers valid during a
            # rolling deploy.
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
    ]
