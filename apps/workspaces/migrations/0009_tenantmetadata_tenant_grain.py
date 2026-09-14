"""Step 3 of moving ``TenantMetadata`` to tenant grain (#305).

Only safe after 0008: the unique constraint that ``OneToOneField`` adds would
reject any tenant that still has duplicate rows, and ``NOT NULL`` would reject
any row the backfill missed.

Reverse re-adds ``tenant_membership`` as a nullable column and relaxes ``tenant``
back to a plain FK; 0008's reverse then repopulates the memberships, and 0007's
restores ``NOT NULL``. Reversing this migration alone therefore leaves
``tenant_membership`` NULL — carry on to 0006, or 0008's reverse, to get a
consistent membership-grain table back.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0009_alter_tenantmembership_options_and_more"),
        ("workspaces", "0008_backfill_tenantmetadata_tenant"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="tenantmetadata",
            name="tenant_membership",
        ),
        migrations.AlterField(
            model_name="tenantmetadata",
            name="tenant",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="metadata",
                to="users.tenant",
            ),
        ),
    ]
