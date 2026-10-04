"""One agent double for every chat turn, live or resumed.

``FakeAgent`` is a real compiled LangGraph over the app's ``AgentState``, its answer
node named as the app names it, with a scripted model in place of Claude. So
``astream`` (resumes and the held-request flush, via apps/chat/resume_stream.py),
``astream_events`` (live chat), ``aget_state`` and ``aupdate_state`` (synthetic
failure messages) all keep LangGraph's own contract, and what a turn writes lands
in a checkpoint the test can read back. There is deliberately no ``ainvoke``:
nothing in the app runs a turn that way, and a test that did would skip the
streamed path.

It is one node and one model call with plain-text content: no tool loop, no
subagent-tagged tokens, no Anthropic content blocks. test_resume_stream.py's
own graphs cover how the stream filters and joins those.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import aclosing, contextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from apps.agents.graph.base import AGENT_NODE
from apps.agents.graph.state import AgentState

DEFAULT_REPLY = "There were forty-two visits last week, most of them in Kisumu."


@dataclass(frozen=True)
class AgentRun:
    input_state: dict
    config: dict

    @property
    def messages(self) -> list:
        return self.input_state["messages"]


class FakeAgent:
    """A turn's agent: records each run's input, then answers ``reply``.

    ``during`` runs inside the answer node once the input is checkpointed and
    before the model answers, with the node's state: raise to fail the turn,
    sleep to stall it, write to the database to race it. ``fails_to_start``
    raises before the graph runs, so nothing of the turn is checkpointed;
    ``fails_after_reply`` raises once the answer has streamed, before it is saved.
    """

    def __init__(
        self,
        reply: str = DEFAULT_REPLY,
        *,
        during: Callable[[dict], Awaitable[None]] | None = None,
        fails_to_start: Exception | None = None,
        fails_after_reply: Exception | None = None,
    ):
        self.reply = reply
        self.during = during
        self.fails_to_start = fails_to_start
        self.fails_after_reply = fails_after_reply
        self.checkpointer = InMemorySaver()
        self.runs: list[AgentRun] = []
        self._graph = self._compile()

    def _compile(self):
        async def answer(state: AgentState) -> dict:
            if self.during is not None:
                await self.during(state)
            model = GenericFakeChatModel(messages=iter([AIMessage(content=self.reply)]))
            answered = await model.ainvoke(state["messages"])
            if self.fails_after_reply is not None:
                raise self.fails_after_reply
            return {"messages": [answered]}

        graph = StateGraph(AgentState)
        graph.add_node(AGENT_NODE, answer)
        graph.add_edge(START, AGENT_NODE)
        graph.add_edge(AGENT_NODE, END)
        return graph.compile(checkpointer=self.checkpointer)

    def _record(self, input_state, config) -> None:
        self.runs.append(AgentRun(input_state=input_state, config=config or {}))

    async def _iterate(self, open_stream):
        # As LangGraph's own streams do, a run fails once iterated, not when created.
        if self.fails_to_start is not None:
            raise self.fails_to_start
        async with aclosing(open_stream()) as items:
            async for item in items:
                yield item

    def astream(self, input_state, config=None, **kwargs):
        self._record(input_state, config)
        return self._iterate(lambda: self._graph.astream(input_state, config, **kwargs))

    def astream_events(self, input_state, config=None, **kwargs):
        self._record(input_state, config)
        return self._iterate(lambda: self._graph.astream_events(input_state, config, **kwargs))

    async def aget_state(self, config, **kwargs):
        return await self._graph.aget_state(config, **kwargs)

    async def aupdate_state(self, config, values, **kwargs):
        return await self._graph.aupdate_state(config, values, **kwargs)

    @property
    def last_run(self) -> AgentRun:
        assert self.runs, "the agent never ran"
        return self.runs[-1]

    async def thread_messages(self, thread_id) -> list:
        """The conversation the thread's checkpoint holds."""
        state = await self.aget_state({"configurable": {"thread_id": str(thread_id)}})
        return list(state.values.get("messages", []))


@contextmanager
def serving(agent: FakeAgent):
    """Hand ``agent`` to every place a background turn builds one, and read the
    thread's checkpoint from it, as the real agent and checkpointer share one."""
    build = AsyncMock(return_value=agent)
    with (
        patch("apps.workspaces.tasks._build_agent_for_resume", build),
        patch("apps.workspaces.services.reconciliation.build_agent_for_resume", build),
        patch(
            "apps.chat.pending_requests.ensure_checkpointer",
            AsyncMock(return_value=agent.checkpointer),
        ),
    ):
        yield agent
