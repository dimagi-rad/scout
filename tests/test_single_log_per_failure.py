"""One failure must produce one Sentry-eligible log record (#371).

Sentry's LoggingIntegration promotes every ERROR record into an event, so a
handler that logs at ERROR *and* re-raises produces two events and two issue
groups for one failure. Production showed exactly that: identical pairs of
groups differing only in ``logger`` (SCOUT-DJANGO-2W/2V, 2X/2Y), with matching
timestamps and event counts.

The rule this pins: **a handler that re-raises logs at WARNING; the handler
that stops propagation logs at ERROR.** The WARNING still reaches CloudWatch
and becomes a breadcrumb on the single event.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from mcp_server.services import materializer

# Handlers in run_pipeline that swallow the exception rather than re-raising.
# These are the ONLY log of their failure, so they legitimately stay at ERROR.
_SWALLOWING_HANDLERS = {
    "Failed to generate system assets for %s; continuing pipeline",
    "Failed to generate Connect assets for %s; continuing pipeline",
    "Transform phase failed for schema %s",
    "Rollback failed for source %s; connection may be in a broken state",
}


def _first_string_arg(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        if isinstance(value, str):
            return value
    return None


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    """True if the handler ends by propagating, i.e. a bare `raise`."""
    return any(isinstance(node, ast.Raise) and node.exc is None for node in ast.walk(handler))


def _exception_logs_in_reraising_handlers() -> list[str]:
    """Message templates logged via logger.exception inside a re-raising handler."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(materializer)))
    offenders = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        if not _handler_reraises(handler):
            continue
        for node in ast.walk(handler):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "exception"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                message = _first_string_arg(node)
                if message and message not in _SWALLOWING_HANDLERS:
                    offenders.append(message)
    return offenders


def test_no_reraising_handler_logs_at_error():
    """A re-raising handler must not log.exception — the caller already does.

    Static rather than behavioural because the double-log is a property of the
    code shape, and this catches a new one being added anywhere in the module
    rather than only on the paths a test happens to exercise.
    """
    offenders = _exception_logs_in_reraising_handlers()
    assert offenders == [], (
        "These handlers log at ERROR and then re-raise, so the caller's own "
        f"ERROR log mints a second Sentry group for the same failure: {offenders}. "
        "Use logger.warning(..., exc_info=True) — it still reaches CloudWatch and "
        "becomes a breadcrumb on the caller's event."
    )


@pytest.mark.parametrize(
    "message",
    [
        "Source %s failed for schema %s; earlier sources stay committed",
        "Materialization run %s failed before any source committed",
    ],
)
def test_the_two_known_double_log_sites_are_warnings(message):
    """Pin the specific sites from #371 at WARNING, not merely 'not ERROR'.

    WARNING is below LoggingIntegration's default ``event_level`` of ERROR, so
    the record becomes a breadcrumb instead of an event.
    """
    source = inspect.getsource(materializer)
    idx = source.index(message)
    call_start = source[:idx].rindex("logger.")
    assert source[call_start:idx].startswith("logger.warning"), (
        f"{message!r} must be logged at WARNING; it re-raises and the caller logs the ERROR."
    )
