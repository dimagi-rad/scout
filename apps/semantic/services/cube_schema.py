"""Build, validate, and promote Cube schemas for semantic models."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import close_old_connections, transaction

from apps.semantic.models import CubeSchema, SemanticModel
from apps.semantic.services.catalog import ensure_semantic_model
from apps.semantic.services.cube import generate_cube_schema_yaml
from apps.semantic.services.cube_client import CubeClient
from mcp_server.context import QueryContext, load_workspace_context

logger = logging.getLogger(__name__)


class CubeSchemaBuildError(RuntimeError):
    """Raised when generated Cube schema content cannot be promoted."""


def ensure_cube_schema(workspace, *, model: SemanticModel | None = None) -> CubeSchema:
    """Return an active CubeSchema, building one when absent."""
    model = model or ensure_semantic_model(workspace)
    active = (
        CubeSchema.objects.filter(
            workspace=workspace,
            semantic_model=model,
            status=CubeSchema.Status.ACTIVE,
        )
        .order_by("-updated_at")
        .first()
    )
    if active is not None:
        return active
    return build_and_promote_cube_schema(workspace, model=model)


def build_and_promote_cube_schema(workspace, *, model: SemanticModel | None = None) -> CubeSchema:
    """Generate Cube YAML, validate it, and promote it if valid."""
    try:
        close_old_connections()
        model = model or ensure_semantic_model(workspace)
        content = generate_cube_schema_yaml(model)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        filename = f"workspace_{workspace.id}_{content_hash[:12]}.yaml"
        validation = async_to_sync(CubeClient().validate_schema)(content)
        diagnostics = _diagnostics_from_validation(validation)

        if not validation.get("valid", False):
            CubeSchema.objects.update_or_create(
                workspace=workspace,
                semantic_model=model,
                filename=filename,
                defaults={
                    "content": content,
                    "content_hash": content_hash,
                    "status": CubeSchema.Status.ERROR,
                    "diagnostics": diagnostics,
                },
            )
            model.status = SemanticModel.Status.ERROR
            model.diagnostics = diagnostics
            model.save(update_fields=["status", "diagnostics", "updated_at"])
            if settings.CUBE_SCHEMA_VALIDATION_REQUIRED:
                raise CubeSchemaBuildError("Generated Cube schema failed validation.")
            raise CubeSchemaBuildError(_diagnostics_message(diagnostics))

        with transaction.atomic():
            CubeSchema.objects.filter(
                workspace=workspace,
                semantic_model=model,
                status=CubeSchema.Status.ACTIVE,
            ).update(status=CubeSchema.Status.DRAFT)
            cube_schema, _ = CubeSchema.objects.update_or_create(
                workspace=workspace,
                semantic_model=model,
                filename=filename,
                defaults={
                    "content": content,
                    "content_hash": content_hash,
                    "status": CubeSchema.Status.ACTIVE,
                    "diagnostics": diagnostics,
                },
            )
            model.status = SemanticModel.Status.ACTIVE
            model.save(update_fields=["status", "updated_at"])

        try:
            ctx = async_to_sync(load_workspace_context)(str(workspace.id))
            async_to_sync(CubeClient().invalidate_schema_cache)(
                security_context=build_cube_security_context(
                    workspace,
                    model,
                    cube_schema,
                    ctx,
                )
            )
        except Exception:
            logger.exception("Failed to invalidate Cube schema cache for workspace %s", workspace.id)

        return cube_schema
    finally:
        close_old_connections()


def build_cube_security_context(
    workspace,
    model: SemanticModel,
    cube_schema: CubeSchema,
    ctx: QueryContext,
    *,
    user_id: str = "",
) -> dict[str, Any]:
    """Security context embedded in Cube JWTs and consumed by cube_config/cube.js."""
    return {
        "workspaceId": str(workspace.id),
        "userId": str(user_id or ""),
        "semanticModelId": str(model.id),
        "semanticModelVersion": model.version,
        "cubeSchemaId": str(cube_schema.id),
        "cubeSchemaHash": cube_schema.content_hash,
        "schemaName": ctx.schema_name,
        "readonlyRole": ctx.readonly_role,
        "dataSourceType": "postgres",
    }


def _diagnostics_from_validation(validation: dict[str, Any]) -> list[dict[str, Any]]:
    errors = validation.get("errors") or []
    diagnostics: list[dict[str, Any]] = []
    for error in errors:
        diagnostics.append({"level": "error", "message": str(error)})
    if validation.get("skipped"):
        diagnostics.append(
            {
                "level": "warning",
                "message": "Cube schema validation skipped because CUBE_VALIDATOR_URL is not configured.",
            }
        )
    return diagnostics


def _diagnostics_message(diagnostics: list[dict[str, Any]]) -> str:
    for diagnostic in diagnostics:
        if diagnostic.get("level") == "error" and diagnostic.get("message"):
            return str(diagnostic["message"])
    return "Generated Cube schema failed validation."
