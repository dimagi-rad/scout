"""Creating a workspace queues its first load as the creator (#353)."""

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.users.models import Tenant
from apps.workspaces.models import TenantLoadGeneration, Workspace
from apps.workspaces.services import workspace_service
from tests.tenant_access import grant_tenant_access


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def t1(db):
    return Tenant.objects.create(provider="commcare", external_id="t1", canonical_name="One")


@pytest.fixture
def t2(db):
    return Tenant.objects.create(provider="commcare", external_id="t2", canonical_name="Two")


@pytest.fixture
def defer():
    with patch.object(workspace_service.materialize_workspace, "defer") as mock:
        yield mock


def _create(client, user, *tenants):
    for tenant in tenants:
        grant_tenant_access(user, tenant)
    client.force_login(user)
    resp = client.post(
        "/api/workspaces/",
        {"name": "New", "tenant_ids": [str(t.id) for t in tenants]},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    return Workspace.objects.get(id=resp.json()["id"])


@pytest.mark.django_db
class TestCreateQueuesLoad:
    def test_one_load_is_queued_after_commit_as_the_creator(
        self, client, user, t1, t2, defer, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            ws = _create(client, user, t1, t2)
        defer.assert_not_called()

        for callback in callbacks:
            callback()

        defer.assert_called_once()
        kwargs = defer.call_args.kwargs
        assert kwargs["workspace_id"] == str(ws.id)
        assert kwargs["user_id"] == str(user.id)
        assert kwargs["only_unserved"] is True
        assert kwargs["notify_thread"] is False
        assert kwargs["load_intent"] == {str(t1.id): 1, str(t2.id): 1}

    def test_a_first_load_already_in_flight_is_joined_not_requested_again(
        self, client, user, t1, defer, django_capture_on_commit_callbacks
    ):
        TenantLoadGeneration.objects.create(
            tenant=t1, requested_generation=1, published_generation=0, loading_generation=1
        )

        with django_capture_on_commit_callbacks(execute=True):
            _create(client, user, t1)

        defer.assert_called_once()
        assert defer.call_args.kwargs["load_intent"] == {str(t1.id): 1}
        assert TenantLoadGeneration.objects.get(tenant=t1).requested_generation == 1

    def test_sources_already_loaded_elsewhere_are_published_not_reloaded(
        self, client, user, t1, defer, django_capture_on_commit_callbacks
    ):
        TenantLoadGeneration.objects.create(
            tenant=t1, requested_generation=2, published_generation=2
        )

        with django_capture_on_commit_callbacks(execute=True):
            _create(client, user, t1)

        defer.assert_called_once()
        assert defer.call_args.kwargs["load_intent"] == {str(t1.id): 2}
        assert defer.call_args.kwargs["only_unserved"] is True
        assert TenantLoadGeneration.objects.get(tenant=t1).requested_generation == 2

    def test_a_create_without_sources_is_refused_and_queues_nothing(
        self, client, user, defer, django_capture_on_commit_callbacks
    ):
        client.force_login(user)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post("/api/workspaces/", {"name": "New", "tenant_ids": []}, format="json")

        assert resp.status_code == 400
        defer.assert_not_called()

    def test_a_queue_outage_does_not_fail_the_create(
        self, client, user, t1, defer, django_capture_on_commit_callbacks
    ):
        defer.side_effect = RuntimeError("queue down")

        with django_capture_on_commit_callbacks(execute=True):
            ws = _create(client, user, t1)

        assert Workspace.objects.filter(id=ws.id).exists()
