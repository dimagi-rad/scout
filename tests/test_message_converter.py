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
