# Knowledge

The knowledge layer tells the agent what a workspace's data *means*: metric definitions, business rules, and corrections learned from earlier mistakes. Each workspace has its own knowledge.

## How the agent uses it

Every conversation's system prompt includes the workspace's knowledge, in this order:

1. **Knowledge Base**: every knowledge entry, ordered by title.
2. **Table Context**: table annotations (see [Table knowledge](#table-knowledge)).
3. **Learned Corrections**: the 20 highest-confidence active agent learnings.

The combined knowledge context is capped at 6,000 characters. Anything past the cap, including learnings when entries are long, is cut off with a note pointing to the Knowledge page, so keep entries short. SQL code blocks and lines that start with a SQL statement are replaced with a placeholder before they reach the agent, because the agent queries through the semantic model rather than writing SQL from examples.

## Knowledge entries

A knowledge entry is a markdown document with a **title**, **content**, and optional **tags**. Use tags to categorize entries, for example `metric`, `rule`, or `glossary`.

Common uses:

- **Metric definitions**: how a number such as "active users" is defined.
- **Business rules**: gotchas like "amounts are in cents" or "exclude test sites".
- **Domain glossary**: definitions for domain-specific terms.
- **Data quality notes**: known problems, such as duplicate rows in a date range.

Example:

```markdown
Title: Active FLW
Tags: metric

An active frontline worker is one who submitted at least one visit in the
last 30 days. Exclude users whose username starts with "test".
```

## Agent learnings

Learnings are corrections the agent saves with its `save_learning` tool after it finds and fixes a mistake, such as a filter a question needs or a field that means something other than its name suggests. Each learning has:

- **Description**: the correction, in at least 20 characters.
- **Category**: one of type mismatch, missing required filter, join pattern, aggregation gotcha, naming convention, data quality issue, business logic correction, or other.
- **Tables**: the tables it applies to.
- **Confidence**: starts at 0.5. When the agent saves a learning with the same description again, confidence rises by 0.1 and the times-applied count goes up by one.

The agent only has `save_learning` in conversations with **Read-Write** and **Manager** members. Learnings cannot be created by hand, but they can be edited and deleted on the Knowledge page.

## Table knowledge

Table knowledge annotates individual tables: a description, use cases, data quality notes, owner, refresh frequency, related tables with join hints, and per-column notes. The agent sees it in the prompt, and the semantic catalog uses the column notes as field descriptions.

There is no page for table knowledge in the Scout UI. It is edited in the Django admin. For CommCare Connect workspaces, Scout fills in column notes for the visits table from the opportunity's form definitions when data loads.

## The Knowledge page

Open **Knowledge** in the sidebar. It lists entries and learnings, with a type filter (**All**, **Entries**, **Learnings**) and a search box.

| Action | Who can do it |
|--------|---------------|
| View and export | Any workspace member |
| **New** entry, **Import** | Read-Write and Manager |
| **Edit** and **Delete** | Read-Write and Manager |

When editing a learning you can change its description, category, and tables. Its original error and confidence are shown for reference but cannot be edited. Deleting asks for confirmation first.

## Import and export

### Exporting

**Export** downloads the workspace's knowledge entries as a zip file, with one markdown file per entry. Learnings and table knowledge are not included. Each file has YAML frontmatter:

```markdown
---
title: Active FLW
tags:
- metric
---
An active frontline worker is one who submitted at least one visit...
```

### Importing

**Import** uploads a zip of markdown files:

- Only `.md` files are read, and they must be UTF-8. The zip may be at most 25 MB uncompressed.
- The title comes from the `title` frontmatter field. A file without frontmatter uses its first line (with any leading `#` removed) as the title. Files with no title are skipped.
- `tags` can be a YAML list or a comma-separated string.
- An entry with the same title as an existing entry updates that entry's content and tags; otherwise a new entry is created.
- If saving fails, the whole import is rolled back.

After an import the Knowledge page reloads the list. The import API (`POST /api/workspaces/<workspace_id>/knowledge/import/`) also returns counts of created, updated, and skipped entries and any per-file errors.
