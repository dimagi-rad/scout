# Knowledge

The knowledge layer provides semantic context that goes beyond the auto-generated data dictionary. It helps the agent understand what the data *means*, not just what columns exist.

## Knowledge types

### Table knowledge

Enriched metadata for individual tables:

- **Description** -- human-written explanation of what the table represents and when to use it.
- **Use cases** -- what questions this table helps answer (e.g., "Revenue reporting", "User retention analysis").
- **Data quality notes** -- known quirks (e.g., "created_at is UTC", "amount is in cents not dollars").
- **Owner** -- team or person responsible for the data quality.
- **Refresh frequency** -- how often the data updates (e.g., "hourly", "daily at 3am UTC").
- **Related tables** -- tables commonly joined with this one, with join hints.
- **Column notes** -- per-column annotations (e.g., `status` values: active, churned, trial).

### Canonical metrics

Agreed-upon definitions for business metrics. When the agent needs to compute a metric, it uses the canonical definition if one exists.

Each metric includes:

- **Name** -- e.g., "MRR", "DAU", "Churn Rate".
- **Definition** -- plain English explanation.
- **SQL template** -- the canonical SQL for computing the metric. May include `{{date_range}}` or other variables.
- **Unit** -- e.g., "USD", "users", "percentage".
- **Caveats** -- known limitations (e.g., "Excludes enterprise contracts billed annually").
- **Tags** -- categorization (e.g., "finance", "growth", "product").

### Verified queries

Query patterns known to produce correct results. When a user asks a question similar to one covered by a verified query, the agent uses or closely adapts the verified pattern.

Each verified query includes:

- **Name** -- short identifier.
- **Description** -- what question it answers, in natural language.
- **SQL** -- the verified query.
- **Tables used** -- for efficient lookup.
- **Verified by / at** -- who verified it and when.

### Business rules

Institutional knowledge that isn't captured in the schema or metrics:

- "In the APAC region, 'active user' means logged in within 7 days, not 30."
- "The orders table has duplicate rows for Q1 2024 due to a migration bug."
- "Revenue numbers before 2023 are in the `legacy_revenue` table."

Each rule can specify which tables and metrics it applies to.

### Agent learnings

Corrections the agent discovers through trial and error. When a query fails, the agent investigates, fixes the issue, and saves the pattern so it doesn't repeat the mistake.

Learnings include:

- **Category** -- type mismatch, missing filter, join pattern, aggregation gotcha, naming convention, data quality issue, or business logic correction.
- **Original error** -- what went wrong.
- **Original SQL / Corrected SQL** -- the before and after.
- **Confidence score** -- increases when the learning is confirmed useful, decreases if contradicted.

Learnings can be **promoted** to business rules or verified queries by an admin when they've proven reliable.

## Managing knowledge

All knowledge types are managed through the **Knowledge** page in the sidebar. Use the type filter to switch between metrics, business rules, verified queries, and agent learnings.

### Evaluation

Scout includes an evaluation system using **golden queries** -- test cases with known-correct answers. The `run_eval` management command runs all golden queries for a project and reports accuracy:

```bash
uv run manage.py run_eval --project-slug my-project
```

Use evaluations to measure how well the knowledge layer is helping the agent produce correct results.

## Importing knowledge

Bulk-import knowledge from a YAML or JSON file:

```bash
uv run manage.py import_knowledge --project-slug my-project --file knowledge.yaml
```
