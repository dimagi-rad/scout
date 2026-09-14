from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("chat", "0007_alter_thread_title_threadartifact")]

    operations = [
        migrations.AddField(
            model_name="threadjob",
            name="materialization_preflight_failures",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
