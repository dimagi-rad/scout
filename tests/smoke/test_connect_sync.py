"""Smoke test: full Connect sync pipeline against live data.

Runs the complete Discover → Load → Transform pipeline for each
configured opportunity ID. Requires real TenantMembership + OAuth
token in the platform DB.

Configure in tests/smoke/.env:
    CONNECT_OPPORTUNITY_IDS=814,765
"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)


@pytest.mark.smoke
@pytest.mark.django_db(transaction=True)
class TestConnectSync:
    def _resolve_credential(self, tm):
        """Resolve OAuth credential for a TenantMembership, mirroring server.py logic."""
        from apps.users.models import TenantCredential

        try:
            cred_obj = TenantCredential.objects.select_related("tenant_membership").get(
                tenant_membership=tm
            )
        except TenantCredential.DoesNotExist:
            pytest.fail(
                f"No TenantCredential for membership {tm.id} "
                f"(tenant_id={tm.tenant_id}). Create one first."
            )

        if cred_obj.credential_type == TenantCredential.API_KEY:
            from apps.users.adapters import decrypt_credential

            return {"type": "api_key", "value": decrypt_credential(cred_obj.encrypted_credential)}

        from allauth.socialaccount.models import SocialToken

        token_obj = SocialToken.objects.filter(
            account__user=tm.user,
            account__provider__startswith="commcare_connect",
        ).first()

        if not token_obj:
            pytest.fail(
                f"No SocialToken for user {tm.user.email} with provider "
                f"commcare_connect. User must OAuth into Connect first."
            )
        return {"type": "oauth", "value": token_obj.token}

    def test_full_pipeline(self, connect_opportunity_id):
        """Run the full Connect sync pipeline for one opportunity."""
        from apps.users.models import TenantMembership
        from mcp_server.pipeline_registry import get_registry
        from mcp_server.services.materializer import run_pipeline

        opp_id = connect_opportunity_id

        # ── Resolve TenantMembership ──────────────────────────────────────
        try:
            tm = TenantMembership.objects.select_related("user").get(
                tenant_id=opp_id, provider="commcare_connect"
            )
        except TenantMembership.DoesNotExist:
            pytest.fail(
                f"No TenantMembership(provider='commcare_connect', tenant_id='{opp_id}'). "
                f"A user must connect their Connect account for this opportunity."
            )

        # ── Resolve credential ────────────────────────────────────────────
        credential = self._resolve_credential(tm)

        # ── Resolve pipeline ──────────────────────────────────────────────
        pipeline_config = get_registry().get("connect_sync")
        assert pipeline_config is not None, "connect_sync pipeline not found in registry"

        # ── Run pipeline ──────────────────────────────────────────────────
        progress_log = []

        def on_progress(current: int, total: int, message: str) -> None:
            progress_log.append(message)
            logger.info("[%d/%d] %s", current, total, message)

        result = run_pipeline(tm, credential, pipeline_config, on_progress)

        # ── Assert results ────────────────────────────────────────────────
        assert result["status"] == "completed", f"Pipeline failed: {result}"
        assert result.get("schema"), "No schema in result"

        # Log summary for human review
        logger.info("Pipeline completed for opportunity %s", opp_id)
        logger.info("  Schema: %s", result["schema"])
        logger.info("  Run ID: %s", result.get("run_id"))
        sources = result.get("sources", {})
        total_rows = 0
        for source_name, row_count in sources.items():
            logger.info("  %s: %d rows", source_name, row_count)
            total_rows += row_count
        logger.info("  Total: %d rows across %d sources", total_rows, len(sources))

        # At least some data should have been loaded
        assert total_rows > 0, (
            f"Pipeline completed but loaded 0 rows for opportunity {opp_id}. "
            f"Is there data in this opportunity?"
        )
