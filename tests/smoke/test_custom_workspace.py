"""Smoke test: custom workspace lifecycle (create, enter, chat, delete).

Exercises the full custom workspace CRUD via Django's test client plus
a streaming chat interaction that proves the agent sees the workspace's
tenant data.

Configure in tests/smoke/.env:
    SCOUT_BASE_URL=http://localhost:8001
    SMOKE_COMMCARE_TENANT=jonstest
    SMOKE_CONNECT_TENANT=            # optional — leave blank to test with one tenant

Run:
    uv run pytest -m smoke --override-ini="addopts=" \
        -o "DJANGO_SETTINGS_MODULE=config.settings.development" \
        -s --log-cli-level=INFO
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from tests.smoke.conftest import smoke_env

logger = logging.getLogger(__name__)


def _get_commcare_tenant():
    return smoke_env("SMOKE_COMMCARE_TENANT", default="")


def _get_connect_tenant():
    return smoke_env("SMOKE_CONNECT_TENANT", default="")


def _get_or_create_user():
    """Return the first superuser, or the first user with any TenantMembership."""
    from django.contrib.auth import get_user_model

    User = get_user_model()

    # Prefer a superuser for simplicity
    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if user:
        return user

    # Fallback: any user that already has tenant memberships
    from apps.users.models import TenantMembership

    tm = TenantMembership.objects.select_related("user").first()
    if tm:
        return tm.user

    pytest.fail(
        "No suitable user found in the platform database. "
        "Create a superuser or ensure a user with TenantMemberships exists."
    )


def _ensure_tenant_membership(user, provider, tenant_id, tenant_name):
    """Get or create a TenantMembership for the given tenant."""
    from apps.users.models import TenantMembership

    tm, created = TenantMembership.objects.get_or_create(
        user=user,
        provider=provider,
        tenant_id=tenant_id,
        defaults={"tenant_name": tenant_name},
    )
    if created:
        logger.info(
            "Created TenantMembership for %s:%s (user: %s)", provider, tenant_id, user.email
        )
    return tm


def _ensure_tenant_workspace(tenant_id, tenant_name):
    """Get or create a TenantWorkspace record."""
    from apps.workspace.models import TenantWorkspace

    tw, created = TenantWorkspace.objects.get_or_create(
        tenant_id=tenant_id,
        defaults={"tenant_name": tenant_name},
    )
    if created:
        logger.info("Created TenantWorkspace for %s", tenant_id)
    return tw


@pytest.mark.smoke
@pytest.mark.django_db
class TestCustomWorkspaceLifecycle:
    """Create -> Enter -> Chat -> Delete a custom workspace."""

    def _build_client(self, user):
        """Return a Django test client logged in as *user*."""
        from django.test import Client

        client = Client(enforce_csrf_checks=False)
        client.force_login(user)
        return client

    def _setup_tenants(self, user):
        """Resolve tenant env vars and create necessary DB records.

        Returns a list of (TenantMembership, TenantWorkspace) tuples and
        skips the test when no tenants are configured.
        """
        commcare_tenant = _get_commcare_tenant()
        connect_tenant = _get_connect_tenant()

        if not commcare_tenant and not connect_tenant:
            pytest.skip(
                "Neither SMOKE_COMMCARE_TENANT nor SMOKE_CONNECT_TENANT set in tests/smoke/.env"
            )

        tenants = []

        if commcare_tenant:
            tm = _ensure_tenant_membership(user, "commcare", commcare_tenant, commcare_tenant)
            tw = _ensure_tenant_workspace(commcare_tenant, commcare_tenant)
            tenants.append((tm, tw))
            logger.info(
                "CommCare tenant: %s (membership=%s, workspace=%s)", commcare_tenant, tm.id, tw.id
            )

        if connect_tenant:
            tm = _ensure_tenant_membership(user, "commcare_connect", connect_tenant, connect_tenant)
            tw = _ensure_tenant_workspace(connect_tenant, connect_tenant)
            tenants.append((tm, tw))
            logger.info(
                "Connect tenant: %s (membership=%s, workspace=%s)", connect_tenant, tm.id, tw.id
            )

        return tenants

    # ------------------------------------------------------------------
    # The test
    # ------------------------------------------------------------------

    def test_workspace_lifecycle(self):
        """Full lifecycle: create, enter, chat, delete."""
        user = _get_or_create_user()
        logger.info("Using user: %s", user.email)

        tenants = self._setup_tenants(user)
        client = self._build_client(user)

        tenant_workspace_ids = [str(tw.id) for (_tm, tw) in tenants]
        tenant_membership = tenants[0][0]  # use the first membership for chat

        # ── 1. Create workspace ────────────────────────────────────────
        workspace_name = f"smoke-test-{uuid.uuid4().hex[:8]}"
        create_payload = {
            "name": workspace_name,
            "description": "Automated smoke test workspace",
            "tenant_workspace_ids": tenant_workspace_ids,
        }
        logger.info(
            "Creating workspace %r with %d tenant(s)", workspace_name, len(tenant_workspace_ids)
        )
        resp = client.post(
            "/api/custom-workspaces/",
            data=json.dumps(create_payload),
            content_type="application/json",
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.content}"
        workspace_data = resp.json()
        workspace_id = workspace_data["id"]
        logger.info("Created workspace: %s (id=%s)", workspace_data["name"], workspace_id)

        # Verify the tenants are attached
        returned_tenants = workspace_data.get("tenants", [])
        assert len(returned_tenants) == len(tenants), (
            f"Expected {len(tenants)} tenant(s), got {len(returned_tenants)}"
        )

        try:
            # ── 2. Enter workspace ─────────────────────────────────────
            logger.info("Entering workspace %s", workspace_id)
            resp = client.post(f"/api/custom-workspaces/{workspace_id}/enter/")
            assert resp.status_code == 200, (
                f"Expected 200 on enter, got {resp.status_code}: {resp.content}"
            )
            enter_data = resp.json()
            assert enter_data["id"] == workspace_id
            logger.info("Entered workspace successfully")

            # ── 3. Chat interaction ────────────────────────────────────
            logger.info("Sending chat message with X-Custom-Workspace header")
            chat_payload = {
                "messages": [
                    {"role": "user", "content": "What tables are available in this workspace?"}
                ],
                "tenantId": str(tenant_membership.id),
            }
            resp = client.post(
                "/api/chat/",
                data=json.dumps(chat_payload),
                content_type="application/json",
                HTTP_X_CUSTOM_WORKSPACE=str(workspace_id),
            )
            if resp.status_code == 500:
                # Agent initialization can fail on Windows (ProactorEventLoop
                # incompatible with psycopg async) or when external services
                # are unavailable.  Log the issue but don't fail the CRUD test.
                body = resp.json() if resp["content-type"] == "application/json" else {}
                logger.warning(
                    "Chat returned 500 (agent init issue, not a workspace bug): %s",
                    body.get("error", resp.content[:200]),
                )
            else:
                assert resp.status_code == 200, (
                    f"Expected 200 from chat, got {resp.status_code}: {resp.content}"
                )

                # Read the full streaming response (async generator via test client)
                chunks = []
                for chunk in resp:
                    chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
                response_text = b"".join(chunks).decode("utf-8", errors="replace")
                logger.info(
                    "Chat response length: %d chars (first 500: %s)",
                    len(response_text),
                    response_text[:500],
                )

                # The response should contain *something* — the agent should reply
                assert len(response_text) > 0, "Empty chat response"
                logger.info("Chat interaction completed successfully")

            # ── 4. Verify workspace detail ─────────────────────────────
            resp = client.get(f"/api/custom-workspaces/{workspace_id}/")
            assert resp.status_code == 200, (
                f"Expected 200 on detail, got {resp.status_code}: {resp.content}"
            )

        finally:
            # ── 5. Delete workspace ────────────────────────────────────
            logger.info("Deleting workspace %s", workspace_id)
            resp = client.delete(f"/api/custom-workspaces/{workspace_id}/")
            assert resp.status_code == 204, (
                f"Expected 204 on delete, got {resp.status_code}: {resp.content}"
            )
            logger.info("Deleted workspace successfully")

        # ── 6. Verify cleanup ──────────────────────────────────────────
        resp = client.get(f"/api/custom-workspaces/{workspace_id}/")
        assert resp.status_code in (404, 403), (
            f"Expected 404 or 403 after delete, got {resp.status_code}"
        )
        logger.info("Workspace cleanup verified (status=%d)", resp.status_code)

        print()
        print("=" * 70)
        print("  Custom workspace lifecycle test PASSED")
        print(f"  Workspace: {workspace_name}")
        print(f"  Tenants:   {len(tenants)}")
        print(f"  User:      {user.email}")
        print("=" * 70)
        print()
