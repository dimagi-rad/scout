"""Client helpers for Scout's Cube Core service."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from typing import Any

import httpx
import jwt
from django.conf import settings

from apps.common.errors import ExpectedStateError

logger = logging.getLogger(__name__)

# Cube's /v1/load long-polls up to continueWaitTimeout (~5s default) and then
# returns {"error": "Continue wait"}; the caller is expected to re-issue the
# same request until the result is ready. Budget enough re-polls to cover the
# 30s Postgres statement_timeout plus compile overhead.
CONTINUE_WAIT_ERROR = "continue wait"
QUERY_TOTAL_TIMEOUT_SECONDS = 60.0
CONTINUE_WAIT_POLL_DELAY_SECONDS = 0.5
MAX_TRANSIENT_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.5
RETRYABLE_HTTP_STATUSES = {429, 502, 503, 504}
TRANSIENT_ERROR_MARKERS = (
    "connection terminated due to connection timeout",
    "connection terminated unexpectedly",
    "connection reset by peer",
    "connection refused",
    "econnreset",
    "econnrefused",
    "etimedout",
    "eai_again",
    "too many clients already",
    "the database system is starting up",
    "the database system is shutting down",
)

SCHEMA_REQUEST_ATTEMPTS = 3
# Long enough for a restarted validator or a busy Cube to come back.
SCHEMA_RETRY_BASE_DELAY_SECONDS = 2.0
# The validator abandons a compile at 60s (CUBE_VALIDATOR_COMPILE_TIMEOUT_MS) and
# answers 503; reading past that lets its answer arrive instead of a ReadTimeout.
VALIDATE_TIMEOUT = httpx.Timeout(70.0, connect=5.0)
# One wall-clock budget across attempts: fast failures (a restarting validator,
# a dropped connection) get their retries, but a compile that is simply too slow
# cannot triple a canvas commit's wait. See INTERACTIVE_VALIDATOR_SLOT_WAIT_SECONDS.
VALIDATE_BUDGET_SECONDS = 70.0
# /v1/meta only starts the compile: Cube finishes it after the client disconnects
# (large multi-source models outlast 30s, SCOUT-DJANGO-3N), so neither a longer
# wait nor a retry buys anything, and the warm-up can run on a request thread.
META_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
META_ATTEMPTS = 1
# Retrying with less than this left would only manufacture another timeout.
MIN_SCHEMA_ATTEMPT_SECONDS = 5.0


class CubeConfigurationError(RuntimeError):
    """Raised when Cube is not configured for live query execution."""


class CubeQueryError(RuntimeError):
    """Raised when Cube accepts the request but rejects the query payload."""


class CubeAuthenticationError(CubeQueryError):
    """Scout's server-to-server Cube credentials were rejected; not a user grant."""


class CubeConnectionError(RuntimeError):
    """A transient Cube failure, distinct from an invalid semantic query."""


class CubeServiceUnavailable(CubeConnectionError, ExpectedStateError):
    """Cube or its validator stayed unreachable through a schema operation's retries.

    Expected under ``apps.common.errors``' test: overload and restarts are routine
    for a single Cube container; the caller records the failed build (or skips a
    warm-up) at WARNING; and the last good schema keeps serving until the next
    rebuild retries.
    """


class CubeClient:
    """Small REST client for Cube Core."""

    def __init__(self, *, base_url: str | None = None, api_secret: str | None = None) -> None:
        self.base_url = (base_url if base_url is not None else settings.CUBE_API_URL).rstrip("/")
        self.api_secret = api_secret if api_secret is not None else settings.CUBEJS_API_SECRET

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_secret)

    def _headers(self, security_context: dict[str, Any]) -> dict[str, str]:
        if not self.api_secret:
            raise CubeConfigurationError("CUBEJS_API_SECRET is not configured.")
        token = jwt.encode(security_context, self.api_secret, algorithm="HS256")
        return {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    async def execute_query(
        self,
        cube_query: dict[str, Any],
        *,
        security_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a Cube query and return Scout's tabular result shape."""
        if not self.base_url:
            raise CubeConfigurationError("CUBE_API_URL is not configured.")

        # POST rather than GET: filter-heavy queries can exceed URL limits.
        url = f"{self.base_url}/cubejs-api/v1/load"
        headers = self._headers(security_context)
        deadline = time.monotonic() + QUERY_TOTAL_TIMEOUT_SECONDS
        # One wall-clock budget includes HTTP calls, transient retries, and
        # Continue-wait polling. Never multiply the timeout by the attempts.
        try:
            async with asyncio.timeout(QUERY_TOTAL_TIMEOUT_SECONDS):
                payload = await self._load(url, headers, cube_query, deadline)
        except TimeoutError as exc:
            raise CubeConnectionError(
                f"Cube query timed out after {QUERY_TOTAL_TIMEOUT_SECONDS:.0f}s."
            ) from exc
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise TypeError("Cube returned an unexpected data payload.")
        columns = _columns_from_cube_payload(data, payload)
        rows = [[row.get(column) for column in columns] for row in data]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }

    async def _load(
        self,
        url: str,
        headers: dict[str, str],
        cube_query: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        transient_failures = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                retry_reason = None
                retry_after = None
                last_error = None
                try:
                    response = await client.post(
                        url,
                        json={"query": cube_query},
                        headers=headers,
                        timeout=min(30.0, remaining),
                    )
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    error = payload.get("error") if isinstance(payload, dict) else None
                    # Authentication and malformed requests always fail fast,
                    # even if their message happens to mention a connection.
                    if response.status_code in {401, 403}:
                        raise CubeAuthenticationError(
                            str(error) if error else "Cube rejected Scout's service credentials."
                        )
                    if response.status_code in {400, 404, 422}:
                        if error:
                            raise CubeQueryError(str(error))
                        response.raise_for_status()
                    if response.status_code in RETRYABLE_HTTP_STATUSES:
                        retry_reason = f"http_{response.status_code}"
                        with suppress(ValueError):
                            retry_after = max(0.0, float(response.headers.get("Retry-After", "")))
                    elif error and any(
                        marker in str(error).lower() for marker in TRANSIENT_ERROR_MARKERS
                    ):
                        retry_reason = "upstream_connection"
                    else:
                        if (
                            response.is_success
                            and isinstance(error, str)
                            and error.strip().lower() == CONTINUE_WAIT_ERROR
                        ):
                            await asyncio.sleep(CONTINUE_WAIT_POLL_DELAY_SECONDS)
                            continue
                        if error:
                            raise CubeQueryError(str(error))
                        response.raise_for_status()
                        if not isinstance(payload, dict) or "data" not in payload:
                            raise CubeConnectionError("Cube returned an invalid query response.")
                        if transient_failures:
                            logger.info(
                                "Cube query recovered after %s transient failure(s)",
                                transient_failures,
                            )
                        return payload
                except httpx.TransportError as exc:
                    retry_reason = "transport"
                    last_error = exc

                transient_failures += 1
                if transient_failures >= MAX_TRANSIENT_ATTEMPTS:
                    logger.warning("Cube query exhausted transient retries (%s)", retry_reason)
                    raise CubeConnectionError(
                        "Cube is temporarily unavailable. Please retry the query."
                    ) from last_error
                # This is a read-only /load operation despite using POST. Keep
                # the identical query and authorization context across retries.
                delay = RETRY_BASE_DELAY_SECONDS * (2 ** (transient_failures - 1))
                delay *= random.uniform(0.5, 1.5)  # noqa: S311 -- retry jitter, not security
                if retry_after is not None:
                    delay = max(delay, retry_after)
                logger.warning(
                    "Retrying Cube query after transient failure (%s), retry %s/%s",
                    retry_reason,
                    transient_failures,
                    MAX_TRANSIENT_ATTEMPTS - 1,
                )
                if delay >= deadline - time.monotonic():
                    raise CubeConnectionError(
                        "Cube retry delay exceeds the remaining query timeout budget. Please retry later."
                    ) from last_error
                await asyncio.sleep(delay)

    async def invalidate_schema_cache(self, *, security_context: dict[str, Any]) -> None:
        """Force Cube to observe the latest schemaVersion for this context."""
        if not self.is_configured:
            return
        await self._schema_request(
            "GET",
            f"{self.base_url}/cubejs-api/v1/meta",
            operation="schema warm-up",
            limits=META_TIMEOUT,
            attempts=META_ATTEMPTS,
            budget_seconds=META_TIMEOUT.read,
            headers=self._headers(security_context),
        )

    async def _schema_request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        limits: httpx.Timeout,
        budget_seconds: float,
        attempts: int = SCHEMA_REQUEST_ATTEMPTS,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a schema-maintenance request, retrying only transient failures.

        Retrying validation is cheap: the validator joins a repeated request to
        the compile already in flight for that schema hash.
        """
        if attempts < 1:
            raise ValueError(f"Cube {operation} needs at least one attempt.")
        last_error: Exception | None = None
        deadline = time.monotonic() + budget_seconds
        async with httpx.AsyncClient(timeout=limits) as client:
            for attempt in range(1, attempts + 1):
                remaining = deadline - time.monotonic()
                try:
                    response = await client.request(
                        method,
                        url,
                        timeout=httpx.Timeout(min(limits.read, remaining), connect=limits.connect),
                        **kwargs,
                    )
                    response.raise_for_status()
                except httpx.TransportError as exc:
                    # A budget-shortened retry timing out says less than the 503 before it.
                    if not (
                        isinstance(exc, httpx.TimeoutException)
                        and isinstance(last_error, httpx.HTTPStatusError)
                    ):
                        last_error = exc
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in RETRYABLE_HTTP_STATUSES:
                        raise
                    last_error = exc
                else:
                    if attempt > 1:
                        logger.info(
                            "Cube %s recovered after %s transient failure(s)",
                            operation,
                            attempt - 1,
                        )
                    return response
                delay = SCHEMA_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                delay *= random.uniform(0.5, 1.5)  # noqa: S311 -- retry jitter, not security
                remaining = deadline - time.monotonic() - delay
                if attempt == attempts or remaining < MIN_SCHEMA_ATTEMPT_SECONDS:
                    break
                logger.info(
                    "Retrying Cube %s after transient failure (%s), retry %s/%s",
                    operation,
                    _describe_transient(last_error),
                    attempt,
                    attempts - 1,
                )
                await asyncio.sleep(delay)
        raise CubeServiceUnavailable(
            f"Cube {operation} failed after {attempt} attempt(s): {_describe_transient(last_error)}"
        ) from last_error

    async def validate_schema(self, content: str) -> dict[str, Any]:
        """Validate Cube YAML through the optional validator sidecar."""
        validator_url = settings.CUBE_VALIDATOR_URL.rstrip("/")
        if not validator_url:
            has_content = bool(content.strip())
            return {
                "valid": has_content,
                "errors": [] if has_content else ["Cube schema content is empty."],
                "skipped": True,
            }
        if not self.api_secret:
            raise CubeConfigurationError("CUBEJS_API_SECRET is not configured.")
        response = await self._schema_request(
            "POST",
            f"{validator_url}/internal/validate-cube-schema",
            operation="schema validation",
            limits=VALIDATE_TIMEOUT,
            budget_seconds=VALIDATE_BUDGET_SECONDS,
            json={"schema": content},
            headers={"Authorization": f"Bearer {self.api_secret}"},
        )
        return response.json()


def _describe_transient(error: Exception | None) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def _columns_from_cube_payload(data: list[dict[str, Any]], payload: dict[str, Any]) -> list[str]:
    annotation = payload.get("annotation") or {}
    ordered = []
    for section in ("timeDimensions", "dimensions", "measures"):
        section_payload = annotation.get(section) or {}
        if isinstance(section_payload, dict):
            ordered.extend(section_payload.keys())
    if ordered:
        return [column for column in ordered if any(column in row for row in data)]
    if not data:
        return []
    columns: list[str] = []
    for row in data:
        for column in row:
            if column not in columns:
                columns.append(column)
    return columns
