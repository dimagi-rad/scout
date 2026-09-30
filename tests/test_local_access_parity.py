"""Sync, async and bulk local access agree on every membership/coverage/role fact.

The authorizer decides local access in three places: the sync gate, its async twin
and the bulk listing path. These pin that the three reach the same outcome, with
the same missing sources, across role thresholds, membership, any-of and all-of
coverage, upstream freshness and a workspace with no sources, so a rule changed in
one cannot silently diverge in the others.
"""

import logging
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.access import (
    INSUFFICIENT_ROLE,
    NO_SOURCES,
    NOT_MEMBER,
    TENANT_ACCESS_LOST,
    aresolve_local_access_many,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.workspaces.services.access_freshness import CREDENTIAL_MISSING, VerificationBudget
from tests.tenant_access import grant_tenant_access

User = get_user_model()

LOGGER = "apps.workspaces.access"
COVERAGE_EVENT = "workspace_access_denied_coverage"
NO_SOURCES_EVENT = "workspace_access_denied_no_sources"

READ, READ_WRITE, MANAGE = WorkspaceRole.READ, WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE

# The user's standing on each workspace source:
#   ok: a live membership with a usable credential
#   absent: no membership at all
#   archived: a usable membership since revoked (a tombstone)
#   unbound: a live membership with no credential bound (a legacy row)
SOURCES = {
    "no_sources": (),
    "covered": ("ok",),
    "all_covered": ("ok", "ok"),
    "partial": ("ok", "absent"),
    "one_archived": ("ok", "archived"),
    "one_unbound": ("ok", "unbound"),
    "uncovered": ("absent", "absent"),
    "only_archived": ("archived",),
    "only_unbound": ("unbound",),
}

# off: no freshness check; on: the interactive data gate with the switch on;
# recovery: the switch on but ``verification=None`` (recovery metadata).
FRESHNESS = ("off", "on", "recovery")


def _user(label):
    return User.objects.create_user(email=f"{label}-{uuid.uuid4().hex[:8]}@example.com")


def _source(user, workspace, standing):
    tenant = Tenant.objects.create(
        provider="commcare", external_id=f"t-{uuid.uuid4().hex[:12]}", canonical_name="Source"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    if standing in ("ok", "archived"):
        grant_tenant_access(user, tenant)
    if standing == "archived":
        TenantMembership.all_objects.filter(user=user, tenant=tenant).update(
            archived_at=timezone.now()
        )
    if standing == "unbound":
        # bulk_create, so the auto-create-workspace signal does not fire.
        TenantMembership.objects.bulk_create([TenantMembership(user=user, tenant=tenant)])
    return tenant


def _setup(sources, *, role=MANAGE, membership="member"):
    user = _user("subject")
    workspace = Workspace.objects.create(name="Parity", created_by=user)
    for standing in SOURCES[sources]:
        _source(user, workspace, standing)
    held = None
    if membership == "member":
        held = WorkspaceMembership.objects.create(workspace=workspace, user=user, role=role)
    elif membership == "foreign":
        other = _user("other")
        held = WorkspaceMembership.objects.create(workspace=workspace, user=other, role=role)
    return user, workspace, held


def _outcome(result):
    missing = sorted((t.tenant_id, t.gap_code, t.recovery) for t in result.missing_tenants)
    held = (result.workspace.pk, result.membership.pk) if result.granted else None
    return result.granted, result.denied_reason, missing, held


def _configure(settings, *, all_of, freshness):
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = all_of
    settings.UPSTREAM_ACCESS_FRESHNESS_ENFORCED = freshness != "off"
    return None if freshness == "recovery" else VerificationBudget.INTERACTIVE


def _gates(user, workspace, *, minimum_role, verification):
    sync = resolve_workspace_access_ex(
        user, workspace.id, minimum_role=minimum_role, verification=verification
    )
    async_ = async_to_sync(aresolve_workspace_access_ex)(
        user, workspace.id, minimum_role=minimum_role, verification=verification
    )
    return sync, async_


def _bulk(user, memberships, *, minimum_role):
    return async_to_sync(aresolve_local_access_many)(user, memberships, minimum_role=minimum_role)


def _expected_local_reason(sources, *, all_of, role, minimum_role):
    standings = SOURCES[sources]
    if not standings:
        return NO_SOURCES
    live = {"ok", "unbound"}
    covered = all(s == "ok" for s in standings) if all_of else any(s in live for s in standings)
    if not covered:
        return TENANT_ACCESS_LOST
    ranks = [READ, READ_WRITE, MANAGE]
    if ranks.index(role) < ranks.index(minimum_role):
        return INSUFFICIENT_ROLE
    return None


def _expected_gate_reason(sources, *, all_of, freshness, role, minimum_role):
    local = _expected_local_reason(sources, all_of=all_of, role=role, minimum_role=minimum_role)
    # A live binding with no credential can never be rechecked upstream (any-of only:
    # all-of already denies it locally).
    if local is None and freshness == "on" and "unbound" in SOURCES[sources]:
        return CREDENTIAL_MISSING
    return local


@pytest.mark.django_db
@pytest.mark.parametrize("freshness", FRESHNESS)
@pytest.mark.parametrize("all_of", [True, False], ids=["all_of", "any_of"])
@pytest.mark.parametrize("sources", list(SOURCES))
@pytest.mark.parametrize(
    "role, minimum_role", [(MANAGE, READ), (READ, READ_WRITE)], ids=["role_ok", "role_short"]
)
def test_member_outcome_agrees_across_sync_async_and_bulk(
    settings, sources, all_of, freshness, role, minimum_role
):
    verification = _configure(settings, all_of=all_of, freshness=freshness)
    user, workspace, membership = _setup(sources, role=role)

    sync, async_ = _gates(user, workspace, minimum_role=minimum_role, verification=verification)
    bulk = _bulk(user, [membership], minimum_role=minimum_role)[workspace.id]

    assert _outcome(sync) == _outcome(async_)
    assert sync.denied_reason == _expected_gate_reason(
        sources, all_of=all_of, freshness=freshness, role=role, minimum_role=minimum_role
    )
    # Bulk is the local decision only: no upstream admission or proof freshness.
    assert bulk.denied_reason == _expected_local_reason(
        sources, all_of=all_of, role=role, minimum_role=minimum_role
    )
    if bulk.denied_reason == sync.denied_reason:
        assert _outcome(bulk) == _outcome(sync)
    if bulk.denied_reason == TENANT_ACCESS_LOST:
        assert bulk.missing_tenants
    if bulk.granted:
        assert bulk.membership.pk == membership.pk


@pytest.mark.django_db
@pytest.mark.parametrize("freshness", FRESHNESS)
@pytest.mark.parametrize("minimum_role", [READ, READ_WRITE, MANAGE])
@pytest.mark.parametrize("role", [READ, READ_WRITE, MANAGE])
def test_role_threshold_agrees_across_sync_async_and_bulk(settings, role, minimum_role, freshness):
    verification = _configure(settings, all_of=True, freshness=freshness)
    user, workspace, membership = _setup("all_covered", role=role)

    sync, async_ = _gates(user, workspace, minimum_role=minimum_role, verification=verification)
    bulk = _bulk(user, [membership], minimum_role=minimum_role)[workspace.id]

    expected = _expected_local_reason(
        "all_covered", all_of=True, role=role, minimum_role=minimum_role
    )
    assert _outcome(sync) == _outcome(async_) == _outcome(bulk)
    assert sync.denied_reason == expected


@pytest.mark.django_db
@pytest.mark.parametrize("all_of", [True, False], ids=["all_of", "any_of"])
@pytest.mark.parametrize("sources", ["no_sources", "covered", "uncovered"])
@pytest.mark.parametrize("membership", ["absent", "foreign"])
def test_non_member_is_denied_before_sources_or_coverage(settings, membership, sources, all_of):
    verification = _configure(settings, all_of=all_of, freshness="on")
    user, workspace, foreign = _setup(sources, membership=membership)

    sync, async_ = _gates(user, workspace, minimum_role=READ, verification=verification)
    exempt = resolve_workspace_access_ex(user, workspace.id, require_coverage=False)
    bulk = _bulk(user, [foreign] if foreign else [], minimum_role=READ)

    assert sync.denied_reason == async_.denied_reason == exempt.denied_reason == NOT_MEMBER
    if foreign is None:
        assert bulk == {}
    else:
        assert _outcome(bulk[workspace.id]) == _outcome(sync)


@pytest.mark.django_db
@pytest.mark.parametrize("freshness", ["off", "on"])
@pytest.mark.parametrize("all_of", [True, False], ids=["all_of", "any_of"])
@pytest.mark.parametrize("sources", list(SOURCES))
@pytest.mark.parametrize(
    "role, minimum_role", [(MANAGE, READ), (READ, READ_WRITE)], ids=["role_ok", "role_short"]
)
def test_coverage_exemption_is_sync_only_and_keeps_the_role_check(
    settings, sources, all_of, freshness, role, minimum_role
):
    """``require_coverage=False`` lets in only what coverage (or no sources) denied."""
    verification = _configure(settings, all_of=all_of, freshness=freshness)
    user, workspace, _membership = _setup(sources, role=role)

    gate = resolve_workspace_access_ex(user, workspace.id, minimum_role=minimum_role)
    exempt = resolve_workspace_access_ex(
        user,
        workspace.id,
        minimum_role=minimum_role,
        verification=verification,
        require_coverage=False,
    )

    exempted = gate.denied_reason == NO_SOURCES or (
        all_of and gate.denied_reason == TENANT_ACCESS_LOST
    )
    if not exempted:
        assert _outcome(exempt) == _outcome(gate)
    elif role == MANAGE:
        assert exempt.granted
        assert exempt.missing_tenants == ()
    else:
        assert exempt.denied_reason == INSUFFICIENT_ROLE


@pytest.mark.django_db
def test_bulk_keeps_own_result_when_a_foreign_membership_shares_the_workspace(settings):
    """A foreign row for the same workspace never overwrites the caller's own result."""
    _configure(settings, all_of=True, freshness="off")
    user, workspace, own = _setup("covered", role=MANAGE)
    foreign = WorkspaceMembership.objects.create(
        workspace=workspace, user=_user("other"), role=MANAGE
    )

    for memberships in ([own, foreign], [foreign, own]):
        result = _bulk(user, memberships, minimum_role=READ)[workspace.id]
        assert result.granted
        assert result.membership.pk == own.pk


@pytest.mark.django_db
@pytest.mark.parametrize("all_of", [True, False], ids=["all_of", "any_of"])
def test_bulk_over_many_workspaces_matches_each_single_gate(settings, all_of):
    _configure(settings, all_of=all_of, freshness="off")
    user = _user("many")
    memberships = []
    for sources, role in [
        ("covered", MANAGE),
        ("partial", MANAGE),
        ("uncovered", MANAGE),
        ("no_sources", MANAGE),
        ("one_archived", READ),
        ("all_covered", READ),
    ]:
        workspace = Workspace.objects.create(name=sources, created_by=user)
        for standing in SOURCES[sources]:
            _source(user, workspace, standing)
        memberships.append(
            WorkspaceMembership.objects.create(workspace=workspace, user=user, role=role)
        )
    stranger = _setup("covered", membership="foreign")[2]

    bulk = _bulk(user, [*memberships, stranger], minimum_role=READ_WRITE)

    assert set(bulk) == {m.workspace_id for m in [*memberships, stranger]}
    assert bulk[stranger.workspace_id].denied_reason == NOT_MEMBER
    for m in memberships:
        single = resolve_workspace_access_ex(
            user, m.workspace_id, minimum_role=READ_WRITE, verification=None
        )
        assert _outcome(bulk[m.workspace_id]) == _outcome(single)


def _events(caplog, event):
    return [r for r in caplog.records if r.name == LOGGER and r.getMessage().startswith(event)]


@pytest.mark.django_db
def test_coverage_denial_is_logged_by_single_gates_but_not_by_bulk(settings, caplog):
    _configure(settings, all_of=True, freshness="off")
    caplog.set_level(logging.INFO, logger=LOGGER)
    user, workspace, membership = _setup("partial")

    assert _bulk(user, [membership], minimum_role=READ)[workspace.id].denied_reason == (
        TENANT_ACCESS_LOST
    )
    assert _events(caplog, COVERAGE_EVENT) == []

    sync, async_ = _gates(user, workspace, minimum_role=READ, verification=None)

    assert sync.denied_reason == async_.denied_reason == TENANT_ACCESS_LOST
    assert len(_events(caplog, COVERAGE_EVENT)) == 2


@pytest.mark.django_db
def test_no_sources_denial_is_logged_by_every_path(settings, caplog):
    _configure(settings, all_of=True, freshness="off")
    caplog.set_level(logging.INFO, logger=LOGGER)
    user, workspace, membership = _setup("no_sources")

    _gates(user, workspace, minimum_role=READ, verification=None)
    _bulk(user, [membership], minimum_role=READ)

    assert len(_events(caplog, NO_SOURCES_EVENT)) == 3
    assert _events(caplog, COVERAGE_EVENT) == []


@pytest.mark.django_db
def test_exempt_resolution_logs_the_gate_denial_but_not_its_own(settings, caplog):
    """The exemption's second, coverage-free pass adds no denial log of its own."""
    _configure(settings, all_of=True, freshness="off")
    caplog.set_level(logging.INFO, logger=LOGGER)
    user, workspace, _membership = _setup("partial")

    assert resolve_workspace_access_ex(user, workspace.id, require_coverage=False).granted
    assert len(_events(caplog, COVERAGE_EVENT)) == 1
