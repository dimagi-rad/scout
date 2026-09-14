"""Step 1 of moving ``TenantMetadata`` to tenant grain (#305).

``tenant`` lands as a plain nullable FK — no ``OneToOne``, no unique constraint —
so the 0008 backfill cannot fail half way through on a tenant that still has
duplicate rows. ``tenant_membership`` is relaxed to nullable in the same step
because 0009 drops it: reversing 0009 can only re-add the column as nullable, so
the ``NOT NULL`` has to be gone from the historical state before then or the
reverse chain wedges.

Reverse: drop ``tenant``, restore ``NOT NULL`` on ``tenant_membership`` (still
fully populated at this point, since 0008's reverse refills it).
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0009_alter_tenantmembership_options_and_more"),
        ("workspaces", "0006_workspaceinvite"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantmetadata",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="metadata",
                to="users.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="tenantmetadata",
            name="tenant_membership",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="metadata",
                to="users.tenantmembership",
            ),
        ),
    ]
