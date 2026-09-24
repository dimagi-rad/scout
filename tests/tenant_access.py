"""Provision genuinely usable tenant credentials for tests.

The all-of workspace gate (#380) needs every member to hold a *usable* credential for
every workspace tenant, so a bare ``TenantMembership`` stops granting access. These
helpers attach a real encrypted API-key connection instead of bypassing the
authorizer, so tests exercise the same readiness check production does.
"""

from apps.users.adapters import encrypt_credential
from apps.users.models import TenantConnection, TenantMembership


def _connection_fields(user, provider) -> dict:
    return {"user": user, "provider": provider, "credential_type": TenantConnection.API_KEY}


def _new_connection_fields(user, provider) -> dict:
    # The packed "user:key" shape providers expect, so a later verification call
    # sees a well-formed credential rather than an indeterminate one.
    return _connection_fields(user, provider) | {
        "encrypted_credential": encrypt_credential("test-user:test-api-key")
    }


def _reusable(user, provider):
    # A blank key is a placeholder the readiness gate rejects; never hand one back.
    return TenantConnection.objects.filter(**_connection_fields(user, provider)).exclude(
        encrypted_credential=""
    )


def usable_connection(user, provider) -> TenantConnection:
    # Not get_or_create: several API-key connections per user and provider are a
    # supported state, and get_or_create raises MultipleObjectsReturned on them.
    return _reusable(user, provider).first() or TenantConnection.objects.create(
        **_new_connection_fields(user, provider)
    )


async def ausable_connection(user, provider) -> TenantConnection:
    return await _reusable(user, provider).afirst() or await TenantConnection.objects.acreate(
        **_new_connection_fields(user, provider)
    )


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
