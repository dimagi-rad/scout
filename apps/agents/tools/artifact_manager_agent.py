"""Parent-facing Artifact Manager subagent tool.

The parent Scout agent sees a single ``artifact_manager`` tool. That tool runs
a smaller LangGraph with artifact-specific tools and forwards the subagent's
tool events into the active chat stream as nested child events.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field, ValidationError

from apps.agents.graph.state import AgentState
from apps.agents.subagents.data_requirements import DATA_REQUIREMENTS, validate_data_requirements
from apps.agents.subagents.events import (
    SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    emit_subagent_event,
    reset_subagent_event_queue,
    set_subagent_event_queue,
)
from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools
from apps.semantic.services.date_context import agent_date_context

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

SUBAGENT_NAME = "artifact_manager"
NESTED_MCP_TOOL_NAMES = frozenset(
    {
        "list_datasets",
        "describe_dataset",
        "semantic_query",
    }
)
NESTED_RECURSION_LIMIT = 50
NESTED_MAX_TOKENS = 8192
MAX_RUNTIME_FAILURES = 8
SUBAGENT_TRACE_MAX_EVENTS = 200
SUBAGENT_MESSAGE_MAX_CHARS = 40_000
ARTIFACT_MANAGER_TASK_REQUIRED_MESSAGE = (
    "artifact_manager requires a non-empty task. Retry the same tool call with "
    "a complete, self-contained `task` string. Do not call artifact_manager "
    "with only `intent` or only `artifact_id`."
)


class ArtifactManagerInput(BaseModel):
    task: str = Field(
        min_length=1,
        description=(
            "Required specific artifact task to perform: create, revise, inspect, or "
            "check a semantic story artifact. Do not call artifact_manager without "
            "a non-empty task."
        ),
    )
    artifact_id: str | None = Field(default=None, description="Existing artifact id, if any.")
    # Injected by the parent graph. Hidden from the model-facing schema in
    # apps.agents.graph.base._llm_tool_schemas.
    tool_call_id: str | None = None
    subagent_event_queue: Any | None = None


ARTIFACT_MANAGER_SYSTEM_PROMPT = (
    """
You are Scout's Artifact Manager subagent. Your only job is to create, inspect,
repair, and validate semantic story artifacts. Be concise and deterministic.

You have these tools:
- `artifact_graph_overview`: inspect the full current `story_doc`, its compact
  summary, diagnostics, and semantic-query manifest. Read `story_doc` before
  applying a complex edit so you can preserve existing config exactly.
- `get_artifact_semantic_queries`: inspect saved semantic-query dependencies.
- `artifact_write`: create, replace, apply ops to, or check a story artifact.
- `list_datasets`, `describe_dataset`, `semantic_query`: discover semantic
  members and verify query feasibility when needed.

Story artifacts are stored as `data.story_doc` with:
- `schema_version`: always 1.
- `name`: durable artifact name.
- `prd`: short durable spec of the user question, audience, data, and sections.
- `blocks`: ordered typed blocks.

Block types and config keys:
- `title`: `config.text`, optional `config.subtitle`.
- `section`: `config.title`, `config.body`. Body is markdown. Do not use
  `config.text`.
- `question`: `config.text`, optional `config.context`.
- `tldr`: optional `config.title`, plus `config.content` for one summary string
  or `config.items` for a list of short strings. Do not use `config.text`.
- `markdown`: `config.body` or `config.content`. Do not use `config.text`.
- `date_filter`: `config.label`, `config.default`.
- `period_selector`: `config.label`, `config.default_range`,
  `config.default_comparison`.
- `semantic_query`: `config.queries`, optional `config.compare`. Each named
  query publishes rows as `<block_id>.<query_name>`.
- `graph`: `config.title`, optional `config.subtitle`, `config.chart_type`, `config.x_key`,
  `config.y_key`, `config.series`, `config.data_label`, `config.query`,
  `config.stacked`, `config.y_format`, `config.height`, `config.x_label`,
  `config.y_label`, `config.style`, or `config.recharts`
  for an explicit Recharts element tree. Bind data with `inputs.data.$ref`.
- `table`: `config.title`, `config.columns`, `config.query`. Bind data with
  `inputs.data.$ref`.
- `stat`: `config.title`, `config.label`, `config.value_path`,
  `config.value_key`, `config.format`, `config.delta_path`, optional
  `config.prefix`, `config.suffix`, and `config.comparison`. Bind current rows
  with `inputs.current.$ref` and comparison rows with `inputs.previous.$ref`.

How to build data-backed blocks:
- Prefer hidden `semantic_query` blocks for all reusable data. Bind visible
  graph/table/stat blocks to those outputs.
- Render every chart with Recharts, using the compact line, bar, area, pie, or donut
  config when possible and `config.recharts` for advanced composition.
- Choose charts by the comparison: one headline measure -> stat; time plus
  measure -> line; category plus measure -> sorted bar; two numeric measures ->
  ScatterChart; a few part-to-whole categories -> donut or 100% stacked bar;
  detailed/high-cardinality results -> table. Use horizontal bars for long labels.
- Compact `config.style` supports only: `palette` (`categorical`, `status`,
  `sequential`, `monochrome`), `legend` (`auto`, `top`, `bottom`, `none`),
  `grid` (`horizontal`, `both`, `none`), `curve` (`monotone`, `linear`, `step`),
  `orientation` (`vertical`, `horizontal`), and `labels` (`none`, `value`).
- Stat `config.comparison` supports `type` (`none`, `absolute`, `percent`),
  `format`, `label`, and semantic `goal` (`higher`, `lower`, `neutral`). Keep
  the goal neutral unless metric meaning makes favorable direction explicit.
- Prefer a quiet grid, a maximum of five meaningful category colors, and a
  subtitle that states units/date window/denominator when needed. Do not use
  color as decoration or invent a takeaway title.
- Plotly is not available. Never produce Plotly code or specifications.
- Never store query result rows in `story_doc`.
- Never write SQL or raw Cube query keys.
- Query specs support only `measures`, `dimensions`, `time_dimension`,
  `granularity`, `filters`, `order_by`, and `limit`.
- Filters use `field`, `operator`, and values. Do not use `member`.
- If a query is bound to `date_range` or uses comparison, include
  `time_dimension`.
- Time-bucketed rows expose the bucket as `date`.
- Member result keys are snake_case: `visits.count` -> `visits_count`.
- Graph artifacts do not support transform/bucketing config. If a derived
  category is missing, return the data-model prerequisite to the parent as
  described below. You cannot create semantic fields or datasets yourself.
- For rolling windows use `date_filter`
  with `inputs.date_range={"$ref":"<date_filter_block_id>.value"}` on EVERY affected query.
  For comparisons use `period_selector`, bind
  `inputs.compare={"$ref":"<period_selector_block_id>.pair"}`, and set `config.compare=true`
  on the semantic_query block. Replace the ref prefix with the actual control block's id.
  Supported presets: today, yesterday, last_7_days, last_30_days, last_90_days,
  month_to_date. Last N days includes today and N-1 preceding calendar days.
  Never pass a preset such as last_90_days to inDateRange. For exploratory
  semantic_query calls use date_range={"preset":"last_90_days"} instead.
  Keep the user's requested period even when it contains no rows; do not widen
  it or substitute all-time data to make a chart look populated. Date controls
  are resolved by Scout in its reporting timezone, not by model arithmetic.

Use `artifact_write(action="create")` for a new artifact, `replace` when
rewriting the whole doc, `apply` for targeted edits, and `check` for runtime
validation. Use the backend's typed `runtime.failures`, not message matching:
- `invalid_document` or `invalid_query`: correct the documented defect, then validate again.
- `missing_model_dependency`: recheck the member name and kind with `list_datasets` / `describe_dataset`. If a discovered existing member satisfies the requested meaning, correct the query/artifact reference and validate again without changing the model. A missing name alone proves neither a typo nor a missing capability; never substitute a similarly named member with different semantics. Only a confirmed capability gap warrants a structured proposal to the parent; never invent member names.
- `data_unavailable`: return the backend `recovery_action`; do not rewrite the artifact or start provider loads yourself. If the action is absent, say that the repair could not be determined and retain the diagnostics; do not guess a repair.
- `permission_required` or `configuration_required`: explain the required access/operator intervention, without retrying.
- `transient_runtime_failure`: do not change the model/document; at most one bounded retry when `retryable=true`.
- Unknown `runtime_failure`: stop and report it; do not guess a destructive repair.
Treat `runtime.success=false`, `diagnostics`, and `key_warnings` as blocking
publication failures. Do not set `run_check=false` to publish a user-facing
artifact.

The top-level `description` argument to `artifact_write` is library-card metadata,
not the rendered `story_doc.prd`. Set it on create, replace, or apply when requested.
For edits, omitting it or passing null preserves the existing description; an empty
string clears it. For a description-only edit, use apply without ops. Read the
persisted `artifact.description` in the tool result or artifact_graph_overview
before claiming the description changed; runtime validation alone does not verify
the requested metadata.
When the tool returns `runtime_validation: "not_required_metadata_only"`, the
unchanged document was preserved without contacting the data service. Do not
run an additional check just to save metadata or claim the data was freshly verified.

For `action="apply"`, `ops` supports only these exact shapes:
- Set a story field: `{"op":"set","target":"story/name","value":"..."}`
  or targets `story/prd` and `story/tags`.
- Set any block field or nested config without rewriting the rest of the block:
  `{"op":"set","target":"block/<block_id>/config/<key>","value":...}`.
  Other useful paths include `block/<id>/inputs`, `block/<id>/row_group`, and
  `block/<id>/config/recharts`. Target is a slash-delimited string, never a
  JSON object and never a `path` field.
- Add a block: `{"op":"add_block","after":"<block_id>"|"start"|"end","block":{...}}`.
- Remove a block: `{"op":"remove_block","id":"<block_id>"}`.
- Move a block: `{"op":"move_block","id":"<block_id>","after":"<block_id>"|"start"|"end"}`.
Batch related ops into one atomic apply call. Prefer targeted `set` and
`add_block` ops for revisions so existing blocks remain intact.

If discovery confirms a missing analytical capability, return
`status: "needs_data_model"` and structured `data_requirements` matching the
schema below. Choose dimension, measure, dataset, or relationship from the
actual data types, row grain, keys, and supported operations, not the provider
name. Use exact discovered source_datasets/source_members; describe the need,
grain, and unresolved decisions. Do not invent member names, require a new
dataset for every field, infer join keys, or save a placeholder artifact.
Requirements are proposals, never authorization. The parent must verify them
and obtain explicit user approval before delegating to `canvas_manager`.
Existing schema or query execution errors must follow the typed outcome above,
not invent a replacement data model. Keep raw-data inspection and SQL outside
this subagent.

Final response: return a compact JSON object in text with keys:
`status`, `artifact_id`, `artifact_version`, `touched_blocks`, `diagnostics`,
`runtime_summary`, and `message`. Include `data_requirements` only when the
parent must prepare missing analytical capabilities.
"""
    + "\nData requirements JSON Schema:\n"
    + json.dumps(DATA_REQUIREMENTS.json_schema())
)


def create_artifact_manager_tool(
    workspace: Workspace,
    user: User | None,
    mcp_tools: list,
    conversation_id: str | None = None,
):
    """Create the parent-facing Artifact Manager tool."""

    @tool(args_schema=ArtifactManagerInput)
    async def artifact_manager(
        task: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
        subagent_event_queue: Any | None = None,
    ) -> dict[str, Any]:
        """Delegate semantic story artifact work to the Artifact Manager subagent."""
        parent_tool_call_id = tool_call_id or f"missing-parent-{uuid.uuid4().hex[:8]}"
        queue_token = set_subagent_event_queue(subagent_event_queue)
        messages: list[Any] = []
        final_text = ""
        run_to_tool_call_id: dict[str, str] = {}
        pending_tool_starts: dict[str, dict[str, Any]] = {}
        message_buffers: dict[tuple[str, str], str] = {}
        trace = _SubagentTraceRecorder()
        try:
            await _emit_subagent_event(
                _subagent_status_event(
                    parent_tool_call_id,
                    phase="running",
                    message="Artifact Manager started.",
                ),
                trace,
            )
            if not task.strip():
                return await _artifact_manager_failure_result(
                    parent_tool_call_id,
                    trace,
                    messages,
                    final_text,
                    ARTIFACT_MANAGER_TASK_REQUIRED_MESSAGE,
                )
            graph = _build_artifact_manager_graph(workspace, user, mcp_tools, conversation_id)
            prompt = _format_artifact_manager_task(task, artifact_id)
            input_state = {
                "messages": [HumanMessage(content=prompt)],
                "workspace_id": str(workspace.id),
                "user_id": str(user.id) if user else "",
                "user_role": "analyst",
                "thread_id": conversation_id or "",
            }
            config = {
                "configurable": {"thread_id": f"{conversation_id or 'artifact'}:artifact-manager"},
                "recursion_limit": NESTED_RECURSION_LIMIT,
                "run_name": SUBAGENT_NAME,
                "tags": ["subagent", SUBAGENT_NAME],
                "metadata": {
                    "subagent": SUBAGENT_NAME,
                    "parent_tool_call_id": parent_tool_call_id,
                    "workspace_id": str(workspace.id),
                    "conversation_id": conversation_id or "",
                },
            }
            async for event in graph.astream_events(input_state, config=config, version="v2"):
                await _forward_nested_event(
                    event,
                    parent_tool_call_id,
                    run_to_tool_call_id,
                    pending_tool_starts,
                    message_buffers,
                    trace,
                )
                if event.get("event") == "on_tool_end":
                    tool_output = event.get("data", {}).get("output")
                    if isinstance(tool_output, ToolMessage):
                        messages.append(tool_output)
                output = event.get("data", {}).get("output")
                if event.get("event") == "on_chain_end" and isinstance(output, dict):
                    maybe_messages = output.get("messages")
                    if isinstance(maybe_messages, list):
                        messages = maybe_messages
            if messages:
                final_text = _extract_final_text(messages)
            result = _summarize_result(messages, final_text)
            await _emit_subagent_event(
                _subagent_status_event(
                    parent_tool_call_id,
                    phase="completed",
                    message="Artifact Manager completed.",
                    artifact_id=result.get("artifact_id"),
                    artifact_version=result.get("artifact_version"),
                ),
                trace,
            )
            result["subagent_trace"] = trace.to_dict()
            return result
        except GraphRecursionError as exc:
            logger.warning(
                "Artifact Manager recursion limit reached for workspace %s conversation %s",
                workspace.id,
                conversation_id,
                exc_info=True,
            )
            return await _artifact_manager_failure_result(
                parent_tool_call_id,
                trace,
                messages,
                final_text,
                _recursion_failure_message(exc),
            )
        except Exception as exc:
            logger.exception(
                "Artifact Manager failed for workspace %s conversation %s",
                workspace.id,
                conversation_id,
            )
            return await _artifact_manager_failure_result(
                parent_tool_call_id,
                trace,
                messages,
                final_text,
                _generic_failure_message(exc),
            )
        finally:
            reset_subagent_event_queue(queue_token)

    artifact_manager.handle_validation_error = ARTIFACT_MANAGER_TASK_REQUIRED_MESSAGE
    artifact_manager.name = "artifact_manager"
    return artifact_manager


def _format_artifact_manager_task(
    task: str,
    artifact_id: str | None,
) -> str:
    lines = [f"Task: {task.strip()}"]
    if artifact_id:
        lines.append(f"Artifact id: {artifact_id}")
    return "\n".join(lines)


async def _artifact_manager_failure_result(
    parent_tool_call_id: str,
    trace: _SubagentTraceRecorder,
    messages: list[Any],
    final_text: str,
    message: str,
) -> dict[str, Any]:
    if messages and not final_text:
        final_text = _extract_final_text(messages)
    result = _summarize_result(messages, final_text)
    result["status"] = "error"
    result["message"] = message[:1200]
    result.pop("data_requirements", None)
    result.pop("requirement_errors", None)
    result.pop("subagent_message", None)
    await _emit_subagent_event(
        _subagent_error_event(parent_tool_call_id, result["message"]),
        trace,
    )
    await _emit_subagent_event(
        _subagent_status_event(
            parent_tool_call_id,
            phase="failed",
            message=result["message"],
            artifact_id=result.get("artifact_id"),
            artifact_version=result.get("artifact_version"),
        ),
        trace,
    )
    result["subagent_trace"] = trace.to_dict()
    return result


def _recursion_failure_message(exc: BaseException) -> str:
    detail = _exception_detail(exc)
    if detail:
        return f"Artifact Manager stopped before completion: {detail}"
    return "Artifact Manager stopped before completion: recursion limit reached."


def _generic_failure_message(exc: BaseException) -> str:
    detail = _exception_detail(exc)
    if detail:
        return f"Artifact Manager failed before completion: {detail}"
    return f"Artifact Manager failed before completion: {exc.__class__.__name__}"


def _exception_detail(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__
    return text[:600]


def _subagent_error_event(parent_tool_call_id: str, message: str) -> dict[str, Any]:
    return {
        "type": "data-subagent-error",
        "id": f"{SUBAGENT_NAME}:{parent_tool_call_id}:error",
        "data": {
            "parentToolCallId": parent_tool_call_id,
            "subagentName": SUBAGENT_NAME,
            "message": message,
        },
    }


def _build_artifact_manager_graph(
    workspace: Workspace,
    user: User | None,
    mcp_tools: list,
    conversation_id: str | None,
):
    primitive_tools = create_artifact_graph_tools(workspace, user, conversation_id)
    nested_mcp_tools = [
        tool_obj
        for tool_obj in mcp_tools
        if getattr(tool_obj, "name", None) in NESTED_MCP_TOOL_NAMES
    ]
    tools = [*primitive_tools, *nested_mcp_tools]
    tool_node = _make_nested_tool_node(ToolNode(tools))
    llm = ChatAnthropic(model=settings.DEFAULT_LLM_MODEL, max_tokens=NESTED_MAX_TOKENS)
    llm_with_tools = llm.bind_tools(_nested_llm_tool_schemas(tools))

    system_prompt = ARTIFACT_MANAGER_SYSTEM_PROMPT + agent_date_context()

    async def agent_node(state: AgentState) -> dict[str, Any]:
        state_messages = [m for m in list(state["messages"]) if not isinstance(m, SystemMessage)]
        messages = [
            SystemMessage(content=system_prompt),
            *state_messages,
        ]
        response = await llm_with_tools.ainvoke(messages)
        return {"messages": [response]}

    def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
        messages = state.get("messages", [])
        if not messages:
            return END
        last_message = messages[-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def _nested_llm_tool_schemas(tools: list) -> list:
    result = []
    hidden = {
        "workspace_id",
        "user_id",
        "thread_id",
        "tool_call_id",
        "runtime",
        SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    }
    for tool_obj in tools:
        schema = tool_obj.get_input_schema().model_json_schema()
        props = schema.get("properties", {})
        to_hide = hidden & set(props)
        if not to_hide:
            result.append(tool_obj)
            continue
        trimmed_props = {k: v for k, v in props.items() if k not in to_hide}
        trimmed_required = [r for r in schema.get("required", []) if r not in to_hide]
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool_obj.name,
                    "description": tool_obj.description or "",
                    "parameters": {
                        "type": "object",
                        "properties": trimmed_props,
                        "required": trimmed_required,
                    },
                },
            }
        )
    return result


def _make_nested_tool_node(base_tool_node: ToolNode):
    async def injecting_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        messages = list(state["messages"])
        last_msg = messages[-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            modified_msg = copy.copy(last_msg)
            modified_calls = []
            for tc in last_msg.tool_calls:
                if tc["name"] in NESTED_MCP_TOOL_NAMES:
                    tc_id = tc.get("id") or ""
                    extra = {
                        "workspace_id": state.get("workspace_id", ""),
                        "user_id": state.get("user_id", ""),
                        "thread_id": state.get("thread_id", ""),
                        "tool_call_id": tc_id,
                    }
                    tc = {**tc, "args": {**tc["args"], **extra}}
                modified_calls.append(tc)
            modified_msg.tool_calls = modified_calls
            messages = [*messages[:-1], modified_msg]
        return await base_tool_node.ainvoke({"messages": messages}, config=config)

    injecting_node.__annotations__["config"] = RunnableConfig | None
    return injecting_node


async def _forward_nested_event(
    event: dict[str, Any],
    parent_tool_call_id: str,
    run_to_tool_call_id: dict[str, str],
    pending_tool_starts: dict[str, dict[str, Any]],
    message_buffers: dict[tuple[str, str], str],
    trace: _SubagentTraceRecorder,
) -> None:
    event_type = event.get("event")
    if event_type == "on_tool_start":
        await _forward_nested_tool_start(
            event,
            parent_tool_call_id,
            run_to_tool_call_id,
            pending_tool_starts,
            trace,
        )
    elif event_type == "on_tool_end":
        await _forward_nested_tool_end(
            event,
            parent_tool_call_id,
            run_to_tool_call_id,
            pending_tool_starts,
            trace,
        )
    elif event_type == "on_chat_model_stream":
        await _forward_nested_chat_stream(event, parent_tool_call_id, message_buffers, trace)
    elif event_type in {"on_tool_error", "on_chain_error"}:
        await _emit_subagent_event(
            {
                "type": "data-subagent-error",
                "id": f"{SUBAGENT_NAME}:{event.get('run_id') or uuid.uuid4().hex}:error",
                "data": {
                    "parentToolCallId": parent_tool_call_id,
                    "subagentName": SUBAGENT_NAME,
                    "message": str(event.get("data", {}).get("error") or "Subagent error"),
                },
            },
            trace,
        )


async def _forward_nested_tool_start(
    event: dict[str, Any],
    parent_tool_call_id: str,
    run_to_tool_call_id: dict[str, str],
    pending_tool_starts: dict[str, dict[str, Any]],
    trace: _SubagentTraceRecorder,
) -> None:
    from apps.chat.stream import _redact_tool_input

    raw_input = event.get("data", {}).get("input")
    run_id = str(event.get("run_id") or "")
    tool_call_id = None
    if isinstance(raw_input, dict):
        tool_call_id = raw_input.get("tool_call_id")
    if not tool_call_id:
        if run_id:
            pending_tool_starts[run_id] = {
                "toolName": event.get("name", "unknown"),
                "input": _redact_tool_input(raw_input),
            }
            return
        tool_call_id = uuid.uuid4().hex
    if run_id:
        run_to_tool_call_id[run_id] = str(tool_call_id)
    child_id = _child_tool_call_id(tool_call_id or run_id)
    await _emit_subagent_event(
        {
            "type": "data-subagent-tool-input",
            "id": f"{child_id}:input",
            "data": {
                "parentToolCallId": parent_tool_call_id,
                "subagentName": SUBAGENT_NAME,
                "toolCallId": child_id,
                "toolName": event.get("name", "unknown"),
                "input": _redact_tool_input(raw_input),
            },
        },
        trace,
    )


async def _forward_nested_tool_end(
    event: dict[str, Any],
    parent_tool_call_id: str,
    run_to_tool_call_id: dict[str, str],
    pending_tool_starts: dict[str, dict[str, Any]],
    trace: _SubagentTraceRecorder,
) -> None:
    from apps.chat.stream import _tool_content_to_str, _truncate_tool_output

    tool_output = event.get("data", {}).get("output")
    if not tool_output:
        return
    run_id = str(event.get("run_id") or "")
    output_tool_call_id = getattr(tool_output, "tool_call_id", None)
    started_tool_call_id = run_to_tool_call_id.get(run_id)
    raw_id = output_tool_call_id or started_tool_call_id or run_id or uuid.uuid4().hex
    child_id = _child_tool_call_id(str(raw_id))
    pending_start = pending_tool_starts.pop(run_id, None)
    if not started_tool_call_id or (
        output_tool_call_id and output_tool_call_id != started_tool_call_id
    ):
        await _emit_subagent_event(
            {
                "type": "data-subagent-tool-input",
                "id": f"{child_id}:input",
                "data": {
                    "parentToolCallId": parent_tool_call_id,
                    "subagentName": SUBAGENT_NAME,
                    "toolCallId": child_id,
                    "toolName": event.get("name", "unknown"),
                    "input": (pending_start or {}).get("input", {}),
                },
            },
            trace,
        )
    await _emit_subagent_event(
        {
            "type": "data-subagent-tool-output",
            "id": f"{child_id}:output",
            "data": {
                "parentToolCallId": parent_tool_call_id,
                "subagentName": SUBAGENT_NAME,
                "toolCallId": child_id,
                "toolName": event.get("name", "unknown"),
                "output": _truncate_tool_output(_tool_content_to_str(tool_output)),
            },
        },
        trace,
    )


async def _forward_nested_chat_stream(
    event: dict[str, Any],
    parent_tool_call_id: str,
    message_buffers: dict[tuple[str, str], str],
    trace: _SubagentTraceRecorder,
) -> None:
    chunk = event.get("data", {}).get("chunk")
    if not chunk or not hasattr(chunk, "content") or not chunk.content:
        return
    run_id = str(event.get("run_id") or uuid.uuid4().hex)
    for kind, text in _extract_chunk_texts(chunk.content):
        buffer_key = (run_id, kind)
        next_text = f"{message_buffers.get(buffer_key, '')}{text}"
        if len(next_text) > SUBAGENT_MESSAGE_MAX_CHARS:
            next_text = next_text[-SUBAGENT_MESSAGE_MAX_CHARS:]
        message_buffers[buffer_key] = next_text
        await _emit_subagent_event(
            {
                "type": f"data-subagent-{kind}",
                "id": f"{SUBAGENT_NAME}:{run_id}:{kind}",
                "data": {
                    "parentToolCallId": parent_tool_call_id,
                    "subagentName": SUBAGENT_NAME,
                    "kind": kind,
                    "text": next_text,
                    "delta": text,
                },
            },
            trace,
        )


def _extract_chunk_texts(content: Any) -> list[tuple[str, str]]:
    if isinstance(content, str):
        return [("text", content)] if content else []
    result: list[tuple[str, str]] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    result.append(("text", block["text"]))
                elif block.get("type") == "thinking" and block.get("thinking"):
                    result.append(("reasoning", block["thinking"]))
            elif hasattr(block, "text") and block.text:
                result.append(("text", block.text))
    return result


class _SubagentTraceRecorder:
    """Bounded, JSON-safe subagent event trace persisted in the parent tool result."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._index_by_key: dict[tuple[str, str], int] = {}

    def add(self, event: dict[str, Any]) -> None:
        clean = _json_safe_event(event)
        event_type = clean.get("type")
        event_id = clean.get("id")
        if isinstance(event_type, str) and isinstance(event_id, str):
            key = (event_type, event_id)
            existing = self._index_by_key.get(key)
            if existing is not None:
                self.events[existing] = clean
                return
            self._index_by_key[key] = len(self.events)
        if len(self.events) >= SUBAGENT_TRACE_MAX_EVENTS:
            return
        self.events.append(clean)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subagentName": SUBAGENT_NAME,
            "events": self.events,
        }


async def _emit_subagent_event(
    event: dict[str, Any],
    trace: _SubagentTraceRecorder,
) -> None:
    trace.add(event)
    await emit_subagent_event(event)


def _subagent_status_event(
    parent_tool_call_id: str,
    *,
    phase: str,
    message: str,
    artifact_id: Any = None,
    artifact_version: Any = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "parentToolCallId": parent_tool_call_id,
        "subagentName": SUBAGENT_NAME,
        "phase": phase,
        "message": message,
    }
    if artifact_id:
        data["artifactId"] = artifact_id
    if artifact_version is not None:
        data["artifactVersion"] = artifact_version
    return {
        "type": "data-subagent-status",
        "id": f"{SUBAGENT_NAME}:{parent_tool_call_id}:status",
        "data": data,
    }


def _json_safe_event(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(event, default=str))


def _child_tool_call_id(raw_id: str) -> str:
    if raw_id.startswith(f"{SUBAGENT_NAME}:"):
        return raw_id
    return f"{SUBAGENT_NAME}:{raw_id}"


def _extract_final_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            return str(message.text)
    return ""


def _summarize_result(messages: list[Any], final_text: str) -> dict[str, Any]:
    parsed_final = _parse_json_object(final_text)
    requested_id = parsed_final.get("artifact_id") if isinstance(parsed_final, dict) else None
    artifact_result = _artifact_write_result_for_summary(messages, requested_id)
    artifact = artifact_result.get("artifact") if isinstance(artifact_result, dict) else None
    runtime = artifact_result.get("runtime") if isinstance(artifact_result, dict) else None
    diagnostics = artifact_result.get("diagnostics") if isinstance(artifact_result, dict) else None
    if isinstance(runtime, dict) and isinstance(runtime.get("diagnostics"), list):
        diagnostics = list(diagnostics) if isinstance(diagnostics, list) else []
        diagnostics.extend(item for item in runtime["diagnostics"] if item not in diagnostics)
    if isinstance(parsed_final, dict):
        status = parsed_final.get("status", artifact_result.get("status") or "done")
        message = parsed_final.get("message") or final_text
        touched_blocks = parsed_final.get("touched_blocks") or _touched_blocks_from_artifact_result(
            artifact_result
        )
    else:
        status = artifact_result.get("status") or "done"
        message = final_text or "Artifact manager completed."
        touched_blocks = _touched_blocks_from_artifact_result(artifact_result)
    if not isinstance(status, str) or not status.strip():
        fallback = artifact_result.get("status")
        if isinstance(fallback, str) and fallback.strip():
            status = fallback
            message = (
                f"Artifact operation returned {fallback}; the manager's final status was invalid."
            )
        else:
            status = "error"
            message = "Artifact Manager returned an invalid status; no model change is authorized."
    summary = {
        "status": status,
        "artifact_id": artifact.get("id") if isinstance(artifact, dict) else None,
        "artifact_version": artifact.get("version") if isinstance(artifact, dict) else None,
        "touched_blocks": touched_blocks,
        "diagnostics": diagnostics or [],
        "runtime_summary": (
            "Metadata-only edit; data was not revalidated."
            if artifact_result.get("runtime_validation") == "not_required_metadata_only"
            else _runtime_summary(runtime)
        ),
        "message": message[:1200] if isinstance(message, str) else str(message)[:1200],
    }
    # Access can change after graph creation; a denied write is never a model gap.
    if artifact_result.get("status") == "denied":
        denial = artifact_result.get("message")
        denial = (
            denial[:1200]
            if isinstance(denial, str) and denial
            else "Artifact write access is required."
        )
        summary.update(
            status="error",
            artifact_id=None,
            artifact_version=None,
            touched_blocks=[],
            message=denial,
            runtime_failures=[
                {
                    "code": "FORBIDDEN",
                    "category": "permission_required",
                    "message": denial,
                    "retryable": False,
                    "recovery_action": None,
                }
            ],
        )
        return summary
    # Failed writes return a soft-deleted candidate, not a published revision.
    # A failed check, in contrast, still refers to an existing artifact.
    if artifact_result.get("status") == "error":
        summary["artifact_id"] = None
        summary["artifact_version"] = None
    if isinstance(runtime, dict):
        failures = runtime.get("failures")
        if isinstance(failures, list) and failures:
            # Preserve each distinct cause before spending the bounded handoff
            # on repeated errors. A late permission/runtime failure must not
            # disappear behind eight earlier missing-model failures.
            representative = []
            repeated = []
            seen = set()
            for failure in failures:
                category = failure.get("category") if isinstance(failure, dict) else None
                category = category if isinstance(category, str) else "runtime_failure"
                if category in seen:
                    repeated.append(failure)
                else:
                    seen.add(category)
                    representative.append(failure)
            summary["runtime_failures"] = (representative + repeated)[:MAX_RUNTIME_FAILURES]
    artifact_failed = artifact_result.get("status") == "error" or (
        isinstance(runtime, dict) and runtime.get("success") is False
    )
    runtime_failures = runtime.get("failures") if isinstance(runtime, dict) else None
    has_model_gap = isinstance(runtime_failures, list) and any(
        isinstance(failure, dict) and failure.get("category") == "missing_model_dependency"
        for failure in runtime_failures
    )
    if (
        isinstance(parsed_final, dict)
        and parsed_final.get("status") == "needs_data_model"
        and (not artifact_failed or has_model_gap)
    ):
        try:
            summary["data_requirements"] = validate_data_requirements(
                parsed_final.get("data_requirements")
            )
        except ValidationError as exc:
            summary["status"] = "invalid_data_requirements"
            summary["requirement_errors"] = [
                {
                    "path": ("/".join(str(part) for part in error["loc"]) or "data_requirements")[
                        :200
                    ],
                    "code": error["type"],
                    "message": error["msg"][:250],
                }
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )[:8]
            ]
            gap_description = parsed_final.get("message")
            if isinstance(gap_description, str) and gap_description.strip():
                summary["subagent_message"] = gap_description[:1200]
            summary["message"] = (
                "Artifact Manager returned an invalid data-model proposal. "
                "Retry with complete, bounded structured data_requirements; no model change is authorized."
            )
    # A missing-model response is not a success claim. Preserve both a valid
    # handoff and its correctable validation errors when every typed failure
    # describes that gap. Other runtime failures must still take precedence.
    model_gap_response = (
        summary["status"] in {"needs_data_model", "invalid_data_requirements"}
        and isinstance(runtime_failures, list)
        and bool(runtime_failures)
        and all(
            isinstance(failure, dict) and failure.get("category") == "missing_model_dependency"
            for failure in runtime_failures
        )
    )
    if artifact_failed and not model_gap_response:
        summary["status"] = "error"
        # Valid proposals remain context for a real model gap, not authorization
        # to act before the other typed failures have been resolved.
        summary.pop("requirement_errors", None)
        summary.pop("subagent_message", None)
        error_message = artifact_result.get("message")
        summary["message"] = (
            error_message[:1200]
            if isinstance(error_message, str) and error_message
            else "Artifact validation failed. Follow its diagnostics and typed runtime failures."
        )
    return summary


def _artifact_write_result_for_summary(messages: list[Any], requested_id: Any) -> dict[str, Any]:
    results = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.name == "artifact_write":
            results.append(_parse_json_object(message.content) or {})
    latest = results[-1] if results else {}
    if not isinstance(requested_id, str) or not requested_id:
        return latest

    # Cleanup of another artifact must not replace the requested deliverable.
    # Only tool-confirmed results can authorize the model's selected target.
    selected_index = None
    for index, result in enumerate(results):
        runtime = result.get("runtime")
        if result.get("status") not in ("created", "updated", "replaced", "checked") or (
            isinstance(runtime, dict) and runtime.get("success") is False
        ):
            continue
        artifact = result.get("artifact")
        artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
        if (
            isinstance(artifact_id, str)
            and artifact_id
            and (artifact_id == requested_id or result.get("previous_artifact_id") == requested_id)
        ):
            requested_id = artifact_id
            selected_index = index
    if selected_index is None:
        return latest

    # A later failure stays authoritative, but an earlier failed attempt must
    # not invalidate a subsequent successful publish or check of this target.
    for result in reversed(results[selected_index + 1 :]):
        runtime = result.get("runtime")
        if result.get("status") in ("error", "denied") or (
            isinstance(runtime, dict) and runtime.get("success") is False
        ):
            return result
        if result.get("status") not in ("created", "updated", "replaced", "checked"):
            return latest
    return results[selected_index]


def _parse_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _touched_blocks_from_artifact_result(result: dict[str, Any]) -> list[str]:
    manifest = result.get("manifest") if isinstance(result, dict) else None
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return []
    block_ids = []
    for entry in entries:
        block_id = entry.get("block_id") if isinstance(entry, dict) else None
        if isinstance(block_id, str) and block_id not in block_ids:
            block_ids.append(block_id)
    return block_ids


def _runtime_summary(runtime: Any) -> str:
    if not isinstance(runtime, dict):
        return ""
    summary = runtime.get("summary")
    if isinstance(summary, str):
        return summary
    status = runtime.get("status")
    if isinstance(status, str):
        return status
    return ""
