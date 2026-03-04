import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0015_add_discovering_state"),
        ("users", "0005_tenant"),
    ]

    operations = [
        # Remove old fields
        migrations.RemoveField(
            model_name="tenantworkspace",
            name="tenant_id",
        ),
        migrations.RemoveField(
            model_name="tenantworkspace",
            name="tenant_name",
        ),
        # Add tenant OneToOneField (nullable first for existing rows)
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
        # Make it non-nullable
        migrations.AlterField(
            model_name="tenantworkspace",
            name="tenant",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="workspace",
                to="users.tenant",
            ),
        ),
        # Update ordering
        migrations.AlterModelOptions(
            name="tenantworkspace",
            options={"ordering": ["tenant__canonical_name"]},
        ),
    ]
