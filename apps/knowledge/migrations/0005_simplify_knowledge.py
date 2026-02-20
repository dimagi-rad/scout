"""
Simplify knowledge system: replace CanonicalMetric, VerifiedQuery, BusinessRule
with a single KnowledgeEntry model. Remove promoted_to fields from AgentLearning.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def migrate_data_forward(apps, schema_editor):
    """Convert existing knowledge records to KnowledgeEntry."""
    KnowledgeEntry = apps.get_model("knowledge", "KnowledgeEntry")
    CanonicalMetric = apps.get_model("knowledge", "CanonicalMetric")
    VerifiedQuery = apps.get_model("knowledge", "VerifiedQuery")
    BusinessRule = apps.get_model("knowledge", "BusinessRule")

    # Migrate CanonicalMetric -> KnowledgeEntry
    for metric in CanonicalMetric.objects.all():
        parts = [metric.definition]
        if metric.sql_template:
            parts.append(f"\n\n```sql\n{metric.sql_template.strip()}\n```")
        if metric.unit:
            parts.append(f"\n\n**Unit:** {metric.unit}")
        if metric.caveats:
            caveats_str = "\n".join(f"- {c}" for c in metric.caveats)
            parts.append(f"\n\n**Caveats:**\n{caveats_str}")

        tags = ["metric"]
        if metric.tags:
            tags.extend(t for t in metric.tags if t != "metric")

        KnowledgeEntry.objects.create(
            id=uuid.uuid4(),
            project=metric.project,
            title=metric.name,
            content="".join(parts),
            tags=tags,
            created_by=metric.updated_by,
        )

    # Migrate VerifiedQuery -> KnowledgeEntry
    for query in VerifiedQuery.objects.all():
        parts = [query.description]
        if query.sql:
            parts.append(f"\n\n```sql\n{query.sql.strip()}\n```")
        if query.tables_used:
            tables_str = ", ".join(f"`{t}`" for t in query.tables_used)
            parts.append(f"\n\n**Tables:** {tables_str}")

        tags = ["query"]
        if query.tags:
            tags.extend(t for t in query.tags if t != "query")

        KnowledgeEntry.objects.create(
            id=uuid.uuid4(),
            project=query.project,
            title=query.name,
            content="".join(parts),
            tags=tags,
            created_by=query.verified_by,
        )

    # Migrate BusinessRule -> KnowledgeEntry
    for rule in BusinessRule.objects.all():
        parts = [rule.description]
        context_parts = []
        if rule.applies_to_tables:
            context_parts.append(f"Tables: {', '.join(rule.applies_to_tables)}")
        if rule.applies_to_metrics:
            context_parts.append(f"Metrics: {', '.join(rule.applies_to_metrics)}")
        if context_parts:
            parts.append(f"\n\n**Applies to:** {'; '.join(context_parts)}")

        tags = ["rule"]
        if rule.tags:
            tags.extend(t for t in rule.tags if t != "rule")

        KnowledgeEntry.objects.create(
            id=uuid.uuid4(),
            project=rule.project,
            title=rule.title,
            content="".join(parts),
            tags=tags,
            created_by=rule.created_by,
        )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("knowledge", "0004_alter_agentlearning_confidence_score"),
    ]

    operations = [
        # 1. Create KnowledgeEntry model
        migrations.CreateModel(
            name="KnowledgeEntry",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                (
                    "content",
                    models.TextField(help_text="Markdown content for this knowledge entry."),
                ),
                ("tags", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="knowledge_entries",
                        to="projects.project",
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at"],
                "verbose_name_plural": "Knowledge entries",
            },
        ),
        # 2. Migrate data from old models to KnowledgeEntry
        migrations.RunPython(migrate_data_forward, migrations.RunPython.noop),
        # 3. Remove promoted_to fields from AgentLearning
        migrations.RemoveField(model_name="agentlearning", name="promoted_to"),
        migrations.RemoveField(model_name="agentlearning", name="promoted_to_id"),
        # 4. Delete old models
        migrations.DeleteModel(name="CanonicalMetric"),
        migrations.DeleteModel(name="VerifiedQuery"),
        migrations.DeleteModel(name="BusinessRule"),
    ]
