"""Background Celery tasks for schema lifecycle management."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def refresh_tenant_schema(schema_id: str, membership_id: str) -> dict:
    """Provision a new schema and run the materialization pipeline.

    On success: marks state=ACTIVE, drops old active schemas for the tenant.
    On failure: drops the new schema, marks state=FAILED.
    """
    import psycopg
    import psycopg.sql

    from apps.projects.models import SchemaState, TenantSchema
    from apps.projects.services.schema_manager import SchemaManager, get_managed_db_connection
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
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    psycopg.sql.Identifier(new_schema.schema_name)
                )
            )
            cursor.close()
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to create schema '%s'", new_schema.schema_name)
        new_schema.state = SchemaState.FAILED
        new_schema.save(update_fields=["state"])
        return {"error": "Failed to create schema"}

    # Step 2: Resolve credential and run materialization pipeline
    credential = _resolve_credential(membership)
    if credential is None:
        _drop_schema_and_fail(new_schema)
        return {"error": "No credential available"}

    try:
        from mcp_server.pipeline_registry import get_registry
        from mcp_server.services.materializer import run_pipeline

        pipeline_config = get_registry().get("commcare_sync")
        run_pipeline(membership, credential, pipeline_config)
    except Exception:
        logger.exception("Materialization failed for schema '%s'", new_schema.schema_name)
        _drop_schema_and_fail(new_schema)
        return {"error": "Materialization failed"}

    # Step 3: Drop old active schemas for this tenant
    old_schemas = TenantSchema.objects.filter(
        tenant=new_schema.tenant,
        state=SchemaState.ACTIVE,
    ).exclude(id=new_schema.id)
    for old_schema in old_schemas:
        try:
            SchemaManager().teardown(old_schema)
            old_schema.state = SchemaState.EXPIRED
            old_schema.save(update_fields=["state"])
        except Exception:
            logger.exception("Failed to tear down old schema '%s'", old_schema.schema_name)

    # Step 4: Mark new schema as active
    new_schema.state = SchemaState.ACTIVE
    new_schema.save(update_fields=["state"])

    logger.info("Refresh complete: schema '%s' is now active", new_schema.schema_name)
    return {"status": "active", "schema_id": schema_id}


def _resolve_credential(membership) -> dict | None:
    """Resolve a credential dict for a TenantMembership, or return None."""
    from apps.users.models import TenantCredential

    try:
        cred_obj = TenantCredential.objects.get(tenant_membership=membership)
    except TenantCredential.DoesNotExist:
        return None

    if cred_obj.credential_type == TenantCredential.API_KEY:
        from apps.users.adapters import decrypt_credential

        try:
            decrypted = decrypt_credential(cred_obj.encrypted_credential)
            return {"type": "api_key", "value": decrypted}
        except Exception:
            logger.exception("Failed to decrypt API key for membership %s", membership.id)
            return None

    # OAuth credential
    from allauth.socialaccount.models import SocialToken

    provider = membership.tenant.provider
    if provider == "commcare_connect":
        token_obj = SocialToken.objects.filter(
            account__user=membership.user,
            account__provider__startswith="commcare_connect",
        ).first()
    else:
        token_obj = (
            SocialToken.objects.filter(
                account__user=membership.user,
                account__provider__startswith="commcare",
            )
            .exclude(account__provider__startswith="commcare_connect")
            .first()
        )

    if not token_obj:
        return None
    return {"type": "oauth", "value": token_obj.token}


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
        teardown_schema.delay(str(schema.id))


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
        schema.state = SchemaState.EXPIRED
        schema.save(update_fields=["state"])
    except Exception:
        schema.state = SchemaState.ACTIVE  # rollback to safe state
        schema.save(update_fields=["state"])
        raise
