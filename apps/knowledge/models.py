"""
Knowledge layer models for Scout data agent platform.

Provides semantic knowledge beyond the auto-generated data dictionary:
- TableKnowledge: Enriched table metadata
- KnowledgeEntry: General-purpose knowledge (title + markdown + tags)
- AgentLearning: Agent-discovered corrections
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
        "projects.TenantWorkspace",
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
    # Semantic relationships not captured by FKs
    related_tables = models.JSONField(
        default=list,
        help_text='Tables commonly joined with this one. E.g. [{"table": "users", "join_hint": "orders.user_id = users.id"}]',
    )
    # Important column-level annotations
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
        return f"{self.table_name} ({self.workspace.tenant_name})"


class KnowledgeEntry(models.Model):
    """
    General-purpose knowledge entry with title, markdown content, and tags.

    Replaces the previous CanonicalMetric, VerifiedQuery, and BusinessRule
    models with a single flexible model. Use tags to categorize entries
    (e.g. "metric", "query", "rule").
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "projects.TenantWorkspace",
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
        return f"{self.title} ({self.workspace.tenant_name})"


class AgentLearning(models.Model):
    """
    A correction the agent discovered through trial and error.

    When a query fails or produces suspicious results, the agent
    investigates, fixes the issue, and saves the pattern so it
    doesn't repeat the same mistake.
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
        "projects.TenantWorkspace",
        on_delete=models.CASCADE,
        related_name="learnings",
        null=True,
        blank=True,
    )

    # What the agent learned
    description = models.TextField(
        help_text="Plain English description of the learning. This is what gets injected into the prompt."
    )
    category = models.CharField(
        max_length=50,
        choices=CATEGORY_CHOICES,
        default="other",
    )
    applies_to_tables = models.JSONField(default=list, help_text="Tables this learning applies to.")

    # Evidence: what triggered this learning
    original_error = models.TextField(
        blank=True, help_text="The error message or suspicious result."
    )
    original_sql = models.TextField(blank=True, help_text="The SQL that failed.")
    corrected_sql = models.TextField(blank=True, help_text="The SQL that worked.")

    # Confidence and lifecycle
    confidence_score = models.FloatField(
        default=0.5,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="0-1 score. Increases when the learning is confirmed useful, decreases if contradicted.",
    )
    times_applied = models.IntegerField(
        default=0, help_text="How many times this learning has been used."
    )
    is_active = models.BooleanField(default=True)

    # Source
    discovered_in_conversation = models.CharField(max_length=255, blank=True)
    discovered_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-confidence_score", "-times_applied"]
        indexes = [
            models.Index(fields=["workspace", "is_active", "-confidence_score"]),
        ]

    def __str__(self):
        return f"Learning: {self.description[:80]}..."

    def increase_confidence(self, amount: float = 0.1) -> float:
        """Increase the confidence score, capping at 1.0."""
        self.confidence_score = min(1.0, self.confidence_score + amount)
        self.save(update_fields=["confidence_score"])
        return self.confidence_score

    def decrease_confidence(self, amount: float = 0.1) -> float:
        """Decrease the confidence score, flooring at 0.0."""
        self.confidence_score = max(0.0, self.confidence_score - amount)
        self.save(update_fields=["confidence_score"])
        return self.confidence_score
