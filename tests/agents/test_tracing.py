"""Tests for Langfuse tracing helper."""

import contextlib

import pytest
from langfuse.langchain import CallbackHandler

from apps.agents.tracing import get_langfuse_callback, langfuse_trace_context


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
