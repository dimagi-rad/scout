"""Background Celery tasks for schema lifecycle management."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

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
    from apps.projects.services.schema_manager import SchemaManager

    try:
        SchemaManager().teardown(schema)
    except Exception:
        logger.exception("Failed to drop schema '%s' during cleanup", schema.schema_name)
    schema.state = SchemaState.FAILED
    schema.save(update_fields=["state"])


@shared_task
def expire_inactive_schemas() -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    Schemas with last_accessed_at older than SCHEMA_TTL_HOURS are expired.
    Schemas with null last_accessed_at are never auto-expired.
    """
    from apps.projects.models import SchemaState, TenantSchema

    cutoff = timezone.now() - timedelta(hours=settings.SCHEMA_TTL_HOURS)
    stale = TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    )
    for schema in stale:
        schema.state = SchemaState.TEARDOWN
        schema.save(update_fields=["state"])
        schema_id = str(schema.id)
        transaction.on_commit(lambda sid=schema_id: teardown_schema.delay(sid))


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
