"""History pruning must never drop the request the current turn is answering (#919).

KK's revisit-opportunity question was asked into a workspace that was still
loading. The resume turn ran parallel tool calls; after seven rounds the
20-message window had slid past both the resume marker and the question, so
the model concluded the "message came through empty" and replied with a
generic menu of things to look at.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from apps.agents.graph.state import DEFAULT_MAX_MESSAGES, prune_messages
from apps.chat.constants import SYSTEM_RESUME_MARKER

KK_QUESTION = (
    "what is the projected increase in number of children gained from the revisit "
    "opportunity? i.e. compared to the number of health service delivery visits done "
    "in the primary opportunity for eha (opp name: CHC - NG - EHA - RCT - AUG 26), what "
    "is the projected % increase in hsd visits from the revisit opp (opp name: CHC - NG - "
    "EHA - RCT - AUG 26 - R1) The R1 opps is still in progress and has not completed. "
    "Also, it was not launched for all wards , so you may first want to decide the "
    "projection rules."
)


def _parallel_tool_rounds(rounds: int, calls_per_round: int = 2, prefix: str = "t") -> list:
    messages = []
    for r in range(rounds):
        calls = [
            {"name": "query", "args": {"sql": "SELECT 1"}, "id": f"{prefix}{r}-{c}"}
            for c in range(calls_per_round)
        ]
        messages.append(AIMessage(content="", tool_calls=calls))
        messages += [
            ToolMessage(content="{}", tool_call_id=call["id"], name="query") for call in calls
        ]
    return messages


def _texts(messages) -> list[str]:
    return [m.content for m in messages if isinstance(m, HumanMessage)]


def _assert_tool_pairs_intact(messages) -> None:
    issued = {call["id"] for m in messages if isinstance(m, AIMessage) for call in m.tool_calls}
    for m in messages:
        if isinstance(m, ToolMessage):
            assert m.tool_call_id in issued


def test_short_history_is_untouched():
    history = [HumanMessage(content="hi"), AIMessage(content="hello")]
    assert prune_messages(history) == history


def test_kk_resume_turn_keeps_marker_and_question():
    marker = HumanMessage(content=f"{SYSTEM_RESUME_MARKER} Materialization just completed.")
    question = HumanMessage(content=KK_QUESTION)
    history = [marker, question, *_parallel_tool_rounds(10)]

    pruned = prune_messages(history)

    assert len(pruned) <= DEFAULT_MAX_MESSAGES
    assert _texts(pruned) == [marker.content, KK_QUESTION]
    assert pruned[:2] == [marker, question]
    assert pruned[-1] is history[-1]
    _assert_tool_pairs_intact(pruned)


def test_resume_marker_alone_keeps_the_question_it_resumes():
    """Without a held request the question sits in the turn that started the load."""
    question = HumanMessage(content=KK_QUESTION)
    started = AIMessage(
        content="", tool_calls=[{"name": "run_materialization", "args": {}, "id": "load"}]
    )
    ack = ToolMessage(content="started", tool_call_id="load", name="run_materialization")
    marker = HumanMessage(content=f"{SYSTEM_RESUME_MARKER} Materialization just completed.")
    history = [
        question,
        started,
        ack,
        AIMessage(content="Loading."),
        marker,
        *_parallel_tool_rounds(10),
    ]

    pruned = prune_messages(history)

    assert len(pruned) <= DEFAULT_MAX_MESSAGES
    assert _texts(pruned) == [KK_QUESTION, marker.content]
    _assert_tool_pairs_intact(pruned)


def test_earlier_turns_still_age_out():
    history = []
    for i in range(30):
        history += [HumanMessage(content=f"q{i}"), AIMessage(content=f"a{i}")]
    history += [HumanMessage(content="latest"), *_parallel_tool_rounds(10)]

    pruned = prune_messages(history)

    assert _texts(pruned) == ["q29", "latest"]
    assert len(pruned) <= DEFAULT_MAX_MESSAGES


def test_short_reply_keeps_the_question_it_answers():
    """Agreeing to a reload: question, "yes", load, then the resume marker."""
    question = HumanMessage(content=KK_QUESTION)
    marker = HumanMessage(content=f"{SYSTEM_RESUME_MARKER} Materialization just completed.")
    history = [
        question,
        AIMessage(content="The data isn't loaded. Load it now?"),
        HumanMessage(content="yes"),
        AIMessage(content="", tool_calls=[{"name": "run_materialization", "args": {}, "id": "l"}]),
        ToolMessage(content="started", tool_call_id="l", name="run_materialization"),
        AIMessage(content="Loading."),
        marker,
        *_parallel_tool_rounds(10),
    ]

    pruned = prune_messages(history)

    assert _texts(pruned) == [KK_QUESTION, "yes", marker.content]
    assert history[1] in pruned
    _assert_tool_pairs_intact(pruned)


def test_an_earlier_turns_wide_round_still_ages_out():
    history = [
        HumanMessage(content="earlier"),
        *_parallel_tool_rounds(1, calls_per_round=25, prefix="old"),
        AIMessage(content="Done."),
        HumanMessage(content="next"),
        *_parallel_tool_rounds(1, calls_per_round=3, prefix="new"),
    ]

    pruned = prune_messages(history)

    assert len(pruned) <= DEFAULT_MAX_MESSAGES
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id.startswith("old") for m in pruned)
    _assert_tool_pairs_intact(pruned)


def test_resume_into_a_window_filling_round_keeps_the_question_over_the_marker():
    question = HumanMessage(content=KK_QUESTION)
    marker = HumanMessage(content=f"{SYSTEM_RESUME_MARKER} Materialization just completed.")
    history = [
        question,
        AIMessage(
            content="", tool_calls=[{"name": "run_materialization", "args": {}, "id": "load"}]
        ),
        ToolMessage(content="started", tool_call_id="load", name="run_materialization"),
        AIMessage(content="Loading."),
        marker,
        *_parallel_tool_rounds(1, calls_per_round=19),
    ]

    pruned = prune_messages(history)

    # The round alone fills the window; the question outranks the bound and the marker.
    assert pruned == [question, *history[-20:]]
    _assert_tool_pairs_intact(pruned)


def test_continue_after_step_limit_keeps_the_question():
    history = [
        HumanMessage(content=KK_QUESTION),
        *_parallel_tool_rounds(3),
        AIMessage(content="I ran out of steps. Ask me to continue."),
        HumanMessage(content="continue"),
        *_parallel_tool_rounds(10),
    ]

    pruned = prune_messages(history)

    assert _texts(pruned) == [KK_QUESTION, "continue"]
    assert len(pruned) <= DEFAULT_MAX_MESSAGES


def test_round_filling_the_window_sheds_older_pins_first():
    history = [
        HumanMessage(content="first"),
        AIMessage(content="Which one?"),
        HumanMessage(content="a"),
        HumanMessage(content="b"),
        HumanMessage(content="c"),
        *_parallel_tool_rounds(1, calls_per_round=16),
    ]

    pruned = prune_messages(history)

    assert len(pruned) <= DEFAULT_MAX_MESSAGES
    assert pruned[-17:] == history[-17:]
    assert _texts(pruned)[-1] == "c"
    _assert_tool_pairs_intact(pruned)


def test_round_wider_than_window_keeps_its_results():
    history = [HumanMessage(content=KK_QUESTION), *_parallel_tool_rounds(2, calls_per_round=25)]

    pruned = prune_messages(history)

    last_round = history[-26:]
    assert pruned[-26:] == last_round
    assert _texts(pruned) == [KK_QUESTION]
    _assert_tool_pairs_intact(pruned)


def test_request_inside_window_changes_nothing():
    history = []
    for i in range(30):
        history += [HumanMessage(content=f"q{i}"), AIMessage(content=f"a{i}")]

    assert prune_messages(history) == history[-DEFAULT_MAX_MESSAGES:]


def test_system_messages_are_kept_first():
    system = SystemMessage(content="sys")
    history = [system, HumanMessage(content=KK_QUESTION), *_parallel_tool_rounds(10)]

    pruned = prune_messages(history)

    assert pruned[0] is system
    assert pruned[1].content == KK_QUESTION
