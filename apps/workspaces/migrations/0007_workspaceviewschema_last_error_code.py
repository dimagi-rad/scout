from django.db import migrations, models

# The sentinel this replaces. WorkspaceViewSchema rows are long-lived — one per
# workspace, surviving deploys — so an existing FAILED-by-cascade row must not
# read as a build failure after the switch to last_error_code, or the resume
# prompt would tell the user a system-side fix is required when re-materializing
# is in fact the fix (07#9).
_CASCADE_MARKER = "[cascade-teardown]"


def backfill_last_error_code(apps, schema_editor):
    """Classify existing FAILED rows once, here, instead of at every read.

    A one-time string match in a migration is the honest place for this: it runs
    against a fixed corpus, not against prose that may be reworded later.
    """
    WorkspaceViewSchema = apps.get_model("workspaces", "WorkspaceViewSchema")
    WorkspaceViewSchema.objects.filter(state="failed", last_error__contains=_CASCADE_MARKER).update(
        last_error_code="VIEW_SCHEMA_CASCADE_TEARDOWN"
    )
    WorkspaceViewSchema.objects.filter(state="failed", last_error_code="").exclude(
        last_error=""
    ).update(last_error_code="SCHEMA_BUILD_FAILED")


def clear_last_error_code(apps, schema_editor):
    """No-op: RemoveField drops the column, and last_error still carries the prose."""


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0006_workspaceinvite"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspaceviewschema",
            name="last_error_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "ErrorCode for last_error — what callers branch on. last_error is "
                    "prose and must not be parsed."
                ),
                max_length=64,
            ),
        ),
        migrations.RunPython(backfill_last_error_code, clear_last_error_code),
    ]
