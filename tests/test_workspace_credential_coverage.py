from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.token_refresh import credential_fingerprint
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.credential_coverage import (
    aget_tenant_credential_readiness,
    aget_workspace_credential_coverage,
    get_tenant_credential_readiness,
    get_workspace_credential_coverage,
)

pytestmark = pytest.mark.django_db


def _workspace(name="Coverage"):
    return Workspace.objects.create(name=name)


def _member(workspace, user):
    return WorkspaceMembership.objects.create(
        workspace=workspace,
        user=user,
        role=WorkspaceRole.READ,
    )


def _tenant(workspace, provider, external_id, name):
    tenant = Tenant.objects.create(
        provider=provider,
        external_id=external_id,
        canonical_name=name,
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    return tenant


def _api_membership(user, tenant, *, key="usable-key", team_slug="", team_name=""):
    conn = TenantConnection.objects.create(
        user=user,
        provider=tenant.provider,
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential(key),
    )
    membership = TenantMembership.objects.create(
        user=user,
        tenant=tenant,
        connection=conn,
        team_slug=team_slug,
        team_name=team_name,
    )
    return membership, conn


def _oauth_membership(
    user,
    tenant,
    *,
    team_slug="",
    account_team="",
    connection_scope="",
    account_user=None,
    account_provider=None,
    access_token="access-token",
    refresh_token="refresh-token",
    expires_at=None,
):
    account = SocialAccount.objects.create(
        user=account_user or user,
        provider=account_provider or tenant.provider,
        uid=f"identity-{SocialAccount.objects.count()}"
        + (f"#{account_team}" if account_team else ""),
        extra_data={"team": account_team} if account_team else {},
    )
    app = SocialApp.objects.create(
        provider=tenant.provider,
        name=f"{tenant.provider}-{SocialApp.objects.count()}",
        client_id="client",
        secret="secret",
    )
    token = SocialToken.objects.create(
        account=account,
        app=app,
        token=access_token,
        token_secret=refresh_token,
        expires_at=expires_at or timezone.now() + timedelta(hours=1),
    )
    conn = TenantConnection.objects.create(
        user=user,
        provider=tenant.provider,
        credential_type=TenantConnection.OAUTH,
        scope_key=connection_scope,
        social_account=account,
    )
    membership = TenantMembership.objects.create(
        user=user,
        tenant=tenant,
        connection=conn,
        team_slug=team_slug,
        team_name=team_slug.title(),
    )
    return membership, conn, account, token


def _only_report(workspace, user):
    reports = get_workspace_credential_coverage(
        workspace_ids=[workspace.id],
        user_ids=[user.id],
    )
    assert len(reports) == 1
    return reports[0]


def test_zero_tenant_workspace_is_vacuously_covered(user):
    workspace = _workspace()
    _member(workspace, user)

    report = _only_report(workspace, user)

    assert report.covered is True
    assert report.gaps == ()


def test_explicit_user_tenant_readiness_does_not_require_workspace_rows(user):
    tenant = Tenant.objects.create(
        provider="commcare",
        external_id="candidate-domain",
        canonical_name="Candidate Domain",
    )
    conn = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("usable-key"),
    )
    TenantMembership.objects.bulk_create(
        [TenantMembership(user=user, tenant=tenant, connection=conn)]
    )

    readiness = get_tenant_credential_readiness([(user.id, tenant)])

    assert not WorkspaceMembership.objects.filter(user=user).exists()
    assert len(readiness) == 1
    assert readiness[0].user_id == user.id
    assert readiness[0].tenant_id == str(tenant.id)
    assert readiness[0].usable is True
    assert readiness[0].gap is None


def test_explicit_user_tenant_readiness_reports_candidate_gap_without_workspace_rows(user):
    tenant = Tenant.objects.create(
        provider="commcare",
        external_id="candidate-domain",
        canonical_name="Candidate Domain",
    )

    readiness = get_tenant_credential_readiness([(user.id, tenant)])

    assert readiness[0].usable is False
    assert readiness[0].gap.code == "missing_live_membership"


def test_positive_ocs_multi_team_oauth_coverage(user):
    workspace = _workspace()
    _member(workspace, user)
    acme = _tenant(workspace, "ocs", "bot-a", "Acme Bot")
    globex = _tenant(workspace, "ocs", "bot-b", "Globex Bot")
    _oauth_membership(
        user,
        acme,
        team_slug="acme",
        account_team="acme",
        connection_scope="acme",
    )
    _oauth_membership(
        user,
        globex,
        team_slug="globex",
        account_team="globex",
        connection_scope="globex",
    )

    assert _only_report(workspace, user).covered is True


def test_ocs_api_key_uses_discovered_membership_team_when_connection_scope_is_blank(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "ocs", "bot-a", "Acme Bot")
    _, conn = _api_membership(user, tenant, team_slug="acme", team_name="Acme")

    assert conn.scope_key == ""
    assert _only_report(workspace, user).covered is True


def test_ocs_oauth_membership_with_unknown_legacy_team_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "ocs", "bot-a", "Legacy Bot")
    _oauth_membership(user, tenant, team_slug="", account_team="", connection_scope="")

    report = _only_report(workspace, user)

    assert report.covered is False
    assert report.gaps[0].code == "ocs_team_missing"
    assert report.gaps[0].tenant_name == "Legacy Bot"


def test_ocs_api_key_with_blank_team_is_locally_ready_after_tenant_discovery(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "ocs", "bot-a", "Sessionless Bot")
    _api_membership(user, tenant, team_slug="", team_name="Sessionless Team")

    report = _only_report(workspace, user)

    assert report.covered is True


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("connection_user", "connection_user_mismatch"),
        ("connection_provider", "connection_provider_mismatch"),
        ("account_user", "oauth_account_user_mismatch"),
        ("account_provider", "oauth_account_provider_mismatch"),
    ],
)
def test_cross_user_or_provider_bindings_are_not_covered(user, other_user, mutation, reason):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    membership, conn, account, _token = _oauth_membership(user, tenant)
    if mutation == "connection_user":
        conn.user = other_user
        conn.save(update_fields=["user"])
    elif mutation == "connection_provider":
        conn.provider = "ocs"
        conn.save(update_fields=["provider"])
    elif mutation == "account_user":
        account.user = other_user
        account.save(update_fields=["user"])
    else:
        account.provider = "ocs"
        account.save(update_fields=["provider"])

    report = _only_report(workspace, user)

    assert report.covered is False
    assert report.gaps[0].code == reason
    assert report.gaps[0].tenant_id == str(membership.tenant_id)


@pytest.mark.parametrize(
    ("ciphertext", "reason"),
    [("", "api_key_missing"), ("not-fernet", "api_key_decrypt_failed")],
)
def test_blank_or_undecryptable_api_key_is_not_covered(user, ciphertext, reason):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _membership, conn = _api_membership(user, tenant)
    conn.encrypted_credential = ciphertext
    conn.save(update_fields=["encrypted_credential"])

    report = _only_report(workspace, user)

    assert report.covered is False
    assert report.gaps[0].code == reason


def test_missing_connection_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    TenantMembership.objects.create(user=user, tenant=tenant)

    assert _only_report(workspace, user).gaps[0].code == "missing_connection"


def test_missing_oauth_account_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    membership, conn, _account, _token = _oauth_membership(user, tenant)
    conn.social_account = None
    conn.save(update_fields=["social_account"])

    report = _only_report(workspace, user)

    assert report.gaps[0].code == "oauth_account_missing"
    assert report.gaps[0].membership_id == str(membership.id)


def test_missing_oauth_token_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _membership, _conn, _account, token = _oauth_membership(user, tenant)
    token.delete()

    assert _only_report(workspace, user).gaps[0].code == "oauth_token_missing"


def test_oauth_token_must_be_nonempty(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _membership, _conn, _account, token = _oauth_membership(user, tenant)
    token.token = ""
    token.save(update_fields=["token"])

    assert _only_report(workspace, user).gaps[0].code == "oauth_token_empty"


def test_nonrenewable_expired_oauth_token_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _oauth_membership(
        user,
        tenant,
        refresh_token="",
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    assert _only_report(workspace, user).gaps[0].code == "oauth_token_expired"


def test_nonrenewable_near_expiry_oauth_token_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _oauth_membership(
        user,
        tenant,
        refresh_token="",
        expires_at=timezone.now() + timedelta(seconds=30),
    )

    assert _only_report(workspace, user).gaps[0].code == "oauth_token_expired"


def test_renewable_near_expiry_oauth_token_is_locally_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _oauth_membership(
        user,
        tenant,
        refresh_token="refresh-token",
        expires_at=timezone.now() + timedelta(seconds=30),
    )

    report = _only_report(workspace, user)

    assert report.covered is True
    assert report.readiness == "local_credential_readiness"


def test_current_token_refresh_failure_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _membership, conn, _account, token = _oauth_membership(user, tenant)
    conn.oauth_refresh_failure_fingerprint = credential_fingerprint(token)
    conn.save(update_fields=["oauth_refresh_failure_fingerprint"])

    assert _only_report(workspace, user).gaps[0].code == "oauth_refresh_failed"


def test_current_refresh_failure_wins_over_an_unbound_healthy_token(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _membership, conn, account, failed_token = _oauth_membership(user, tenant)
    alternate_app = SocialApp.objects.create(
        provider="commcare",
        name="alternate",
        client_id="alternate",
        secret="secret",
    )
    SocialToken.objects.create(
        account=account,
        app=alternate_app,
        token="alternate-access-token",
        token_secret="alternate-refresh-token",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    conn.oauth_refresh_failure_fingerprint = credential_fingerprint(failed_token)
    conn.save(update_fields=["oauth_refresh_failure_fingerprint"])

    assert _only_report(workspace, user).gaps[0].code == "oauth_refresh_failed"


def test_oauth_readiness_uses_same_first_token_as_runtime_resolver(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _membership, _conn, account, first_token = _oauth_membership(
        user,
        tenant,
        refresh_token="",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    alternate_app = SocialApp.objects.create(
        provider="commcare",
        name="alternate-healthy",
        client_id="alternate-healthy",
        secret="secret",
    )
    second_token = SocialToken.objects.create(
        account=account,
        app=alternate_app,
        token="healthy-access-token",
        token_secret="healthy-refresh-token",
        expires_at=timezone.now() + timedelta(hours=1),
    )

    assert first_token.pk < second_token.pk
    assert _only_report(workspace, user).gaps[0].code == "oauth_token_expired"


def test_old_token_refresh_failure_does_not_poison_replacement(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    _membership, conn, _account, token = _oauth_membership(user, tenant)
    conn.oauth_refresh_failure_fingerprint = credential_fingerprint(token)
    conn.save(update_fields=["oauth_refresh_failure_fingerprint"])
    token.token = "replacement-token"
    token.save(update_fields=["token"])

    assert _only_report(workspace, user).covered is True


def test_ocs_oauth_requires_matching_nonempty_connection_and_account_scopes(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "ocs", "bot-a", "Acme Bot")
    _membership, conn, account, _token = _oauth_membership(
        user,
        tenant,
        team_slug="acme",
        account_team="acme",
        connection_scope="acme",
    )

    conn.scope_key = "globex"
    conn.save(update_fields=["scope_key"])
    assert _only_report(workspace, user).gaps[0].code == "ocs_connection_scope_mismatch"

    conn.scope_key = "acme"
    conn.save(update_fields=["scope_key"])
    account.uid = "identity#globex"
    account.extra_data = {"team": "globex"}
    account.save(update_fields=["uid", "extra_data"])
    assert _only_report(workspace, user).gaps[0].code == "ocs_account_scope_mismatch"


def test_superseded_oauth_identity_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "ocs", "bot-a", "Acme Bot")
    _membership, _old_conn, _old_account, _token = _oauth_membership(
        user,
        tenant,
        team_slug="acme",
        account_team="acme",
        connection_scope="acme",
    )
    replacement = SocialAccount.objects.create(
        user=user,
        provider="ocs",
        uid="replacement#acme",
        extra_data={"team": "acme"},
    )
    # Raw provider IDs can vary by deployment while canonical_provider still
    # maps them to OCS. The canonical scope must have only one active identity.
    TenantConnection.objects.create(
        user=user,
        provider="ocs_replacement",
        credential_type=TenantConnection.OAUTH,
        scope_key="acme",
        social_account=replacement,
    )

    assert _only_report(workspace, user).gaps[0].code == "oauth_connection_inactive"


def test_ocs_api_key_memberships_on_one_connection_must_have_one_team(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant_a = _tenant(workspace, "ocs", "bot-a", "Acme Bot")
    tenant_b = _tenant(workspace, "ocs", "bot-b", "Globex Bot")
    membership, conn = _api_membership(
        user,
        tenant_a,
        team_slug="acme",
        team_name="Acme",
    )
    TenantMembership.objects.create(
        user=user,
        tenant=tenant_b,
        connection=conn,
        team_slug="globex",
        team_name="Globex",
    )

    report = _only_report(workspace, user)

    assert report.covered is False
    assert {gap.code for gap in report.gaps} == {"ocs_api_key_team_ambiguous"}
    assert all(gap.membership_id for gap in report.gaps)
    assert membership.id


def test_archived_membership_is_not_covered(user):
    workspace = _workspace()
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "domain", "Domain")
    membership, _conn = _api_membership(user, tenant)
    membership.archived_at = timezone.now()
    membership.save(update_fields=["archived_at"])

    assert _only_report(workspace, user).gaps[0].code == "missing_live_membership"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_and_async_results_match(user):
    workspace = await Workspace.objects.acreate(name="Parity")
    await WorkspaceMembership.objects.acreate(
        workspace=workspace,
        user=user,
        role=WorkspaceRole.READ,
    )
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="domain", canonical_name="Domain"
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)

    sync_reports = await sync_to_async(get_workspace_credential_coverage)(
        workspace_ids=[workspace.id], user_ids=[user.id]
    )
    async_reports = await aget_workspace_credential_coverage(
        workspace_ids=[workspace.id], user_ids=[user.id]
    )
    sync_readiness = await sync_to_async(get_tenant_credential_readiness)([(user.id, tenant)])
    async_readiness = await aget_tenant_credential_readiness([(user.id, tenant)])

    assert async_reports == sync_reports
    assert async_readiness == sync_readiness


def test_bulk_query_count_does_not_grow_per_workspace(user):
    for i in range(6):
        workspace = _workspace(f"Workspace {i}")
        _member(workspace, user)
        tenant = _tenant(workspace, "commcare", f"domain-{i}", f"Domain {i}")
        _api_membership(user, tenant)

    with CaptureQueriesContext(connection) as queries:
        reports = get_workspace_credential_coverage()

    assert len(reports) >= 6
    assert len(queries) <= 6


def test_management_command_json_reports_only_ids_names_and_structured_gaps(user):
    workspace = _workspace("Audit Workspace")
    _member(workspace, user)
    tenant = _tenant(workspace, "commcare", "private-domain", "Private Domain")
    membership, conn = _api_membership(user, tenant, key="never-print-this")
    conn.encrypted_credential = "not-fernet"
    conn.save(update_fields=["encrypted_credential"])

    stdout = StringIO()
    call_command("report_workspace_credential_coverage", "--json", stdout=stdout)
    raw = stdout.getvalue()
    payload = json.loads(raw)

    assert payload[0]["workspace_name"] == "Audit Workspace"
    assert payload[0]["user_id"] == user.id
    assert payload[0]["gaps"][0]["membership_id"] == str(membership.id)
    assert payload[0]["gaps"][0]["code"] == "api_key_decrypt_failed"
    assert "email" not in raw
    assert "never-print-this" not in raw
    assert "not-fernet" not in raw
    assert "fingerprint" not in raw


def test_management_command_readable_output_explains_local_readiness(user):
    workspace = _workspace("Readable")
    _member(workspace, user)
    _tenant(workspace, "commcare", "missing", "Missing Domain")

    stdout = StringIO()
    call_command("report_workspace_credential_coverage", stdout=stdout)

    output = stdout.getvalue()
    assert "local credential readiness" in output.lower()
    assert "live upstream confirmation" in output.lower()
    assert "missing_live_membership" in output
