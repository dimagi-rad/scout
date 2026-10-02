from django.db import migrations


def mark_user_renamed_titles(apps, schema_editor):
    """Every other row keeps the column default: its stored title is the first message.

    Kept apart from 0012 so this scan does not run under the ALTER TABLE lock.
    """
    Thread = apps.get_model("chat", "Thread")
    Thread.objects.filter(title_is_custom=True).update(title_source="user")


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0012_thread_title_source"),
    ]

    operations = [
        migrations.RunPython(mark_user_renamed_titles, migrations.RunPython.noop),
    ]
