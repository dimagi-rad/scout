import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("users", "0013_upstream_denial")]

    operations = [
        migrations.CreateModel(
            name="VerificationControl",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "connection",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="verification_control",
                        to="users.tenantconnection",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="UpstreamAccessProof",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("credential_fingerprint", models.CharField(max_length=64)),
                (
                    "account_identity",
                    models.CharField(blank=True, db_default="", default="", max_length=64),
                ),
                (
                    "scope_key",
                    models.CharField(blank=True, db_default="", default="", max_length=255),
                ),
                ("observed_denied_at", models.DateTimeField(blank=True, null=True)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_attempt_result",
                    models.CharField(blank=True, db_default="", default="", max_length=40),
                ),
                (
                    "last_error_code",
                    models.CharField(blank=True, db_default="", default="", max_length=80),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_proofs",
                        to="users.tenantconnection",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_proofs",
                        to="users.tenant",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("connection", "tenant"),
                        name="unique_access_proof_connection_tenant",
                    )
                ],
            },
        ),
    ]
