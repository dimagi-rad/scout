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
from pydantic import BaseModel, Field

from apps.agents.graph.state import AgentState
from apps.agents.subagents.events import (
    SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    emit_subagent_event,
    reset_subagent_event_queue,
    set_subagent_event_queue,
)
from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools

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
        )
    )
    artifact_id: str | None = Field(default=None, description="Existing artifact id, if any.")
    # Injected by the parent graph. Hidden from the model-facing schema in
    # apps.agents.graph.base._llm_tool_schemas.
    tool_call_id: str | None = None
    subagent_event_queue: Any | None = None


ARTIFACT_MANAGER_SYSTEM_PROMPT = """
You are Scout's Artifact Manager subagent. Your only job is to create, inspect,
repair, and validate semantic story artifacts. Be concise and deterministic.

You have these tools:
- `artifact_graph_overview`: inspect the current story artifact doc,
  diagnostics, and semantic-query manifest.
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
- `question`: `config.text`.
- `tldr`: `config.content` for one summary string, or `config.items` for a
  list of short strings. Do not use `config.text`.
- `markdown`: `config.body` or `config.content`. Do not use `config.text`.
- `date_filter`: `config.label`, `config.default`.
- `period_selector`: `config.label`, `config.default_range`,
  `config.default_comparison`.
- `semantic_query`: `config.queries`, optional `config.compare`. Each named
  query publishes rows as `<block_id>.<query_name>`.
- `graph`: `config.title`, `config.chart_type`, `config.x_key`,
  `config.y_key`, `config.series`, `config.data_label`, `config.query`,
  `config.stacked`, `config.y_format`, `config.height`. Bind data with
  `inputs.data.$ref`.
- `table`: `config.title`, `config.columns`, `config.query`. Bind data with
  `inputs.data.$ref`.
- `stat`: `config.title`, `config.label`, `config.value_path`,
  `config.value_key`, `config.format`, `config.delta_path`. Bind current rows
  with `inputs.current.$ref`.

How to build data-backed blocks:
- Prefer hidden `semantic_query` blocks for all reusable data. Bind visible
  graph/table/stat blocks to those outputs.
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
  category is needed, query or create a real semantic field/dataset for it, or
  chart the produced category directly and explain the mapping in markdown.
- When adding `date_filter` or `period_selector` controls, choose defaults that
  cover rows you have verified. For demo/library artifacts, prefer
  `last_90_days` unless you have confirmed `last_30_days` returns data.

Use `artifact_write(action="create")` for a new artifact, `replace` when
rewriting the whole doc, `apply` for targeted edits, and `check` for runtime
validation. If validation fails, correct the doc and call `artifact_write`
again rather than explaining the failure to the parent. Treat
`runtime.success=false`, `diagnostics`, and `key_warnings` as blocking
publication failures. Do not set `run_check=false` to publish a user-facing
artifact.

Final response: return a compact JSON object in text with keys:
`status`, `artifact_id`, `artifact_version`, `touched_blocks`, `diagnostics`,
`runtime_summary`, and `message`.
"""


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
                "configurable": {
                    "thread_id": f"{conversation_id or 'artifact'}:artifact-manager"
                },
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

    async def agent_node(state: AgentState) -> dict[str, Any]:
        state_messages = [m for m in list(state["messages"]) if not isinstance(m, SystemMessage)]
        messages = [SystemMessage(content=ARTIFACT_MANAGER_SYSTEM_PROMPT), *state_messages]
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
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


def _summarize_result(messages: list[Any], final_text: str) -> dict[str, Any]:
    parsed_final = _parse_json_object(final_text)
    artifact_result = _last_artifact_write_result(messages)
    artifact = artifact_result.get("artifact") if isinstance(artifact_result, dict) else None
    runtime = artifact_result.get("runtime") if isinstance(artifact_result, dict) else None
    diagnostics = artifact_result.get("diagnostics") if isinstance(artifact_result, dict) else None
    if isinstance(parsed_final, dict):
        status = parsed_final.get("status") or artifact_result.get("status") or "done"
        message = parsed_final.get("message") or final_text
        touched_blocks = parsed_final.get("touched_blocks") or _touched_blocks_from_artifact_result(
            artifact_result
        )
    else:
        status = artifact_result.get("status") or "done"
        message = final_text or "Artifact manager completed."
        touched_blocks = _touched_blocks_from_artifact_result(artifact_result)
    return {
        "status": status,
        "artifact_id": artifact.get("id") if isinstance(artifact, dict) else None,
        "artifact_version": artifact.get("version") if isinstance(artifact, dict) else None,
        "touched_blocks": touched_blocks,
        "diagnostics": diagnostics or [],
        "runtime_summary": _runtime_summary(runtime),
        "message": message[:1200] if isinstance(message, str) else str(message)[:1200],
    }


def _last_artifact_write_result(messages: list[Any]) -> dict[str, Any]:
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and message.name == "artifact_write":
            parsed = _parse_json_object(message.content)
            return parsed if isinstance(parsed, dict) else {}
    return {}


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
