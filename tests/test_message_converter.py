from langchain_core.messages import AIMessage, HumanMessage

from apps.chat.message_converter import langchain_messages_to_ui
from apps.workspaces.tasks import SYSTEM_RESUME_MARKER


def test_system_resume_markers_are_filtered():
    msgs = [
        HumanMessage(content="Load and analyze"),
        AIMessage(content="I've started loading."),
        HumanMessage(content=f"{SYSTEM_RESUME_MARKER} ..."),
        AIMessage(content="Done — here are the results."),
    ]
    ui = langchain_messages_to_ui(msgs)
    # Each UI message has a "parts" list; gather all text from parts
    flat = str([m.get("parts") for m in ui])
    assert SYSTEM_RESUME_MARKER not in flat
    assert "Load and analyze" in flat
    assert "Done — here are the results." in flat


def test_reasoning_parts_survive_reload():
    """Extended-thinking blocks must be re-emitted as ``reasoning`` parts on
    reload so the Thinking card survives a page refresh / post-materialization
    refetch (arch #246, 13#5)."""
    msgs = [
        HumanMessage(content="Why is revenue down?"),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "Let me reason about the join keys..."},
                {"type": "text", "text": "Revenue dropped because of churn."},
            ]
        ),
    ]
    ui = langchain_messages_to_ui(msgs)
    assistant = next(m for m in ui if m["role"] == "assistant")
    part_types = [p["type"] for p in assistant["parts"]]
    assert "reasoning" in part_types, f"reasoning part missing; got {part_types}"
    reasoning = next(p for p in assistant["parts"] if p["type"] == "reasoning")
    assert "join keys" in reasoning["text"]
    # Reasoning must come before the visible answer text.
    assert part_types.index("reasoning") < part_types.index("text")
    # Visible text still present.
    text = next(p for p in assistant["parts"] if p["type"] == "text")
    assert "churn" in text["text"]


def test_reasoning_part_omitted_when_no_thinking():
    """Plain text AIMessages must not gain a spurious empty reasoning part."""
    msgs = [
        HumanMessage(content="Hi"),
        AIMessage(content="Hello!"),
    ]
    ui = langchain_messages_to_ui(msgs)
    assistant = next(m for m in ui if m["role"] == "assistant")
    assert all(p["type"] != "reasoning" for p in assistant["parts"])
