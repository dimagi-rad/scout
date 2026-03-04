"""Tool for downloading CommCare app form definitions as knowledge entries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests
from langchain_core.tools import tool

from mcp_server.loaders.commcare_metadata import extract_form_definitions, fetch_app_by_id

if TYPE_CHECKING:
    from apps.users.models import TenantMembership, User
    from apps.workspace.models import TenantWorkspace

logger = logging.getLogger(__name__)


def _localized_str(value: Any) -> str:
    """Extract a plain string from a possibly-multilingual CommCare value."""
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), "") or ""
    return str(value) if value is not None else ""


def _build_knowledge_content(app_name: str, form_definitions: dict[str, dict]) -> str:
    """Build markdown content for a KnowledgeEntry from form definitions."""
    lines = [f"# {app_name}", ""]
    lines.append("Form definitions from CommCare HQ for the `visits.form_json` column.")
    lines.append("")

    for xmlns, fd in form_definitions.items():
        name = _localized_str(fd.get("name", xmlns))
        case_type = _localized_str(fd.get("case_type", ""))
        lines.append(f"## {name}")
        if case_type:
            lines.append(f"- **Case type**: {case_type}")
        lines.append(f"- **xmlns**: {xmlns}")
        lines.append("")

        questions = fd.get("questions", [])
        if questions:
            lines.append("### Fields")
            lines.append("| Label | Path |")
            lines.append("|-------|------|")
            for q in questions:
                label = _localized_str(q.get("label", ""))
                path = q.get("value", "")
                if label:
                    lines.append(f"| {label} | {path} |")
            lines.append("")

    return "\n".join(lines)


def create_commcare_knowledge_tool(
    workspace: TenantWorkspace,
    user: User | None,
    tenant_membership: TenantMembership,
):
    """Factory function to create a tool that downloads CommCare form definitions as knowledge."""

    @tool
    async def download_commcare_knowledge() -> dict[str, Any]:
        """Download CommCare app form definitions and save them as knowledge entries.

        Fetches the learn and deliver app definitions from CommCare HQ for the
        current Connect opportunity, then creates or updates KnowledgeEntry records
        with the form structure. This helps the agent understand the fields inside
        the `visits.form_json` column.

        Requires:
        - A CommCare Connect workspace (provider must be commcare_connect)
        - A linked CommCare OAuth account for API access
        - A completed data sync (so TenantMetadata with opportunity detail exists)

        Returns a summary of created/updated knowledge entries.
        """
        from asgiref.sync import sync_to_async

        from apps.agents.utils.commcare_auth import get_commcare_credential
        from apps.knowledge.models import KnowledgeEntry
        from apps.workspace.models import TenantMetadata

        # 1. Verify provider
        if tenant_membership.provider != "commcare_connect":
            return {
                "status": "error",
                "message": (
                    "This tool only works with CommCare Connect workspaces. "
                    f"Current provider is '{tenant_membership.provider}'."
                ),
            }

        # 2. Look up TenantMetadata for the opportunity detail
        tenant_metadata = await TenantMetadata.objects.filter(
            tenant_membership=tenant_membership,
        ).afirst()

        if tenant_metadata is None:
            return {
                "status": "error",
                "message": (
                    "No tenant metadata found. Please run /refresh-data first "
                    "to sync data and discover opportunity details."
                ),
            }

        opp_detail = (tenant_metadata.metadata or {}).get("opportunity", {})
        if not opp_detail:
            return {
                "status": "error",
                "message": "No opportunity detail found in tenant metadata.",
            }

        # 3. Get CommCare OAuth credential
        commcare_credential = await sync_to_async(get_commcare_credential)(user)
        if commcare_credential is None:
            return {
                "status": "error",
                "message": (
                    "No CommCare account linked. To download form definitions, "
                    "link your CommCare account via OAuth in the account settings."
                ),
            }

        # 4. Build authenticated session
        from mcp_server.loaders.commcare_base import build_auth_header

        session = requests.Session()
        session.headers.update(build_auth_header(commcare_credential))

        # 5. Fetch apps
        apps_fetched: list[dict] = []
        apps_failed: list[str] = []

        for app_key in ("learn_app", "deliver_app"):
            app_ref = opp_detail.get(app_key)
            if not app_ref or not isinstance(app_ref, dict):
                continue
            domain = app_ref.get("cc_domain")
            app_id = app_ref.get("cc_app_id")
            if not domain or not app_id:
                continue

            app_def = await sync_to_async(fetch_app_by_id)(
                domain, app_id, session=session
            )
            if app_def is not None:
                apps_fetched.append(app_def)
            else:
                app_name = app_ref.get("name", app_key)
                apps_failed.append(f"{app_name} ({domain}/{app_id})")

        if not apps_fetched:
            msg = "Could not fetch any CommCare apps."
            if apps_failed:
                msg += f" Failed: {', '.join(apps_failed)}"
            msg += (
                " Your CommCare account may not have access to the apps "
                "configured for this opportunity."
            )
            return {"status": "error", "message": msg}

        # 6. Create/update knowledge entries per app
        entries_created = []
        entries_updated = []
        tags = ["commcare", "form_definition", "auto-generated"]

        for app_def in apps_fetched:
            app_name = _localized_str(app_def.get("name", "Unknown App"))
            title = f"CommCare App: {app_name}"

            # Extract form definitions for just this app
            app_forms = extract_form_definitions([app_def])
            content = _build_knowledge_content(app_name, app_forms)

            # Idempotent: update if title matches
            existing = await KnowledgeEntry.objects.filter(
                workspace=workspace, title=title
            ).afirst()

            if existing:
                existing.content = content
                existing.tags = tags
                await existing.asave(update_fields=["content", "tags", "updated_at"])
                entries_updated.append(title)
                logger.info("Updated knowledge entry: %s", title)
            else:
                await KnowledgeEntry.objects.acreate(
                    workspace=workspace,
                    title=title,
                    content=content,
                    tags=tags,
                    created_by=user,
                )
                entries_created.append(title)
                logger.info("Created knowledge entry: %s", title)

        # 7. Build summary
        summary_parts = []
        if entries_created:
            summary_parts.append(f"Created {len(entries_created)} knowledge entries: "
                                 f"{', '.join(entries_created)}")
        if entries_updated:
            summary_parts.append(f"Updated {len(entries_updated)} knowledge entries: "
                                 f"{', '.join(entries_updated)}")
        if apps_failed:
            summary_parts.append(f"Warning: could not fetch {len(apps_failed)} apps: "
                                 f"{', '.join(apps_failed)}")

        total_forms = sum(
            len(extract_form_definitions([app]))
            for app in apps_fetched
        )

        return {
            "status": "success",
            "message": ". ".join(summary_parts) + ".",
            "apps_processed": len(apps_fetched),
            "forms_found": total_forms,
            "entries_created": len(entries_created),
            "entries_updated": len(entries_updated),
        }

    download_commcare_knowledge.name = "download_commcare_knowledge"
    return download_commcare_knowledge
