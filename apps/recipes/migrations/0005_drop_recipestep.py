from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0004_remove_sharing_fields"),
    ]

    operations = [
        # A plain drop, unlike workspaces 0018: the orphaned table's foreign key to
        # recipes_recipe would break TRUNCATE-based test flushes if it lingered. The
        # only rolling-deploy exposure is an old process hard-deleting a Workspace
        # (manager-only, rare, self-healing) cascading into the dropped table.
        migrations.DeleteModel(
            name="RecipeStep",
        ),
    ]
