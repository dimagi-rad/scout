from django.db import migrations, models


def backfill_denied(apps, schema_editor):
    # A recorded denial stamps the archive and the connection with one time, so rows
    # still matching their connection's latest stamp are denials. Earlier per-tenant
    # denials were restamped away and stay unknown ("").
    TenantMembership = apps.get_model("users", "TenantMembership")
    TenantMembership.objects.filter(
        archived_at__isnull=False,
        connection__upstream_denied_at__isnull=False,
        archived_at=models.F("connection__upstream_denied_at"),
    ).update(archived_reason="denied")


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0018_tenant_commcare_server"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantmembership",
            name="archived_reason",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text=(
                    'Why the row was archived: "denied" (a recorded upstream denial) or '
                    '"unlisted" (dropped from the provider\'s listing). Empty for '
                    "disconnects and older rows. Meaningless while archived_at is null."
                ),
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_denied, migrations.RunPython.noop),
    ]
