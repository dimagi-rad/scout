import uuid

import django.db.models.deletion
from django.db import migrations, models


def _delete_legacy_canvases(apps, schema_editor):
    # Pre-changeset canvases were workspace-scoped JSON drafts with no thread
    # binding; they are unreachable under the thread-bound design.
    apps.get_model("semantic", "SemanticCanvas").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0007_alter_thread_title_threadartifact"),
        ("semantic", "0002_semanticdataset_source_kind_cubeschema_customdataset_and_more"),
    ]

    operations = [
        migrations.RunPython(_delete_legacy_canvases, migrations.RunPython.noop),
        migrations.RemoveField(model_name="semanticcanvas", name="changes"),
        migrations.RemoveField(model_name="semanticcanvas", name="diagnostics"),
        migrations.AlterField(
            model_name="semanticcanvas",
            name="status",
            field=models.CharField(
                choices=[("open", "Open"), ("discarded", "Discarded")],
                default="open",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="semanticcanvas",
            name="thread",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="semantic_canvas",
                to="chat.thread",
            ),
        ),
        migrations.CreateModel(
            name="SemanticCanvasChange",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "object_type",
                    models.CharField(
                        choices=[
                            ("dataset", "Dataset"),
                            ("field", "Field"),
                            ("relationship", "Relationship"),
                            ("custom_dataset", "Custom dataset"),
                        ],
                        max_length=30,
                    ),
                ),
                ("object_uuid", models.UUIDField()),
                (
                    "change_type",
                    models.CharField(
                        choices=[
                            ("create", "Create"),
                            ("update", "Update"),
                            ("delete", "Delete"),
                        ],
                        max_length=20,
                    ),
                ),
                ("fields", models.JSONField(blank=True, default=dict)),
                ("base_fingerprint", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "canvas",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="changes",
                        to="semantic.semanticcanvas",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
                "indexes": [
                    models.Index(
                        fields=["canvas", "object_type"], name="semantic_se_canvas__26bdb7_idx"
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="semanticcanvaschange",
            constraint=models.UniqueConstraint(
                fields=("canvas", "object_uuid"), name="one_canvas_change_per_object"
            ),
        ),
    ]
