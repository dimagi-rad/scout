from django.db import migrations


def migrate_public_to_tenant(apps, schema_editor):
    SharedArtifact = apps.get_model("artifacts", "SharedArtifact")
    SharedArtifact.objects.filter(access_level="public").update(access_level="tenant")


class Migration(migrations.Migration):
    dependencies = [
        ("artifacts", "0005_artifact_soft_delete"),
    ]

    operations = [
        migrations.RunPython(migrate_public_to_tenant, migrations.RunPython.noop),
    ]
