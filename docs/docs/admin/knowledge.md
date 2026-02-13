# Knowledge

The knowledge layer provides semantic context that goes beyond the auto-generated data dictionary. It helps the agent understand what the data *means*, not just what columns exist.

## Knowledge types

### Knowledge entries

Flexible markdown documents that capture any kind of institutional knowledge. Each entry has a **title**, **content** (markdown), and **tags** for categorization.

Common uses:

- **Metric definitions** -- canonical SQL for computing MRR, DAU, churn rate, etc. Tag with `metric`.
- **Business rules** -- institutional gotchas like "amounts are in cents, not dollars" or "APAC active users means 7-day window". Tag with `rule`.
- **Verified queries** -- query patterns known to produce correct results. Tag with `query`.
- **Domain glossary** -- definitions for domain-specific terms.
- **Data quality notes** -- known issues like "duplicate rows in Q1 2024 due to migration bug".

Example entry:

```markdown
Title: MRR (Monthly Recurring Revenue)
Tags: metric, finance

Definition: Sum of active subscription amounts, excluding annual contracts billed upfront.

SQL:
    SELECT SUM(amount) FROM subscriptions WHERE status = 'active'

Unit: USD
Caveats:
- Excludes enterprise contracts billed annually.
- Amounts are in cents.
```

### Table knowledge

Enriched metadata for individual tables:

- **Description** -- human-written explanation of what the table represents and when to use it.
- **Use cases** -- what questions this table helps answer (e.g., "Revenue reporting", "User retention analysis").
- **Data quality notes** -- known quirks (e.g., "created_at is UTC", "amount is in cents not dollars").
- **Owner** -- team or person responsible for the data quality.
- **Refresh frequency** -- how often the data updates (e.g., "hourly", "daily at 3am UTC").
- **Related tables** -- tables commonly joined with this one, with join hints.
- **Column notes** -- per-column annotations (e.g., `status` values: active, churned, trial).

Table knowledge is managed through the [data dictionary](data-dictionary.md) annotation system rather than the Knowledge page.

### Agent learnings

Corrections the agent discovers through trial and error. When a query fails, the agent investigates, fixes the issue, and saves the pattern so it doesn't repeat the mistake.

Learnings include:

- **Category** -- type mismatch, missing filter, join pattern, aggregation gotcha, naming convention, data quality issue, or business logic correction.
- **Original error** -- what went wrong.
- **Original SQL / Corrected SQL** -- the before and after.
- **Confidence score** -- increases when the learning is confirmed useful, decreases if contradicted.

Learnings are fully editable and deletable from the Knowledge page. Edit the description, category, or associated tables to refine them over time.

## Managing knowledge

Knowledge entries and agent learnings are managed through the **Knowledge** page in the sidebar. Use the type filter to switch between entries and learnings.

### Creating entries

Click **New** to create a knowledge entry. Fill in the title, markdown content, and optional comma-separated tags.

### Editing

Click **Edit** on any item. Both entries and learnings can be edited. For learnings, the description, category, and associated tables are editable while the original error, SQL, and confidence score are shown read-only for reference.

### Deleting

Click **Delete** on any item to remove it. A confirmation dialog appears before deletion.

## Import and export

### Exporting

Click **Export** to download the project's knowledge entries as a zip file. Each entry becomes a markdown file with YAML frontmatter:

```markdown
---
title: MRR (Monthly Recurring Revenue)
tags:
  - metric
  - finance
---
Definition: Sum of active subscription amounts...
```

### Importing

Click **Import** to upload a zip file of markdown files. Each `.md` file in the zip is parsed and created as a knowledge entry. The file must have YAML frontmatter with at least a `title` field.

### CLI import

Bulk-import knowledge from a directory of markdown files:

```bash
uv run manage.py import_knowledge --project-slug my-project --dir /path/to/knowledge/
```

The command recursively finds all `.md` files, parses their YAML frontmatter, and creates or updates entries (matched by title).

## Evaluation

Scout includes an evaluation system using **golden queries** -- test cases with known-correct answers. The `run_eval` management command runs all golden queries for a project and reports accuracy:

```bash
uv run manage.py run_eval --project-slug my-project
```

Use evaluations to measure how well the knowledge layer is helping the agent produce correct results.
