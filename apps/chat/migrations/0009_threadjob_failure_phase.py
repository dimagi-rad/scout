from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("chat", "0008_threadjob_materialization_preflight_failures")]

    operations = [
        migrations.AddField(
            model_name="threadjob",
            name="failure_phase",
            field=models.CharField(
                blank=True,
                choices=[("materialization", "Materialization"), ("resume", "Follow-up response")],
                db_default="",
                default="",
                max_length=20,
            ),
        ),
    ]
