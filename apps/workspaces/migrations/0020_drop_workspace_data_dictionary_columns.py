from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0019_workspace_tenant_last_load"),
    ]

    operations = [
        # Deploy only once 0018 is live everywhere: pre-0018 processes still SELECT and
        # INSERT these columns and fail without them (dimagi-rad/scout#733).
        # 0018 removed these fields from state only, so RemoveField can't drop them.
        # IF EXISTS keeps the drop idempotent for databases where the columns are
        # already gone. The reverse re-adds empty nullable columns (data is not restored) so
        # migration tests that step back to 0015-0017 state can still insert Workspaces.
        migrations.RunSQL(
            "ALTER TABLE workspaces_workspace "
            "DROP COLUMN IF EXISTS data_dictionary, "
            "DROP COLUMN IF EXISTS data_dictionary_generated_at",
            reverse_sql=(
                "ALTER TABLE workspaces_workspace "
                "ADD COLUMN IF NOT EXISTS data_dictionary jsonb NULL, "
                "ADD COLUMN IF NOT EXISTS data_dictionary_generated_at timestamptz NULL"
            ),
        ),
    ]
