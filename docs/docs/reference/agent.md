# Agent architecture

Scout's AI agent is built on LangGraph with Claude as the LLM backend. This document covers the agent's architecture, tools, prompt construction, and response processing.

## Overview

The agent implements a self-correcting conversation graph that automatically retries failed queries up to three times:

```
START
  → agent (call LLM)
    → should_continue? (has tool calls?)
      → tools (execute)
        → check_result (error detection)
          → result_ok? (error found?)
            → diagnose_and_retry → agent (up to 3 retries)
            → agent (success)
      → END (no tool calls)
```

Key characteristics:

- **LLM Backend**: Claude (configurable model per project)
- **Framework**: LangGraph for conversation flow and state management
- **Persistence**: PostgreSQL checkpointer for conversation history
- **Self-correction**: Automatic error detection and retry with diagnosis

## Agent state

The agent maintains state across conversation turns:

| Field | Type | Description |
|-------|------|-------------|
| `messages` | list | Conversation history (auto-deduplicated by message ID) |
| `project_id` | string | Scopes all data access to this project |
| `project_name` | string | For display in responses |
| `user_id` | string | For audit logging |
| `user_role` | string | Permission level (viewer, analyst, admin) |
| `needs_correction` | bool | Flag set when a query fails |
| `retry_count` | int | Current retry attempt (max 3) |
| `correction_context` | dict | Error details for diagnosis |

Message history is automatically pruned to keep the last 20 messages plus system messages. Orphaned tool messages (those whose parent AI message was pruned) are removed.

## Tools

The agent has access to six tools, created dynamically based on project configuration.

### execute_sql

Execute SELECT queries against the project database.

**Parameters:**
- `query` (string, required): SQL SELECT query to execute

**Returns:**
- `columns`: List of column names
- `rows`: Result data
- `row_count`: Number of rows returned
- `truncated`: Whether results hit the limit
- `sql_executed`: The actual SQL run (may include injected LIMIT)
- `tables_accessed`: Tables referenced in the query
- `caveats`: Warnings about the results
- `error`: Error message if failed

**Security features:**
- Only SELECT statements allowed
- Single statement enforcement (no semicolon-separated queries)
- 40+ dangerous functions blocked (file access, remote DB, etc.)
- Table/schema allowlist enforcement
- Automatic LIMIT injection (respects project's `max_rows_per_query`)
- Query timeout enforcement
- Rate limiting per user

**Blocked functions include:**
- File system: `pg_read_file`, `pg_ls_dir`, `pg_stat_file`
- Large objects: `lo_import`, `lo_export`, `lo_create`
- Remote access: `dblink`, `dblink_connect`, `dblink_exec`
- System: `pg_terminate_backend`, `pg_cancel_backend`

### create_artifact

Create interactive visualizations and content.

**Parameters:**
- `title` (string, required): Human-readable title
- `artifact_type` (string, required): One of `react`, `plotly`, `html`, `markdown`, `svg`
- `code` (string, required): Source code for the artifact
- `description` (string, optional): What the artifact visualizes
- `data` (dict, optional): JSON data passed to React components as `data` prop
- `source_queries` (list, optional): SQL queries that generated the data

**Artifact types:**

| Type | Use case | Code format |
|------|----------|-------------|
| `react` | Interactive dashboards, charts with Recharts | JSX with default export |
| `plotly` | Statistical charts, 3D plots, heatmaps | Plotly JSON specification |
| `html` | Simple tables, static content | HTML markup |
| `markdown` | Documentation, reports | Markdown text |
| `svg` | Custom diagrams, flowcharts | SVG markup |

**React artifacts:**
- Recharts is pre-loaded (no imports from CDN needed)
- Tailwind CSS classes available
- Data passed via `data` prop to the default export component

### update_artifact

Create a new version of an existing artifact.

**Parameters:**
- `artifact_id` (string, required): UUID of artifact to update
- `code` (string, required): Complete new source code
- `title` (string, optional): New title
- `data` (dict, optional): New data payload

Creates an `ArtifactVersion` record preserving history.

### save_learning

Save discovered corrections for future queries.

**Parameters:**
- `description` (string, required): Detailed, actionable learning (min 20 chars)
- `category` (string, required): One of:
  - `type_mismatch`: Column type different than expected
  - `filter_required`: Query needs specific WHERE clause
  - `join_pattern`: Correct way to join tables
  - `aggregation`: Gotcha with grouping
  - `naming`: Column/table naming convention
  - `data_quality`: Known data issues
  - `business_logic`: Domain-specific rules
  - `other`: Anything else
- `tables` (list, required): Table names this applies to
- `original_sql` (string, optional): SQL that failed
- `corrected_sql` (string, optional): SQL that worked

Learnings are automatically injected into future prompts via the knowledge retriever. Duplicate learnings increase confidence score rather than creating new records.

### save_as_recipe

Save conversation workflows as reusable templates.

**Parameters:**
- `name` (string, required): Recipe name
- `description` (string, required): What the recipe does
- `variables` (list, required): Variable definitions, each with:
  - `name`: Identifier for `{{name}}` placeholders
  - `type`: One of `string`, `number`, `date`, `boolean`, `select`
  - `label`: Human-readable label
  - `default` (optional): Default value
  - `options` (required for select): Allowed values
- `steps` (list, required): Step definitions, each with:
  - `prompt_template`: Prompt with `{{variable}}` placeholders
  - `expected_tool` (optional): Tool the agent should use
  - `description` (optional): What this step does
- `is_shared` (bool, optional): Whether all project members can use it

### describe_table

Get detailed column information for a table. Only available when the schema has more than 15 tables (too large to fit full details in the system prompt).

**Parameters:**
- `table_name` (string, required): Name of the table to describe

**Returns:** Markdown-formatted documentation with columns, types, constraints, sample values, and any column notes from TableKnowledge.

## Prompt construction

The system prompt is assembled from multiple sources at runtime:

### 1. Base system prompt

Core agent behavior (~150 lines):

- **Core principles**: Precision over speed, data-driven responses, explain reasoning, acknowledge uncertainty
- **Response format**: Markdown tables for small results, summaries for large results
- **Query explanation**: Mandatory plain English breakdown for every SQL query
- **Provenance requirements**: Source tables, filters, aggregation method, row counts, time range
- **Knowledge entries**: Use metric definitions and business rules from the knowledge base
- **Error handling**: Explain in plain language, identify cause, suggest fix
- **Security constraints**: SELECT only, schema-scoped, no system tables

### 2. Artifact prompt

Instructions for creating visualizations:

- When to create artifacts vs. use tables
- Artifact type selection guidelines
- React component patterns with Recharts examples
- Data handling best practices

### 3. Project system prompt

Custom instructions from the project configuration. Use for:

- Domain-specific terminology
- Default assumptions (e.g., "amounts are in cents")
- Preferred output formats

### 4. Knowledge context

Assembled by the `KnowledgeRetriever` from three sources:

**Knowledge entries** (always included):
```markdown
## Knowledge Base

### MRR (Monthly Recurring Revenue)
Definition: Sum of active subscription amounts, excluding annual contracts billed upfront.

SQL:
    SELECT SUM(amount) FROM subscriptions WHERE status = 'active'

Unit: USD
Caveats:
- Excludes enterprise contracts billed annually.
- Amounts are in cents.

### APAC Active Users
In the APAC region, 'active user' means logged in within 7 days, not 30.
Applies to: users, sessions tables.
```

**Table knowledge** (enriched metadata):
```markdown
## Table Context

### orders
Order transactions from all channels.

**Column Notes:**
- `amount`: Stored in cents, not dollars
- `status`: Values: pending, completed, refunded

**Data Quality Notes:**
- Duplicate rows exist for Q1 2024 due to migration

**Related Tables:**
- `customers`: `orders.customer_id = customers.id`
```

**Agent learnings** (top 20 by confidence):
```markdown
## Learned Corrections

- The events.timestamp column stores Unix epoch milliseconds, not seconds. Use to_timestamp(timestamp / 1000.0).
  - *Tables: `events`*
  - *Confidence: 90% (applied 15 times)*
```

### 5. Data dictionary

Schema information with two modes:

**Small schemas (≤15 tables)**: Full inline detail with columns, types, and constraints.

**Large schemas (>15 tables)**: Table listing only. Agent uses `describe_table` tool for details.

### 6. Query configuration

```markdown
## Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds
- Schema: public
```

## Response processing

### Stream translation

The agent's LangGraph output is translated to Vercel AI SDK v6 format for the frontend:

| LangGraph Event | UI Stream Chunk |
|-----------------|-----------------|
| Agent starts | `{"type":"start"}`, `{"type":"start-step"}` |
| Text generation | `{"type":"text-delta","delta":"..."}` |
| Tool called | `{"type":"tool-input-available","toolName":"..."}` |
| Tool result | `{"type":"tool-output-available","output":"..."}` |
| Artifact created | `{"type":"data-artifact","id":"...","data":{...}}` |
| Agent finishes | `{"type":"finish-step"}`, `{"type":"finish"}` |

Artifact detection looks for:
- UUID pattern in tool output
- Keywords: "artifact_id", "artifact created", "chart saved", "visualization created"

### Error correction flow

When `check_result_node` detects an error:

1. **Error classification**: Categorizes as syntax, column_not_found, table_not_found, permission, timeout, type_mismatch, or execution
2. **Context capture**: Stores error message, failed SQL, tables accessed
3. **Diagnosis prompt**: Injects guidance specific to the error type
4. **Retry**: Routes back to agent node with correction context

After 3 retries, the agent is instructed to:
- Explain what it was trying to do
- Describe the error in plain language
- Suggest alternative approaches
- Optionally save a learning for future reference

### Error-specific guidance

| Error Type | Guidance |
|------------|----------|
| `syntax` | Check commas, parentheses, quotes, keywords |
| `column_not_found` | Use describe_table, check case sensitivity, verify aliases |
| `table_not_found` | Check data dictionary, verify schema, check underscores vs hyphens |
| `permission` | SELECT only, no system tables, check excluded tables |
| `timeout` | Add WHERE conditions, use LIMIT, avoid SELECT * |
| `type_mismatch` | Check column types, use explicit casts, handle NULLs |

## Conversation persistence

Conversations are persisted using LangGraph's PostgreSQL checkpointer:

- **Thread ID**: Unique identifier for each conversation
- **Checkpoints**: Full state saved after each turn
- **Connection pooling**: Max 20 connections for checkpoint operations
- **Fallback**: MemorySaver for development/testing

To continue a conversation, pass the same `thread_id` in the config:

```python
config = {"configurable": {"thread_id": "conversation-123"}}
result = graph.invoke(state, config=config)
```

## Configuration

### Project settings that affect the agent

| Setting | Default | Effect |
|---------|---------|--------|
| `llm_model` | `claude-sonnet-4-5-20250929` | Model used for generation |
| `max_rows_per_query` | 500 | LIMIT injected/capped |
| `max_query_timeout_seconds` | 30 | PostgreSQL statement_timeout |
| `system_prompt` | empty | Project-specific instructions |
| `allowed_tables` | [] (all) | Whitelist for table access |
| `excluded_tables` | [] (none) | Blacklist for table access |
| `db_schema` | public | Schema scope for queries |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API authentication |
| `DB_CREDENTIAL_KEY` | Fernet key for credential encryption |
| `MAX_QUERIES_PER_MINUTE` | Rate limit (default: 60) |
| `MAX_CONNECTIONS_PER_PROJECT` | Connection pool size (default: 5) |
