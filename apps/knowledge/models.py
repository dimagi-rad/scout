"""
Knowledge layer models for Scout data agent platform.

Provides semantic knowledge beyond the auto-generated data dictionary:
- TableKnowledge: Enriched table metadata
- KnowledgeEntry: General-purpose knowledge (title + markdown + tags)
- AgentLearning: Workspace memory, shared notes about the workspace's data
"""

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TableKnowledge(models.Model):
    """
    Enriched table metadata beyond what the data dictionary provides.

    The data dictionary gives you columns and types. This model adds:
    - Human-written descriptions of what the table *means*
    - Use cases (what questions this table helps answer)
    - Data quality notes and gotchas
    - Ownership and freshness information
    - Relationships not captured by foreign keys
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="table_knowledge",
        null=True,
        blank=True,
    )

    table_name = models.CharField(max_length=255)
    description = models.TextField(
        help_text="Human-written description of what this table represents and when to use it."
    )
    use_cases = models.JSONField(
        default=list,
        help_text='What questions this table helps answer. E.g. ["Revenue reporting", "User retention analysis"]',
    )
    data_quality_notes = models.JSONField(
        default=list,
        help_text='Known quirks. E.g. ["created_at is UTC", "amount is in cents not dollars"]',
    )
    owner = models.CharField(
        max_length=255,
        blank=True,
        help_text="Team or person responsible for this table's data quality.",
    )
    refresh_frequency = models.CharField(
        max_length=100,
        blank=True,
        help_text='How often this data updates. E.g. "hourly", "daily at 3am UTC", "real-time"',
    )
    related_tables = models.JSONField(
        default=list,
        help_text='Tables commonly joined with this one. E.g. [{"table": "users", "join_hint": "orders.user_id = users.id"}]',
    )
    column_notes = models.JSONField(
        default=dict,
        help_text='Per-column notes. E.g. {"status": "Values: active, churned, trial"}',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        unique_together = ["workspace", "table_name"]
        ordering = ["table_name"]
        verbose_name_plural = "Table knowledge"

    def __str__(self):
        return f"{self.table_name} ({self.workspace})"


class KnowledgeEntry(models.Model):
    """
    General-purpose knowledge entry with title, markdown content, and tags.

    Replaces the previous CanonicalMetric, VerifiedQuery, and BusinessRule
    models with a single flexible model. Use tags to categorize entries
    (e.g. "metric", "query", "rule").
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="knowledge_entries",
        null=True,
        blank=True,
    )

    title = models.CharField(max_length=255)
    content = models.TextField(help_text="Markdown content for this knowledge entry.")
    tags = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name_plural = "Knowledge entries"

    def __str__(self):
        return f"{self.title} ({self.workspace})"


class AgentLearning(models.Model):
    """
    A workspace memory: a shared note about how this workspace's data should be
    combined or interpreted, injected into every member's chats (#849).

    Saved by the agent from a chat or added on the Memory page. Rows from before
    #849 are data-model corrections with a category and tables; neither is
    required now.
    """

    CATEGORY_CHOICES = [
        ("type_mismatch", "Column type mismatch"),
        ("filter_required", "Missing required filter"),
        ("join_pattern", "Correct join pattern"),
        ("aggregation", "Aggregation gotcha"),
        ("naming", "Column/table naming convention"),
        ("data_quality", "Data quality issue"),
        ("business_logic", "Business logic correction"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="learnings",
        null=True,
        blank=True,
    )

    description = models.TextField(
        help_text="Plain English description of the learning. This is what gets injected into the prompt."
    )
    category = models.CharField(
        max_length=50,
        choices=CATEGORY_CHOICES,
        default="other",
    )
    applies_to_tables = models.JSONField(
        default=list, blank=True, help_text="Tables this memory applies to, if any."
    )

    original_error = models.TextField(
        blank=True, help_text="The error message or suspicious result."
    )
    original_sql = models.TextField(
        blank=True, help_text="Disabled legacy field retained for old learning records."
    )
    corrected_sql = models.TextField(
        blank=True, help_text="Disabled legacy field retained for old learning records."
    )

    confidence_score = models.FloatField(
        default=0.5,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="0-1 score. Increases when the learning is confirmed useful, decreases if contradicted.",
    )
    times_applied = models.IntegerField(
        default=0, help_text="How many times this learning has been used."
    )
    is_active = models.BooleanField(default=True)

    discovered_in_conversation = models.CharField(max_length=255, blank=True)
    discovered_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-confidence_score", "-times_applied"]
        indexes = [
            models.Index(fields=["workspace", "is_active", "-confidence_score"]),
            models.Index(fields=["workspace", "is_active", "-created_at"]),
        ]

    def __str__(self):
        return f"Learning: {self.description[:80]}..."
