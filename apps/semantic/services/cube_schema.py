"""Build, validate, and promote Cube schemas for semantic models."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.semantic.models import CubeSchema, SemanticModel
from apps.semantic.services.catalog import ensure_semantic_model
from apps.semantic.services.cube import generate_cube_schema_yaml
from apps.semantic.services.cube_client import CubeClient
from mcp_server.context import QueryContext, load_workspace_context

logger = logging.getLogger(__name__)

# Inactive (DRAFT/ERROR) schema rows kept per model for debugging; older ones
# are pruned at promote time so rebuilds don't accumulate rows forever.
KEEP_INACTIVE_CUBE_SCHEMAS = 5


class CubeSchemaBuildError(RuntimeError):
    """Raised when generated Cube schema content cannot be promoted."""


def get_active_cube_schema(workspace, *, model: SemanticModel) -> CubeSchema:
    """Return the current active Cube schema without generating a new one."""
    active = (
        CubeSchema.objects.filter(
            workspace=workspace,
            semantic_model=model,
            status=CubeSchema.Status.ACTIVE,
        )
        .order_by("-updated_at")
        .first()
    )
    if active is None:
        raise CubeSchemaBuildError("No active Cube schema is available. Refresh workspace data.")
    return active


def build_and_promote_cube_schema(workspace, *, model: SemanticModel | None = None) -> CubeSchema:
    """Generate Cube YAML, validate it, and promote it if valid.

    A failed build must not take down a workspace that already has an ACTIVE
    schema: the previous schema keeps serving (Cube reads the ACTIVE row) and
    the model stays readable. The failure is recorded on
    ``model.metadata["last_build"]`` so the resume task can disclose it; the
    model is flipped to ERROR only when there is no active schema to fall
    back to.
    """
    try:
        close_old_connections()
        model = model or ensure_semantic_model(workspace)
        try:
            return _build_validate_and_promote(workspace, model)
        except Exception as exc:
            _record_build_failure(workspace, model, exc)
            raise
    finally:
        close_old_connections()


def _build_validate_and_promote(workspace, model: SemanticModel) -> CubeSchema:
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
        stale_ids = list(
            CubeSchema.objects.filter(workspace=workspace, semantic_model=model)
            .exclude(status=CubeSchema.Status.ACTIVE)
            .order_by("-updated_at")
            .values_list("id", flat=True)[KEEP_INACTIVE_CUBE_SCHEMAS:]
        )
        if stale_ids:
            CubeSchema.objects.filter(id__in=stale_ids).delete()
        model.status = SemanticModel.Status.ACTIVE
        model.diagnostics = diagnostics
        _set_last_build(model, ok=True, content_hash=content_hash)
        model.save(update_fields=["status", "diagnostics", "metadata", "updated_at"])

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


def _set_last_build(model: SemanticModel, *, ok: bool, error: str = "", content_hash: str = "") -> None:
    entry: dict[str, Any] = {"ok": ok, "at": timezone.now().isoformat()}
    if error:
        entry["error"] = error[:500]
    if content_hash:
        entry["content_hash"] = content_hash
    model.metadata = {**(model.metadata or {}), "last_build": entry}


def _record_build_failure(workspace, model: SemanticModel, exc: Exception) -> None:
    """Persist a build failure without breaking last-known-good reads."""
    try:
        has_active = CubeSchema.objects.filter(
            workspace=workspace,
            semantic_model=model,
            status=CubeSchema.Status.ACTIVE,
        ).exists()
        _set_last_build(model, ok=False, error=str(exc))
        model.diagnostics = [{"level": "error", "message": str(exc)[:500]}]
        if not has_active:
            model.status = SemanticModel.Status.ERROR
        model.save(update_fields=["status", "diagnostics", "metadata", "updated_at"])
    except Exception:
        logger.exception(
            "Failed to record Cube schema build failure for workspace %s", workspace.id
        )


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
