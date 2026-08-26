"""Guardrail for the MCP context-injection contract (arch-review finding 07#0).

The agent graph's injecting tool node (`_make_injecting_tool_node` in
`apps.agents.graph.base`) adds ``workspace_id``, ``user_id``, ``thread_id`` and
``tool_call_id`` to the args of **every** MCP tool call. The read tools
(``semantic_catalog``, ``semantic_query``, …) declare ``workspace_id``,
``user_id`` and ``thread_id`` (the last two carry the actor into the MCP audit
trail — arch #257, finding 08#8); only the per-call ``tool_call_id`` remains
undeclared on them.
That residual extra succeeds today only because TWO independent library
behaviours silently tolerate it:

1. **LangChain** — the MCP tools are ``StructuredTool``s whose ``args_schema`` is
   the raw MCP ``inputSchema`` *dict* (not a Pydantic model). ``BaseTool.
   _parse_input`` returns dict-schema input unvalidated, so the undeclared keys
   pass straight through to the tool coroutine (which sinks ``**arguments``).
2. **FastMCP** — on the server the arguments are validated against a generated
   ``ArgModelBase`` subclass whose Pydantic config ignores unknown fields
   (``extra='ignore'`` — Pydantic's default). The extras are dropped before the
   Python tool function is called.

If *either* library tightens (LangChain validating dict schemas, or FastMCP
switching to ``extra='forbid'``) every MCP tool call would start failing at
once, in production, with no local signal. These tests pin both halves so that
regression fails loudly here instead.
"""

import pytest
from langchain_core.tools import StructuredTool

from apps.agents.graph.base import MCP_TOOL_NAMES
from mcp_server.server import mcp

# The exact arg set the injecting node writes onto every MCP tool call. Mirrors
# the ``injections`` mapping in ``build_agent_graph`` (workspace_id/user_id/
# thread_id) plus the per-call ``tool_call_id``. Pinned here so a change to the
# injection set is a deliberate edit to this contract.
INJECTED_ARGS = {
    "workspace_id": "ws-1",
    "user_id": "user-1",
    "thread_id": "thread-1",
    "tool_call_id": "tc-1",
}

# Read tools whose signature declares the actor set (workspace_id/user_id/
# thread_id) but NOT the per-call ``tool_call_id`` — so ``tool_call_id`` is the
# one injected arg they still rely on the libraries to silently drop.
# ``run_materialization`` is excluded because it deliberately declares all four.
ACTOR_DECLARED_TOOLS = ["semantic_catalog", "semantic_query", "get_schema_status"]

# The args these tools declare (everything injected except ``tool_call_id``).
DECLARED_ACTOR_ARGS = {"workspace_id", "user_id", "thread_id"}


def test_langchain_parse_input_passes_injected_extras_through_unvalidated():
    """LangChain half: a dict-schema StructuredTool (built the way
    langchain-mcp-adapters builds MCP tools) must accept the injected extras
    without validation and forward them untouched."""

    async def call_tool(**arguments):
        # langchain-mcp-adapters' coroutine sinks arbitrary kwargs.
        return ("ok", None)

    # args_schema is the raw MCP inputSchema dict — declares only workspace_id.
    dict_args_schema = {
        "type": "object",
        "properties": {"workspace_id": {"type": "string"}},
        "required": [],
    }
    tool = StructuredTool(
        name="list_tables",
        description="",
        args_schema=dict_args_schema,
        coroutine=call_tool,
        response_format="content_and_artifact",
    )

    parsed = tool._parse_input(dict(INJECTED_ARGS), INJECTED_ARGS["tool_call_id"])

    # The undeclared keys are NOT stripped or rejected on the LangChain side —
    # they survive to be sent to the MCP server. If LangChain ever starts
    # validating dict schemas this assertion fails loudly.
    assert isinstance(parsed, dict)
    for key in INJECTED_ARGS:
        assert key in parsed, f"LangChain dropped/rejected injected arg {key!r}"


@pytest.mark.parametrize("tool_name", ACTOR_DECLARED_TOOLS)
def test_fastmcp_arg_model_accepts_and_ignores_injected_extras(tool_name):
    """FastMCP half: the server-side arg model for a read tool must declare the
    actor set (workspace_id/user_id/thread_id), accept the full injected arg set
    (no ValidationError), and silently drop the still-undeclared ``tool_call_id``
    before the tool fn runs."""
    assert tool_name in MCP_TOOL_NAMES  # sanity: this is an injected MCP tool

    tool = mcp._tool_manager.get_tool(tool_name)
    arg_model = tool.fn_metadata.arg_model

    declared = set(arg_model.model_fields)
    assert DECLARED_ACTOR_ARGS.issubset(declared), (
        f"{tool_name} no longer declares all actor args; declared {declared}"
    )
    assert "tool_call_id" not in declared, (
        f"{tool_name} now declares tool_call_id; update this guardrail if the "
        "injected-arg surface changed"
    )

    # Must not raise — extra='forbid' would turn this into a ValidationError,
    # which is exactly the prod-wide breakage we are guarding against.
    validated = arg_model.model_validate(dict(INJECTED_ARGS))

    # FastMCP forwards only declared fields to the tool fn; the still-undeclared
    # ``tool_call_id`` must have been dropped here.
    forwarded = validated.model_dump_one_level()
    for key in DECLARED_ACTOR_ARGS:
        assert forwarded[key] == INJECTED_ARGS[key]
    assert "tool_call_id" not in forwarded
