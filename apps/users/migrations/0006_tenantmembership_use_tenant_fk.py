import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0005_tenant"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Remove old unique_together first
        migrations.AlterUniqueTogether(
            name="tenantmembership",
            unique_together=set(),
        ),
        # Remove old fields
        migrations.RemoveField(
            model_name="tenantmembership",
            name="provider",
        ),
        migrations.RemoveField(
            model_name="tenantmembership",
            name="tenant_id",
        ),
        migrations.RemoveField(
            model_name="tenantmembership",
            name="tenant_name",
        ),
        # Add tenant FK (nullable first so existing rows can be handled)
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
        # Make it non-nullable
        migrations.AlterField(
            model_name="tenantmembership",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="memberships",
                to="users.tenant",
            ),
        ),
        # Add new unique_together
        migrations.AlterUniqueTogether(
            name="tenantmembership",
            unique_together={("user", "tenant")},
        ),
        # Update ordering
        migrations.AlterModelOptions(
            name="tenantmembership",
            options={
                "ordering": ["-last_selected_at", "tenant__canonical_name"],
            },
        ),
    ]
