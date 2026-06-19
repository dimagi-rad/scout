"""Assemble a cross-opp semantic workspace and generate its Cube model.

Creates (idempotently) a multi-tenant Scout workspace over several already-materialized
Connect-Labs opportunities, resolves a starter set of canonical KMC measures against each
opp's app structure, and writes the two-tier Data-Blending Cube model (per-opp cubes + a
blended cube) into ``cube/model/<ws_hash>/``.

Usage:
    uv run python manage.py build_crossopp_workspace \
        --name "KMC Cross-Opp" --opps 10012 10013 10014 ... 10022

Requires: the opps already materialized (``seed_connect_labs``), MANAGED_DATABASE_URL,
ANTHROPIC_API_KEY (the resolver calls the LLM).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.transformations.services.crossopp_cube_builder import OppRef, render_crossopp_model
from apps.transformations.services.measure_resolver import (
    CanonicalMeasureSpec,
    gather_measure_candidates,
    resolve_measure,
)
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager

LABS_PROVIDER = "commcare_connect_labs"
ADMIN_USER_EMAIL = "admin@example.com"
BLENDED_CUBE = "kmc_cross_opp"

# Starter KMC measure catalog (the four the demo asks for). Cube is the catalog; these are
# just the resolver inputs — domain concepts named in plain language, not fields.
STARTER_MEASURES = [
    CanonicalMeasureSpec("birth_weight", "newborn weight in grams recorded at registration", "numeric"),
    CanonicalMeasureSpec("mortality", "whether the child has died / is no longer alive", "rate"),
    CanonicalMeasureSpec("kmc_hours", "hours of skin-to-skin kangaroo mother care provided", "numeric"),
    CanonicalMeasureSpec(
        "danger_sign_referral_rate",
        "a clinical danger sign was detected and the child referred to hospital",
        "rate",
    ),
]


class Command(BaseCommand):
    help = "Assemble a cross-opp workspace over several labs opps and generate its Cube model."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="KMC Cross-Opp")
        parser.add_argument("--opps", nargs="+", type=str, required=True, help="opp external ids")

    def handle(self, *args, **options):
        name = options["name"]
        opp_ids = options["opps"]
        self.stdout.write(self.style.MIGRATE_HEADING(f"=== build_crossopp_workspace: {name} ({len(opp_ids)} opps) ==="))

        user = User.objects.get(email=ADMIN_USER_EMAIL)
        workspace, ws_created = Workspace.objects.get_or_create(
            name=name, defaults={"created_by": user, "is_auto_created": False}
        )
        WorkspaceMembership.objects.get_or_create(
            workspace=workspace, user=user, defaults={"role": WorkspaceRole.MANAGE}
        )
        self.stdout.write(f"  [workspace] {workspace.name} id={workspace.id} ({'created' if ws_created else 'existing'})")

        # ── Collect each opp's schema + measure candidates (sync ORM) ────────────
        opps: list[OppRef] = []
        candidates_by_opp = {}
        for ext in opp_ids:
            try:
                tenant = Tenant.objects.get(provider=LABS_PROVIDER, external_id=ext)
            except Tenant.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"  opp {ext}: tenant not found — run seed_connect_labs first"))
                continue
            schema = TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).first()
            if schema is None:
                self.stderr.write(self.style.ERROR(f"  opp {ext}: no active schema"))
                continue
            WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)
            tm = TenantMembership.objects.filter(tenant=tenant).first()
            form_defs = (getattr(getattr(tm, "metadata", None), "metadata", None) or {}).get("form_definitions", {})
            opps.append(OppRef(ext, schema.schema_name))
            candidates_by_opp[ext] = gather_measure_candidates(form_defs)
            self.stdout.write(f"  [opp {ext}] schema={schema.schema_name} candidates={len(candidates_by_opp[ext])}")

        if not opps:
            self.stderr.write(self.style.ERROR("No usable opps; aborting."))
            return

        # ── Route: a WorkspaceViewSchema row carrying the ws_<hash> the JWT/model dir use.
        # The blended cube reads the tenant schemas directly, so no physical ws_ schema or
        # build_view_schema views are needed for the semantic path — only this routing row.
        ws_hash = SchemaManager()._view_schema_name(workspace.id)
        WorkspaceViewSchema.objects.update_or_create(
            workspace=workspace, defaults={"schema_name": ws_hash, "state": SchemaState.ACTIVE}
        )
        self.stdout.write(f"  [view-schema] {ws_hash} (ACTIVE, routing only)")

        # ── Resolve every measure for every opp (async LLM) ──────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  Resolving {len(STARTER_MEASURES)} measures x {len(opps)} opps ..."))
        resolutions_by_opp = asyncio.run(self._resolve_all(opps, candidates_by_opp))

        # ── Render + write the model ─────────────────────────────────────────────
        model_yaml = render_crossopp_model(BLENDED_CUBE, opps, STARTER_MEASURES, resolutions_by_opp)
        model_path = Path("cube/model") / ws_hash / "canonical.yml"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_text(model_yaml)

        # ── Coverage report ──────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS("\n=== DONE ==="))
        self.stdout.write(f"  workspace_id : {workspace.id}")
        self.stdout.write(f"  schema_name  : {ws_hash}")
        self.stdout.write(f"  model        : {model_path}")
        self.stdout.write("  coverage (measure x opp):")
        for m in STARTER_MEASURES:
            cells = []
            for opp in opps:
                r = resolutions_by_opp.get(opp.external_id, {}).get(m.name)
                mark = {"resolved": "Y", "low_confidence": "?", "absent": "-"}.get(getattr(r, "status", "absent"), "-")
                cells.append(f"{opp.external_id}:{mark}")
            self.stdout.write(f"    {m.name:28} {' '.join(cells)}")

    async def _resolve_all(self, opps, candidates_by_opp):
        out: dict[str, dict] = {}
        for opp in opps:
            cands = candidates_by_opp[opp.external_id]
            rmap = {}
            for m in STARTER_MEASURES:
                rmap[m.name] = await resolve_measure(m, cands)
            out[opp.external_id] = rmap
        return out
