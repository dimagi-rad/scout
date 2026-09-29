# Agent architecture

Scout's AI agent is built on LangGraph with Claude as the LLM backend. This document covers the agent's architecture, tools, prompt construction, and response processing.

## Overview

`build_agent_graph` (`apps/agents/graph/base.py`) compiles a three-node graph:

```
START
  → agent (call LLM)
    → tool calls? → tools (execute, with workspace/user/thread IDs injected)
        → workspace access denied, or a schema-error streak? → escalate → END
        → otherwise → agent
    → no tool calls → END
```

- **agent** prepends the system prompt, prunes history, repairs any tool call
  left without a result, and calls Claude.
- **tools** runs the calls. MCP tools get `workspace_id`, `user_id` and
  `thread_id` from state. MCP tools and the `artifact_manager` and
  `canvas_manager` subagents also get `tool_call_id`, taken from the call itself.
  All of these are hidden from the model's tool schemas, so the model can't
  choose its own data scope.
- **escalate** ends the turn with a fixed message and makes no further tool calls
  (see [Escalation](#escalation)).

There is no dedicated retry node. A failed tool returns an error envelope, and the
model reads it on its next pass through `agent`. The caller's `recursion_limit`
caps the loop. It is 50 for chat and recipe runs, and
`AGENT_RESUME_RECURSION_LIMIT` (default 20) for the turn that resumes a chat
after materialization.

Key characteristics:

- **LLM Backend**: Claude (`DEFAULT_LLM_MODEL`)
- **Framework**: LangGraph for conversation flow and state management
- **Persistence**: PostgreSQL checkpointer for chat threads; headless runs have none
- **Role-aware**: tools and prompts depend on the user's workspace role
  (see [Roles and run modes](#roles-and-run-modes))
- **Semantic-first access**: Canonical metrics use the semantic model; read-only SQL is available when the model cannot express the question

## Agent state

The agent maintains state across conversation turns:

| Field | Type | Description |
|-------|------|-------------|
| `messages` | list | Conversation history (auto-deduplicated by message ID) |
| `workspace_id` | string | Injected into every MCP tool call to scope data access to this workspace |
| `user_id` | string | The user the turn runs for |
| `thread_id` | string | Links background jobs started from this conversation back to it |

The state carries no role. Tools that write re-check the user's workspace role each time they run.

Message history is automatically pruned to keep the last 20 messages plus system messages. Orphaned tool messages (those whose parent AI message was pruned) are removed.

## Roles and run modes

The graph is built per turn with two inputs that change its tools and prompts:

- **write-capable**: the user has at least the read-write workspace role
  (`read_write` or `manage`). It is resolved when the graph is built and fails
  closed: the run is read-only when there is no authenticated user or access is
  denied.
- **interactive**: `True` for chat. `False` for headless runs such as recipes.

| Tool | Read-only member | Write-capable, chat | Write-capable, headless |
|------|------------------|---------------------|-------------------------|
| MCP read tools (`semantic_query`, `query`, ...) | yes | yes | yes |
| `run_materialization`, `cancel_materialization` | no | MCP (starts a run and returns) | local blocking `run_materialization`; MCP `cancel_materialization` |
| `artifact_manager` | no | yes | yes |
| `artifact_graph_overview`, `get_artifact_semantic_queries` | yes | via `artifact_manager` | via `artifact_manager` |
| `canvas_read` | chat only | yes | no |
| `canvas_manager` | no | yes | no |
| `save_learning`, `save_as_recipe` | no | yes | yes |

Canvas tools need a conversation ID as well as an interactive run.

`teardown_schema` is never exposed. Leaving a write tool out isn't the only
guard: each write operation re-checks the role when it runs and is refused if
the role has changed. The local tools do this in the tool, and the MCP server
does it for MCP materialization. The `artifact_manager` and `canvas_manager`
subagents rely on the checks in the tools they call.

The prompt follows the same split. `select_base_system_prompt` picks one of three
base prompts:

- **Read-only**: never offers a rebuild. It refers repairs to a workspace member
  with write access, and says a generic query error doesn't prove data is missing.
- **Chat**: when an error's `recovery_action` is `materialization`, it asks before
  re-materializing, unless the user already asked for a refresh. Once the run
  starts it ends the turn, and the conversation resumes when loading finishes.
- **Headless**: when `recovery_action` is `materialization`, it calls the
  blocking `run_materialization` at most once per run and continues in the same
  run.

The artifact, data-availability and canvas sections also have read-only
variants.

### Headless runs

`RecipeRunner` builds the graph with `interactive=False`, no checkpointer, and
a synthetic `recipe-run-<id>` thread ID. There is no chat thread to resume
into, so `run_materialization` is a local tool that blocks until loading
completes. The canvas tools are left out. If the turn ends on the escalation
node, the recipe run is marked failed rather than completed. The runner spots
this from the `scout_escalation` response metadata, not the message text.

## Escalation

After each tool round, the graph routes to `escalate` instead of back to
`agent` in two cases:

- **Workspace access denied**: a tool in the latest round returned
  `WORKSPACE_ACCESS_DENIED`. The denial applies to every remaining call, so
  the turn ends with the authorizer's message instead of retrying.
- **Schema-error streak**: the last three tool results all returned
  `NOT_FOUND` or `VALIDATION_ERROR`, which usually means the tables aren't
  queryable. The graph matches the structured `error.code`, not the message
  text. Any other result breaks the streak.

For a schema-error streak, the message depends on the run. Read-only members
are told that someone with write access can refresh the data. Chat asks whether
to run materialization. Headless runs report that the data needs
re-materializing. The message carries `scout_escalation` metadata
(`workspace_access_denied` or `schema_errors`).

## MCP integration

The agent accesses workspace data through a Model Context Protocol (MCP) server rather than connecting directly. The MCP server (`mcp_server/`) runs as a separate process and provides:

- **Semantic catalog access** for curated datasets, measures, dimensions, and time dimensions
- **Structured semantic query execution** with row limits and timeout enforcement
- **Read-only SQL fallback** with validated functions, schema scope, row limits, and timeouts
- **Response envelopes** with consistent error codes, timing data, and audit logging
- **Shared-secret auth**: every request must carry an `X-Scout-MCP-Secret` header matching `MCP_SHARED_SECRET`; an unset secret rejects every request

The backend communicates with the MCP server via `langchain-mcp-adapters`, which exposes MCP tools as LangChain tools that the LangGraph agent can call. The client (`apps/agents/mcp_client.py`) caches the tool list and opens a circuit breaker after five consecutive connection failures. The MCP server URL is configured via the `MCP_SERVER_URL` environment variable (default: `http://localhost:8100/mcp`).

All MCP tools require a `workspace_id` parameter. The agent graph injects this
server-side so the model does not choose or override the data scope.

## Tools

The agent has access to tools provided by the MCP server and local tools for artifact and knowledge management.

### semantic_catalog

List business-facing datasets and members available in the workspace semantic model.

### describe_dataset

Describe one semantic dataset, including its measures, dimensions, time dimensions, and relationships.

### semantic_query

Run a structured query against the semantic model.

**Parameters:**
- `measures` (list, optional): Semantic measure members such as `visits.count`
- `dimensions` (list, optional): Semantic dimension members such as `visits.username`
- `time_dimension` (string, optional): Semantic time dimension member
- `granularity` (string, optional): `day`, `week`, `month`, `quarter`, or `year`
- `date_range` (object, optional): A preset such as `{"preset": "last_30_days"}` or inclusive `start`/`end` dates; requires `time_dimension`
- `query_context` (object, optional): Reporting context; only `timezone` is used
- `filters` (list, optional): Structured filters
- `order_by` (list, optional): Structured ordering
- `limit` (number, optional): Maximum rows (default 100, clamped server-side)

**Returns:**
- `columns`: List of column names
- `rows`: Result data
- `row_count`: Number of rows returned
- `truncated`: Whether results hit the limit
- `semantic_query`: Canonical semantic query spec
- `members`: Semantic members used by the result

Results and errors use the standard MCP response envelope.

Scout backend code compiles semantic query specs into trusted parameterized
database requests. Canonical metrics must use this path, even when raw SQL
would be easier.

### query

Execute read-only SQL when the semantic model cannot express the question, such
as inspecting text columns, exploring fields without semantic members, or
examining individual rows. Discover the actual schema with `list_tables`,
`describe_table`, and `get_metadata` before writing SQL. Explain the fallback
and label any resulting custom calculations separately from canonical metrics.

**Parameter:** `sql` (string, required), containing a single SELECT. Workspace,
user, and thread identifiers are injected server-side.

**Result data:** `columns`, `rows`, `row_count`, `truncated`, `sql_executed`, and
`tables_accessed`, inside the standard MCP response envelope.

The server accepts read-only CTEs, joins, set operations, and an explicit
allowlist of core PostgreSQL analytics functions. Custom and extension
functions and casts to custom or OID-alias types are not supported. Use `LIMIT`
for row counts; `FETCH FIRST`, row locks, and explicit `OPERATOR(...)` calls are
rejected. Ordinary allowed functions resolve through
`pg_catalog`; the database query runs under the workspace's read-only role in
a read-only transaction. Unsupported statements/functions and system catalog
reads are rejected before execution. Row limits and timeouts still apply.
See [Security](security.md#raw-sql-validation) for the enforcement details.

`teardown_schema` remains excluded from the agent's tools.

### artifact_manager

Delegates story artifact work to the Artifact Manager subagent, which has its
own tools and recursion limit. It discovers data, checks the semantic queries,
and writes the story through `artifact_write`. That is the only write path for
story artifacts, and it validates the story before publishing. The parent agent
passes a task description. If the model sends an empty task, the graph builds
one from the user's latest message.

Read-only members get `artifact_graph_overview` and
`get_artifact_semantic_queries` instead, to inspect existing stories.

See [Artifact types](artifact-types.md) for the story document format.

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

Learnings are automatically injected into future prompts via the knowledge retriever. New learnings start at 50% confidence; saving a duplicate raises the existing learning's confidence by 10 points instead of creating a new record.

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
- `prompt` (string, required): Markdown prompt template with `{{variable}}` placeholders, sent to the agent when the recipe runs
- `is_shared` (bool, optional, default `false`): Stored on the recipe. The recipe list API returns every recipe in the workspace regardless of this flag.

### describe_table

Get detailed column information for a table before using the raw SQL fallback.

**Parameters:**
- `table_name` (string, required): Name of the table to describe

**Returns:** The table description and its columns (name, type, nullable, default, description) inside the standard MCP response envelope. JSONB columns carry summaries from the CommCare discover phase when available. TableKnowledge notes reach the agent through the [knowledge context](#4-knowledge-context), not this tool.

## Prompt construction

The system prompt is assembled from multiple sources at runtime:

### 1. Base system prompt

Core agent behavior, in a read-only, chat or headless variant (see [Roles and run modes](#roles-and-run-modes)):

- **Core principles**: Precision over speed, data-driven responses, explain reasoning, acknowledge uncertainty
- **Response format**: Markdown tables for small results, summaries for large results
- **Query explanation**: Mandatory plain English breakdown for every semantic or raw SQL query
- **Provenance requirements**: Datasets or tables, semantic members or SQL filters, aggregation method, row counts, time range
- **Knowledge entries**: Use metric definitions and business rules from the knowledge base
- **Error handling**: Explain in plain language, identify cause, suggest fix
- **Security constraints**: Semantic-first analysis, validated read-only SQL fallback, workspace scope, no system catalogs

### 2. Artifact prompt

Instructions for creating visualizations. Read-only members get a variant that only covers inspecting existing stories.

- When to create an artifact, and delegating it to `artifact_manager`
- Runtime date controls (`date_filter`, `period_selector`) for story artifacts
- What to do when an artifact needs a semantic model change

### 3. Workspace instructions

The workspace's `system_prompt`, under a `## Workspace Instructions` heading. Use for:

- Domain-specific terminology
- Default assumptions (e.g., "amounts are in cents")
- Preferred output formats

### 4. Knowledge context

Assembled by the `KnowledgeRetriever` from three sources, in this order. The
combined section is capped at 6,000 characters: longer text is cut at the cap
and a truncation notice appended. SQL in knowledge content (fenced `sql` code blocks
and lines starting with `SELECT`, `WITH`, `INSERT`, `UPDATE`, `DELETE`,
`CREATE`, `DROP` or `ALTER`) is replaced with a note to use semantic members
instead.

**Knowledge entries** (ordered by title; each entry's content is markdown):
```markdown
## Knowledge Base

### APAC Active Users
In the APAC region, 'active user' means logged in within 7 days, not 30.
```

**Table knowledge** (enriched metadata):
```markdown
## Table Context (beyond schema)

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

**Agent learnings** (active learnings, top 20 by confidence; the confidence line appears only at 80% or above):
```markdown
## Learned Corrections

- The events.timestamp column stores Unix epoch milliseconds, not seconds. Use to_timestamp(timestamp / 1000.0).
  - *Tables: `events`*
  - *Confidence: 90% (applied 15 times)*
```

### 5. Dataset discovery and query configuration

Only for workspaces with at least one data source (tenant). The
`## Workspace And Dataset Discovery` section maps intents to tools: dataset
discovery (`list_workspaces`, `list_datasets`), dataset details
(`describe_dataset`), analysis (`semantic_query`), the raw SQL fallback
(`list_tables`, `describe_table`, `query`), and dataset edits through the
Semantic Canvas. It ends with the query limits:

```markdown
## Semantic Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
```

This text is fixed in the prompt. It carries no schema name, because queries
run against the workspace's own tenant or view schema, which the tools resolve
from state. The limits are enforced elsewhere. `semantic_query` caps its limit
at 500 rows, and Cube's database driver sets a 30-second `statement_timeout`
(`cube_config/cube.js`). The MCP server applies a 500-row cap and a 30-second
`statement_timeout` to raw SQL.

### 6. Semantic Canvas

Interactive runs only. In a chat thread, members who can write get
instructions for delegating dataset edits to `canvas_manager`. Other
interactive runs, including all read-only members, get a variant that allows
`canvas_read` and explains that saving changes needs a read-write role.

### 7. Data availability

Sections 1–6 form the stable, cached prefix. For workspaces with a data
source, a `## Data Availability` section follows it, outside the cache, because
it changes whenever data is materialized. It
holds the semantic catalog for the workspace's active datasets. While a refresh
runs outside the serving data, it holds the catalog with a note that results
don't include the refresh yet. During a first load or an unsafe refresh, or when
no catalog is available, it holds guidance on what to do instead, depending on
the run mode and the member's role.
Workspaces with more than one tenant also get a warning when sources are
excluded from the active view, or when its coverage is unknown.

### 8. Current date context

Every run ends with a `## Current date context` section, also outside the
cache: the reporting timezone and today's date, with an instruction to use
date presets and never infer today from model memory or data timestamps.

## Response processing

### Stream translation

`langgraph_to_ui_stream` (`apps/chat/stream.py`) translates the agent's
LangGraph events into the Vercel AI SDK v6 UI message stream:

| LangGraph Event | UI Stream Chunk |
|-----------------|-----------------|
| Agent starts | `{"type":"start"}`, `{"type":"start-step"}` |
| Text generation | `text-start`, `{"type":"text-delta","id":"...","delta":"..."}`, `text-end` |
| Extended thinking | `reasoning-start`, `reasoning-delta`, `reasoning-end` |
| Tool called | `{"type":"tool-input-available","toolCallId":"...","toolName":"...","input":{...}}` |
| Tool result | `{"type":"tool-output-available","toolCallId":"...","output":"..."}` |
| Subagent activity | `data-subagent-*` parts (status, text, reasoning, tool input/output, error) tagged with the parent `toolCallId` |
| Escalation | The escalation message for the run mode, streamed as text |
| Transient Anthropic overload | `{"type":"data-chat-status","data":{"kind":"retryable-error",...},"transient":true}` |
| Other failure | An apology text part, then `{"type":"error","errorText":"... Ref: <ref>"}` |
| Agent finishes | `{"type":"finish-step"}`, `{"type":"finish","finishReason":"stop"}` |

Tool inputs are redacted of the injected parameters, and tool outputs over
100,000 characters are truncated with a marker. `artifact_manager` and
`canvas_manager` activity is streamed live as `data-subagent-*` parts. Events
that arrive before their parent tool call ID is known are held back and sent
just before that tool's output.

There is no separate artifact event. The frontend finds artifacts in tool
output (`frontend/src/components/ChatMessage/ChatMessage.tsx`):

- An artifact ID is a string `artifact_id` or `artifact.id` in the tool
  output, parsed as JSON.
- The subagent cards (`artifact_manager`, `canvas_manager`) show an
  open-artifact button when their output carries one.
- Any other tool part whose output carries one renders as an open-artifact
  button in place of the tool card.

Thread-to-artifact links are stored server-side in `ThreadArtifact`.
Artifact tools link an artifact when they create, update or inspect it
(`apps/agents/tools/artifact_graph_tool.py`). When a thread's artifacts are
listed, `backfill_thread_artifact_links` (`apps/chat/artifact_links.py`) also
links artifacts by conversation ID, and by `artifact_id`, `artifact.id`,
`previous_artifact_id` and `previous_version_id` keys found anywhere in saved
messages. This covers threads from before tool-side linking.

## Conversation persistence

Conversations are persisted using LangGraph's PostgreSQL checkpointer:

- **Thread ID**: Unique identifier for each conversation
- **Checkpoints**: Full state saved after each turn
- **Connection pooling**: `LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE` connections per process (default 20; 4 in development settings)
- **No in-memory fallback**: if Postgres is unavailable, chat returns an error rather than silently dropping history

To continue a conversation, pass the same `thread_id` in the config:

```python
config = {"configurable": {"thread_id": "conversation-123"}}
result = graph.invoke(state, config=config)
```

## Configuration

### Settings that affect the agent

| Setting | Default | Effect |
|---------|---------|--------|
| `DEFAULT_LLM_MODEL` (env) | `claude-opus-4-8` | Model for the agent and its subagents |
| Workspace `system_prompt` | empty | Workspace instructions in the system prompt |
| Workspace member role | — | Tools and prompt variants (see [Roles and run modes](#roles-and-run-modes)) |

Row limits and query timeouts aren't configurable per workspace. They're fixed
in code (see [Dataset discovery and query configuration](#5-dataset-discovery-and-query-configuration)).
Queries run against the workspace's tenant or view schema, not a configured schema.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API authentication |
| `DB_CREDENTIAL_KEY` | Fernet key for encrypting API-key connection credentials (see [Security](security.md#encrypted-credentials)) |
| `MCP_SERVER_URL` | MCP server endpoint (default: `http://localhost:8100/mcp`) |
| `MCP_SHARED_SECRET` | Shared secret the agent sends to the MCP server; required in production |
