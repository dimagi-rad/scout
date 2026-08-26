from django.db import migrations, models
from django.utils import timezone


def retire_plotly_artifacts(apps, schema_editor):
    Artifact = apps.get_model("artifacts", "Artifact")
    Artifact.objects.filter(artifact_type="plotly", is_deleted=False).update(
        is_deleted=True,
        deleted_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("artifacts", "0005_artifact_semantic_query_manifest_and_records"),
    ]

    operations = [
        migrations.AlterField(
            model_name="artifact",
            name="artifact_type",
            field=models.CharField(
                choices=[
                    ("react", "React Component"),
                    ("html", "HTML Document"),
                    ("markdown", "Markdown Document"),
                    ("svg", "SVG Graphic"),
                    ("story", "Story"),
                ],
                help_text="The type of artifact (react, html, markdown, svg, story).",
                max_length=20,
            ),
        ),
        migrations.RunPython(retire_plotly_artifacts, migrations.RunPython.noop),
    ]
