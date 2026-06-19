"""Management command: materialize a Connect-Labs opportunity into Scout.

Idempotently provisions a Scout workspace backed by the real Connect-Labs API
(https://labs.connect.dimagi.com/api) for a given opportunity ID.

Usage:
    uv run python manage.py seed_connect_labs --opp 10007

Requires:
  - 1Password CLI (`op`) signed in — fetches the PAT automatically.
  - CONNECT_LABS_API_URL set in the environment (or .env).
  - MANAGED_DATABASE_URL and DB_CREDENTIAL_KEY configured.
"""

from __future__ import annotations

import logging
import subprocess
from asgiref.sync import async_to_sync

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership, User
from apps.users.services.credential_resolver import aresolve_credential
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.schema_manager import SchemaManager
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import run_pipeline

logger = logging.getLogger(__name__)

LABS_PROVIDER = "commcare_connect_labs"
ADMIN_USER_EMAIL = "admin@example.com"
OP_REF = "op://AI-Agents/ip74jhirsi55ly6ic3ivj3ntzy/mcp_token"


def _fetch_pat() -> str:
    """Retrieve the MCP PAT from 1Password via the `op` CLI."""
    result = subprocess.run(
        ["op", "read", OP_REF],
        capture_output=True,
        text=True,
        check=True,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("op read returned an empty token — is 1Password signed in?")
    return token


class Command(BaseCommand):
    help = (
        "Materialize a Connect-Labs opportunity into Scout using the existing Connect loaders. "
        "Idempotent — safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--opp",
            type=int,
            default=10007,
            help="Connect-Labs opportunity ID to materialize (default: 10007).",
        )

    def handle(self, *args, **options):  # noqa: C901
        opp_id = options["opp"]
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"=== seed_connect_labs: opp {opp_id} ==="
            )
        )

        # ── Validate env ─────────────────────────────────────────────────────
        labs_url = getattr(settings, "CONNECT_LABS_API_URL", "") or ""
        if not labs_url:
            self.stderr.write(
                self.style.ERROR(
                    "CONNECT_LABS_API_URL is not set. "
                    "Add it to .env: CONNECT_LABS_API_URL=https://labs.connect.dimagi.com/api"
                )
            )
            raise SystemExit(1)
        self.stdout.write(f"  [env] CONNECT_LABS_API_URL = {labs_url}")

        # ── Fetch PAT from 1Password ─────────────────────────────────────────
        self.stdout.write("  [pat] Fetching PAT from 1Password …")
        try:
            pat = _fetch_pat()
        except subprocess.CalledProcessError as exc:
            self.stderr.write(
                self.style.ERROR(
                    f"Failed to read PAT from 1Password: {exc.stderr.strip()}"
                )
            )
            raise SystemExit(1) from exc
        self.stdout.write("  [pat] OK (token length={})".format(len(pat)))

        # ── Resolve admin user ────────────────────────────────────────────────
        try:
            user = User.objects.get(email=ADMIN_USER_EMAIL)
            self.stdout.write(f"  [user] {user.email} (id={user.pk})")
        except User.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(
                    f"User '{ADMIN_USER_EMAIL}' not found. "
                    "Run: uv run python manage.py createsuperuser"
                )
            )
            raise SystemExit(1) from None

        # ── Tenant ────────────────────────────────────────────────────────────
        tenant, tenant_created = Tenant.objects.get_or_create(
            provider=LABS_PROVIDER,
            external_id=str(opp_id),
            defaults={"canonical_name": f"Labs Opp {opp_id}"},
        )
        self.stdout.write(
            f"  [tenant] {tenant} ({'created' if tenant_created else 'existing'})"
        )

        # ── TenantConnection (API key) ────────────────────────────────────────
        # One PAT-backed connection per user+provider. Upsert: always refresh
        # the encrypted credential so a rotated PAT takes effect on re-run.
        encrypted_pat = encrypt_credential(pat)
        connection, conn_created = TenantConnection.objects.get_or_create(
            user=user,
            provider=LABS_PROVIDER,
            credential_type=TenantConnection.API_KEY,
            defaults={"encrypted_credential": encrypted_pat},
        )
        if not conn_created:
            # Refresh the stored PAT so a re-run with a rotated token works.
            connection.encrypted_credential = encrypted_pat
            connection.save(update_fields=["encrypted_credential", "updated_at"])
        self.stdout.write(
            f"  [connection] id={connection.id} "
            f"({'created' if conn_created else 'updated encrypted_credential'})"
        )

        # ── TenantMembership ──────────────────────────────────────────────────
        membership, mem_created = TenantMembership.objects.get_or_create(
            user=user,
            tenant=tenant,
            defaults={"connection": connection},
        )
        if not mem_created and membership.connection_id != connection.id:
            membership.connection = connection
            membership.save(update_fields=["connection"])
        self.stdout.write(
            f"  [membership] id={membership.id} "
            f"({'created' if mem_created else 'existing'}), connection wired"
        )

        # ── Provision schema ──────────────────────────────────────────────────
        manager = SchemaManager()
        tenant_schema = manager.provision(tenant)
        schema_name = tenant_schema.schema_name
        self.stdout.write(
            f"  [schema] '{schema_name}' state={tenant_schema.state}"
        )

        # ── Workspace ──────────────────────────────────────────────────────────
        workspace_name = f"Labs Opp {opp_id}"
        workspace, ws_created = Workspace.objects.get_or_create(
            name=workspace_name,
            defaults={"created_by": user, "is_auto_created": False},
        )
        self.stdout.write(
            f"  [workspace] '{workspace.name}' id={workspace.id} "
            f"({'created' if ws_created else 'existing'})"
        )
        WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)
        WorkspaceMembership.objects.get_or_create(
            workspace=workspace,
            user=user,
            defaults={"role": WorkspaceRole.MANAGE},
        )

        # ── Resolve credential via the standard resolver ──────────────────────
        # select_related is required by aresolve_credential.
        membership_qs = TenantMembership.objects.select_related(
            "connection", "user"
        ).get(id=membership.id)
        credential = async_to_sync(aresolve_credential)(membership_qs)
        if credential is None:
            self.stderr.write(
                self.style.ERROR(
                    "aresolve_credential returned None — credential not stored correctly"
                )
            )
            raise SystemExit(1)
        self.stdout.write(f"  [credential] type={credential['type']} resolved OK")

        # ── Pipeline config ───────────────────────────────────────────────────
        registry = get_registry()
        pipeline = registry.get("connect_labs_sync")
        if pipeline is None:
            self.stderr.write(
                self.style.ERROR("Pipeline 'connect_labs_sync' not found in registry")
            )
            raise SystemExit(1)
        self.stdout.write(
            f"  [pipeline] '{pipeline.name}' provider={pipeline.provider} "
            f"sources={[s.name for s in pipeline.sources]}"
        )

        # ── Run pipeline ──────────────────────────────────────────────────────
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n  Materializing opp {opp_id} → schema '{schema_name}' …\n"
                "  (This pages through the labs API — may take 30–120 s)"
            )
        )

        def _progress(p: dict) -> None:
            src = p.get("source") or ""
            msg = p.get("message") or ""
            rows = p.get("rows_loaded", 0)
            rows_total = p.get("rows_total")
            denom = f"/{rows_total}" if rows_total else ""
            self.stdout.write(
                f"    [{p.get('step')}/{p.get('total_steps')}] {src} {msg} "
                f"rows={rows}{denom}"
            )

        result = run_pipeline(
            membership_qs,
            credential,
            pipeline,
            progress_updater=_progress,
        )

        # ── Report ────────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS("\n=== seed_connect_labs: DONE ==="))
        self.stdout.write(f"  workspace_id  : {workspace.id}")
        self.stdout.write(f"  schema_name   : {schema_name}")
        self.stdout.write(f"  run_id        : {result.get('run_id')}")
        self.stdout.write(f"  status        : {result.get('status')}")
        self.stdout.write(f"  rows_loaded   : {result.get('rows_loaded')}")
        self.stdout.write("  per-source:")
        for src_name, src_info in (result.get("sources") or {}).items():
            self.stdout.write(
                f"    {src_name}: state={src_info.get('state')} "
                f"rows={src_info.get('rows')}"
            )
        if result.get("transform_error"):
            self.stdout.write(
                self.style.WARNING(f"  transform_error: {result['transform_error']}")
            )

        # ── Verify raw_visits count ───────────────────────────────────────────
        self.stdout.write("\n  Verifying raw_visits count …")
        self._verify_schema(schema_name)

    def _verify_schema(self, schema_name: str) -> None:
        """Query the tenant schema and print raw_visits count + stg_visits info."""
        import psycopg

        url = settings.MANAGED_DATABASE_URL
        if not url:
            self.stderr.write(self.style.WARNING("  MANAGED_DATABASE_URL not set; skipping verify"))
            return

        with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
            # raw_visits count
            try:
                cur.execute(
                    psycopg.sql.SQL(
                        "SELECT COUNT(*) FROM {}.raw_visits"
                    ).format(psycopg.sql.Identifier(schema_name))
                )
                (count,) = cur.fetchone()
                self.stdout.write(
                    self.style.SUCCESS(f"  raw_visits count = {count}")
                )
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"  raw_visits query failed: {exc}"))

            # sample raw_visits row keys
            try:
                cur.execute(
                    psycopg.sql.SQL(
                        "SELECT * FROM {}.raw_visits LIMIT 1"
                    ).format(psycopg.sql.Identifier(schema_name))
                )
                desc = cur.description
                row = cur.fetchone()
                if desc and row:
                    keys = [d.name for d in desc]
                    self.stdout.write(f"  raw_visits columns ({len(keys)}): {keys}")
                    row_dict = dict(zip(keys, row))
                    # Print a subset of key fields
                    sample = {
                        k: row_dict[k]
                        for k in ("visit_id", "status", "username", "visit_date", "form_json")
                        if k in row_dict
                    }
                    self.stdout.write(f"  sample row keys/values: {sample}")
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"  raw_visits sample query failed: {exc}"))

            # stg_visits existence + columns
            try:
                cur.execute(
                    psycopg.sql.SQL(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = 'stg_visits' "
                        "ORDER BY ordinal_position"
                    ),
                    (schema_name,),
                )
                stg_cols = [r[0] for r in cur.fetchall()]
                if stg_cols:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  stg_visits exists — {len(stg_cols)} columns: {stg_cols}"
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING("  stg_visits not found (transform may not have run)")
                    )
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"  stg_visits query failed: {exc}"))
