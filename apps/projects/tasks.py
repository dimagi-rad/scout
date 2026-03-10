"""Background Celery tasks for schema lifecycle management."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.projects.services.schema_manager import SchemaManager
from apps.users.services.credential_resolver import resolve_credential

logger = logging.getLogger(__name__)


@shared_task
def refresh_tenant_schema(schema_id: str, membership_id: str) -> dict:
    """Provision a new schema and run the materialization pipeline.

    On success: marks state=ACTIVE, schedules teardown of old active schemas.
    On failure: drops the new schema, marks state=FAILED.
    """
    from apps.projects.models import SchemaState, TenantSchema
    from apps.projects.services.schema_manager import SchemaManager
    from apps.users.models import TenantMembership

    try:
        new_schema = TenantSchema.objects.select_related("tenant").get(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.error("refresh_tenant_schema: schema %s not found", schema_id)
        return {"error": "Schema not found"}

    try:
        membership = TenantMembership.objects.select_related("tenant", "user").get(id=membership_id)
    except TenantMembership.DoesNotExist:
        new_schema.state = SchemaState.FAILED
        new_schema.save(update_fields=["state"])
        return {"error": "Membership not found"}

    # Step 1: Create the physical schema in the managed database
    try:
        SchemaManager().create_physical_schema(new_schema)
    except Exception:
        logger.exception("Failed to create schema '%s'", new_schema.schema_name)
        new_schema.state = SchemaState.FAILED
        new_schema.save(update_fields=["state"])
        return {"error": "Failed to create schema"}

    # Step 2: Resolve credential and run materialization pipeline
    credential = resolve_credential(membership)
    if credential is None:
        _drop_schema_and_fail(new_schema)
        return {"error": "No credential available"}

    try:
        from mcp_server.pipeline_registry import get_registry
        from mcp_server.services.materializer import run_pipeline

        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}
        pipeline_name = provider_pipeline_map.get(membership.tenant.provider)
        if pipeline_name is None:
            _drop_schema_and_fail(new_schema)
            return {"error": f"No pipeline configured for provider '{membership.tenant.provider}'"}
        pipeline_config = registry.get(pipeline_name)
        run_pipeline(membership, credential, pipeline_config)
    except Exception:
        logger.exception("Materialization failed for schema '%s'", new_schema.schema_name)
        _drop_schema_and_fail(new_schema)
        return {"error": "Materialization failed"}

    # Step 3: Mark new schema as active
    new_schema.state = SchemaState.ACTIVE
    new_schema.save(update_fields=["state"])

    # Step 4: Schedule teardown of previously active schemas with a delay to allow
    # in-flight queries against the old schema to complete before it is dropped.
    old_schemas = TenantSchema.objects.filter(
        tenant=new_schema.tenant,
        state=SchemaState.ACTIVE,
    ).exclude(id=new_schema.id)
    for old_schema in old_schemas:
        old_schema.state = SchemaState.TEARDOWN
        old_schema.save(update_fields=["state"])
        teardown_schema.apply_async((str(old_schema.id),), countdown=30 * 60)

    logger.info("Refresh complete: schema '%s' is now active", new_schema.schema_name)
    return {"status": "active", "schema_id": schema_id}


def _drop_schema_and_fail(schema) -> None:
    """Drop the physical schema and mark the record as FAILED."""
    from apps.projects.models import SchemaState

    try:
        SchemaManager().teardown(schema)
    except Exception:
        logger.exception("Failed to drop schema '%s' during cleanup", schema.schema_name)
    schema.state = SchemaState.FAILED
    schema.save(update_fields=["state"])


@shared_task
def expire_inactive_schemas() -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    Handles both TenantSchema and WorkspaceViewSchema records.
    Schemas with null last_accessed_at are never auto-expired.
    """
    from apps.projects.models import SchemaState, TenantSchema, WorkspaceViewSchema

    cutoff = timezone.now() - timedelta(hours=settings.SCHEMA_TTL_HOURS)

    # Expire stale tenant schemas
    stale_tenant = TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    )
    for schema in stale_tenant:
        schema.state = SchemaState.TEARDOWN
        schema.save(update_fields=["state"])
        teardown_schema.delay_on_commit(str(schema.id))

    # Expire stale view schemas
    stale_views = WorkspaceViewSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    )
    for vs in stale_views:
        vs.state = SchemaState.TEARDOWN
        vs.save(update_fields=["state"])
        teardown_view_schema_task.delay_on_commit(str(vs.id))


@shared_task
def rebuild_workspace_view_schema(workspace_id: str) -> dict:
    """Build (or rebuild) the UNION ALL view schema for a multi-tenant workspace.

    On success: marks WorkspaceViewSchema.state = ACTIVE.
    On failure: marks state = FAILED and returns an error dict.
    """
    from apps.projects.models import Workspace

    try:
        workspace = Workspace.objects.prefetch_related("tenants").get(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.error("rebuild_workspace_view_schema: workspace %s not found", workspace_id)
        return {"error": "Workspace not found"}

    try:
        vs = SchemaManager().build_view_schema(workspace)
    except Exception:
        # build_view_schema already saves state=FAILED before re-raising;
        # no need to write it again here (doing so risks overwriting a
        # concurrent state transition, e.g. TEARDOWN set by expire_inactive_schemas).
        logger.exception("Failed to build view schema for workspace %s", workspace_id)
        return {"error": "Failed to build view schema"}

    logger.info(
        "View schema '%s' is now active for workspace %s",
        vs.schema_name,
        workspace_id,
    )
    return {"status": "active", "schema_name": vs.schema_name}


@shared_task
def teardown_view_schema_task(view_schema_id: str) -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema and mark EXPIRED."""
    from apps.projects.models import SchemaState, WorkspaceViewSchema

    try:
        vs = WorkspaceViewSchema.objects.get(id=view_schema_id)
    except WorkspaceViewSchema.DoesNotExist:
        logger.error("teardown_view_schema_task: view schema %s not found", view_schema_id)
        return

    try:
        SchemaManager().teardown_view_schema(vs)
    except Exception:
        logger.exception("Failed to drop view schema '%s'", vs.schema_name)
        raise

    vs.state = SchemaState.EXPIRED
    vs.save(update_fields=["state"])


@shared_task
def teardown_schema(schema_id: str) -> None:
    """Drop a tenant schema in the managed database and mark it EXPIRED."""
    from apps.projects.models import SchemaState, TenantSchema
    from apps.projects.services.schema_manager import SchemaManager

    try:
        schema = TenantSchema.objects.get(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.error("teardown_schema: schema %s not found", schema_id)
        return

    try:
        SchemaManager().teardown(schema)
    except Exception:
        schema.state = SchemaState.ACTIVE  # rollback: physical schema still exists
        schema.save(update_fields=["state"])
        raise
    try:
        schema.state = SchemaState.EXPIRED
        schema.save(update_fields=["state"])
    except Exception:
        # Physical schema is already dropped; don't pretend it's ACTIVE.
        logger.exception(
            "teardown_schema: failed to mark schema %s EXPIRED after teardown", schema.id
        )
        raise
