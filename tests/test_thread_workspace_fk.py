"""Tests for Task 3.4: Thread.workspace FK (replaces tenant_membership)."""

from django.contrib.auth import get_user_model

User = get_user_model()


def test_thread_belongs_to_workspace(workspace, user):
    from apps.chat.models import Thread

    thread = Thread.objects.create(workspace=workspace, user=user, title="Test")
    assert thread.workspace == workspace


def test_thread_has_no_tenant_membership_field(workspace, user):
    from apps.chat.models import Thread

    thread = Thread.objects.create(workspace=workspace, user=user, title="Test")
    assert not hasattr(thread, "tenant_membership_id")


def test_thread_has_no_is_public_field(workspace, user):
    from apps.chat.models import Thread

    thread = Thread.objects.create(workspace=workspace, user=user, title="Test")
    assert not hasattr(thread, "is_public")


def test_thread_sharing_uses_is_shared_and_share_token(workspace, user):
    from apps.chat.models import Thread

    thread = Thread.objects.create(workspace=workspace, user=user, title="Test")
    assert thread.is_shared is False
    assert thread.share_token is None

    thread.is_shared = True
    thread.save()
    thread.refresh_from_db()
    assert thread.share_token is not None


def test_thread_workspace_deletion_cascades(workspace, user):
    from apps.chat.models import Thread

    thread = Thread.objects.create(workspace=workspace, user=user, title="To delete")
    thread_id = thread.id
    workspace.delete()
    assert Thread.objects.filter(id=thread_id).count() == 0


def test_thread_list_view_scoped_to_workspace(db, workspace, user):
    from django.test import Client

    from apps.chat.models import Thread

    Thread.objects.create(workspace=workspace, user=user, title="Thread 1")
    Thread.objects.create(workspace=workspace, user=user, title="Thread 2")

    client = Client(enforce_csrf_checks=False)
    client.force_login(user)
    response = client.get(f"/api/workspaces/{workspace.id}/threads/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    titles = {t["title"] for t in data}
    assert "Thread 1" in titles
    assert "Thread 2" in titles
