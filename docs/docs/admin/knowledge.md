# Knowledge

The knowledge layer tells the agent what a workspace's data *means*: metric definitions, business rules, and corrections learned from earlier mistakes (kept as [memory](#memory)). Each workspace has its own knowledge.

## How the agent uses it

Every conversation's system prompt includes the workspace's knowledge, in this order:

1. **Knowledge Base**: every knowledge entry, ordered by title.
2. **Table Context**: table annotations (see [Table knowledge](#table-knowledge)).
3. **Workspace Memory**: up to 50 active workspace memories, newest first. Each is one line, with a `Tables:` sub-line when tables are set.

Personal memory is not part of this context. It is added to interactive chats as its own `## Saved Personal Preferences` block after the stable prompt.

The combined knowledge context is capped at 6,000 characters. Anything past the cap, including workspace memory when entries are long, is cut off with a note pointing to the Knowledge page, so keep entries short. SQL code blocks and lines that start with a SQL statement are replaced with a placeholder before they reach the agent (for a workspace memory, every line but its first), because the agent queries through the semantic model rather than writing SQL from examples.

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

## Memory

Memory is a separate feature from knowledge entries: short notes the agent or a member saves so later conversations start with them. There are two layers, managed on the **Memory** page (see [Memory](#memory-page)).

- **Workspace memory**: notes about how to combine or interpret this workspace's data, shared with every member. The agent saves them with its `save_workspace_memory` tool, and members can add them by hand. Each is 3 to 500 characters and can list the tables it applies to. A workspace holds up to 50 active memories, and saves are refused once they would no longer all fit in the prompt, so every saved memory reaches the agent.
- **Personal memory**: a member's private preferences, such as how they like answers presented. Saved with `save_personal_memory`, 3 to 500 characters each, up to 50 per person and 3,400 characters in total. Personal memory is private to its owner, applies in all their workspaces, and goes only into interactive chats, not recipe runs.

The agent saves a memory only when the user asks, or for a clearly lasting preference or confirmed fact, never for a one-off instruction. Presentation preferences go to personal memory and facts about the dataset go to workspace memory. If an identical active workspace memory (ignoring case) already exists, the save returns `already_saved` and nothing changes.

Both tools are offered in every interactive chat. Read members can save personal memory, but a workspace save from a Read member is refused. Recipe runs get neither tool. Each save shows a **Saved to memory** chip in chat with **Undo** and a link to the Memory page.

### Memory page

Open **Memory** in the sidebar. The **Personal** section shows only your own memories. The **Workspace** section shows the active workspace's memories.

| Action | Who can do it |
|--------|---------------|
| View workspace memory | Any workspace member |
| Add workspace memory | Read-Write and Manager |
| Edit or delete a workspace memory | Its author (while still Read-Write or higher), or a Manager |

Every create, edit, and delete of workspace memory is recorded with the actor, the source (a chat or the Memory page), and the text before and after, and is logged to the `scout.memory.audit` logger.

## Table knowledge

Table knowledge annotates individual tables: a description, use cases, data quality notes, owner, refresh frequency, related tables with join hints, and per-column notes. The agent sees it in the prompt, and the semantic catalog uses the column notes as field descriptions.

There is no page for table knowledge in the Scout UI. It is edited in the Django admin. For CommCare Connect workspaces, Scout fills in column notes for the visits table from the opportunity's form definitions when data loads.

## The Knowledge page

Open **Knowledge** in the sidebar. It lists knowledge entries, with a search box. Memory is on its own page.

| Action | Who can do it |
|--------|---------------|
| View and export | Any workspace member |
| **New** entry, **Import** | Read-Write and Manager |
| **Edit** and **Delete** | Read-Write and Manager |

Deleting asks for confirmation first.

## Import and export

### Exporting

**Export** downloads the workspace's knowledge entries as a zip file, with one markdown file per entry. Memory and table knowledge are not included. Each file has YAML frontmatter:

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
