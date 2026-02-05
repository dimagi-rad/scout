# Data Agent Platform — Implementation Plan

## For Claude Code

This is the build plan for a self-hosted data agent platform. The full design is spread
across three spec documents — this plan consolidates them into a single ordered sequence.

**Reference Documents** (read these for detailed designs, models, and code):
- `data-agent-platform-spec.md` — Base spec: infrastructure, models, SQL tool, agent graph, Chainlit, Docker
- `data-agent-platform-addendum.md` — Addendum 1: React artifacts, recipes, sharing, multi-provider OAuth
- `data-agent-platform-addendum-2.md` — Addendum 2: Knowledge layer, self-learning, evals, provenance

**Before starting each phase**, read the relevant sections of these documents for the detailed
model definitions, code samples, and design rationale. This plan tells you *what* to build
and *when* — the specs tell you *how*.

---

## Tech Stack

- **Backend**: Django 5+ with Django REST Framework
- **Agent**: LangGraph + langchain-anthropic
- **SQL Validation**: sqlglot
- **Database**: PostgreSQL 16 (platform DB + project DBs)
- **Frontend**: Chainlit
- **Artifact Rendering**: Sandboxed iframe with React 18, Recharts, Plotly, D3, Tailwind
- **Auth**: django-allauth (multi-provider OAuth)
- **Encryption**: cryptography (Fernet) for DB credentials
- **Visualization**: Plotly, Pandas

---

## Phase 1: Project Scaffold & Data Models (Week 1)

**Goal**: Django project running with all models defined, admin interface, and data dictionary generation working.

### 1.1 Django Project Scaffold
- [x] Create project with `config/settings/{base,development,production}.py` structure
- [x] Configure PostgreSQL as the platform database
- [x] Set up `pyproject.toml` with all dependencies (see base spec Section 11)
- [x] Create `.env.example` with required environment variables
- [x] Create `Dockerfile` and `docker-compose.yml` with platform-db and api services (see base spec Section 10)

### 1.2 Core Models — `apps/projects/`
Create the models from base spec Section 2:
- [x] `Project` — with encrypted DB credentials (Fernet), connection params, agent config
- [x] `ProjectMembership` — user-project link with roles (viewer, analyst, admin)
- [x] `ProjectRole` — TextChoices enum
- [x] `SavedQuery` — for saving and sharing SQL queries
- [x] `ConversationLog` — conversation history for audit

Key details:
- [x] DB credentials use Fernet encryption via `DB_CREDENTIAL_KEY` env var
- [x] `Project.get_connection_params()` returns psycopg2-compatible dict with `search_path` and `statement_timeout`
- [x] `allowed_tables` and `excluded_tables` are JSONFields for table-level access control

### 1.3 Knowledge Models — `apps/knowledge/`
Create the models from addendum 2 Section B1:
- [x] `TableKnowledge` — enriched table metadata (description, use cases, data quality notes, column notes, ownership)
- [x] `CanonicalMetric` — agreed-upon metric definitions with canonical SQL
- [x] `VerifiedQuery` — query patterns known to produce correct results
- [x] `BusinessRule` — institutional knowledge and gotchas
- [x] `AgentLearning` — agent-discovered corrections (from addendum 2 Section B2)

### 1.4 User Model — `apps/users/`
- [x] Custom User model (extend AbstractUser)
- [x] Basic auth setup (will add OAuth later in Phase 4)

### 1.5 Admin Interface
- [x] Register all models with Django admin
- [x] Custom admin for Project (hide encrypted fields, show connection test button)
- [x] Custom admin for knowledge models (inline editing, bulk import)
- [x] Admin action to regenerate data dictionary

### 1.6 Data Dictionary Generator — `apps/projects/services/data_dictionary.py`
Implement the `DataDictionaryGenerator` class from base spec Section 3:
- [x] Introspects PostgreSQL schema via `information_schema` queries
- [x] Extracts: tables, columns, types, PKs, FKs, indexes, enums, approximate row counts
- [x] Fetches sample values (skipping sensitive columns)
- [x] Respects `allowed_tables` / `excluded_tables`
- [x] Saves to `Project.data_dictionary` JSONField
- [x] `render_for_prompt()` formats for system prompt (full detail for ≤15 tables, table listing for larger schemas)

### 1.7 Knowledge Import Command — `apps/knowledge/management/commands/import_knowledge.py`
- [x] Bulk import from JSON/YAML files following the directory structure:
  ```
  knowledge/
  ├── tables/*.json
  ├── metrics/*.json
  ├── queries/*.sql
  └── business/*.json
  ```
- [x] Upsert semantics (update existing, create new)
- [x] `--recreate` flag for fresh start

### 1.8 Data Dictionary Command — `apps/projects/management/commands/generate_data_dictionary.py`
From base spec Section 9:
- [x] `--project-slug` for single project
- [x] `--all` for all projects
- [x] `--dry-run` to print without saving

### 1.9 Tests
- [x] Test data dictionary generation against a test PostgreSQL schema
- [x] Test credential encryption/decryption
- [x] Test table filtering (allowed/excluded)
- [x] Test knowledge import command

---

## Phase 2: Agent Core (Week 2)

**Goal**: Working LangGraph agent that can answer questions about a project's database with knowledge-grounded SQL, self-correction, and provenance.

### 2.1 SQL Validator — `apps/agents/tools/sql_tool.py`
Implement `SQLValidator` and `SQLValidationError` from base spec Section 4:
- [x] Parse SQL with sqlglot into AST
- [x] Reject non-SELECT statements (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, etc.)
- [x] Reject multiple statements
- [x] Reject dangerous functions (pg_read_file, dblink, lo_import, etc.)
- [x] Enforce schema/table allowlist
- [x] `inject_limit()` — add or cap LIMIT clause

### 2.2 SQL Tool — `apps/agents/tools/sql_tool.py`
Implement `create_sql_tool()` factory from base spec Section 4:
- [x] Validates query via SQLValidator
- [x] Injects LIMIT if missing
- [x] Connects to project DB with read-only session (`conn.set_session(readonly=True)`)
- [x] Returns structured result: columns, rows, row_count, truncated, sql_executed
- [x] Add provenance metadata to response (from addendum 2 Section B4):
  - tables_accessed
  - metric_used (if canonical metric applied)
  - knowledge_applied (list of applied knowledge descriptions)
  - caveats
- [x] Handle errors: QueryCanceled (timeout), general psycopg2 errors

### 2.3 Knowledge Retriever — `apps/knowledge/services/retriever.py`
Implement `KnowledgeRetriever` from addendum 2 Section B1:
- [x] Always includes: canonical metrics, business rules
- [x] Includes table knowledge (all if <20 tables, otherwise match on question)
- [x] Includes top-10 verified query patterns
- [x] Includes active agent learnings (ordered by confidence)
- [x] Returns formatted string for system prompt injection

### 2.4 Base System Prompt — `apps/agents/prompts/base_system.py`
From base spec Section 5, plus additions from addendum 2 Sections B4-B5:
- [x] Core behavior: precision, data-driven, explain reasoning
- [x] Response format: markdown tables for ≤20 rows, summaries for larger
- [x] Error handling: explain errors, suggest fixes, never fabricate
- [x] Provenance requirements: always explain HOW the answer was computed
- [x] Query explanation: plain English explanation alongside SQL
- [x] Canonical metric enforcement: MUST use canonical SQL when available
- [x] Security: SELECT only, schema-scoped

### 2.5 Agent State — `apps/agents/graph/state.py`
From base spec Section 5:
- [x] `AgentState(TypedDict)` with: messages, project_id, project_name, user_id, user_role
- [x] Add: `needs_correction: bool`, `retry_count: int`, `correction_context: dict`

### 2.6 Agent Graph — `apps/agents/graph/base.py`
Build the LangGraph agent with self-correction from base spec Section 5 + addendum 2 Section B2:

Graph structure:
```
START → agent → should_continue? → tools → check_result → result_ok?
                └→ END                                       ├── yes → agent
                                                             └── no → diagnose_and_retry → agent
                                                                      (max 3 retries)
```

Nodes:
- [x] `agent_node` — calls LLM with system prompt (base + project + knowledge + data dictionary)
- [x] `tool_node` — executes tool calls (ToolNode from langgraph.prebuilt)
- [x] `check_result_node` — examines tool results for errors (addendum 2 Section B2)
- [x] `diagnose_and_retry_node` — asks agent to diagnose and fix errors (addendum 2 Section B2)

The `build_agent_graph(project, checkpointer)` function:
- [x] Creates tools: execute_sql, describe_table (for large schemas), save_learning
- [x] Binds tools to LLM (ChatAnthropic)
- [x] Assembles system prompt: base + project.system_prompt + knowledge retriever output + data dictionary
- [x] Compiles graph with checkpointer

### 2.7 Save Learning Tool
From addendum 2 Section B2:
- [x] Agent calls this after successfully correcting a query error
- [x] Persists an `AgentLearning` record
- [x] Learning gets injected into future prompts via KnowledgeRetriever

### 2.8 Describe Table Tool
From base spec Section 5:
- [x] For schemas with >15 tables
- [x] Returns detailed column info for a specific table from the data dictionary
- [x] Keeps the system prompt small while giving on-demand detail

### 2.9 Tests
Write comprehensive tests FIRST, then implement:
- [x] **SQL Validator tests**: injection attempts, blocked statements, schema enforcement, multi-statement rejection, dangerous functions, LIMIT injection
- [x] **SQL Tool tests**: successful query, timeout handling, error handling, read-only enforcement
- [x] **Knowledge Retriever tests**: correct assembly with various knowledge combinations
- [ ] **Agent end-to-end tests**: simple question → SQL → result, error → retry → success

---

## Phase 3: Frontend & Artifacts (Week 3)

**Goal**: Working Chainlit UI with auth, project selection, chat, and rich artifact rendering in sandboxed iframes.

### 3.1 Chainlit App — `chainlit_app/app.py`
From base spec Section 7:
- [x] Basic password auth for development (`@cl.password_auth_callback`)
- [x] `@cl.on_chat_start`: load user's projects, show project selector via `cl.ChatSettings`
- [x] `setup_agent()`: build LangGraph agent for selected project, store in session
- [x] `@cl.on_message`: route to agent, stream response
- [x] In-memory checkpointer for MVP (MemorySaver)
- [x] `.chainlit/config.toml` configuration

### 3.2 Artifact Models — `apps/artifacts/`
From addendum 1 Section A1:
- [x] `Artifact` — stores code (JSX/HTML/MD/Plotly/SVG), data (JSON), versioning, source queries
- [x] `ArtifactType` — TextChoices: react, html, markdown, plotly, svg
- [x] `SharedArtifact` — share links with access levels (public, project, specific users), expiry, view tracking

### 3.3 Artifact Sandbox
From addendum 1 Section A1:
- [x] `ArtifactSandboxView` — serves the sandbox HTML template
- [x] Sandbox HTML loads: React 18, Babel standalone, Recharts, Plotly, D3, Lodash, Tailwind CSS (all from CDN)
- [x] Receives artifact code + data via `postMessage` from parent
- [x] Transpiles JSX on the fly, renders React component
- [x] CSP headers: allow CDN scripts, block all network access from artifact code
- [x] iframe sandbox: `allow-scripts` only (no `allow-same-origin`)
- [x] `ArtifactDataView` — API to fetch artifact code/data (with access control)

### 3.4 Artifact Tools — `apps/agents/tools/artifact_tool.py`
From addendum 1 Section A1:
- [x] `create_artifact` tool — accepts title, type, code, data, source_queries. Stores in DB, returns render URL.
- [x] `update_artifact` tool — creates new version of existing artifact, preserves history.
- [x] These REPLACE the simpler `create_visualization` tool from the base spec.

### 3.5 Artifact Prompt Addition
From addendum 1 Section A1 (ARTIFACT_PROMPT_ADDITION):
- [x] Instructions for when to use each artifact type
- [x] React component guidelines: available libraries, Tailwind for styling, data prop pattern
- [x] Example React artifact code
- [x] Data best practices

### 3.6 Artifact Rendering in Chainlit
- [x] `chainlit_app/artifacts.py` — helpers for rendering artifacts in chat
- [x] When agent calls `create_artifact`, render an iframe pointing to the sandbox URL
- [x] Handle `postMessage` for resize, error reporting
- [x] For markdown artifacts: render inline (no iframe needed)
- [x] For Plotly: use `cl.Plotly` element as fallback

### 3.7 Django URL Configuration
- [x] `/artifacts/<id>/sandbox` → ArtifactSandboxView
- [x] `/artifacts/<id>/data` → ArtifactDataView
- [x] `/shared/<token>` → SharedArtifactView

### 3.8 Tests
- [x] Artifact CRUD
- [x] Sandbox CSP headers
- [x] Access control (project members only)
- [x] Artifact versioning

---

## Phase 4: Auth, Sharing & Recipes (Week 4)

**Goal**: Multi-provider OAuth, artifact sharing, and recipe system.

### 4.1 django-allauth Integration
From addendum 1 Section A3:
- [x] Install and configure django-allauth
- [x] Add Google and GitHub providers
- [x] Configure `SOCIALACCOUNT_AUTO_SIGNUP`, `ACCOUNT_EMAIL_REQUIRED`, etc.
- [x] Provider credentials stored in DB via Django admin (SocialApp model)

### 4.2 Custom OAuth Provider Pattern
From addendum 1 Section A3:
- [x] Example CommCare provider implementation (~50 lines)
- [x] `CommCareProvider` (extract_uid, extract_common_fields)
- [x] `CommCareOAuth2Adapter` (token URL, authorize URL, profile URL, complete_login)
- [x] URL configuration

### 4.3 Chainlit Auth Bridge — `chainlit_app/auth.py`
From addendum 1 Section A3:
- [x] `@cl.oauth_callback` — looks up Django user via allauth SocialAccount, auto-creates if needed
- [x] `@cl.header_auth_callback` — for reverse proxy setups (oauth2-proxy, Authelia)
- [x] `@cl.password_auth_callback` — development fallback

### 4.4 Artifact Sharing
From addendum 1 Section A1:
- [x] Share link generation API (creates SharedArtifact with token)
- [x] Access levels: public (anyone with link), project (members only), specific (named users)
- [x] Optional expiry (expires_at)
- [x] View count tracking
- [x] `SharedArtifactView` — standalone viewer page (no chat context needed)

### 4.5 Recipe Models — `apps/recipes/`
From addendum 1 Section A2:
- [x] `Recipe` — name, description, variable definitions (name, type, label, default, options)
- [x] `RecipeStep` — ordered steps with prompt templates, `{{variable}}` substitution, expected tools
- [x] `RecipeRun` — execution tracking (status, variable values, step results, timing)

### 4.6 Recipe Tools
From addendum 1 Section A2:
- [x] `save_as_recipe` tool — agent extracts steps from conversation, identifies variables, saves recipe
- [x] Include example in tool docstring showing the expected structure

### 4.7 Recipe Runner — `apps/recipes/services/runner.py`
From addendum 1 Section A2:
- [x] `RecipeRunner` class: validates variables, creates RecipeRun, iterates steps
- [x] Each step: render prompt template → send to agent → collect results → update RecipeRun
- [x] Uses same thread for context continuity across steps
- [x] Handles success/failure per step

### 4.8 Recipe UI in Chainlit
- [ ] Recipe browser: list available recipes for the current project
- [ ] Run recipe: variable input form → execute → show progress per step → display results
- [ ] Recipe sharing: toggle is_shared flag for project members

### 4.9 Tests
- [x] OAuth flow (mock providers)
- [x] Share link access control (public, project, specific, expired)
- [x] Recipe CRUD
- [x] Recipe variable substitution
- [x] Recipe runner end-to-end

---

## Phase 5: Evals & Production Hardening (Week 5)

**Goal**: Evaluation system, production infrastructure, and operational tooling.

### 5.1 Eval System — `apps/knowledge/`
From addendum 2 Section B3:
- [x] `GoldenQuery` model — question, expected_sql, expected_result, comparison_mode, tolerance
- [x] `EvalRun` model — results aggregation (total, passed, failed, accuracy), per-query details
- [x] Comparison modes: exact, approximate (with tolerance), row_count, contains, structure

### 5.2 Eval Runner — `apps/knowledge/services/eval_runner.py`
- [x] Takes a project + optional filters (tags, difficulty)
- [x] Runs each golden query through the agent
- [x] Compares results against expected values
- [x] Records per-query pass/fail with details
- [x] Calculates overall accuracy

### 5.3 Eval Management Command — `apps/knowledge/management/commands/run_eval.py`
- [x] `python manage.py run_eval --project-slug X`
- [x] `python manage.py run_eval --project-slug X --tag finance --difficulty easy`
- [x] Output: summary table + detailed failures

### 5.4 PostgreSQL Checkpointer
- [x] Switch from MemorySaver to `PostgresSaver` from `langgraph-checkpoint-postgres`
- [x] Persistent conversation memory across sessions
- [x] Configure connection to platform DB

### 5.5 Database Role Setup Script — `scripts/setup_project_db.py`
From base spec Section 8:
- [x] Creates read-only PostgreSQL role per project
- [x] Grants: CONNECT, USAGE on schema, SELECT on all tables + future tables
- [x] Revokes: everything on public schema
- [x] Sets connection limit
- [x] Idempotent (safe to re-run)

### 5.6 Connection Pooling
- [x] Add connection pooling for project database connections
- [x] Options: pgbouncer (external) or Django persistent connections
- [x] Connection pool per project, with max connection limits

### 5.7 Docker Compose — Final
From base spec Section 10:
- [x] platform-db: PostgreSQL 16
- [x] api: Django server
- [x] chainlit: Chainlit frontend
- [x] Shared volumes, environment variables, proper networking

### 5.8 Knowledge Curation Workflow
- [x] Admin action: promote AgentLearning → BusinessRule or VerifiedQuery
- [x] Review interface for learnings (approve, reject, edit)
- [x] Learning confidence score management (increases when confirmed, decreases when contradicted)

### 5.9 Tests
- [ ] Eval runner with known golden queries
- [ ] Connection pooling under load
- [ ] Docker Compose full-stack smoke test

---

## Phase 6: Polish & Documentation (Week 6)

**Goal**: Production-ready with documentation, monitoring, and operational guides.

### 6.1 Streaming Responses
- [x] Switch from `graph.invoke()` to `graph.astream()` in Chainlit handler
- [x] Stream tokens to the user as they're generated
- [x] Show tool call steps in real-time

### 6.2 Artifact Export
- [x] Download artifact as standalone HTML (embed data + libraries)
- [x] Export as PNG (using playwright headless browser)
- [x] Export as PDF

### 6.3 Rate Limiting
- [x] Per-user query rate limits
- [x] Per-project daily query budgets
- [ ] Query cost estimation (optional: estimate before executing large queries)

### 6.4 Conversation Logging
- [x] Store full conversation history in ConversationLog
- [x] Include tool calls, SQL executed, artifacts created
- [x] Queryable audit trail

### 6.5 Production Deployment Guide
- [ ] Environment variable reference
- [ ] Nginx/Caddy reverse proxy configuration
- [ ] SSL/TLS setup
- [ ] Backup strategy for platform DB
- [ ] Monitoring and alerting recommendations

### 6.6 User Documentation
- [ ] Getting started guide for project admins
- [ ] Knowledge authoring guide (how to write good table descriptions, metrics, business rules)
- [ ] Recipe creation guide
- [ ] Eval authoring guide (how to write golden queries)

---

## File Structure Reference

```
data-agent-platform/
├── manage.py
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── config/
│   ├── settings/
│   │   ├── base.py
│   │   ├── development.py
│   │   └── production.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   ├── projects/
│   │   ├── models.py              # Project, ProjectMembership, SavedQuery, ConversationLog
│   │   ├── admin.py
│   │   ├── api/
│   │   │   ├── serializers.py
│   │   │   └── views.py
│   │   ├── services/
│   │   │   ├── data_dictionary.py  # DataDictionaryGenerator
│   │   │   └── db_manager.py       # Connection management
│   │   ├── management/
│   │   │   └── commands/
│   │   │       └── generate_data_dictionary.py
│   │   └── migrations/
│   ├── agents/
│   │   ├── graph/
│   │   │   ├── base.py            # build_agent_graph (with self-correction nodes)
│   │   │   ├── nodes.py           # check_result, diagnose_and_retry, save_learning nodes
│   │   │   └── state.py           # AgentState
│   │   ├── tools/
│   │   │   ├── sql_tool.py        # SQLValidator, create_sql_tool
│   │   │   ├── artifact_tool.py   # create_artifact, update_artifact
│   │   │   ├── recipe_tool.py     # save_as_recipe
│   │   │   ├── learning_tool.py   # save_learning
│   │   │   └── registry.py        # Tool registration per project
│   │   ├── prompts/
│   │   │   ├── base_system.py     # BASE_SYSTEM_PROMPT
│   │   │   ├── artifact_prompt.py # ARTIFACT_PROMPT_ADDITION
│   │   │   └── templates.py       # Prompt assembly logic
│   │   └── memory/
│   │       └── checkpointer.py
│   ├── artifacts/
│   │   ├── models.py              # Artifact, SharedArtifact
│   │   ├── admin.py
│   │   ├── views.py               # ArtifactSandboxView, ArtifactDataView, SharedArtifactView
│   │   ├── urls.py
│   │   └── migrations/
│   ├── recipes/
│   │   ├── models.py              # Recipe, RecipeStep, RecipeRun
│   │   ├── admin.py
│   │   ├── services/
│   │   │   └── runner.py          # RecipeRunner
│   │   ├── api/
│   │   │   ├── serializers.py
│   │   │   └── views.py
│   │   ├── urls.py
│   │   └── migrations/
│   ├── knowledge/
│   │   ├── models.py              # TableKnowledge, CanonicalMetric, VerifiedQuery,
│   │   │                          #   BusinessRule, AgentLearning, GoldenQuery, EvalRun
│   │   ├── admin.py
│   │   ├── services/
│   │   │   ├── retriever.py       # KnowledgeRetriever
│   │   │   └── eval_runner.py     # EvalRunner
│   │   ├── management/
│   │   │   └── commands/
│   │   │       ├── run_eval.py
│   │   │       └── import_knowledge.py
│   │   ├── api/
│   │   │   ├── serializers.py
│   │   │   └── views.py
│   │   └── migrations/
│   └── users/
│       ├── models.py              # Custom User
│       ├── admin.py
│       ├── providers/             # Custom OAuth providers
│       │   └── commcare/
│       │       ├── provider.py
│       │       ├── views.py
│       │       └── urls.py
│       └── auth.py
├── chainlit_app/
│   ├── app.py                     # Main Chainlit entrypoint
│   ├── auth.py                    # Multi-provider auth bridge
│   ├── handlers.py                # Message routing
│   ├── artifacts.py               # Artifact iframe rendering
│   └── .chainlit/
│       └── config.toml
├── scripts/
│   └── setup_project_db.py        # Read-only role setup
└── tests/
    ├── test_sql_tool.py
    ├── test_data_dictionary.py
    ├── test_knowledge_retriever.py
    ├── test_agent.py
    ├── test_artifacts.py
    ├── test_recipes.py
    ├── test_auth.py
    └── test_eval.py
```

---

## Key Dependencies

```toml
[project]
name = "data-agent-platform"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    # Django
    "django>=5.0",
    "djangorestframework>=3.15",
    "django-environ>=0.11",
    "psycopg2-binary>=2.9",

    # Auth
    "django-allauth>=65.0",
    "cryptography>=42.0",

    # LangGraph / LangChain
    "langgraph>=0.2",
    "langchain-anthropic>=0.2",
    "langchain-core>=0.3",
    "langgraph-checkpoint-postgres>=2.0",

    # SQL validation
    "sqlglot>=25.0",

    # Visualization & data
    "plotly>=5.0",
    "pandas>=2.0",
    "kaleido>=0.2",

    # Frontend
    "chainlit>=1.3",

    # Artifact export
    "playwright>=1.40",

    # Utilities
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-django>=4.8",
    "pytest-asyncio>=0.23",
    "factory-boy>=3.3",
    "ruff>=0.5",
]
```

---

## Notes for Claude Code

1. **Read the spec documents** for detailed model definitions and code samples before implementing each phase. The specs have complete model code, SQL queries, prompt text, and HTML templates ready to use.

2. **Write tests first** for the SQL validator (Phase 2.1) — this is security-critical. Test injection attempts, multi-statement attacks, schema boundary violations, and dangerous function calls.

3. **The knowledge layer should be populated early** — even with just a few TableKnowledge entries and BusinessRules, the agent's accuracy improves dramatically. Include seed data fixtures for testing.

4. **The artifact sandbox HTML** (addendum 1) is a single self-contained template — it loads all libraries from CDN and communicates via postMessage. Copy it as-is from the spec.

5. **System prompt assembly** happens in `build_agent_graph()` — it concatenates: base prompt + project prompt + artifact prompt + knowledge retriever output + data dictionary. Keep each piece in its own file for maintainability.

6. **The self-correction loop** (Phase 2.6) adds 2 extra nodes to the LangGraph graph. Keep the retry limit at 3 and make the diagnosis prompt explicit about what went wrong.

7. **For the MVP**, use MemorySaver (in-memory checkpointer) and keyword matching for knowledge retrieval. Don't add embeddings/vector DB until a project outgrows the simple approach.

8. **Django admin is sufficient** for project and knowledge management in the MVP. Don't build custom management UIs until Phase 6.
