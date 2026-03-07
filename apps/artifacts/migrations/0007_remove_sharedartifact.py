from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("artifacts", "0006_migrate_public_to_tenant"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.DeleteModel(
            name="SharedArtifact",
        ),
    ]
