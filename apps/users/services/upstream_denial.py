"""Persist authoritative denial only while the observed credential still owns access."""

import logging

from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection, TenantMembership, User
from apps.users.services.oauth_scope import account_scope, provider_accounts

logger = logging.getLogger(__name__)

_AUTHORITATIVE_DENIAL_CODES = frozenset(
    {ErrorCode.AUTH_TOKEN_EXPIRED, ErrorCode.AUTH_ACCESS_DENIED}
)


def credential_is_current(connection, credential, token_snapshot=None):
    """Check the observed credential inside a transaction holding the user lock."""
    if connection.credential_type == TenantConnection.API_KEY:
        return True  # The caller's encrypted credential snapshot is checked under the lock.
    tokens = (
        SocialToken.objects.select_for_update(of=("self",))
        .filter(
            account__in=provider_accounts(connection.user_id, connection.provider), token=credential
        )
        .select_related("account")
    )
    if token_snapshot is not None:
        token_id, refresh_secret, app_id = token_snapshot
        tokens = tokens.filter(pk=token_id, token_secret=refresh_secret, app_id=app_id)
    if connection.social_account_id:
        tokens = tokens.filter(account_id=connection.social_account_id)
    return any(account_scope(token.account) == connection.scope_key for token in tokens)


def record_upstream_denial(connection, *, credential, code, tenant_id=None, token_snapshot=None):
    """Archive this observed connection's access, or just the denied tenant for a 403.

    An unbound/new identity cannot revoke the connection it failed to replace.
    The user lock matches discovery/reconnect/disconnect's serialization boundary.
    """
    if connection is None:
        logger.info("Skipping upstream denial: missing connection id=None")
        return 0
    if code not in _AUTHORITATIVE_DENIAL_CODES:
        logger.info("Skipping upstream denial: unsupported code for connection=%s", connection.pk)
        return 0
    with transaction.atomic():
        if not User.objects.select_for_update().filter(pk=connection.user_id).exists():
            logger.info("Skipping upstream denial: missing user for connection=%s", connection.pk)
            return 0
        current = TenantConnection.objects.filter(
            pk=connection.pk,
            user_id=connection.user_id,
            provider=connection.provider,
            social_account_id=connection.social_account_id,
            encrypted_credential=connection.encrypted_credential,
        ).first()
        if current is None:
            logger.warning("Skipping upstream denial: connection changed id=%s", connection.pk)
            return 0
        if not credential_is_current(current, credential, token_snapshot):
            logger.warning(
                "Skipping upstream denial: stale credential for connection=%s", connection.pk
            )
            return 0
        return record_validated_upstream_denial(current, code=code, tenant_id=tenant_id)


def record_validated_upstream_denial(connection, *, code, tenant_id=None, now=None):
    """Persist denial after the caller has locked and validated the credential."""
    if code not in _AUTHORITATIVE_DENIAL_CODES:
        logger.info("Skipping upstream denial: unsupported code for connection=%s", connection.pk)
        return None
    now = now or timezone.now()
    memberships = TenantMembership.all_objects.filter(
        connection=connection,
        user_id=connection.user_id,
        tenant__provider=connection.provider,
        archived_at__isnull=True,
    )
    if tenant_id is not None:
        memberships = memberships.filter(tenant_id=tenant_id)
    else:
        if connection.provider == "ocs" and connection.credential_type == TenantConnection.OAUTH:
            memberships = memberships.filter(
                Q(provider_metadata__team_slug=connection.scope_key)
                | Q(provider_metadata__team_slug__isnull=True)
                | Q(provider_metadata__team_slug="")
            )
        connection.upstream_denial_code = code
    connection.upstream_denied_at = now
    connection.save(update_fields=["upstream_denial_code", "upstream_denied_at"])
    return memberships.update(archived_at=now)


arecord_upstream_denial = sync_to_async(record_upstream_denial)


async def adiscovery_connection(user, provider, access_token, account=None):
    """Snapshot the scope before discovery, including a binding it may replace."""
    if account is None:
        token = (
            await SocialToken.objects.filter(
                account__in=provider_accounts(user.pk, provider), token=access_token
            )
            .select_related("account")
            .afirst()
        )
        account = token.account if token is not None else None
    connection = await TenantConnection.objects.filter(
        user=user,
        provider=provider,
        credential_type=TenantConnection.OAUTH,
        scope_key=account_scope(account),
    ).afirst()
    return connection
