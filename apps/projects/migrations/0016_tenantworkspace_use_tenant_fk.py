import django.db.models.deletion
from django.db import migrations, models


def populate_tenant_fk(apps, schema_editor):
    """Set TenantWorkspace.tenant FK from the old legacy_tenant_id/tenant_name string fields.

    Looks up the Tenant rows created by the users 0006 migration. If TenantWorkspace
    is empty (fresh install), this is a no-op.
    """
    TenantWorkspace = apps.get_model("projects", "TenantWorkspace")
    Tenant = apps.get_model("users", "Tenant")
    for ws in TenantWorkspace.objects.all():
        # Tenant rows were already created by 0006_tenantmembership_use_tenant_fk;
        # use get_or_create as a safety net for workspaces with no membership.
        tenant, _ = Tenant.objects.get_or_create(
            provider="commcare",  # TenantWorkspace was commcare-only before this migration
            external_id=ws.legacy_tenant_id,
            defaults={"canonical_name": ws.tenant_name or ws.legacy_tenant_id},
        )
        ws.tenant = tenant
        ws.save(update_fields=["tenant"])


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0015_add_discovering_state"),
        ("users", "0006_tenantmembership_use_tenant_fk"),
    ]

    operations = [
        # Step 1: Rename old tenant_id CharField to avoid collision with the new FK
        # (Django maps OneToOneField 'tenant' to a DB column named 'tenant_id')
        migrations.RenameField(
            model_name="tenantworkspace",
            old_name="tenant_id",
            new_name="legacy_tenant_id",
        ),
        # Step 2: Add nullable OneToOneField (old columns still intact for data migration)
        migrations.AddField(
            model_name="tenantworkspace",
            name="tenant",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="workspace",
                to="users.tenant",
            ),
        ),
        # Step 3: Populate the FK from old string fields
        migrations.RunPython(populate_tenant_fk, migrations.RunPython.noop),
        # Step 4: Make FK non-nullable now that all rows are populated
        migrations.AlterField(
            model_name="tenantworkspace",
            name="tenant",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="workspace",
                to="users.tenant",
            ),
        ),
        # Step 5: Remove old string fields
        migrations.RemoveField(
            model_name="tenantworkspace",
            name="legacy_tenant_id",
        ),
        migrations.RemoveField(
            model_name="tenantworkspace",
            name="tenant_name",
        ),
        # Step 6: Update ordering
        migrations.AlterModelOptions(
            name="tenantworkspace",
            options={"ordering": ["tenant__canonical_name"]},
        ),
    ]
