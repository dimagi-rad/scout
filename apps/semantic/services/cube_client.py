"""Client helpers for Scout's Cube Core service."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import jwt
from django.conf import settings

logger = logging.getLogger(__name__)


class CubeConfigurationError(RuntimeError):
    """Raised when Cube is not configured for live query execution."""


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

        url = f"{self.base_url}/cubejs-api/v1/load"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                url,
                params={"query": json.dumps(cube_query)},
                headers=self._headers(security_context),
            )
            response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
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

    async def invalidate_schema_cache(self, *, security_context: dict[str, Any]) -> None:
        """Force Cube to observe the latest schemaVersion for this context."""
        if not self.is_configured:
            return
        url = f"{self.base_url}/cubejs-api/v1/meta"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers(security_context))
            response.raise_for_status()

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
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{validator_url}/internal/validate-cube-schema",
                json={"schema": content},
                headers={"Authorization": f"Bearer {self.api_secret}"},
            )
            response.raise_for_status()
        return response.json()


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
