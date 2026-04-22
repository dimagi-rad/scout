from django.db import migrations


def convert_empty_emails_to_null(apps, schema_editor):
    User = apps.get_model("users", "User")
    User.objects.filter(email="").update(email=None)


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0002_allow_null_email"),
    ]

    operations = [
        migrations.RunPython(convert_empty_emails_to_null, migrations.RunPython.noop),
    ]
