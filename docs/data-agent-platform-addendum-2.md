# Data Agent Platform — Spec Addendum 2: Knowledge Layer, Self-Learning & Evals

Inspired by OpenAI's in-house data agent ("Kepler") and the surrounding discussion. This
addendum identifies gaps in our current spec and proposes concrete changes.

---

## Summary of Changes

Our spec does a good job on the infrastructure layer (DB isolation, SQL validation, auth,
artifacts). But the OpenAI article reveals we're underinvesting in the **intelligence layer** —
the context, learning, and verification systems that make the agent actually trustworthy.
The HN discussion reinforces this: the hard problem isn't generating SQL, it's ensuring the
SQL is *correct* and that non-technical users can trust the results.

| Area | Current Spec | Gap | Proposed Change |
|------|-------------|-----|-----------------|
| Context | Auto-generated data dictionary (schema only) | No business semantics, no tribal knowledge | Add Knowledge Layer with 4 context types |
| Learning | None — agent starts fresh each conversation | Repeats same mistakes across sessions | Add self-learning loop with persistent learnings |
| Verification | None | No way to know if answers are correct | Add eval system with golden queries |
| Provenance | Shows SQL executed | No formal provenance chain | Add provenance tracking to every result |
| Metrics | None | Business terms undefined, "duelling dashboards" | Add canonical metric definitions |
| Self-correction | Basic LangGraph tool loop | Agent doesn't retry or investigate errors | Add explicit retry/correction node to graph |
| Query explanation | Shows SQL | Non-technical users can't read SQL | Add natural language query explanation |

---

## B1. Knowledge Layer

The data dictionary from the base spec gives the agent *structural* knowledge (tables, columns,
types). But OpenAI's insight is that you need multiple layers of *semantic* knowledge to avoid
"wrong join" mistakes and produce trustworthy results.

### Knowledge Types

Replace the single `data_dictionary` JSON field on the Project model with a richer knowledge
system. The data dictionary remains as auto-generated structural knowledge, but it's now one
layer among several.

```
knowledge/
├── tables/          # Table-level metadata (auto-generated + human-enriched)
├── metrics/         # Canonical metric definitions (human-curated)
├── queries/         # Verified query patterns (human-curated + agent-discovered)
├── business/        # Business rules, gotchas, institutional context
└── learnings/       # Agent-discovered error patterns (auto-generated)
```

### New Models

Add to `apps/knowledge/models.py`:

```python
import uuid
from django.db import models
from django.conf import settings


class TableKnowledge(models.Model):
    """
    Enriched table metadata beyond what the data dictionary provides.

    The data dictionary gives you columns and types. This model adds:
    - Human-written descriptions of what the table *means*
    - Use cases (what questions this table helps answer)
    - Data quality notes and gotchas
    - Ownership and freshness information
    - Relationships not captured by foreign keys

    This is the "Table Usage Metadata" and "Human Annotations" layers
    from OpenAI's architecture.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="table_knowledge")

    table_name = models.CharField(max_length=255)
    description = models.TextField(
        help_text="Human-written description of what this table represents and when to use it."
    )
    use_cases = models.JSONField(
        default=list,
        help_text='What questions this table helps answer. E.g. ["Revenue reporting", "User retention analysis"]'
    )
    data_quality_notes = models.JSONField(
        default=list,
        help_text='Known quirks. E.g. ["created_at is UTC", "amount is in cents not dollars", "status=deleted means soft-deleted"]'
    )
    owner = models.CharField(
        max_length=255, blank=True,
        help_text="Team or person responsible for this table's data quality."
    )
    refresh_frequency = models.CharField(
        max_length=100, blank=True,
        help_text='How often this data updates. E.g. "hourly", "daily at 3am UTC", "real-time"'
    )
    # Semantic relationships not captured by FKs
    related_tables = models.JSONField(
        default=list,
        help_text='Tables commonly joined with this one. E.g. [{"table": "users", "join_hint": "orders.user_id = users.id", "note": "Use for user demographics"}]'
    )
    # Important column-level annotations that go beyond the data dictionary
    column_notes = models.JSONField(
        default=dict,
        help_text='Per-column notes. E.g. {"status": "Values: active, churned, trial. NULL means legacy record.", "revenue": "Stored in cents. Divide by 100 for dollars."}'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        unique_together = ["project", "table_name"]
        ordering = ["table_name"]

    def __str__(self):
        return f"{self.table_name} ({self.project.name})"


class CanonicalMetric(models.Model):
    """
    An agreed-upon metric definition.

    This is the single source of truth for "what does MRR mean" or
    "how do we count active users". When the agent needs to compute
    a metric, it MUST use the canonical definition if one exists,
    rather than inventing its own.

    This directly addresses the "duelling dashboards" problem — when
    different teams compute the same metric differently.

    The metric includes a SQL template that the agent should use
    (or adapt) when computing this metric.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="canonical_metrics")

    name = models.CharField(max_length=255, help_text='Metric name. E.g. "MRR", "DAU", "Churn Rate"')
    definition = models.TextField(
        help_text="Plain English definition. E.g. 'Sum of active subscription amounts, excluding trials and refunds.'"
    )
    sql_template = models.TextField(
        help_text="The canonical SQL for computing this metric. May include {{date_range}} or other variables."
    )
    unit = models.CharField(max_length=50, blank=True, help_text='E.g. "USD", "users", "percentage"')
    owner = models.CharField(max_length=255, blank=True, help_text="Who owns the definition of this metric.")
    caveats = models.JSONField(
        default=list,
        help_text='Known limitations. E.g. ["Excludes enterprise contracts billed annually", "Lag of ~2 hours from real-time"]'
    )
    tags = models.JSONField(default=list, blank=True, help_text='E.g. ["finance", "growth", "product"]')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

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
    (or closely adapt) the verified pattern rather than generating from scratch.

    Verified queries can come from:
    - Human analysts who've confirmed correctness
    - The agent, after human review of a generated query
    - Recipes that have been validated

    This is the "Query Patterns" layer from the Dash/OpenAI architecture.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="verified_queries")

    name = models.CharField(max_length=255, help_text="Short name for this query pattern.")
    description = models.TextField(
        help_text="What question does this query answer? Written in natural language."
    )
    sql = models.TextField(help_text="The verified SQL query.")
    # Tags for retrieval
    tags = models.JSONField(default=list, blank=True)
    # Tables involved (for efficient lookup)
    tables_used = models.JSONField(default=list, help_text="List of table names this query uses.")

    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.project.name})"


class BusinessRule(models.Model):
    """
    Institutional knowledge that isn't captured in schema or metrics.

    Examples:
    - "In the APAC region, 'active user' means logged in within 7 days, not 30"
    - "The orders table has duplicate rows for Q1 2024 due to a migration bug"
    - "Revenue numbers before 2023 are in the legacy_revenue table, not orders"
    - "For CommCare projects, 'form submission' and 'case update' are different"

    These are the "gotchas" and tribal knowledge that save hours of debugging.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="business_rules")

    title = models.CharField(max_length=255)
    description = models.TextField()
    # Which tables/metrics this rule applies to
    applies_to_tables = models.JSONField(default=list, blank=True)
    applies_to_metrics = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return f"{self.title} ({self.project.name})"
```

### Knowledge Retrieval for Prompt Assembly

The agent needs the right knowledge at query time. For small projects (< 20 tables, < 50 knowledge items),
stuff everything into the system prompt. For larger projects, use retrieval.

```python
"""
apps/knowledge/services/retriever.py

Retrieves relevant knowledge for the agent based on the user's question.

For small projects: returns all knowledge (fits in context).
For larger projects: uses keyword matching on table names and embeddings
on descriptions/questions to find the most relevant subset.

The retriever assembles a "knowledge block" that gets injected into
the agent's system prompt alongside the data dictionary.
"""

class KnowledgeRetriever:
    """
    Builds the knowledge context block for a given user question.

    Strategy:
    1. Always include: canonical metrics, business rules (these are small and critical)
    2. For table knowledge: include all if < 20 tables, otherwise match on mentioned tables
    3. For verified queries: retrieve top-5 most relevant based on description similarity
    4. For learnings: retrieve all for mentioned tables

    Returns a formatted string for inclusion in the system prompt.
    """

    def __init__(self, project):
        self.project = project

    def retrieve(self, user_question: str = "") -> str:
        lines = []

        # 1. Canonical Metrics (always include — they're the source of truth)
        metrics = self.project.canonical_metrics.all()
        if metrics.exists():
            lines.append("## Canonical Metric Definitions")
            lines.append("IMPORTANT: When computing these metrics, you MUST use the canonical SQL below.")
            lines.append("Do NOT invent your own definition for these metrics.")
            lines.append("")
            for m in metrics:
                lines.append(f"### {m.name}")
                lines.append(f"Definition: {m.definition}")
                if m.unit:
                    lines.append(f"Unit: {m.unit}")
                if m.caveats:
                    lines.append(f"Caveats: {'; '.join(m.caveats)}")
                lines.append(f"Canonical SQL:\n```sql\n{m.sql_template}\n```")
                lines.append("")

        # 2. Business Rules (always include — they prevent expensive mistakes)
        rules = self.project.business_rules.all()
        if rules.exists():
            lines.append("## Business Rules & Gotchas")
            lines.append("These are critical institutional knowledge. Violating these rules")
            lines.append("will produce incorrect results.")
            lines.append("")
            for r in rules:
                lines.append(f"- **{r.title}**: {r.description}")
            lines.append("")

        # 3. Table Knowledge (enriched metadata beyond the data dictionary)
        table_knowledge = self.project.table_knowledge.all()
        if table_knowledge.exists():
            lines.append("## Table Context (beyond schema)")
            lines.append("")
            for tk in table_knowledge:
                lines.append(f"### {tk.table_name}")
                lines.append(tk.description)
                if tk.data_quality_notes:
                    lines.append("Data quality notes:")
                    for note in tk.data_quality_notes:
                        lines.append(f"  - {note}")
                if tk.column_notes:
                    lines.append("Column notes:")
                    for col, note in tk.column_notes.items():
                        lines.append(f"  - {col}: {note}")
                if tk.refresh_frequency:
                    lines.append(f"Data freshness: {tk.refresh_frequency}")
                lines.append("")

        # 4. Verified Query Patterns (top 5 most relevant)
        # For MVP: include all. For scale: use embedding similarity.
        verified = self.project.verified_queries.all()[:10]
        if verified.exists():
            lines.append("## Verified Query Patterns")
            lines.append("These queries are known to produce correct results.")
            lines.append("Prefer adapting these patterns over writing from scratch.")
            lines.append("")
            for vq in verified:
                lines.append(f"### {vq.name}")
                lines.append(f"Question: {vq.description}")
                lines.append(f"```sql\n{vq.sql}\n```")
                lines.append("")

        # 5. Learnings (agent-discovered corrections — see B2)
        learnings = AgentLearning.objects.filter(
            project=self.project, is_active=True
        ).order_by("-confidence_score")[:20]
        if learnings.exists():
            lines.append("## Learned Corrections")
            lines.append("These are patterns discovered from previous errors. Apply them.")
            lines.append("")
            for l in learnings:
                lines.append(f"- {l.description}")
            lines.append("")

        return "\n".join(lines)
```

---

## B2. Self-Learning Loop

This is the biggest conceptual gap in our spec. OpenAI's agent improves over time without
retraining — it saves corrections from errors and applies them to future queries. The Dash
project calls this "GPU-poor continuous learning."

### How It Works

```
User Question
     ↓
Retrieve Knowledge + Learnings ←──────────────────┐
     ↓                                              │
Generate SQL (grounded in knowledge)                │
     ↓                                              │
Execute Query                                       │
     ↓                                              │
 ┌───┴────┐                                         │
 ↓        ↓                                         │
Success   Error/Suspicious Result                   │
 ↓        ↓                                         │
 ↓        Diagnose → Self-correct → Retry           │
 ↓        ↓                                         │
 ↓        If fix found: Save as Learning ───────────┘
 ↓        (so the same error never happens again)
 ↓
Return result with provenance
```

### Learning Model

```python
class AgentLearning(models.Model):
    """
    A correction the agent discovered through trial and error.

    When a query fails or produces suspicious results, the agent
    investigates, fixes the issue, and saves the pattern so it
    doesn't repeat the same mistake.

    Examples:
    - "The 'position' column in the results table is TEXT, not INTEGER. Cast before comparing."
    - "The orders table uses soft deletes. Always add WHERE deleted_at IS NULL."
    - "Date columns in the events table are stored as epoch milliseconds, not timestamps."
    - "user_count in the summary table is a running total, not a daily count. Use the raw events table for daily counts."

    Learnings are automatically injected into the agent's context via the KnowledgeRetriever.
    They can be reviewed and promoted to verified knowledge by admins.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="learnings")

    # What the agent learned
    description = models.TextField(
        help_text="Plain English description of the learning. This is what gets injected into the prompt."
    )
    # Structured metadata for the learning
    category = models.CharField(
        max_length=50,
        choices=[
            ("type_mismatch", "Column type mismatch"),
            ("filter_required", "Missing required filter"),
            ("join_pattern", "Correct join pattern"),
            ("aggregation", "Aggregation gotcha"),
            ("naming", "Column/table naming convention"),
            ("data_quality", "Data quality issue"),
            ("business_logic", "Business logic correction"),
            ("other", "Other"),
        ],
        default="other",
    )
    applies_to_tables = models.JSONField(default=list, help_text="Tables this learning applies to.")

    # Evidence: what triggered this learning
    original_error = models.TextField(blank=True, help_text="The error message or suspicious result.")
    original_sql = models.TextField(blank=True, help_text="The SQL that failed.")
    corrected_sql = models.TextField(blank=True, help_text="The SQL that worked.")

    # Confidence and lifecycle
    confidence_score = models.FloatField(
        default=0.5,
        help_text="0-1 score. Increases when the learning is confirmed useful, decreases if contradicted."
    )
    times_applied = models.IntegerField(default=0, help_text="How many times this learning has been used.")
    is_active = models.BooleanField(default=True)

    # Can be promoted to a BusinessRule or VerifiedQuery by an admin
    promoted_to = models.CharField(
        max_length=50, blank=True,
        choices=[("business_rule", "Business Rule"), ("verified_query", "Verified Query")]
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
```

### Self-Correction Node in LangGraph

Add a retry/correction node to the agent graph. Instead of the simple `agent → tools → agent` loop,
add an explicit error handling path:

```python
"""
Updated agent graph with self-correction.

    START → agent → should_continue? → tools → check_result → agent → ...
                  └→ END                       ↓
                                           result_ok? ──no──→ diagnose → retry (up to 3x)
                                               │                           ↓
                                              yes                    save_learning
                                               ↓
                                             agent
"""

def check_result_node(state: AgentState):
    """
    Examine the tool result for errors or suspicious patterns.

    Checks for:
    - SQL execution errors → trigger diagnosis
    - Empty results when data is expected → suggest broader query
    - Suspiciously round numbers → may indicate aggregation error
    - Results that contradict known metrics → flag discrepancy
    """
    last_message = state["messages"][-1]

    # Parse tool results
    if hasattr(last_message, "content"):
        try:
            result = json.loads(last_message.content)
        except (json.JSONDecodeError, TypeError):
            return state  # Not a tool result, pass through

        if "error" in result:
            return {
                **state,
                "needs_correction": True,
                "correction_context": {
                    "error": result["error"],
                    "retry_count": state.get("retry_count", 0),
                }
            }

    return {**state, "needs_correction": False}


def diagnose_and_retry_node(state: AgentState):
    """
    When a query fails, ask the agent to diagnose and fix the issue.
    Inject the error context and any relevant learnings.
    """
    correction_context = state["correction_context"]
    retry_count = correction_context.get("retry_count", 0)

    if retry_count >= 3:
        # Give up after 3 retries, explain the issue to the user
        return {
            **state,
            "messages": state["messages"] + [
                SystemMessage(content=(
                    "You've tried to fix this query 3 times without success. "
                    "Explain the issue to the user clearly and suggest an alternative approach."
                ))
            ],
            "needs_correction": False,
        }

    diagnosis_prompt = f"""Your previous query encountered an error:
{correction_context['error']}

Please:
1. Diagnose what went wrong
2. Check if any of the learned corrections or business rules in your context apply
3. Generate a corrected query
4. Explain what you changed and why

This is retry {retry_count + 1} of 3."""

    return {
        **state,
        "messages": state["messages"] + [SystemMessage(content=diagnosis_prompt)],
        "retry_count": retry_count + 1,
        "needs_correction": False,
    }


def save_learning_node(state: AgentState):
    """
    After a successful correction, save the pattern as a learning.
    Called when: a query failed, was corrected, and the correction succeeded.

    The agent generates the learning description as part of its correction response.
    This node extracts it and persists it.
    """
    # This is handled by a tool the agent can call:
    # save_learning(description, category, tables, original_sql, corrected_sql)
    # See the save_learning tool below.
    return state
```

### Save Learning Tool

```python
@tool
def save_learning(
    description: str,
    category: str,
    tables: list[str],
    original_sql: str = "",
    corrected_sql: str = "",
) -> dict:
    """Save a learned correction for future queries.

    Call this AFTER you've successfully corrected a query error.
    The learning will be applied to future queries automatically,
    preventing the same mistake from recurring.

    Args:
        description: Clear, actionable description of the learning.
            Good: "The events.timestamp column is epoch milliseconds, not a PostgreSQL timestamp. Use to_timestamp(timestamp/1000) to convert."
            Bad: "The timestamp column is wrong."
        category: One of: type_mismatch, filter_required, join_pattern,
                  aggregation, naming, data_quality, business_logic, other.
        tables: Which tables this learning applies to.
        original_sql: The SQL that failed (for reference).
        corrected_sql: The SQL that worked (for reference).
    """
    from apps.knowledge.models import AgentLearning

    learning = AgentLearning.objects.create(
        project=project,
        description=description,
        category=category,
        applies_to_tables=tables,
        original_sql=original_sql,
        corrected_sql=corrected_sql,
        discovered_by_user=user,
    )

    return {
        "learning_id": str(learning.id),
        "status": "saved",
        "message": f"Learning saved. This correction will be applied to future queries involving {', '.join(tables)}.",
    }
```

---

## B3. Evaluation System

OpenAI uses their Evals API to unit-test the agent's answers against golden sets. Without
this, you have no way to know if a prompt change, model upgrade, or knowledge update
improves or breaks accuracy.

### Golden Query Model

```python
class GoldenQuery(models.Model):
    """
    A test case for evaluating agent accuracy.

    Each golden query represents a question with a known-correct answer.
    The eval system asks the agent the question, compares the result
    against the expected answer, and reports accuracy.

    Golden queries are the "ground truth" that prevents regressions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="golden_queries")

    # The test case
    question = models.TextField(help_text="The natural language question to ask the agent.")
    expected_sql = models.TextField(
        blank=True,
        help_text="Optional: the expected SQL (for structural comparison)."
    )
    expected_result = models.JSONField(
        help_text="The expected result. Can be exact values, ranges, or patterns."
    )
    # How to compare results
    comparison_mode = models.CharField(
        max_length=20,
        choices=[
            ("exact", "Exact match on values"),
            ("approximate", "Values within tolerance"),
            ("row_count", "Correct number of rows"),
            ("contains", "Result contains expected values"),
            ("structure", "Correct columns and types"),
        ],
        default="exact",
    )
    tolerance = models.FloatField(
        default=0.01,
        help_text="For approximate comparison: relative tolerance (0.01 = 1%)."
    )

    # Categorization
    difficulty = models.CharField(
        max_length=20,
        choices=[("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard")],
        default="medium",
    )
    tags = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["difficulty", "question"]

    def __str__(self):
        return f"[{self.difficulty}] {self.question[:80]}..."


class EvalRun(models.Model):
    """
    A single evaluation run across all golden queries for a project.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="eval_runs")

    # Configuration snapshot
    model_used = models.CharField(max_length=100)
    knowledge_snapshot = models.JSONField(
        default=dict,
        help_text="Snapshot of knowledge state at eval time (counts, last modified)."
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
        help_text="List of {golden_query_id, passed, expected, actual, error, latency_ms}"
    )

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Eval {self.started_at}: {self.accuracy:.0%} ({self.passed}/{self.total_queries})"
```

### Management Command

```python
"""
apps/knowledge/management/commands/run_eval.py

Usage:
    python manage.py run_eval --project-slug my-project
    python manage.py run_eval --project-slug my-project --tag finance
    python manage.py run_eval --project-slug my-project --difficulty easy
"""
```

---

## B4. Provenance Tracking

Every answer the agent gives should be traceable back to the data source, the query used,
and the knowledge that informed it. This is especially important for non-technical users
who can't verify SQL themselves.

### What Provenance Looks Like in Practice

When the agent returns a result, it should include:

```
## Answer
Monthly revenue for Q4 2024 was $2.3M, up 15% from Q3.

## How This Was Computed
- **Metric used**: MRR (canonical definition by Finance team)
- **Tables queried**: orders, subscriptions
- **Filters applied**: status = 'completed', date between 2024-10-01 and 2024-12-31
- **Data freshness**: orders table last updated 2 hours ago
- **Query**: [expandable SQL block]
- **Caveats**: Excludes enterprise contracts billed annually (per canonical metric definition)
```

### Implementation

Add a `provenance` field to the SQL tool response:

```python
# In the execute_sql tool, after successful execution:
return {
    "columns": columns,
    "rows": rows,
    "row_count": len(rows),
    "truncated": len(rows) >= project.max_rows_per_query,
    "sql_executed": validated_sql,
    # NEW: provenance metadata
    "provenance": {
        "tables_accessed": extract_tables(validated_sql),
        "metric_used": metric_name if canonical_metric_applied else None,
        "data_freshness": get_table_freshness(project, tables),
        "knowledge_applied": [
            k.description for k in applied_knowledge
        ],
        "learnings_applied": [
            l.description for l in applied_learnings
        ],
        "caveats": collect_caveats(project, tables, metric_name),
    }
}
```

Add to the base system prompt:

```
## Provenance Requirements
- Always explain HOW you computed the answer, not just the answer itself.
- State which tables you queried and what filters you applied.
- If you used a canonical metric, name it and note any caveats.
- If the data has known freshness limitations, mention them.
- For non-technical users, explain the query logic in plain English.
- Show the SQL in an expandable block — available but not in the way.
```

---

## B5. Natural Language Query Explanation

The HN discussion highlights that non-technical users can't read SQL to verify results.
Add a prompt instruction and tool capability for the agent to explain queries in plain English.

Add to the base system prompt:

```
## Query Explanations
After executing a query, always provide a plain English explanation of what the query does.
This should be understandable by someone who doesn't know SQL.

Example:
"I looked at all completed orders from October through December 2024,
grouped them by month, and calculated the total revenue for each month.
I excluded refunded orders and trial subscriptions per our standard
revenue definition."

This explanation is as important as the result itself — it lets users verify
that you answered the right question.
```

---

## B6. Updated Project Structure

```
data-agent-platform/
├── apps/
│   ├── projects/              # (unchanged)
│   ├── agents/                # (updated graph with self-correction)
│   ├── artifacts/             # (from addendum 1)
│   ├── recipes/               # (from addendum 1)
│   ├── knowledge/             # NEW
│   │   ├── models.py          # TableKnowledge, CanonicalMetric, VerifiedQuery,
│   │   │                      #   BusinessRule, AgentLearning, GoldenQuery, EvalRun
│   │   ├── admin.py           # Admin UI for knowledge curation
│   │   ├── services/
│   │   │   ├── retriever.py   # KnowledgeRetriever
│   │   │   └── eval_runner.py # EvalRunner
│   │   ├── management/
│   │   │   └── commands/
│   │   │       ├── run_eval.py
│   │   │       └── import_knowledge.py  # Bulk import from JSON/YAML files
│   │   ├── api/
│   │   │   ├── serializers.py
│   │   │   └── views.py
│   │   └── migrations/
│   └── users/                 # (updated with OAuth from addendum 1)
```

---

## B7. Updated Implementation Phases

These changes slot into the existing phases:

### Phase 1: Foundation (Week 1) — add knowledge models
- Add items 1-6 from base spec (unchanged)
- **NEW**: Add knowledge models (TableKnowledge, CanonicalMetric, VerifiedQuery, BusinessRule)
- **NEW**: Add knowledge admin interface
- **NEW**: Add `import_knowledge` management command (bulk load from JSON/YAML)

### Phase 2: Agent Core (Week 2) — add knowledge retrieval + self-correction
- Items 7-12 from base spec (unchanged)
- **NEW**: KnowledgeRetriever service
- **NEW**: Update system prompt assembly to include knowledge context
- **NEW**: Add self-correction node to LangGraph agent
- **NEW**: AgentLearning model and save_learning tool
- **NEW**: Add provenance metadata to SQL tool response

### Phase 3: Frontend + Artifacts (Week 3) — unchanged from addendum 1

### Phase 4: Auth & Sharing (Week 4) — unchanged from addendum 1

### Phase 5: Recipes (Week 5) — unchanged from addendum 1

### Phase 6: Evals & Polish (Week 6) — add eval system
- Items from base spec phase 4 (connection pooling, Docker, etc.)
- **NEW**: GoldenQuery and EvalRun models
- **NEW**: `run_eval` management command
- **NEW**: Eval results dashboard (simple Django admin view or Chainlit page)
- **NEW**: Knowledge curation workflow (promote learnings → business rules / verified queries)

---

## B8. Key Design Decisions — Addendum 2

### Why a knowledge layer instead of just a better data dictionary?

The data dictionary tells you *what* exists. The knowledge layer tells you *what it means*,
*how to use it correctly*, and *what to watch out for*. The HN discussion makes this very clear:
the companies seeing success with data agents (Veezoo, Graphed, Amplitude) all emphasize that
a semantic layer is the foundation. Without it, the agent writes technically valid SQL that
produces the wrong answer because it doesn't understand the business context.

### Why learnings over fine-tuning?

Fine-tuning is expensive, requires data pipelines, and locks you to a specific model. Learnings
are just text that gets injected into the prompt — they work with any model, can be reviewed by
humans, and take effect immediately. This is the "GPU-poor continuous learning" approach from Dash.
It's pragmatic and it works.

### Why golden queries for evals instead of LLM-as-judge?

When the CFO asks for revenue, the number needs to be exactly right — not "judged as reasonable
by another LLM." Golden queries compare computed results against known-correct values
deterministically. This is the same approach OpenAI uses internally with their Evals API.
LLM-as-judge can supplement this for narrative quality, but the numbers must be verified
against ground truth.

### Why include canonical metrics when the agent can just query the database?

Because two analysts computing "active users" will get different numbers if they use different
definitions (7-day vs 30-day window, including vs excluding bots, etc). Canonical metrics are
the "single source of truth" that eliminates this ambiguity. The agent is instructed to
use the canonical SQL when a matching metric exists, ensuring consistency across all users
and conversations.

### Why not use vector embeddings for knowledge retrieval from the start?

For most projects (< 50 tables, < 100 knowledge items), everything fits in the context window
and keyword matching works fine. Vector embeddings add complexity (embedding model, vector DB,
chunking strategy) for marginal benefit at small scale. The retriever is designed with a clean
interface so embeddings can be swapped in later when a project outgrows the simple approach.
