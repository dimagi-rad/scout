"""Tests for Langfuse tracing helper."""

import contextlib
import uuid

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apps.agents.tracing import get_langfuse_callback, langfuse_trace_context
from apps.workspaces.tasks import _resume_langfuse_span


@pytest.mark.django_db
def test_get_langfuse_callback_returns_none_when_not_configured(settings):
    """Returns None gracefully when Langfuse env vars are not set."""
    settings.LANGFUSE_SECRET_KEY = ""
    settings.LANGFUSE_PUBLIC_KEY = ""
    settings.LANGFUSE_BASE_URL = ""

    result = get_langfuse_callback(session_id="s1", user_id="u1")
    assert result is None


@pytest.mark.django_db
def test_get_langfuse_callback_returns_none_when_partially_configured(settings):
    """Returns None when only some Langfuse env vars are set."""
    settings.LANGFUSE_SECRET_KEY = "sk-test"
    settings.LANGFUSE_PUBLIC_KEY = ""
    settings.LANGFUSE_BASE_URL = "https://cloud.langfuse.com"

    result = get_langfuse_callback(session_id="s1", user_id="u1")
    assert result is None


@pytest.mark.django_db
def test_get_langfuse_callback_returns_handler_when_configured(settings):
    """Returns a CallbackHandler when all three env vars are set."""
    settings.LANGFUSE_SECRET_KEY = "sk-test"
    settings.LANGFUSE_PUBLIC_KEY = "pk-test"
    settings.LANGFUSE_BASE_URL = "https://cloud.langfuse.com"

    result = get_langfuse_callback(
        session_id="thread-abc",
        user_id="user-123",
        metadata={"tenant_id": "my-domain"},
    )
    assert isinstance(result, CallbackHandler)


@pytest.mark.django_db
def test_get_langfuse_callback_default_metadata(settings):
    """Metadata defaults to empty dict if not provided."""
    settings.LANGFUSE_SECRET_KEY = "sk-test"
    settings.LANGFUSE_PUBLIC_KEY = "pk-test"
    settings.LANGFUSE_BASE_URL = "https://cloud.langfuse.com"

    # Should not raise even when metadata is omitted
    result = get_langfuse_callback(session_id="s1", user_id="u1")
    assert result is not None


@pytest.mark.django_db
def test_langfuse_trace_context_returns_nullcontext_when_not_configured(settings):
    """Returns a nullcontext when Langfuse is not configured."""

    settings.LANGFUSE_SECRET_KEY = ""
    settings.LANGFUSE_PUBLIC_KEY = ""
    settings.LANGFUSE_BASE_URL = ""

    ctx = langfuse_trace_context(session_id="s1", user_id="u1")
    assert isinstance(ctx, contextlib.AbstractContextManager)


@pytest.mark.django_db
def test_langfuse_trace_context_returns_context_when_configured(settings):
    """Returns a context manager when configured."""

    settings.LANGFUSE_SECRET_KEY = "sk-test"
    settings.LANGFUSE_PUBLIC_KEY = "pk-test"
    settings.LANGFUSE_BASE_URL = "https://cloud.langfuse.com"

    ctx = langfuse_trace_context(session_id="thread-abc", user_id="user-123")
    assert isinstance(ctx, contextlib.AbstractContextManager)


@pytest.fixture
def langfuse_spans(settings):
    """Route the configured Langfuse client into an in-memory exporter.

    A fresh public key per test keeps the SDK's per-key client singleton from
    leaking exporters between tests."""
    public_key = f"pk-test-{uuid.uuid4().hex}"
    settings.LANGFUSE_SECRET_KEY = "sk-test"
    settings.LANGFUSE_PUBLIC_KEY = public_key
    settings.LANGFUSE_BASE_URL = "http://localhost:1"
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=public_key,
        secret_key="sk-test",
        base_url="http://localhost:1",
        span_exporter=exporter,
        tracer_provider=TracerProvider(),
    )

    # A second instance makes get_client() without a key return a disabled client,
    # so these tests fail if the handler stops passing public_key.
    other = Langfuse(
        public_key=f"pk-other-{uuid.uuid4().hex}",
        secret_key="sk-test",
        base_url="http://localhost:1",
        span_exporter=InMemorySpanExporter(),
        tracer_provider=TracerProvider(),
    )

    def finished():
        client.flush()
        return exporter.get_finished_spans()

    yield finished
    client.shutdown()
    other.shutdown()


def _agent_like_chain():
    return RunnableLambda(lambda prompt: prompt) | FakeListChatModel(responses=["the answer"])


def _observation_type(span):
    return span.attributes.get("langfuse.observation.type")


@pytest.mark.asyncio
async def test_chat_turn_propagates_session_to_every_observation(langfuse_spans):
    handler = get_langfuse_callback(session_id="thread-abc", user_id="user-123")
    with langfuse_trace_context(
        session_id="thread-abc", user_id="user-123", metadata={"workspace_id": "ws-1"}
    ):
        await _agent_like_chain().ainvoke("question", config={"callbacks": [handler]})

    spans = langfuse_spans()
    assert "generation" in {_observation_type(s) for s in spans}
    for span in spans:
        assert span.attributes.get("session.id") == "thread-abc", span.name
        assert span.attributes.get("user.id") == "user-123", span.name
        assert span.attributes.get("langfuse.trace.metadata.workspace_id") == "ws-1", span.name


@pytest.mark.asyncio
async def test_resume_span_is_root_and_propagates_session_to_generations(langfuse_spans):
    handler = get_langfuse_callback(session_id="thread-abc", user_id="user-123")
    with _resume_langfuse_span(
        thread_job_id="tj-1",
        thread_id="thread-abc",
        user_id="user-123",
        workspace_id="ws-1",
        status="completed",
    ) as span:
        result = await _agent_like_chain().ainvoke("question", config={"callbacks": [handler]})
        span.update(output=result.content)

    spans = langfuse_spans()
    roots = [s for s in spans if s.parent is None]
    assert [r.name for r in roots] == ["resume_thread_after_materialization"]
    assert "the answer" in roots[0].attributes["langfuse.observation.output"]
    assert "tj-1" in roots[0].attributes["langfuse.observation.input"]
    assert "generation" in {_observation_type(s) for s in spans}
    assert {s.context.trace_id for s in spans} == {roots[0].context.trace_id}
    for s in spans:
        assert s.attributes.get("session.id") == "thread-abc", s.name
        assert s.attributes.get("user.id") == "user-123", s.name


def test_resume_span_yields_none_when_not_configured(settings):
    settings.LANGFUSE_SECRET_KEY = ""
    settings.LANGFUSE_PUBLIC_KEY = ""
    settings.LANGFUSE_BASE_URL = ""

    with _resume_langfuse_span(
        thread_job_id="tj-1",
        thread_id="thread-abc",
        user_id="user-123",
        workspace_id="ws-1",
        status="completed",
    ) as span:
        assert span is None
