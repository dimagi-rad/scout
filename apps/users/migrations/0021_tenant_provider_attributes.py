from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0020_user_last_workspace"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="provider_attributes",
            field=models.JSONField(
                blank=True,
                db_default={},
                default=dict,
                help_text=(
                    "Display/filter hints the provider reports for this tenant (e.g. a Connect "
                    "opportunity's status and program), refreshed by each member's discovery. "
                    "Never used for access decisions; TenantMetadata holds the copy taken at "
                    "materialization."
                ),
            ),
        ),
    ]
