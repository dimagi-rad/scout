"""Provision genuinely usable tenant credentials for tests.

The all-of workspace gate (#380) needs every member to hold a *usable* credential for
every workspace tenant, so a bare ``TenantMembership`` stops granting access. These
helpers attach a real encrypted API-key connection instead of bypassing the
authorizer, so tests exercise the same readiness check production does.

The upstream-freshness gate additionally needs a recent proof that the credential
was verified upstream, so granted access also records one. Its fields are exactly
what ``proofs_are_fresh`` compares against.
"""

import hashlib
from datetime import timedelta

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from cryptography.fernet import InvalidToken
from django.utils import timezone

from apps.users.adapters import decrypt_credential, encrypt_credential
from apps.users.models import TenantConnection, TenantMembership, UpstreamAccessProof
from apps.users.services.access_verification import snapshot_credential

USABLE_TEST_SECRET = "test-user:test-api-key"


def is_usable_test_connection(connection) -> bool:
    if (
        connection.credential_type != TenantConnection.API_KEY
        or not connection.encrypted_credential
    ):
        return False
    try:
        return decrypt_credential(connection.encrypted_credential) == USABLE_TEST_SECRET
    except (InvalidToken, ValueError):
        # Tests deliberately store malformed credentials; those are never usable.
        return False


def _connection_fields(user, provider) -> dict:
    return {"user": user, "provider": provider, "credential_type": TenantConnection.API_KEY}


def _new_connection_fields(user, provider) -> dict:
    # The packed "user:key" shape providers expect, so a later verification call
    # sees a well-formed credential rather than an indeterminate one.
    return _connection_fields(user, provider) | {
        "encrypted_credential": encrypt_credential(USABLE_TEST_SECRET)
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


def _proof_fields(connection, verified_at) -> dict:
    secret = decrypt_credential(connection.encrypted_credential)
    return {
        "credential_fingerprint": hashlib.sha256(secret.encode()).hexdigest(),
        "account_identity": "",
        "scope_key": connection.scope_key,
        "observed_denied_at": connection.upstream_denied_at,
        "verified_at": verified_at or timezone.now(),
        "last_attempt_result": "complete",
    }


def record_fresh_proof(connection, tenant, *, verified_at=None) -> UpstreamAccessProof:
    proof, _ = UpstreamAccessProof.objects.update_or_create(
        connection=connection, tenant=tenant, defaults=_proof_fields(connection, verified_at)
    )
    return proof


async def arecord_fresh_proof(connection, tenant, *, verified_at=None) -> UpstreamAccessProof:
    proof, _ = await UpstreamAccessProof.objects.aupdate_or_create(
        connection=connection, tenant=tenant, defaults=_proof_fields(connection, verified_at)
    )
    return proof


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
    record_fresh_proof(connection, tenant)
    return TenantMembership.objects.select_related("user", "tenant", "connection").get(
        user=user, tenant=tenant
    )


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
    await arecord_fresh_proof(connection, tenant)
    return await TenantMembership.objects.select_related("user", "tenant", "connection").aget(
        user=user, tenant=tenant
    )


def ocs_team_connection(user, team) -> TenantConnection:
    """A genuine per-team OCS OAuth identity (#156): account, token and connection."""
    app, _ = SocialApp.objects.get_or_create(
        provider="ocs", name="OCS", defaults={"client_id": "cid", "secret": "sec"}
    )
    account = SocialAccount.objects.create(
        user=user, provider="ocs", uid=f"{user.pk}#{team}", extra_data={"team": team}
    )
    SocialToken.objects.create(
        account=account,
        app=app,
        token=f"tok-{team}",
        token_secret=f"refresh-{team}",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
        scope_key=team,
        scope_label=team.title(),
        social_account=account,
    )


def grant_ocs_team_access(user, tenant, connection, *, team_slug=None) -> TenantMembership:
    """Live membership on an OCS ``tenant`` through ``connection``.

    ``team_slug`` defaults to the connection's team; pass ``""`` for a legacy row
    with no team, or another slug for a chatbot owned by a different team. Like
    :func:`grant_tenant_access`, an existing or archived row is rewritten.
    """
    slug = connection.scope_key if team_slug is None else team_slug
    metadata = {"team_slug": slug, "team_name": slug.title()} if slug else {}
    TenantMembership.all_objects.bulk_create(
        [TenantMembership(user=user, tenant=tenant)], ignore_conflicts=True
    )
    TenantMembership.all_objects.filter(user=user, tenant=tenant).update(
        connection=connection, provider_metadata=metadata, archived_at=None
    )
    record_fresh_oauth_proof(connection, tenant)
    return TenantMembership.objects.get(user=user, tenant=tenant)


def record_fresh_oauth_proof(connection, tenant) -> UpstreamAccessProof:
    """A just-verified proof for an OAuth ``connection``, as a successful listing leaves."""
    token = SocialToken.objects.filter(account_id=connection.social_account_id).first()
    observation = snapshot_credential(connection, token).observation
    proof, _ = UpstreamAccessProof.objects.update_or_create(
        connection=connection,
        tenant=tenant,
        defaults={
            "credential_fingerprint": observation.credential_fingerprint,
            "account_identity": observation.account_identity,
            "scope_key": observation.scope_key,
            "observed_denied_at": observation.upstream_denied_at,
            "verified_at": timezone.now(),
            "last_attempt_result": "complete",
        },
    )
    return proof
