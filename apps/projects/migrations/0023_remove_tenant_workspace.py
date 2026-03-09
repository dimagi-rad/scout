from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0022_workspaceviewschema_uuid_pk"),
        # All apps that historically FK'd to TenantWorkspace have re-pointed to Workspace
        ("artifacts", "0008_workspace_fk_to_workspace"),
        ("knowledge", "0007_workspace_fk_to_workspace"),
        ("recipes", "0007_workspace_fk_to_workspace"),
    ]

    operations = [
        migrations.DeleteModel(name="TenantWorkspace"),
    ]
