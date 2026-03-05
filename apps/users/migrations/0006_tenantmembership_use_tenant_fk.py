import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def populate_tenant_fk(apps, schema_editor):
    """Create Tenant rows from existing TenantMembership string fields and set the FK.

    If TenantMembership is empty (fresh install), this is a no-op.
    On existing installs, each unique (provider, legacy_tenant_id) pair becomes one Tenant row.
    """
    TenantMembership = apps.get_model("users", "TenantMembership")
    Tenant = apps.get_model("users", "Tenant")
    for tm in TenantMembership.objects.all():
        tenant, _ = Tenant.objects.get_or_create(
            provider=tm.provider,
            external_id=tm.legacy_tenant_id,
            defaults={"canonical_name": tm.tenant_name or tm.legacy_tenant_id},
        )
        tm.tenant = tenant
        tm.save(update_fields=["tenant"])


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0005_tenant"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Step 1: Rename old tenant_id CharField to avoid collision with the new FK
        # (Django maps ForeignKey 'tenant' to a DB column named 'tenant_id')
        migrations.RenameField(
            model_name="tenantmembership",
            old_name="tenant_id",
            new_name="legacy_tenant_id",
        ),
        # Step 2: Add nullable FK (old columns still intact for the data migration)
        migrations.AddField(
            model_name="tenantmembership",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="memberships",
                to="users.tenant",
            ),
        ),
        # Step 3: Populate the FK from old string fields
        migrations.RunPython(populate_tenant_fk, migrations.RunPython.noop),
        # Step 4: Make FK non-nullable now that all rows are populated
        migrations.AlterField(
            model_name="tenantmembership",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="memberships",
                to="users.tenant",
            ),
        ),
        # Step 5: Remove old unique_together before dropping legacy columns
        migrations.AlterUniqueTogether(
            name="tenantmembership",
            unique_together=set(),
        ),
        # Step 6: Remove old string fields
        migrations.RemoveField(
            model_name="tenantmembership",
            name="provider",
        ),
        migrations.RemoveField(
            model_name="tenantmembership",
            name="legacy_tenant_id",
        ),
        migrations.RemoveField(
            model_name="tenantmembership",
            name="tenant_name",
        ),
        # Step 7: Add new unique_together and ordering
        migrations.AlterUniqueTogether(
            name="tenantmembership",
            unique_together={("user", "tenant")},
        ),
        migrations.AlterModelOptions(
            name="tenantmembership",
            options={
                "ordering": ["-last_selected_at", "tenant__canonical_name"],
            },
        ),
    ]
