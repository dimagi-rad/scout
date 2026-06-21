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

from django.core.management.base import BaseCommand

from apps.transformations.models import CrossOppMeasureLineage
from apps.transformations.services import crossopp_measure_service as svc
from apps.transformations.services.measure_resolver import CanonicalMeasureSpec
from apps.users.models import Tenant, User
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.cube_roles import provision_workspace_ro_role
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

# Canonical PER-VISIT fields (the growth surface). Resolved per opp like measures, but
# they become cube dimensions/visit-measures (age_days, birthweight_band, avg_visit_weight,
# ci95) rather than averaged measures — so per-visit cross-opp analysis (growth curves)
# works from the semantic layer across heterogeneous apps.
VISIT_FIELD_SPECS = [
    CanonicalMeasureSpec(
        "visit_weight", "the infant's body weight in grams measured at this visit", "numeric"
    ),
    CanonicalMeasureSpec(
        "age_days", "the infant's age in days at the time of this visit", "numeric"
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
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"=== build_crossopp_workspace: {name} ({len(opp_ids)} opps) ==="
            )
        )

        user = User.objects.get(email=ADMIN_USER_EMAIL)
        workspace, ws_created = Workspace.objects.get_or_create(
            name=name, defaults={"created_by": user, "is_auto_created": False}
        )
        WorkspaceMembership.objects.get_or_create(
            workspace=workspace, user=user, defaults={"role": WorkspaceRole.MANAGE}
        )
        self.stdout.write(
            f"  [workspace] {workspace.name} id={workspace.id}"
            f" ({'created' if ws_created else 'existing'})"
        )

        # ── Attach tenants to the workspace (sync ORM) ───────────────────────────
        for ext in opp_ids:
            try:
                tenant = Tenant.objects.get(provider=LABS_PROVIDER, external_id=ext)
            except Tenant.DoesNotExist:
                self.stderr.write(
                    self.style.ERROR(
                        f"  opp {ext}: tenant not found — run seed_connect_labs first"
                    )
                )
                continue
            schema = TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).first()
            if schema is None:
                self.stderr.write(self.style.ERROR(f"  opp {ext}: no active schema"))
                continue
            WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)
            self.stdout.write(f"  [opp {ext}] attached schema={schema.schema_name}")

        # ── Collect opps + candidates from the service ───────────────────────────
        opps, candidates_by_opp = svc.workspace_opps(workspace)
        for opp in opps:
            self.stdout.write(
                f"  [opp {opp.external_id}] candidates={len(candidates_by_opp[opp.external_id])}"
            )

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

        # Least-privilege role: USAGE + SELECT on ONLY this workspace's tenant schemas, so a
        # query for this workspace can never reach another's data (reviewer concern #302).
        # Cube connects as this role per request via driverFactory (deployment integration).
        ro_role = provision_workspace_ro_role(ws_hash, [o.schema_name for o in opps])
        self.stdout.write(f"  [ro-role] {ro_role} (USAGE+SELECT on {len(opps)} schemas only)")

        # ── Resolve + commit measures via the shared service ─────────────────────
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n  Resolving {len(STARTER_MEASURES)} measures x {len(opps)} opps ..."
            )
        )
        for m in STARTER_MEASURES:
            resolutions = asyncio.run(svc.resolve_across_opps_from_candidates(m, candidates_by_opp))
            svc.add_measure(workspace, m, resolutions, opps)
        self.stdout.write(
            self.style.SUCCESS(f"  committed {len(STARTER_MEASURES)} measures via service")
        )

        # ── Resolve + persist the per-visit canonical fields (growth surface) ─────
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n  Resolving {len(VISIT_FIELD_SPECS)} per-visit fields x {len(opps)} opps ..."
            )
        )
        for f in VISIT_FIELD_SPECS:
            resolutions = asyncio.run(svc.resolve_across_opps_from_candidates(f, candidates_by_opp))
            svc.add_visit_field(workspace, f.name, resolutions, opps)
            cov = " ".join(
                f"{opp.external_id}:{(resolutions.get(opp.external_id).column or '-') if resolutions.get(opp.external_id) else '-'}"
                for opp in opps
            )
            self.stdout.write(f"    {f.name:12} -> {cov}")
        self.stdout.write(
            self.style.SUCCESS(f"  committed {len(VISIT_FIELD_SPECS)} per-visit fields (growth surface)")
        )

        # ── Coverage report (read back from persisted lineage) ───────────────────
        lineage_rows = CrossOppMeasureLineage.objects.filter(workspace=workspace)
        lineage_map: dict[str, dict[str, str]] = {}
        for row in lineage_rows:
            lineage_map.setdefault(row.opportunity_id, {})[row.measure] = row.status

        model_path = f"cube/model/{ws_hash}/canonical.yml"
        self.stdout.write(self.style.SUCCESS("\n=== DONE ==="))
        self.stdout.write(f"  workspace_id : {workspace.id}")
        self.stdout.write(f"  schema_name  : {ws_hash}")
        self.stdout.write(f"  model        : {model_path}")
        self.stdout.write("  coverage (measure x opp):")
        for m in STARTER_MEASURES:
            cells = []
            for opp in opps:
                status = lineage_map.get(opp.external_id, {}).get(m.name, "absent")
                mark = {"resolved": "Y", "low_confidence": "?", "absent": "-"}.get(status, "-")
                cells.append(f"{opp.external_id}:{mark}")
            self.stdout.write(f"    {m.name:28} {' '.join(cells)}")
