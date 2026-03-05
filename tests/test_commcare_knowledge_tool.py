"""Tests for apps/agents/tools/commcare_knowledge_tool.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from apps.agents.tools.commcare_knowledge_tool import (
    _build_knowledge_content,
    create_commcare_knowledge_tool,
)


def _mock_app(app_id="app1", app_name="Deliver App"):
    return {
        "id": app_id,
        "name": app_name,
        "modules": [
            {
                "name": "Visit Module",
                "case_type": "visit",
                "forms": [
                    {
                        "xmlns": "http://openrosa.org/formdesigner/deliver1",
                        "name": "Deliver Form",
                        "questions": [
                            {"label": "GPS Location", "tag": "input", "value": "/data/gps"},
                            {"label": "Photo", "tag": "upload", "value": "/data/photo"},
                        ],
                    }
                ],
            }
        ],
    }


def _make_tenant_membership(provider="commcare_connect"):
    tm = MagicMock()
    tm.provider = provider
    tm.tenant_id = "814"
    tm.tenant_name = "CHC Nutrition"
    return tm


def _make_workspace():
    ws = MagicMock()
    ws.tenant_id = "814"
    return ws


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestBuildKnowledgeContent:
    def test_builds_markdown_with_forms_and_questions(self):
        form_defs = {
            "http://openrosa.org/formdesigner/deliver1": {
                "name": "Deliver Form",
                "case_type": "visit",
                "questions": [
                    {"label": "GPS Location", "tag": "input", "value": "/data/gps"},
                    {"label": "Photo", "tag": "upload", "value": "/data/photo"},
                ],
            }
        }
        content = _build_knowledge_content("My App", form_defs)
        assert "# My App" in content
        assert "## Deliver Form" in content
        assert "- **Case type**: visit" in content
        assert "| GPS Location | /data/gps |" in content
        assert "| Photo | /data/photo |" in content

    def test_handles_empty_form_definitions(self):
        content = _build_knowledge_content("Empty App", {})
        assert "# Empty App" in content
        assert "## " not in content

    def test_handles_localized_name(self):
        form_defs = {
            "http://openrosa.org/formdesigner/abc": {
                "name": {"en": "English Name", "fr": "French Name"},
                "case_type": "patient",
                "questions": [],
            }
        }
        content = _build_knowledge_content("Test App", form_defs)
        assert "## English Name" in content


class TestDownloadCommcareKnowledgeTool:
    def test_returns_error_for_non_connect_tenant(self):
        ws = _make_workspace()
        tm = _make_tenant_membership(provider="commcare")
        tool = create_commcare_knowledge_tool(ws, None, tm)

        result = _run(tool.ainvoke({}))
        assert result["status"] == "error"
        assert "CommCare Connect" in result["message"]

    def test_returns_error_when_no_tenant_metadata(self):
        ws = _make_workspace()
        tm = _make_tenant_membership()
        tool = create_commcare_knowledge_tool(ws, None, tm)

        with patch("apps.workspace.models.TenantMetadata.objects") as mock_objects:
            mock_objects.filter.return_value.afirst = AsyncMock(return_value=None)
            result = _run(tool.ainvoke({}))

        assert result["status"] == "error"
        assert "metadata" in result["message"].lower()

    def test_returns_error_when_no_commcare_oauth(self):
        ws = _make_workspace()
        user = MagicMock()
        tm = _make_tenant_membership()
        tool = create_commcare_knowledge_tool(ws, user, tm)

        mock_metadata = MagicMock()
        mock_metadata.metadata = {
            "opportunity": {
                "id": 814,
                "learn_app": {"cc_domain": "test", "cc_app_id": "app1"},
            }
        }

        with (
            patch("apps.workspace.models.TenantMetadata.objects") as mock_objects,
            patch(
                "apps.agents.utils.commcare_auth.get_commcare_credential",
                return_value=None,
            ),
        ):
            mock_objects.filter.return_value.afirst = AsyncMock(return_value=mock_metadata)
            result = _run(tool.ainvoke({}))

        assert result["status"] == "error"
        assert "CommCare account" in result["message"]

    def test_creates_knowledge_entries_for_connect_tenant(self):
        ws = _make_workspace()
        user = MagicMock()
        tm = _make_tenant_membership()
        tool = create_commcare_knowledge_tool(ws, user, tm)

        mock_metadata = MagicMock()
        mock_metadata.metadata = {
            "opportunity": {
                "id": 814,
                "learn_app": {"cc_domain": "test-domain", "cc_app_id": "learn1"},
                "deliver_app": {"cc_domain": "test-domain", "cc_app_id": "deliver1"},
            }
        }

        with (
            patch("apps.workspace.models.TenantMetadata.objects") as mock_tm_objects,
            patch(
                "apps.agents.utils.commcare_auth.get_commcare_credential",
                return_value={"type": "oauth", "value": "test-token"},
            ),
            patch(
                "apps.agents.tools.commcare_knowledge_tool.fetch_app_by_id",
                return_value=_mock_app(),
            ) as mock_fetch,
            patch("apps.knowledge.models.KnowledgeEntry.objects") as mock_ke_objects,
        ):
            mock_tm_objects.filter.return_value.afirst = AsyncMock(return_value=mock_metadata)
            mock_ke_objects.filter.return_value.afirst = AsyncMock(return_value=None)
            mock_ke_objects.acreate = AsyncMock()

            result = _run(tool.ainvoke({}))

        assert result["status"] == "success"
        assert result["entries_created"] > 0
        assert mock_ke_objects.acreate.call_count > 0
        # Both learn_app and deliver_app should trigger fetch calls
        assert mock_fetch.call_count == 2

    def test_updates_existing_knowledge_entry(self):
        ws = _make_workspace()
        user = MagicMock()
        tm = _make_tenant_membership()
        tool = create_commcare_knowledge_tool(ws, user, tm)

        mock_metadata = MagicMock()
        mock_metadata.metadata = {
            "opportunity": {
                "id": 814,
                "deliver_app": {"cc_domain": "test-domain", "cc_app_id": "deliver1"},
            }
        }

        existing_entry = MagicMock()
        existing_entry.asave = AsyncMock()

        with (
            patch("apps.workspace.models.TenantMetadata.objects") as mock_tm_objects,
            patch(
                "apps.agents.utils.commcare_auth.get_commcare_credential",
                return_value={"type": "oauth", "value": "test-token"},
            ),
            patch(
                "apps.agents.tools.commcare_knowledge_tool.fetch_app_by_id",
                return_value=_mock_app(),
            ),
            patch("apps.knowledge.models.KnowledgeEntry.objects") as mock_ke_objects,
        ):
            mock_tm_objects.filter.return_value.afirst = AsyncMock(return_value=mock_metadata)
            mock_ke_objects.filter.return_value.afirst = AsyncMock(return_value=existing_entry)

            result = _run(tool.ainvoke({}))

        assert result["status"] == "success"
        assert result["entries_updated"] > 0
        assert result["entries_created"] == 0
        existing_entry.asave.assert_called()
