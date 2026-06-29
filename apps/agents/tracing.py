"""
Langfuse tracing helper for the Scout agent.

Provides a LangChain CallbackHandler and a trace context manager for per-request
session/user attribution. Returns None / nullcontext when Langfuse env vars are
absent, so tracing is fully optional.
"""

from __future__ import annotations

import contextlib
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _get_langfuse_settings() -> tuple[str, str, str]:
    """Return (secret_key, public_key, host) from Django settings."""
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
    """Create a Langfuse CallbackHandler for LangGraph's config["callbacks"].

    Pair with langfuse_trace_context() (wrapping the astream_events call) to attach
    session_id/user_id. Returns None when Langfuse credentials are unconfigured.
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
    """Context manager that stamps session_id/user_id onto every Langfuse span in
    its scope. Wrap the astream_events call with it. Returns a no-op nullcontext
    when Langfuse is not configured.
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
