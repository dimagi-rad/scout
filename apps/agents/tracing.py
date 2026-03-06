"""
Langfuse tracing helper for the Scout agent.

Provides a LangChain CallbackHandler and a trace context manager for per-request
session/user attribution. Returns None / nullcontext gracefully when Langfuse
environment variables are absent.

Langfuse v3 architecture:
- Credentials are passed to Langfuse() to initialize the SDK global client.
- CallbackHandler() hooks into LangChain/LangGraph pipelines for automatic tracing.
- propagate_attributes() is a context manager that stamps session_id/user_id onto
  every span created within its scope.
"""
from __future__ import annotations

import contextlib
import logging

logger = logging.getLogger(__name__)


def _get_langfuse_settings() -> tuple[str, str, str]:
    """Return (secret_key, public_key, host) from Django settings."""
    from django.conf import settings

    return (
        getattr(settings, "LANGFUSE_SECRET_KEY", ""),
        getattr(settings, "LANGFUSE_PUBLIC_KEY", ""),
        getattr(settings, "LANGFUSE_BASE_URL", ""),
    )


def get_langfuse_callback(
    *,
    session_id: str,
    user_id: str,
    metadata: dict | None = None,
):
    """
    Create a Langfuse CallbackHandler for LangGraph tracing.

    Initializes the Langfuse global client with credentials from Django settings,
    then returns a CallbackHandler ready to pass into LangGraph's config["callbacks"].

    Call langfuse_trace_context() alongside this to attach session_id/user_id to
    the trace — use it as a context manager wrapping the astream_events call.

    Returns None when LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, or LANGFUSE_BASE_URL
    are not configured, so Langfuse is fully optional.
    """
    secret_key, public_key, host = _get_langfuse_settings()
    if not all([secret_key, public_key, host]):
        return None

    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        Langfuse(secret_key=secret_key, public_key=public_key, host=host)
        return CallbackHandler()
    except Exception:
        logger.warning("Failed to initialize Langfuse CallbackHandler", exc_info=True)
        return None


def langfuse_trace_context(
    *,
    session_id: str,
    user_id: str,
    metadata: dict | None = None,
) -> contextlib.AbstractContextManager:
    """
    Return a context manager that stamps session_id/user_id onto Langfuse traces.

    Use this to wrap the astream_events call in chat_view so every span within
    that streaming response is tagged with the correct session and user.

    Returns a no-op nullcontext when Langfuse is not configured.
    """
    secret_key, public_key, host = _get_langfuse_settings()
    if not all([secret_key, public_key, host]):
        return contextlib.nullcontext()

    try:
        from langfuse import propagate_attributes

        return propagate_attributes(
            session_id=session_id,
            user_id=user_id,
            metadata=metadata or {},
        )
    except Exception:
        logger.warning("Failed to create Langfuse trace context", exc_info=True)
        return contextlib.nullcontext()


__all__ = ["get_langfuse_callback", "langfuse_trace_context"]
