"""Management command: seed complete, repeatable demo workspaces for semantic-layer testing.

Creates (idempotently) all platform records and managed-DB artifacts needed to exercise
the Cube semantic layer end-to-end without an LLM or external API.

Seeds TWO workspaces:

Workspace A (tenant 10001):
  Tenant:      provider=commcare_connect, external_id="10001"
  Schema:      t_10001
  stg_visits:  50 deterministic rows
  Expected:    count=50, approval_rate=0.60, muac_confirmation_rate=0.70

Workspace B (tenant 10002) — used for tenant-isolation testing:
  Tenant:      provider=commcare_connect, external_id="10002"
  Schema:      t_10002
  stg_visits:  50 deterministic rows with different distribution
  Expected:    count=50, approval_rate=0.40, muac_confirmation_rate=0.50

Run:
    uv run python manage.py seed_demo
    uv run python manage.py seed_demo --verify   # also queries Cube end-to-end for both
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import psycopg.sql
import yaml
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.transformations.services.cube_model_schema import (
    Cube,
    CubeModel,
    Dimension,
    Measure,
)
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    TenantMetadata,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.schema_manager import SchemaManager

logger = logging.getLogger(__name__)

# ── Workspace A constants ──────────────────────────────────────────────────────

DEMO_TENANT_PROVIDER = "commcare_connect"
DEMO_TENANT_EXTERNAL_ID = "10001"
DEMO_TENANT_CANONICAL_NAME = "Demo Connect Opportunity"
DEMO_WORKSPACE_NAME = "Demo Workspace"
DEMO_USER_EMAIL = "admin@example.com"

# Schema name derived from external_id "10001" via SchemaManager._sanitize_schema_name:
#   "10001" → lowercase, digit-leading → prepend "t_" → "t_10001"
DEMO_SCHEMA_NAME = "t_10001"

# Deterministic seed values:
#   50 rows: 30 approved, 12 pending, 8 rejected → approval_rate = 0.60
#   35 muac_confirmed='yes', 15 'no' → muac_confirmation_rate = 0.70
EXPECTED_COUNT = 50
EXPECTED_APPROVAL_RATE = 0.60
EXPECTED_MUAC_CONFIRMATION_RATE = 0.70

# ── Workspace B constants (tenant-isolation testing) ───────────────────────────

DEMO_B_TENANT_EXTERNAL_ID = "10002"
DEMO_B_TENANT_CANONICAL_NAME = "Demo Connect Opportunity B"
DEMO_B_WORKSPACE_NAME = "Demo Workspace B"
DEMO_B_SCHEMA_NAME = "t_10002"

# Deterministic seed values for workspace B (intentionally different from A):
#   50 rows: 20 approved, 20 pending, 10 rejected → approval_rate = 0.40
#   25 muac_confirmed='yes', 25 'no' → muac_confirmation_rate = 0.50
EXPECTED_B_COUNT = 50
EXPECTED_B_APPROVAL_RATE = 0.40
EXPECTED_B_MUAC_CONFIRMATION_RATE = 0.50

# Form definitions blob (mimics _extract_form_definitions shape).
# Keyed by deliver_unit slug; questions describe muac and muac_confirmed.
DEMO_FORM_DEFINITIONS = {
    "muac_visit": {
        "name": "MUAC Visit Form",
        "deliver_unit": "muac_visit",
        "questions": [
            {
                "label": "MUAC (cm)",
                "value": "/data/muac",
                "type": "Decimal",
                "repeat": False,
                "options": [],
            },
            {
                "label": "MUAC Confirmed",
                "value": "/data/muac_confirmed",
                "type": "Select",
                "repeat": False,
                "options": [
                    {"label": "Yes", "value": "yes"},
                    {"label": "No", "value": "no"},
                ],
            },
        ],
    }
}

# Cube model YAML for the visits cube.  Validated against CubeModel before writing.
CUBE_MODEL_PATH_TEMPLATE = "cube/model/{schema_name}/visits.yml"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _build_visits_cube_model() -> str:
    """Build and validate the Cube visits model YAML via the Pydantic schema."""
    cube = Cube(
        name="visits",
        sql_table="{COMPILE_CONTEXT.security_context.schema_name}.stg_visits",
        dimensions=[
            Dimension(name="visit_id", sql="visit_id", type="string", primary_key=True),
            Dimension(name="status", sql="status", type="string", title="Visit Status"),
            Dimension(name="username", sql="username", type="string", title="FLW Username"),
            Dimension(name="flagged", sql="flagged::text", type="string", title="Flagged"),
            Dimension(name="visit_date", sql="visit_date", type="time", title="Visit Date"),
        ],
        measures=[
            Measure(name="count", type="count", title="Total Visits"),
            Measure(
                name="approval_rate",
                type="number",
                sql="AVG(CASE WHEN status = 'approved' THEN 1.0 ELSE 0 END)",
                title="Approval Rate",
                description="Fraction of visits in approved status",
            ),
            Measure(
                name="muac_confirmation_rate",
                type="number",
                sql="AVG(CASE WHEN muac_confirmed = 'yes' THEN 1.0 ELSE 0 END)",
                title="MUAC Confirmation Rate",
                description="Fraction of visits where MUAC was confirmed",
            ),
        ],
    )
    model = CubeModel(cubes=[cube])
    # Build a YAML document from the validated Pydantic model.
    # The Jinja interpolation in sql_table must be literal — Cube's template engine
    # processes it at compile-time, so we emit it as a plain YAML string.
    data = {
        "cubes": [
            {
                "name": model.cubes[0].name,
                "sql_table": model.cubes[0].sql_table,
                "dimensions": [
                    {k: v for k, v in d.model_dump(exclude_none=True).items()}
                    for d in model.cubes[0].dimensions
                ],
                "measures": [
                    {k: v for k, v in m.model_dump(exclude_none=True).items()}
                    for m in model.cubes[0].measures
                ],
            }
        ]
    }
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _generate_seed_rows(
    *,
    opportunity_id: int = 10001,
    n_approved: int = 30,
    n_pending: int = 12,
    n_rejected: int = 8,
    n_muac_yes: int = 35,
    prefix: str = "visit",
) -> list[dict]:
    """Generate 50 deterministic stg_visits rows.

    Args:
        opportunity_id: Integer opportunity ID written to each row.
        n_approved: Number of rows with status='approved'.
        n_pending: Number of rows with status='pending'.
        n_rejected: Number of rows with status='rejected'.
        n_muac_yes: Number of rows with muac_confirmed='yes' (rest are 'no').
        prefix: Prefix for visit_id strings (e.g. 'visit' → 'visit_0001').
    """
    total = n_approved + n_pending + n_rejected
    n_muac_no = total - n_muac_yes
    statuses = ["approved"] * n_approved + ["pending"] * n_pending + ["rejected"] * n_rejected
    muac_confirmeds = ["yes"] * n_muac_yes + ["no"] * n_muac_no
    muac_values = [round(9.0 + (i % 11) * 0.5, 1) for i in range(total)]
    usernames = ["flw_alice", "flw_bob", "flw_carol", "flw_dave", "flw_eve"]
    base_date = datetime(2024, 1, 1, 8, 0, 0, tzinfo=UTC)

    rows = []
    for i in range(total):
        rows.append(
            {
                "visit_id": f"{prefix}_{i + 1:04d}",
                "opportunity_id": opportunity_id,
                "user_id": f"user_{(i % 5) + 1:04d}",
                "entity_id": f"entity_{i + 1:04d}",
                "status": statuses[i],
                "deliver_unit_id": f"du_{(i % 3) + 1:04d}",
                "username": usernames[i % 5],
                "flagged": i % 7 == 0,  # ~14% flagged
                "visit_date": base_date + timedelta(days=i),
                "muac": muac_values[i],
                "muac_confirmed": muac_confirmeds[i],
            }
        )
    return rows


def _provision_schema_and_data(schema_name: str, rows: list[dict]) -> None:
    """Create the physical stg_visits table and insert seed rows into the managed DB.

    Idempotent: DROP TABLE IF EXISTS before CREATE TABLE so re-runs yield a
    clean state.  The schema itself is created by SchemaManager.provision().
    """
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise RuntimeError("MANAGED_DATABASE_URL is not configured")

    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        schema_id = psycopg.sql.Identifier(schema_name)
        table_id = psycopg.sql.Identifier(schema_name, "stg_visits")

        # Drop and recreate for a clean re-run
        cur.execute(
            psycopg.sql.SQL("DROP TABLE IF EXISTS {}.stg_visits CASCADE").format(schema_id)
        )
        cur.execute(
            psycopg.sql.SQL(
                """
                CREATE TABLE {table} (
                    visit_id        TEXT PRIMARY KEY,
                    opportunity_id  INTEGER,
                    user_id         TEXT,
                    entity_id       TEXT,
                    status          TEXT,
                    deliver_unit_id TEXT,
                    username        TEXT,
                    flagged         BOOLEAN,
                    visit_date      TIMESTAMPTZ,
                    muac            NUMERIC(5,2),
                    muac_confirmed  TEXT
                )
                """
            ).format(table=table_id)
        )

        # Grant SELECT to the read-only role (may already be granted by default
        # privileges, but explicit grant is safe and idempotent).
        ro_role = psycopg.sql.Identifier(f"{schema_name}_ro")
        cur.execute(
            psycopg.sql.SQL("GRANT SELECT ON {table} TO {role}").format(
                table=table_id, role=ro_role
            )
        )

        # Bulk insert via psycopg executemany
        insert_sql = psycopg.sql.SQL(
            """
            INSERT INTO {table}
                (visit_id, opportunity_id, user_id, entity_id, status,
                 deliver_unit_id, username, flagged, visit_date, muac, muac_confirmed)
            VALUES
                (%(visit_id)s, %(opportunity_id)s, %(user_id)s, %(entity_id)s,
                 %(status)s, %(deliver_unit_id)s, %(username)s, %(flagged)s,
                 %(visit_date)s, %(muac)s, %(muac_confirmed)s)
            """
        ).format(table=table_id)
        cur.executemany(insert_sql, rows)


# ── Async verification ─────────────────────────────────────────────────────────


async def _verify_semantic_query(workspace_id: str) -> dict:
    """Run a Semantic SQL query via Cube's SQL API and return the result dict."""
    from mcp_server.services.semantic import semantic_query

    sql = (
        "SELECT "
        "MEASURE(visits.count), "
        "MEASURE(visits.approval_rate), "
        "MEASURE(visits.muac_confirmation_rate) "
        "FROM visits"
    )
    # Cube may need a moment to compile the new model directory on first access.
    last_exc: Exception | None = None
    for attempt in range(6):
        try:
            result = await semantic_query(sql, workspace_id=workspace_id)
            return result
        except Exception as exc:
            last_exc = exc
            if attempt < 5:
                await asyncio.sleep(3)
    raise RuntimeError(f"Semantic query failed after retries: {last_exc}") from last_exc


# ── Shared seeding logic ───────────────────────────────────────────────────────


def _seed_workspace(
    stdout,
    style,
    user: User,
    *,
    tenant_external_id: str,
    tenant_canonical_name: str,
    workspace_name: str,
    expected_schema_name: str,
    seed_rows: list[dict],
    label: str = "A",
) -> tuple[Workspace, str]:
    """Seed a single workspace idempotently.

    Returns ``(workspace, schema_name)`` for use in verification or tests.
    """
    tag = f"[{label}]"

    # ── Tenant ────────────────────────────────────────────────────────────────
    tenant, tenant_created = Tenant.objects.get_or_create(
        provider=DEMO_TENANT_PROVIDER,
        external_id=tenant_external_id,
        defaults={"canonical_name": tenant_canonical_name},
    )
    stdout.write(
        f"  {tag}[tenant] {tenant} ({'created' if tenant_created else 'existing'})"
    )

    # ── TenantMembership ──────────────────────────────────────────────────────
    membership, mem_created = TenantMembership.objects.get_or_create(
        user=user,
        tenant=tenant,
    )
    stdout.write(
        f"  {tag}[membership] user={user.email} ↔ tenant={tenant.external_id} "
        f"({'created' if mem_created else 'existing'})"
    )

    # ── Provision schema ──────────────────────────────────────────────────────
    manager = SchemaManager()
    tenant_schema = manager.provision(tenant)
    stdout.write(
        f"  {tag}[schema] '{tenant_schema.schema_name}' state={tenant_schema.state}"
    )
    if tenant_schema.schema_name != expected_schema_name:
        stdout.write(
            style.WARNING(
                f"  {tag} WARNING: expected schema '{expected_schema_name}', "
                f"got '{tenant_schema.schema_name}'"
            )
        )

    # ── Workspace ──────────────────────────────────────────────────────────────
    workspace, ws_created = Workspace.objects.get_or_create(
        name=workspace_name,
        defaults={"created_by": user, "is_auto_created": False},
    )
    stdout.write(
        f"  {tag}[workspace] '{workspace.name}' id={workspace.id} "
        f"({'created' if ws_created else 'existing'})"
    )

    # Ensure workspace↔tenant link
    WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)

    # Ensure workspace membership
    WorkspaceMembership.objects.get_or_create(
        workspace=workspace,
        user=user,
        defaults={"role": WorkspaceRole.MANAGE},
    )

    # ── TenantMetadata ────────────────────────────────────────────────────────
    meta, meta_created = TenantMetadata.objects.get_or_create(
        tenant_membership=membership,
        defaults={
            "metadata": {"form_definitions": DEMO_FORM_DEFINITIONS},
            "discovered_at": timezone.now(),
        },
    )
    if not meta_created:
        meta.metadata = {"form_definitions": DEMO_FORM_DEFINITIONS}
        meta.discovered_at = timezone.now()
        meta.save(update_fields=["metadata", "discovered_at", "updated_at"])
    stdout.write(
        f"  {tag}[metadata] {'created' if meta_created else 'updated'} "
        f"for membership {membership.id}"
    )

    # ── stg_visits table ──────────────────────────────────────────────────────
    stdout.write(f"  {tag}[data] Provisioning stg_visits in '{tenant_schema.schema_name}' …")
    _provision_schema_and_data(tenant_schema.schema_name, seed_rows)
    stdout.write(
        f"  {tag}[data] Inserted {len(seed_rows)} deterministic rows into "
        f"{tenant_schema.schema_name}.stg_visits"
    )

    # ── Cube model ────────────────────────────────────────────────────────────
    model_yaml = _build_visits_cube_model()
    repo_root = Path(settings.BASE_DIR)
    model_dir = repo_root / "cube" / "model" / tenant_schema.schema_name
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / "visits.yml"
    model_path.write_text(model_yaml, encoding="utf-8")
    stdout.write(f"  {tag}[model] Cube model written: {model_path}")

    return workspace, tenant_schema.schema_name


# ── Command ────────────────────────────────────────────────────────────────────


class Command(BaseCommand):
    help = (
        "Seed two demo workspaces for repeatable semantic-layer testing. "
        "Workspace A (tenant 10001) and Workspace B (tenant 10002). "
        "Idempotent — safe to run multiple times."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--verify",
            action="store_true",
            default=False,
            help="After seeding, run end-to-end Cube semantic queries and confirm results.",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=== seed_demo: starting ===\n"))

        # ── Resolve the admin user ─────────────────────────────────────────────
        try:
            user = User.objects.get(email=DEMO_USER_EMAIL)
            self.stdout.write(f"  [user] {user.email} (id={user.pk})")
        except User.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(
                    f"User '{DEMO_USER_EMAIL}' not found. "
                    "Create a superuser first: manage.py createsuperuser"
                )
            )
            raise SystemExit(1) from None

        # ── Seed Workspace A (tenant 10001) ────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n--- Workspace A (tenant 10001) ---"))
        rows_a = _generate_seed_rows(
            opportunity_id=10001,
            n_approved=30,
            n_pending=12,
            n_rejected=8,
            n_muac_yes=35,
            prefix="visit",
        )
        workspace_a, schema_a = _seed_workspace(
            self.stdout,
            self.style,
            user,
            tenant_external_id=DEMO_TENANT_EXTERNAL_ID,
            tenant_canonical_name=DEMO_TENANT_CANONICAL_NAME,
            workspace_name=DEMO_WORKSPACE_NAME,
            expected_schema_name=DEMO_SCHEMA_NAME,
            seed_rows=rows_a,
            label="A",
        )

        # ── Seed Workspace B (tenant 10002) ────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n--- Workspace B (tenant 10002) ---"))
        rows_b = _generate_seed_rows(
            opportunity_id=10002,
            n_approved=20,
            n_pending=20,
            n_rejected=10,
            n_muac_yes=25,
            prefix="visitb",
        )
        workspace_b, schema_b = _seed_workspace(
            self.stdout,
            self.style,
            user,
            tenant_external_id=DEMO_B_TENANT_EXTERNAL_ID,
            tenant_canonical_name=DEMO_B_TENANT_CANONICAL_NAME,
            workspace_name=DEMO_B_WORKSPACE_NAME,
            expected_schema_name=DEMO_B_SCHEMA_NAME,
            seed_rows=rows_b,
            label="B",
        )

        # ── Summary ────────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS("\n=== seed_demo: DONE ==="))
        self.stdout.write("\nWorkspace A:")
        self.stdout.write(f"  workspace_id                    : {workspace_a.id}")
        self.stdout.write(f"  schema_name                     : {schema_a}")
        self.stdout.write(f"  expected count                  : {EXPECTED_COUNT}")
        self.stdout.write(f"  expected approval_rate          : {EXPECTED_APPROVAL_RATE}")
        self.stdout.write(
            f"  expected muac_confirmation_rate : {EXPECTED_MUAC_CONFIRMATION_RATE}"
        )
        self.stdout.write("\nWorkspace B:")
        self.stdout.write(f"  workspace_id                    : {workspace_b.id}")
        self.stdout.write(f"  schema_name                     : {schema_b}")
        self.stdout.write(f"  expected count                  : {EXPECTED_B_COUNT}")
        self.stdout.write(f"  expected approval_rate          : {EXPECTED_B_APPROVAL_RATE}")
        self.stdout.write(
            f"  expected muac_confirmation_rate : {EXPECTED_B_MUAC_CONFIRMATION_RATE}"
        )

        # ── Optional end-to-end Cube verification ──────────────────────────────
        if options["verify"]:
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    "\n=== seed_demo: verifying end-to-end Cube queries ===\n"
                )
            )
            self.stdout.write(
                "  Cube may need up to ~18 s to compile new model directories …"
            )

            for label, ws, expected_count, expected_approval, expected_muac in [
                (
                    "A",
                    workspace_a,
                    EXPECTED_COUNT,
                    EXPECTED_APPROVAL_RATE,
                    EXPECTED_MUAC_CONFIRMATION_RATE,
                ),
                (
                    "B",
                    workspace_b,
                    EXPECTED_B_COUNT,
                    EXPECTED_B_APPROVAL_RATE,
                    EXPECTED_B_MUAC_CONFIRMATION_RATE,
                ),
            ]:
                self.stdout.write(f"\n  Verifying Workspace {label} (id={ws.id}) …")
                try:
                    result = asyncio.run(_verify_semantic_query(str(ws.id)))
                except Exception as exc:
                    self.stderr.write(
                        self.style.ERROR(f"  [{label}] Verification FAILED: {exc}")
                    )
                    raise SystemExit(1) from exc

                columns = result.get("columns", [])
                rows_out = result.get("rows", [])
                row_count = result.get("row_count", 0)

                self.stdout.write(f"  [{label}] columns: {columns}")
                self.stdout.write(f"  [{label}] rows:    {rows_out}")
                self.stdout.write(f"  [{label}] row_count: {row_count}")

                if row_count == 0 or not rows_out:
                    self.stderr.write(
                        self.style.ERROR(
                            f"  [{label}] Verification FAILED: no rows returned"
                        )
                    )
                    raise SystemExit(1)

                col_map = {col: rows_out[0][i] for i, col in enumerate(columns)}
                count_val = None
                approval_val = None
                muac_val = None
                for col, val in col_map.items():
                    if "count" in col.lower() and "muac" not in col.lower():
                        count_val = val
                    elif "approval_rate" in col.lower():
                        approval_val = val
                    elif "muac_confirmation_rate" in col.lower():
                        muac_val = val

                def _close_enough(a, b, tol=0.001):
                    if a is None or b is None:
                        return False
                    return abs(float(a) - float(b)) < tol

                ok = True
                if count_val != expected_count:
                    self.stderr.write(
                        self.style.ERROR(
                            f"  [{label}] count mismatch: got {count_val}, "
                            f"expected {expected_count}"
                        )
                    )
                    ok = False
                else:
                    self.stdout.write(
                        self.style.SUCCESS(f"  [{label}] count OK: {count_val}")
                    )

                if not _close_enough(approval_val, expected_approval):
                    self.stderr.write(
                        self.style.ERROR(
                            f"  [{label}] approval_rate mismatch: got {approval_val}, "
                            f"expected {expected_approval}"
                        )
                    )
                    ok = False
                else:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [{label}] approval_rate OK: {approval_val}"
                        )
                    )

                if not _close_enough(muac_val, expected_muac):
                    self.stderr.write(
                        self.style.ERROR(
                            f"  [{label}] muac_confirmation_rate mismatch: got {muac_val}, "
                            f"expected {expected_muac}"
                        )
                    )
                    ok = False
                else:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [{label}] muac_confirmation_rate OK: {muac_val}"
                        )
                    )

                if ok:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"\n  [{label}] END-TO-END VERIFICATION PASSED — "
                            f"count={count_val}, approval_rate={approval_val}, "
                            f"muac_confirmation_rate={muac_val}"
                        )
                    )
                else:
                    self.stderr.write(
                        self.style.ERROR(
                            f"\n  [{label}] END-TO-END VERIFICATION FAILED — see above"
                        )
                    )
                    raise SystemExit(1)
