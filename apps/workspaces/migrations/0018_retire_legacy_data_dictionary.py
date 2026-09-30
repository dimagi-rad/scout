from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0017_protect_workspace_tenant_tenant"),
    ]

    operations = [
        # State-only: deploys are rolling, and not-yet-replaced API/MCP/worker
        # processes still SELECT and INSERT these columns on every Workspace query.
        # The columns are nullable, so new code omitting them is valid; the physical
        # drop is 0019, per dimagi-rad/scout#733. RemoveField here only edits state.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="workspace", name="data_dictionary"),
                migrations.RemoveField(model_name="workspace", name="data_dictionary_generated_at"),
            ],
        ),
    ]
