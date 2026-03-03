from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("workspace", "0015_add_discovering_state"),
    ]

    operations = [
        migrations.RunSQL(
            sql="UPDATE django_content_type SET app_label = 'workspace' WHERE app_label = 'projects'",
            reverse_sql="UPDATE django_content_type SET app_label = 'projects' WHERE app_label = 'workspace'",
        ),
    ]
