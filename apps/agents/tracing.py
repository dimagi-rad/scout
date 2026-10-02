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
    """Return (secret_key, public_key, base_url) from Django settings."""
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

    The handler carries no attribution: session_id, user_id and metadata are ignored
    here and reach Langfuse only through langfuse_trace_context(), which must wrap
    the call. Returns None when Langfuse credentials are unconfigured.
    """
    secret_key, public_key, base_url = _get_langfuse_settings()
    if not all([secret_key, public_key, base_url]):
        return None

    try:
        # Kept lazy so a broken langfuse install only disables tracing.
        from langfuse import Langfuse  # noqa: PLC0415
        from langfuse.langchain import CallbackHandler  # noqa: PLC0415

        Langfuse(secret_key=secret_key, public_key=public_key, base_url=base_url)
        # Pin the handler to this client: get_client() without a key returns a
        # disabled client once any second Langfuse instance exists in the process.
        return CallbackHandler(public_key=public_key)
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
    secret_key, public_key, base_url = _get_langfuse_settings()
    if not all([secret_key, public_key, base_url]):
        return contextlib.nullcontext()

    try:
        from langfuse import propagate_attributes  # noqa: PLC0415

        return propagate_attributes(
            session_id=session_id,
            user_id=user_id,
            metadata=metadata or {},
        )
    except Exception:
        logger.warning("Failed to create Langfuse trace context", exc_info=True)
        return contextlib.nullcontext()


__all__ = ["get_langfuse_callback", "langfuse_trace_context"]
