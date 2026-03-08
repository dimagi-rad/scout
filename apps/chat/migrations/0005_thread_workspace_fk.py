"""
Task 3.4: Replace Thread.tenant_membership FK with Thread.workspace FK.

Data migration strategy:
- For threads with a tenant_membership, find the corresponding Workspace via
  the tenant_membership's tenant and assign it. If no workspace is found,
  the thread is orphaned and deleted.
- Threads without a tenant_membership are deleted (orphaned).
"""

import django.db.models.deletion
from django.db import migrations, models


def migrate_threads_to_workspace(apps, schema_editor):
    Thread = apps.get_model("chat", "Thread")
    Workspace = apps.get_model("projects", "Workspace")

    to_update = []
    to_delete = []

    for thread in Thread.objects.select_related("tenant_membership__tenant").all():
        if thread.tenant_membership_id is None:
            to_delete.append(thread.pk)
            continue

        tenant = thread.tenant_membership.tenant
        # Find the workspace linked to this tenant (auto-created or otherwise)
        workspace = Workspace.objects.filter(workspace_tenants__tenant=tenant).first()
        if workspace is None:
            to_delete.append(thread.pk)
        else:
            thread.workspace_id = workspace.pk
            to_update.append(thread)

    if to_delete:
        Thread.objects.filter(pk__in=to_delete).delete()
    for thread in to_update:
        Thread.objects.filter(pk=thread.pk).update(workspace_id=thread.workspace_id)


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0004_rescope_to_workspace"),
        ("projects", "0019_migrate_tenant_workspaces"),
    ]

    operations = [
        # Step 1: Add workspace as nullable so existing rows don't fail
        migrations.AddField(
            model_name="thread",
            name="workspace",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="threads",
                to="projects.workspace",
            ),
        ),
        # Step 2: Data migration — assign workspace from tenant_membership.
        # WARNING: reverse_code is noop because deleted threads cannot be recovered.
        # Rolling back this migration will leave the Thread table without the old
        # tenant_membership column — only roll back if starting from an empty Thread table.
        migrations.RunPython(
            migrate_threads_to_workspace,
            reverse_code=migrations.RunPython.noop,
        ),
        # Step 3: Remove old index, then old fields
        migrations.RemoveIndex(
            model_name="thread",
            name="chat_thread_tm_user_updated",
        ),
        migrations.RemoveField(
            model_name="thread",
            name="tenant_membership",
        ),
        migrations.RemoveField(
            model_name="thread",
            name="is_public",
        ),
        # Step 4: Make workspace non-nullable
        migrations.AlterField(
            model_name="thread",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="threads",
                to="projects.workspace",
            ),
        ),
        # Step 5: Add new index
        migrations.AddIndex(
            model_name="thread",
            index=models.Index(
                fields=["workspace", "user", "-updated_at"],
                name="chat_thread_ws_user_updated",
            ),
        ),
    ]
