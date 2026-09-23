"""Provision genuinely usable tenant credentials for tests.

The all-of workspace gate (#380) needs every member to hold a *usable* credential for
every workspace tenant, so a bare ``TenantMembership`` stops granting access. These
helpers attach a real encrypted API-key connection instead of bypassing the
authorizer, so tests exercise the same readiness check production does.
"""

from apps.users.adapters import encrypt_credential
from apps.users.models import TenantConnection, TenantMembership


def _connection_defaults():
    return {"encrypted_credential": encrypt_credential("test-api-key")}


def usable_connection(user, provider) -> TenantConnection:
    connection, _ = TenantConnection.objects.get_or_create(
        user=user,
        provider=provider,
        credential_type=TenantConnection.API_KEY,
        defaults=_connection_defaults(),
    )
    return connection


async def ausable_connection(user, provider) -> TenantConnection:
    connection, _ = await TenantConnection.objects.aget_or_create(
        user=user,
        provider=provider,
        credential_type=TenantConnection.API_KEY,
        defaults=_connection_defaults(),
    )
    return connection


def grant_tenant_access(user, tenant) -> TenantMembership:
    """Live membership for ``user`` on ``tenant`` backed by a usable credential.

    Uses ``bulk_create`` so the auto-create-workspace signal does not fire; revives
    an archived row rather than leaving a tombstone behind.
    """
    connection = usable_connection(user, tenant.provider)
    TenantMembership.all_objects.bulk_create(
        [TenantMembership(user=user, tenant=tenant, connection=connection)],
        ignore_conflicts=True,
    )
    TenantMembership.all_objects.filter(user=user, tenant=tenant).update(
        connection=connection, archived_at=None
    )
    return TenantMembership.objects.get(user=user, tenant=tenant)


async def agrant_tenant_access(user, tenant) -> TenantMembership:
    """Async twin of :func:`grant_tenant_access`."""
    connection = await ausable_connection(user, tenant.provider)
    await TenantMembership.all_objects.abulk_create(
        [TenantMembership(user=user, tenant=tenant, connection=connection)],
        ignore_conflicts=True,
    )
    await TenantMembership.all_objects.filter(user=user, tenant=tenant).aupdate(
        connection=connection, archived_at=None
    )
    return await TenantMembership.objects.aget(user=user, tenant=tenant)
