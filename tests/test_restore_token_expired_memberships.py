"""Restoring memberships a token-expiry 401 archived, only where the provider still lists them."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.core.management import call_command
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.management.commands import restore_token_expired_memberships as command
from apps.users.models import Tenant, TenantConnection, TenantMembership, UpstreamAccessProof
from apps.users.services import access_verification_service
from apps.users.services.access_verification_types import ProviderVerificationResult


def _run(*args) -> str:
    out = StringIO()
    call_command("restore_token_expired_memberships", *args, stdout=out)
    return out.getvalue()


def _provider(monkeypatch, respond):
    calls = []

    async def provider(snapshot, **kwargs):
        calls.append(snapshot.credential)
        return respond()

    monkeypatch.setattr(command, "verify_provider", provider)
    return calls


def _live(membership) -> bool:
    return TenantMembership.all_objects.get(pk=membership.pk).archived_at is None


@pytest.fixture
def denied(user):
    """An OCS team connection a token-expiry 401 archived, as in the incident."""
    account = SocialAccount.objects.create(
        user=user, provider="ocs", uid="identity#acme", extra_data={"team": "acme"}
    )
    # No refresh token or app, so a 401 retries the same credential instead of refreshing.
    SocialToken.objects.create(
        account=account, token="access", expires_at=timezone.now() + timedelta(hours=1)
    )
    denied_at = timezone.now() - timedelta(hours=1)
    connection = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
        scope_key="acme",
        upstream_denial_code=ErrorCode.AUTH_TOKEN_EXPIRED,
        upstream_denied_at=denied_at,
    )
    bots = [
        Tenant.objects.create(provider="ocs", external_id=f"bot-{i}", canonical_name=f"Bot {i}")
        for i in range(4)
    ]
    memberships = [
        TenantMembership.all_objects.create(
            user=user,
            tenant=bot,
            connection=connection,
            provider_metadata={"team_slug": "acme"},
            archived_at=denied_at,
        )
        for bot in bots
    ]
    earlier, still_live = memberships[2], memberships[3]
    earlier.archived_at = denied_at - timedelta(days=2)
    earlier.save(update_fields=["archived_at"])
    still_live.archived_at = None
    still_live.save(update_fields=["archived_at"])
    return connection, bots, memberships


@pytest.mark.django_db(transaction=True)
def test_dry_run_reports_only_rows_the_token_denial_archived_and_writes_nothing(
    denied, monkeypatch
):
    _connection, _bots, memberships = denied
    calls = _provider(monkeypatch, lambda: pytest.fail("dry run contacted the provider"))

    out = _run()

    assert "Candidate memberships: 2 on 1 connection(s)" in out
    assert calls == []
    assert not any(_live(m) for m in memberships[:3])


@pytest.mark.django_db(transaction=True)
def test_apply_restores_only_what_the_provider_lists_and_is_idempotent(denied, monkeypatch):
    connection, bots, memberships = denied
    listed, omitted, archived_earlier, still_live = memberships
    calls = _provider(
        monkeypatch,
        lambda: ProviderVerificationResult.complete(
            {bots[0].external_id, bots[2].external_id, bots[3].external_id}
        ),
    )

    out = _run("--apply")

    assert calls == ["access"]
    assert _live(listed)
    assert not _live(omitted)
    # Listed, but archived by something other than this denial.
    assert not _live(archived_earlier)
    assert _live(still_live)
    assert "Restored 1 of 2 membership(s)." in out
    connection.refresh_from_db()
    assert not connection.upstream_denial_code

    assert "Candidate memberships: 0" in _run("--apply")
    assert calls == ["access"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("respond", "revoked"),
    [
        (
            lambda: ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED),
            True,
        ),
        (lambda: ProviderVerificationResult.unavailable("verification_unavailable"), False),
    ],
    ids=["revoked", "unavailable"],
)
def test_apply_restores_nothing_without_a_complete_listing(denied, monkeypatch, respond, revoked):
    _connection, _bots, memberships = denied
    monkeypatch.setattr(
        "apps.users.services.access_verification_service._REJECTION_RETRY_PAUSE_SECONDS", 0
    )
    _provider(monkeypatch, respond)

    out = _run("--apply")

    assert not any(_live(m) for m in memberships[:3])
    assert "Restored 0 of 2 membership(s)." in out
    # A current credential rejected twice is a real denial, so it archives the live row too.
    assert _live(memberships[3]) is not revoked


@pytest.mark.django_db(transaction=True)
def test_access_denied_connections_are_not_candidates(denied, monkeypatch):
    connection, _bots, memberships = denied
    connection.upstream_denial_code = ErrorCode.AUTH_ACCESS_DENIED
    connection.save(update_fields=["upstream_denial_code"])
    calls = _provider(monkeypatch, lambda: pytest.fail("not a candidate"))

    out = _run("--apply")

    assert "Candidate memberships: 0" in out
    assert calls == []
    assert not any(_live(m) for m in memberships[:3])


@pytest.mark.django_db(transaction=True)
def test_row_that_turns_live_mid_run_is_not_archived_by_the_trim(denied, monkeypatch, user):
    connection, bots, memberships = denied
    newcomer = Tenant.objects.create(provider="ocs", external_id="bot-new", canonical_name="New")
    original = access_verification_service._map_provider_result

    # After the trim reads membership state, before the listing is mapped to rows.
    async def discovery_then_map(*args, **kwargs):
        await TenantMembership.all_objects.acreate(
            user=user,
            tenant=newcomer,
            connection=connection,
            provider_metadata={"team_slug": "acme"},
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(access_verification_service, "_map_provider_result", discovery_then_map)
    _provider(
        monkeypatch,
        lambda: ProviderVerificationResult.complete(
            {bots[0].external_id, bots[3].external_id, newcomer.external_id}
        ),
    )

    _run("--apply")

    assert _live(memberships[0])
    assert TenantMembership.objects.filter(user=user, tenant=newcomer).exists()


@pytest.mark.django_db(transaction=True)
def test_other_team_rows_are_not_candidates_and_moved_denials_are_reported(denied, monkeypatch):
    connection, _bots, memberships = denied
    other_team = memberships[1]
    other_team.provider_metadata = {"team_slug": "globex"}
    other_team.save(update_fields=["provider_metadata"])

    assert "Candidate memberships: 1 on 1 connection(s)" in _run()

    connection.upstream_denied_at = timezone.now()
    connection.save(update_fields=["upstream_denied_at"])
    out = _run("--apply")

    assert "Candidate memberships: 0" in out
    assert f"connection {connection.pk}" in out
    assert not any(_live(m) for m in memberships[:3])


@pytest.mark.django_db(transaction=True)
def test_a_later_tenant_403_is_not_a_candidate(denied, monkeypatch):
    connection, bots, _memberships = denied
    UpstreamAccessProof.objects.create(
        connection=connection,
        tenant=bots[1],
        credential_fingerprint="x",
        account_identity="x",
        scope_key="acme",
        last_attempt_result="tenant_denied",
        last_error_code=ErrorCode.AUTH_ACCESS_DENIED,
    )

    assert "Candidate memberships: 1 on 1 connection(s)" in _run()
