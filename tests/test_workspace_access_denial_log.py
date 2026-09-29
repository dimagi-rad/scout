"""A coverage denial leaves one operator log line per request, so a member who
reports being locked out can be traced without turning expected denials into
Sentry events."""

import logging

import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant
from apps.workspaces import access_cache
from apps.workspaces.access import (
    TENANT_ACCESS_LOST,
    access_denied_body,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.workspaces.services.credential_coverage import CredentialGapCode

User = get_user_model()

LOGGER = "apps.workspaces.access"
EVENT = "workspace_access_denied_coverage"


def _denial_records(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.getMessage().startswith(EVENT)]


def _uncovered_member(email):
    user = User.objects.create_user(email=email, password="pass")
    tenant = Tenant.objects.create(provider="commcare", external_id="gap", canonical_name="Gap")
    ws = Workspace.objects.create(name="Gap WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return user, tenant, ws


@pytest.fixture
def scope():
    scope, token = access_cache.open_scope()
    yield scope
    access_cache.close_scope(scope)
    access_cache.detach_scope(token)


@pytest.mark.django_db
def test_coverage_denial_logs_once_per_request_at_info(caplog, scope):
    user, tenant, ws = _uncovered_member("denial-log@example.com")
    caplog.set_level(logging.INFO, logger=LOGGER)

    # Distinct minimum roles are distinct cache entries, so both run the gate.
    first = resolve_workspace_access_ex(user, ws.id)
    second = resolve_workspace_access_ex(user, ws.id, minimum_role=WorkspaceRole.MANAGE)

    assert first.denied_reason == second.denied_reason == TENANT_ACCESS_LOST
    [record] = _denial_records(caplog)
    assert record.levelno == logging.INFO
    assert record.user_id == user.pk
    assert record.workspace_id == str(ws.id)
    assert record.tenant_ids == [str(tenant.pk)]
    assert record.gap_codes == [CredentialGapCode.MISSING_LIVE_MEMBERSHIP]
    message = record.getMessage()
    assert f"user_id={user.pk}" in message
    assert f"{tenant.pk}:missing_live_membership:connect_source" in message
    assert user.email not in message
    assert "Gap" not in message


@pytest.mark.django_db
def test_each_request_logs_its_own_denial(caplog):
    user, _tenant, ws = _uncovered_member("denial-log-requests@example.com")
    caplog.set_level(logging.INFO, logger=LOGGER)

    for _ in range(2):
        scope, token = access_cache.open_scope()
        try:
            resolve_workspace_access_ex(user, ws.id)
        finally:
            access_cache.close_scope(scope)
            access_cache.detach_scope(token)

    assert len(_denial_records(caplog)) == 2


@pytest.mark.django_db
def test_granted_and_non_member_decisions_are_not_logged(caplog, scope):
    user = User.objects.create_user(email="denial-log-ok@example.com", password="pass")
    ws = Workspace.objects.create(name="No sources", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.READ)
    outsider = User.objects.create_user(email="denial-log-out@example.com", password="pass")
    caplog.set_level(logging.INFO, logger=LOGGER)

    assert resolve_workspace_access_ex(user, ws.id).granted
    assert not resolve_workspace_access_ex(outsider, ws.id).granted

    assert _denial_records(caplog) == []


@pytest.mark.django_db
def test_gap_code_stays_out_of_the_denial_body(scope):
    user, _tenant, ws = _uncovered_member("denial-log-body@example.com")

    result = resolve_workspace_access_ex(user, ws.id)

    assert result.missing_tenants[0].gap_code == CredentialGapCode.MISSING_LIVE_MEMBERSHIP
    assert "gap_code" not in access_denied_body(result)["missing_tenants"][0]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_coverage_denial_logs_once_per_request(caplog, scope):
    user = await User.objects.acreate_user(email="denial-log-async@example.com", password="pass")
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="agap", canonical_name="AGap"
    )
    ws = await Workspace.objects.acreate(name="Async gap", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    caplog.set_level(logging.INFO, logger=LOGGER)

    await aresolve_workspace_access_ex(user, ws.id)
    await aresolve_workspace_access_ex(user, ws.id, minimum_role=WorkspaceRole.MANAGE)

    [record] = _denial_records(caplog)
    assert record.tenant_ids == [str(tenant.pk)]
