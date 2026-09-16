from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0014_access_verification_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="verificationcontrol",
            name="last_attempt_error_code",
            field=models.CharField(blank=True, db_default="", default="", max_length=80),
        ),
        migrations.AddField(
            model_name="verificationcontrol",
            name="last_attempt_lease_token",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="verificationcontrol",
            name="last_attempt_observation_hash",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="verificationcontrol",
            name="last_attempt_outcome",
            field=models.CharField(blank=True, db_default="", default="", max_length=40),
        ),
    ]
