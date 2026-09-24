"""Pytest plugin: memberships bound to the shared usable test credential start verified.

``tests.tenant_access.usable_connection`` models a member who has just connected
their account, which in production means a successful upstream listing, so the
upstream-freshness gate should see a recent proof for them. Only that credential
qualifies: verification-service suites build their own credentials precisely to
start without a proof, and must keep doing so. Registered from the rootdir
conftest so it also covers ``apps/*/tests``.
"""

import pytest
from django.db.models.signals import post_save

from apps.users.models import TenantMembership
from tests.tenant_access import is_usable_test_connection, record_fresh_proof

_DISPATCH_UID = "tests-bound-membership-proof"


def _record_proof_for_bound_membership(sender, instance, created, raw=False, **kwargs):
    if raw or not created or instance.connection_id is None or instance.archived_at:
        return
    if is_usable_test_connection(instance.connection):
        record_fresh_proof(instance.connection, instance.tenant)


@pytest.fixture(autouse=True)
def _bound_test_memberships_start_verified():
    post_save.connect(
        _record_proof_for_bound_membership, sender=TenantMembership, dispatch_uid=_DISPATCH_UID
    )
    yield
    post_save.disconnect(sender=TenantMembership, dispatch_uid=_DISPATCH_UID)
