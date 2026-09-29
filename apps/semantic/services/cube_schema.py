"""Build, validate, and promote Cube schemas for semantic models."""

from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.common.errors import ExpectedStateError
from apps.semantic.models import CubeSchema, SemanticModel
from apps.semantic.services.catalog import ensure_semantic_model
from apps.semantic.services.cube import DROPPED_JOIN_CODES, cube_schema_yaml, generate_cube_schema
from apps.semantic.services.cube_client import CubeClient, CubeServiceUnavailable
from mcp_server.context import QueryContext, load_workspace_context

logger = logging.getLogger(__name__)

# Inactive (DRAFT/ERROR) schema rows kept per model for debugging; older ones
# are pruned at promote time so rebuilds don't accumulate rows forever.
KEEP_INACTIVE_CUBE_SCHEMAS = 5

# The validator compiles on a single worker thread and, when a request passes its
# 60s timeout, restarts that worker and fails everything pending. On 2026-09-29
# ~20 workspace reloads validated at once, interleaved there, and all timed out
# (SCOUT-DJANGO-3Q/3R/3V). Session advisory locks cap it across worker processes.
VALIDATOR_CONCURRENCY = 2
VALIDATOR_LOCK_CLASS = 0x53435656
VALIDATOR_SLOT_WAIT_SECONDS = 300.0
VALIDATOR_SLOT_POLL_SECONDS = 1.0


class CubeSchemaBuildError(RuntimeError):
    """Raised when generated Cube schema content cannot be promoted."""


class CubeValidatorUnavailableError(CubeSchemaBuildError, ExpectedStateError):
    """Validation could not run, so the build fails and the last good schema serves (#622).

    Expected for the reasons on ``CubeServiceUnavailable``; the failure is
    recorded on ``model.metadata["last_build"]`` for the resume task to disclose.
    """


@contextmanager
def _validator_slot():
    """Hold one of ``VALIDATOR_CONCURRENCY`` slots shared by every worker process."""
    deadline = time.monotonic() + VALIDATOR_SLOT_WAIT_SECONDS
    while (slot := _try_acquire_validator_slot()) is None:
        if time.monotonic() >= deadline:
            raise CubeValidatorUnavailableError(
                "Cube schema validation could not start: every validator slot stayed busy "
                f"for {VALIDATOR_SLOT_WAIT_SECONDS:.0f}s."
            )
        time.sleep(VALIDATOR_SLOT_POLL_SECONDS)
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [VALIDATOR_LOCK_CLASS, slot])
        except Exception:
            logger.exception("Failed to release Cube validator slot %s", slot)
            # A session lock outlives the transaction; only closing frees it.
            connection.close()


def _try_acquire_validator_slot() -> int | None:
    with connection.cursor() as cursor:
        for slot in range(VALIDATOR_CONCURRENCY):
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [VALIDATOR_LOCK_CLASS, slot])
            if cursor.fetchone()[0]:
                return slot
    return None


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
        if model is None:
            return _build_and_promote_refreshed_model(workspace)
        try:
            return _build_validate_and_promote(workspace, model)
        except Exception as exc:
            _record_build_failure(workspace, model, exc)
            raise
    finally:
        close_old_connections()


def record_cube_schema_build_failure(workspace, error: str) -> None:
    """Record a failed or skipped build without creating a semantic model.

    If an ACTIVE Cube schema exists, it remains the readable fallback and the
    model remains ACTIVE; only ``metadata.last_build`` and diagnostics become
    failed. Workspaces without an existing model have no stale result to clear.
    """
    try:
        model = SemanticModel.objects.filter(workspace=workspace).first()
        if model is not None:
            _record_build_failure(workspace, model, CubeSchemaBuildError(error))
    except Exception:
        logger.exception(
            "Failed to record skipped Cube schema build for workspace %s",
            workspace.id,
        )


def record_cube_schema_build_deferred(workspace, reason: str) -> None:
    """Replace stale success metadata without turning active work into an error."""
    try:
        model = SemanticModel.objects.filter(workspace=workspace).first()
        if model is None:
            return
        previous_error = ((model.metadata or {}).get("last_build") or {}).get("error")
        model.metadata = {
            **(model.metadata or {}),
            "last_build": {
                "ok": False,
                "status": "deferred",
                "reason": reason,
                "at": timezone.now().isoformat(),
            },
        }
        if previous_error:
            model.metadata["last_build"]["error"] = previous_error
        model.save(update_fields=["metadata", "updated_at"])
    except Exception:
        logger.exception("Failed to record deferred Cube promotion for workspace %s", workspace.id)


def _build_and_promote_refreshed_model(workspace) -> CubeSchema:
    """Refresh physical datasets and promote them atomically when possible.

    Materialization rebuilds the physical semantic catalog from newly loaded
    tables before compiling Cube YAML. If compilation/validation fails and a
    previous ACTIVE Cube schema exists, keep the whole previous semantic surface
    queryable by rolling back the in-place dataset/field refresh.
    """
    existing_model = SemanticModel.objects.filter(workspace=workspace).first()
    has_active = bool(
        existing_model
        and CubeSchema.objects.filter(
            workspace=workspace,
            semantic_model=existing_model,
            status=CubeSchema.Status.ACTIVE,
        ).exists()
    )

    if not has_active:
        model = ensure_semantic_model(workspace)
        try:
            return _build_validate_and_promote(workspace, model)
        except Exception as exc:
            _record_build_failure(workspace, model, exc)
            raise

    try:
        with transaction.atomic():
            model = ensure_semantic_model(workspace)
            return _build_validate_and_promote(workspace, model)
    except Exception as exc:
        # The atomic block rolled back the attempted catalog refresh, so record
        # the failure on the last-known-good model rather than the rolled-back
        # in-memory instance.
        fallback_model = SemanticModel.objects.filter(workspace=workspace).first()
        if fallback_model is not None:
            _record_build_failure(workspace, fallback_model, exc)
        raise


def _build_validate_and_promote(workspace, model: SemanticModel) -> CubeSchema:
    try:
        schema = generate_cube_schema(model)
        content = cube_schema_yaml(schema)
    except ValueError as exc:
        # Never remove a broken filter/measure silently: that changes the metric.
        # The normal failed-build path retains the previous active publication.
        raise CubeSchemaBuildError(f"Could not generate Cube schema: {exc}") from exc
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    filename = f"workspace_{workspace.id}_{content_hash[:12]}.yaml"
    try:
        with _validator_slot():
            validation = async_to_sync(CubeClient().validate_schema)(content)
    except CubeServiceUnavailable as exc:
        raise CubeValidatorUnavailableError(str(exc)) from exc
    validation_diagnostics = _diagnostics_from_validation(validation)
    diagnostics = [
        *(model.metadata or {}).get("catalog_diagnostics", []),
        *schema["diagnostics"],
        *validation_diagnostics,
    ]

    if not validation.get("valid", False):
        # Identical content shares the serving row's filename; recording the failure on
        # that row would demote the schema Cube is serving. The model still records it.
        serving_identical = CubeSchema.objects.filter(
            workspace=workspace,
            semantic_model=model,
            filename=filename,
            status=CubeSchema.Status.ACTIVE,
        ).exists()
        if serving_identical:
            logger.warning(
                "Revalidation of the serving Cube schema failed for workspace %s: %s",
                workspace.id,
                validation_diagnostics,
            )
        else:
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
        raise CubeSchemaBuildError(_diagnostics_message(validation_diagnostics))

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

    def invalidate_cube_schema_cache() -> None:
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
        except CubeServiceUnavailable as exc:
            # Cube still serves the new schema: it compiles on the first query instead.
            logger.warning(
                "Cube schema warm-up did not complete for workspace %s: %s", workspace.id, exc
            )
        except Exception:
            logger.exception(
                "Failed to invalidate Cube schema cache for workspace %s", workspace.id
            )

    try:
        transaction.on_commit(invalidate_cube_schema_cache)
    except Exception:
        logger.exception(
            "Failed to register Cube schema cache invalidation for workspace %s",
            workspace.id,
        )

    return cube_schema


def _set_last_build(
    model: SemanticModel, *, ok: bool, error: str = "", content_hash: str = ""
) -> None:
    entry: dict[str, Any] = {"ok": ok, "at": timezone.now().isoformat()}
    if error:
        entry["error"] = error[:500]
    if content_hash:
        entry["content_hash"] = content_hash
    model.metadata = {**(model.metadata or {}), "last_build": entry}


def _record_build_failure(workspace, model: SemanticModel, exc: Exception) -> None:
    """Persist a build failure without breaking last-known-good reads."""
    try:
        active = (
            CubeSchema.objects.filter(
                workspace=workspace,
                semantic_model=model,
                status=CubeSchema.Status.ACTIVE,
            )
            .only("diagnostics")
            .order_by("-updated_at")
            .first()
        )
        # The serving schema still lacks the joins it dropped; keep saying so,
        # or the catalog would advertise them as published again.
        serving_relationship_diagnostics = [
            diagnostic
            for diagnostic in (active.diagnostics if active else None) or []
            if isinstance(diagnostic, dict) and diagnostic.get("code") in DROPPED_JOIN_CODES
        ]
        _set_last_build(model, ok=False, error=str(exc))
        model.diagnostics = [
            *(model.metadata or {}).get("catalog_diagnostics", []),
            *serving_relationship_diagnostics,
            {"level": "error", "message": str(exc)[:500]},
        ]
        model.status = SemanticModel.Status.ACTIVE if active else SemanticModel.Status.ERROR
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
