"""
Knowledge layer models for Scout data agent platform.

Provides semantic knowledge beyond the auto-generated data dictionary:
- TableKnowledge: Enriched table metadata
- CanonicalMetric: Agreed-upon metric definitions
- VerifiedQuery: Query patterns known to produce correct results
- BusinessRule: Institutional knowledge and gotchas
- AgentLearning: Agent-discovered corrections
- GoldenQuery: Test cases for evaluation
- EvalRun: Evaluation run results
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
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="table_knowledge"
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
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        unique_together = ["project", "table_name"]
        ordering = ["table_name"]
        verbose_name_plural = "Table knowledge"

    def __str__(self):
        return f"{self.table_name} ({self.project.name})"


class CanonicalMetric(models.Model):
    """
    An agreed-upon metric definition.

    This is the single source of truth for "what does MRR mean" or
    "how do we count active users". When the agent needs to compute
    a metric, it MUST use the canonical definition if one exists.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="canonical_metrics"
    )

    name = models.CharField(
        max_length=255, help_text='Metric name. E.g. "MRR", "DAU", "Churn Rate"'
    )
    definition = models.TextField(
        help_text="Plain English definition. E.g. 'Sum of active subscription amounts, excluding trials.'"
    )
    sql_template = models.TextField(
        help_text="The canonical SQL for computing this metric. May include {{date_range}} or other variables."
    )
    unit = models.CharField(
        max_length=50, blank=True, help_text='E.g. "USD", "users", "percentage"'
    )
    owner = models.CharField(
        max_length=255, blank=True, help_text="Who owns the definition of this metric."
    )
    caveats = models.JSONField(
        default=list,
        help_text='Known limitations. E.g. ["Excludes enterprise contracts billed annually"]',
    )
    tags = models.JSONField(
        default=list, blank=True, help_text='E.g. ["finance", "growth", "product"]'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        unique_together = ["project", "name"]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.project.name})"


class VerifiedQuery(models.Model):
    """
    A query pattern that is known to produce correct results.

    These serve as examples for the agent — when a user asks a question
    similar to one covered by a verified query, the agent should use
    (or closely adapt) the verified pattern.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="verified_queries"
    )

    name = models.CharField(max_length=255, help_text="Short name for this query pattern.")
    description = models.TextField(
        help_text="What question does this query answer? Written in natural language."
    )
    sql = models.TextField(help_text="The verified SQL query.")
    # Tags for retrieval
    tags = models.JSONField(default=list, blank=True)
    # Tables involved (for efficient lookup)
    tables_used = models.JSONField(
        default=list, help_text="List of table names this query uses."
    )

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Verified queries"

    def __str__(self):
        return f"{self.name} ({self.project.name})"


class BusinessRule(models.Model):
    """
    Institutional knowledge that isn't captured in schema or metrics.

    Examples:
    - "In the APAC region, 'active user' means logged in within 7 days, not 30"
    - "The orders table has duplicate rows for Q1 2024 due to a migration bug"
    - "Revenue numbers before 2023 are in the legacy_revenue table"
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="business_rules"
    )

    title = models.CharField(max_length=255)
    description = models.TextField()
    # Which tables/metrics this rule applies to
    applies_to_tables = models.JSONField(default=list, blank=True)
    applies_to_metrics = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return f"{self.title} ({self.project.name})"


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
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="learnings"
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
    applies_to_tables = models.JSONField(
        default=list, help_text="Tables this learning applies to."
    )

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

    # Can be promoted to a BusinessRule or VerifiedQuery by an admin
    promoted_to = models.CharField(
        max_length=50,
        blank=True,
        choices=[
            ("business_rule", "Business Rule"),
            ("verified_query", "Verified Query"),
        ],
    )
    promoted_to_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="UUID of the promoted BusinessRule or VerifiedQuery.",
    )

    # Source
    discovered_in_conversation = models.CharField(max_length=255, blank=True)
    discovered_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-confidence_score", "-times_applied"]
        indexes = [
            models.Index(fields=["project", "is_active", "-confidence_score"]),
        ]

    def __str__(self):
        return f"Learning: {self.description[:80]}..."

    def increase_confidence(self, amount: float = 0.1) -> float:
        """
        Increase the confidence score, capping at 1.0.

        Args:
            amount: The amount to increase (default 0.1)

        Returns:
            The new confidence score
        """
        self.confidence_score = min(1.0, self.confidence_score + amount)
        self.save(update_fields=["confidence_score"])
        return self.confidence_score

    def decrease_confidence(self, amount: float = 0.1) -> float:
        """
        Decrease the confidence score, flooring at 0.0.

        Args:
            amount: The amount to decrease (default 0.1)

        Returns:
            The new confidence score
        """
        self.confidence_score = max(0.0, self.confidence_score - amount)
        self.save(update_fields=["confidence_score"])
        return self.confidence_score

    def promote_to_business_rule(self, user=None) -> "BusinessRule":
        """
        Promote this learning to a BusinessRule.

        Creates a new BusinessRule based on this learning's content,
        marks this learning as promoted, and deactivates it.

        Args:
            user: The user performing the promotion (optional)

        Returns:
            The created BusinessRule instance
        """
        if self.promoted_to:
            raise ValueError(
                f"This learning has already been promoted to {self.promoted_to}"
            )

        business_rule = BusinessRule.objects.create(
            project=self.project,
            title=self.description[:255],
            description=self._build_promotion_description(),
            applies_to_tables=self.applies_to_tables,
            tags=[self.category] if self.category != "other" else [],
            created_by=user,
        )

        self.promoted_to = "business_rule"
        self.promoted_to_id = business_rule.id
        self.is_active = False
        self.save(update_fields=["promoted_to", "promoted_to_id", "is_active"])

        return business_rule

    def promote_to_verified_query(self, user=None) -> "VerifiedQuery":
        """
        Promote this learning to a VerifiedQuery.

        Requires that corrected_sql is present. Creates a new VerifiedQuery
        based on this learning's corrected SQL, marks this learning as
        promoted, and deactivates it.

        Args:
            user: The user performing the promotion (optional)

        Returns:
            The created VerifiedQuery instance

        Raises:
            ValueError: If no corrected_sql is available
        """
        if self.promoted_to:
            raise ValueError(
                f"This learning has already been promoted to {self.promoted_to}"
            )

        if not self.corrected_sql:
            raise ValueError(
                "Cannot promote to VerifiedQuery: no corrected_sql available"
            )

        from django.utils import timezone

        verified_query = VerifiedQuery.objects.create(
            project=self.project,
            name=self.description[:255],
            description=self._build_promotion_description(),
            sql=self.corrected_sql,
            tables_used=self.applies_to_tables,
            tags=[self.category] if self.category != "other" else [],
            verified_by=user,
            verified_at=timezone.now(),
        )

        self.promoted_to = "verified_query"
        self.promoted_to_id = verified_query.id
        self.is_active = False
        self.save(update_fields=["promoted_to", "promoted_to_id", "is_active"])

        return verified_query

    def _build_promotion_description(self) -> str:
        """Build a detailed description for promoted items."""
        parts = [self.description]

        if self.original_error:
            parts.append(f"\n\nOriginal Error:\n{self.original_error}")

        if self.original_sql:
            parts.append(f"\n\nOriginal SQL:\n{self.original_sql}")

        if self.corrected_sql:
            parts.append(f"\n\nCorrected SQL:\n{self.corrected_sql}")

        return "".join(parts)


class GoldenQuery(models.Model):
    """
    A test case for evaluating agent accuracy.

    Each golden query represents a question with a known-correct answer.
    The eval system asks the agent the question, compares the result
    against the expected answer, and reports accuracy.
    """

    COMPARISON_MODE_CHOICES = [
        ("exact", "Exact match on values"),
        ("approximate", "Values within tolerance"),
        ("row_count", "Correct number of rows"),
        ("contains", "Result contains expected values"),
        ("structure", "Correct columns and types"),
    ]

    DIFFICULTY_CHOICES = [
        ("easy", "Easy"),
        ("medium", "Medium"),
        ("hard", "Hard"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="golden_queries"
    )

    # The test case
    question = models.TextField(
        help_text="The natural language question to ask the agent."
    )
    expected_sql = models.TextField(
        blank=True,
        help_text="Optional: the expected SQL (for structural comparison).",
    )
    expected_result = models.JSONField(
        help_text="The expected result. Can be exact values, ranges, or patterns."
    )
    # How to compare results
    comparison_mode = models.CharField(
        max_length=20,
        choices=COMPARISON_MODE_CHOICES,
        default="exact",
    )
    tolerance = models.FloatField(
        default=0.01,
        help_text="For approximate comparison: relative tolerance (0.01 = 1%).",
    )

    # Categorization
    difficulty = models.CharField(
        max_length=20,
        choices=DIFFICULTY_CHOICES,
        default="medium",
    )
    tags = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["difficulty", "question"]
        verbose_name_plural = "Golden queries"

    def __str__(self):
        return f"[{self.difficulty}] {self.question[:80]}..."


class EvalRun(models.Model):
    """
    A single evaluation run across all golden queries for a project.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="eval_runs"
    )

    # Configuration snapshot
    model_used = models.CharField(max_length=100)
    knowledge_snapshot = models.JSONField(
        default=dict,
        help_text="Snapshot of knowledge state at eval time (counts, last modified).",
    )

    # Results
    total_queries = models.IntegerField(default=0)
    passed = models.IntegerField(default=0)
    failed = models.IntegerField(default=0)
    errored = models.IntegerField(default=0)
    accuracy = models.FloatField(default=0.0)

    # Per-query results
    results = models.JSONField(
        default=list,
        help_text="List of {golden_query_id, passed, expected, actual, error, latency_ms}",
    )

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Eval {self.started_at}: {self.accuracy:.0%} ({self.passed}/{self.total_queries})"
