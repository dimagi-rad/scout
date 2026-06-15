# Plan: Real (unmocked) chat<->MCP contract test (arch #234, partial)

## Problem (from issue #234)

The chat->MCP wire has ZERO unmocked test coverage. Every existing test mocks
exactly the seam that hid production bugs:

- `test_mcp_client` patches `MultiServerMCPClient`
- `test_mcp_chat_integration` mocks `get_mcp_tools` with hand-written fakes whose
  schemas can drift from `server.py`
- `test_mcp_server` / `test_mcp_tenant_tools` call handlers in-process with the DB
  mocked at the ORM/psycopg boundary
- `test_agent_graph` asserts `MCP_TOOL_NAMES` against itself

This masking pattern hid: the recipe signature drift, the toolCallId mismatch, the
`get_metadata` "0 tables" card, and the onboarding 404 — as a CLASS. Findings:
10#4, 12#0, 02#6, 07#0.

## Approach: in-process FastMCP over a real MCP wire

The official `mcp` SDK ships an in-memory transport,
`mcp.shared.memory.create_connected_server_and_client_session(server)`, which runs
the REAL `mcp_server.server.mcp` FastMCP instance in a task group and hands back a
real `ClientSession` connected over anyio memory streams. `langchain_mcp_adapters.tools.load_mcp_tools(session)` then loads the server's tools as the same LangChain
tools the agent graph uses — over the real protocol, no mocks on the wire.

Verified by spike: 11 tools load; `list_pipelines` round-trips and returns the real
JSON envelope; `session.list_tools()` returns the real `inputSchema` per tool.

## Contracts to assert (highest value subset; depth over breadth)

1. **Tool-set drift detector** — the set of tools advertised over the wire equals
   `MCP_TOOL_NAMES` ∪ {context-free tools}. Catches rename/add/remove drift that
   `MCP_TOOL_NAMES` (asserted-against-itself today) cannot. (10#4, 02#6)
2. **Per-tool parameter-schema contract** — for each MCP tool the agent injects
   into (`MCP_TOOL_NAMES`), the real `inputSchema` advertises exactly the params the
   graph expects, including the server-injected `workspace_id`/`user_id`/`thread_id`/
   `tool_call_id` on `run_materialization`. Catches signature drift. (02#6, 07#0)
3. **Prompt-vs-tool-schema drift (`pipeline=`)** — the live prompt at
   `graph/base.py` tells the agent to call `run_materialization(pipeline=...)`, but
   the real tool has NO `pipeline` param. Assert the CORRECT contract (prompt must
   not reference a param the tool lacks) and xfail it against the blocking issue.
   (02#6)
4. **Real round-trip result shape** — call `list_pipelines` and `get_schema_status`
   over the wire; assert the documented envelope shape (`success`, `data.*`) that the
   frontend/agent depend on. (10#4)
5. **Server-side enforcement of injected params** — `run_materialization` and
   `get_schema_status` reject/handle a missing/empty `workspace_id` server-side
   rather than trusting the LLM. (07#0)
6. **End-to-end through the injecting tool node** — build the real agent graph's
   injecting tool node over the real wired tools and confirm `workspace_id` flows
   from agent state into the actual MCP call (no mock between state and server).

## DB strategy

Some tools (`get_schema_status`, `list_tables`) hit the managed DB. Reuse the
existing real-DB pattern (`MANAGED_DATABASE_URL`, `get_managed_db_connection`). Tests
that need a managed schema create/drop it and clean up `*_ro` roles. Tests that only
need the platform DB (Django test DB) use `@pytest.mark.django_db(transaction=True)`.
Prefer asserting on tools that DON'T require a fully materialized schema where
possible (schema-status "not_provisioned", list_pipelines, list_tools) to keep the
test hermetic.

## xfail discipline

Any contract currently broken by another issue gets a real assertion for the CORRECT
behavior + `@pytest.mark.xfail(reason="blocked by #NNN", strict=False)`. Never weaken
an assertion; never ship a hard-failing test.

## Out of scope (deferred, finding 10#5)

Frontend unit-test infrastructure. Note in PR + issue.
