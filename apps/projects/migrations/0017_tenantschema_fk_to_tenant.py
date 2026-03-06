# Generated migration: change TenantSchema FK from tenant_membership to tenant,
# and convert last_accessed_at from auto_now to explicit nullable field.

import django.db.models.deletion
from django.db import migrations, models


def populate_tenant_from_membership(apps, schema_editor):
    TenantSchema = apps.get_model("projects", "TenantSchema")
    for ts in TenantSchema.objects.select_related("tenant_membership__tenant").all():
        ts.tenant = ts.tenant_membership.tenant
        ts.save(update_fields=["tenant"])


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0016_tenantworkspace_use_tenant_fk"),
        ("users", "0003_tenantmembership"),
    ]

    operations = [
        # Step 1: Add tenant FK as nullable
        migrations.AddField(
            model_name="tenantschema",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="schemas",
                to="users.tenant",
            ),
        ),
        # Step 2: Populate tenant from existing tenant_membership
        migrations.RunPython(populate_tenant_from_membership, migrations.RunPython.noop),
        # Step 3: Make tenant non-nullable
        migrations.AlterField(
            model_name="tenantschema",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="schemas",
                to="users.tenant",
            ),
        ),
        # Step 4: Remove old tenant_membership FK
        migrations.RemoveField(
            model_name="tenantschema",
            name="tenant_membership",
        ),
        # Step 5: Convert last_accessed_at from auto_now to explicit nullable
        migrations.AlterField(
            model_name="tenantschema",
            name="last_accessed_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
    ]
