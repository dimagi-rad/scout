from apps.agents.graph.state import AgentState


def test_agent_state_has_thread_id_field():
    # TypedDict membership check.
    assert "thread_id" in AgentState.__annotations__
