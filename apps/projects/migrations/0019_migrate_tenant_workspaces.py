"""Data migration: create Workspace + WorkspaceTenant for each TenantWorkspace.

Uses the same UUID as TenantWorkspace so that existing FK references in
artifacts/recipes/knowledge can be repointed without a data migration on those tables.
"""

from django.db import migrations


def create_workspaces_from_tenant_workspaces(apps, schema_editor):
    TenantWorkspace = apps.get_model("projects", "TenantWorkspace")
    TenantMembership = apps.get_model("users", "TenantMembership")
    Workspace = apps.get_model("projects", "Workspace")
    WorkspaceTenant = apps.get_model("projects", "WorkspaceTenant")
    WorkspaceMembership = apps.get_model("projects", "WorkspaceMembership")

    for tw in TenantWorkspace.objects.select_related("tenant").all():
        ws, created = Workspace.objects.get_or_create(
            id=tw.id,
            defaults={
                "name": tw.tenant.canonical_name,
                "is_auto_created": True,
                "system_prompt": tw.system_prompt,
                "data_dictionary": tw.data_dictionary,
                "data_dictionary_generated_at": tw.data_dictionary_generated_at,
            },
        )
        if created:
            WorkspaceTenant.objects.create(workspace=ws, tenant=tw.tenant)

        for tm in TenantMembership.objects.filter(tenant=tw.tenant):
            WorkspaceMembership.objects.get_or_create(
                workspace=ws,
                user=tm.user,
                defaults={"role": "manage"},
            )


def reverse_migration(apps, schema_editor):
    # No-op: Workspace records created here will be deleted when the table is dropped
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0018_workspace_and_membership"),
    ]

    operations = [
        migrations.RunPython(create_workspaces_from_tenant_workspaces, reverse_migration),
    ]
