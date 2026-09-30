from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0017_encrypt_socialtoken_values"),
    ]

    # The new constraint is added before the old one is dropped so tenant identity
    # stays unique at every step; every existing row gets server="" (www).
    operations = [
        migrations.AddField(
            model_name="tenant",
            name="server",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text=(
                    'Which deployment of the provider hosts this tenant (CommCare HQ: "" = '
                    'www, "eu" = EU). Empty for single-deployment providers.'
                ),
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="tenant",
            constraint=models.UniqueConstraint(
                fields=("provider", "server", "external_id"),
                name="unique_tenant_provider_server_external_id",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="tenant",
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name="tenantconnection",
            name="scope_key",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text=(
                    "Provider-native scope this credential authorises (OCS team slug; "
                    'CommCare HQ server key, "" = www). Empty for providers whose tokens '
                    'are account-wide. Never NULL — "" is a real value the uniqueness '
                    "constraint must collapse on."
                ),
                max_length=255,
            ),
        ),
    ]
