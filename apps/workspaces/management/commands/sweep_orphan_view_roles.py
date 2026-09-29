"""Drop PostgreSQL roles left behind by deleted workspace view schemas (#420).

``teardown_view_schema`` drops a view schema's ``_ro``/``_dbt`` roles best-effort
and swallows the failure, and ``backfill_readonly_roles`` only iterates live
``WorkspaceViewSchema`` rows, so a role whose row is gone is reachable by nothing
else. This command enumerates those roles by their generated name and drops them.
"""

import contextlib
import logging
import re
import uuid
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from apps.common.identifiers import dbt_role_name, readonly_role_name
from apps.workspaces.models import TenantSchema, Workspace, WorkspaceViewSchema
from apps.workspaces.services import schema_manager as _schema_manager
from apps.workspaces.services.data_operation import (
    sync_tenant_data_lock,
    sync_workspace_data_lock,
)
from apps.workspaces.services.schema_manager import SchemaManager, _serialize_view_build

logger = logging.getLogger(__name__)

# SchemaManager._view_schema_name mints ws_{first 16 hex of the workspace uuid}; the
# derived role names fit in 63 bytes, so fit_identifier never hashes them.
_VIEW_ROLE_RE = re.compile(r"^ws_(?P<hex>[0-9a-f]{16})_(?P<kind>ro|dbt)$")

_ROLES_SQL = r"""
    SELECT rolname, rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb
                    OR rolreplication OR rolbypassrls
    FROM pg_roles WHERE rolname LIKE 'ws\_%' ORDER BY rolname
"""

# Ownership anywhere in the cluster, or a member other than the managed user
# (who is granted each _dbt role so it can SET ROLE), means something outside
# Scout's lifecycle uses the role.
_IN_USE_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid
        WHERE d.refclassid = 'pg_authid'::regclass AND d.deptype = 'o' AND r.rolname = %(role)s
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members m
        JOIN pg_roles r ON r.oid = m.roleid
        JOIN pg_roles mem ON mem.oid = m.member
        WHERE r.rolname = %(role)s AND mem.rolname <> current_user
    )
"""


class Command(BaseCommand):
    help = (
        "Find _ro/_dbt roles of workspace view schemas whose WorkspaceViewSchema row "
        "is gone and drop them. Reports only unless --apply is given."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        # Report-only is the default so an operator's first run in production is
        # the count #420 asks for, and a forgotten flag can never drop roles.
        mode.add_argument("--dry-run", action="store_true", help="Report only (the default).")
        mode.add_argument("--apply", action="store_true", help="Drop the orphaned roles.")
        parser.add_argument(
            "--role",
            action="append",
            default=[],
            help="Only consider this role (repeatable).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        self._log(f"sweep_orphan_view_roles: {'APPLY' if apply else 'DRY RUN'}")
        mgr = SchemaManager()
        conn = _schema_manager.get_managed_db_connection()
        outcomes = Counter()
        try:
            cursor = conn.cursor()
            for role, privileged in self._candidates(cursor, options["role"]):
                outcome = self._sweep(cursor, mgr, role, privileged, apply=apply)
                outcomes[outcome] += 1
            cursor.close()
        finally:
            conn.close()
        summary = ", ".join(f"{count} {name}" for name, count in sorted(outcomes.items()))
        self._log(f"Done: {summary or 'no candidate roles'}.")
        if outcomes["failed"]:
            raise CommandError(f"{outcomes['failed']} role(s) could not be dropped")

    def _candidates(self, cursor, only_roles):
        cursor.execute(_ROLES_SQL)
        rows = cursor.fetchall()
        if only_roles:
            wanted = set(only_roles)
            rows = [row for row in rows if row[0] in wanted]
            for missing in sorted(wanted - {row[0] for row in rows}):
                self._log(f"  {missing}: not found")

        # _ro first: its default-ACL entries may be owned by the sibling _dbt role.
        def order(row):
            match = _VIEW_ROLE_RE.match(row[0])
            return (row[0] if match is None else match["hex"], row[0].endswith("_dbt"))

        return sorted(rows, key=order)

    def _sweep(self, cursor, mgr, role, privileged, *, apply) -> str:
        match = _VIEW_ROLE_RE.match(role)
        if match is None:
            self._log(f"  {role}: ignored, not a generated view-schema role name")
            return "ignored"
        schema = f"ws_{match['hex']}"
        derive = readonly_role_name if match["kind"] == "ro" else dbt_role_name
        if derive(schema) != role or privileged:
            self._log(f"  {role}: refused, name or attributes differ from what Scout creates")
            return "refused"
        if WorkspaceViewSchema.objects.filter(schema_name=schema).exists():
            self._log(f"  {role}: kept, its view schema is live")
            return "live"
        blocker = self._blocker(cursor, schema, role)
        if blocker:
            self._log(f"  {role}: skipped, {blocker}")
            return "skipped"
        if not apply:
            self._log(f"  {role}: would drop")
            return "would drop"
        try:
            with self._locks(cursor, match["hex"], role):
                # Re-check under the locks: a build may have published between the
                # scan and acquiring W.
                if WorkspaceViewSchema.objects.filter(schema_name=schema).exists():
                    self._log(f"  {role}: skipped, a view schema was published meanwhile")
                    return "live"
                blocker = self._blocker(cursor, schema, role)
                if blocker:
                    self._log(f"  {role}: skipped, {blocker}")
                    return "skipped"
                if match["kind"] == "ro":
                    mgr._drop_readonly_role(cursor, schema)
                else:
                    mgr._drop_dbt_role(cursor, schema)
        except Exception:
            logger.exception("sweep_orphan_view_roles: dropping %s failed", role)
            self.stderr.write(self.style.ERROR(f"  {role}: FAILED (see log)"))
            return "failed"
        self._log(f"  {role}: dropped")
        return "dropped"

    def _blocker(self, cursor, schema, role) -> str | None:
        # A physical schema no row claims can be a publication whose control row is
        # not saved yet (reconcile_view_publication adopts it); never race it.
        cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        if cursor.fetchone():
            return f"physical schema {schema} still exists"
        cursor.execute(_IN_USE_SQL, {"role": role})
        if cursor.fetchone()[0]:
            return "it owns objects or has members; inspect and REASSIGN/DROP OWNED by hand"
        return None

    @contextlib.contextmanager
    def _locks(self, cursor, hex_prefix, role):
        """Take W -> sorted T -> view-build lock, the publication order (#564/#565).

        T covers the tenant schemas the drop will REVOKE on, so it cannot interleave
        with a retirement dropping their relations.
        """
        workspace = Workspace.objects.filter(
            id__gte=uuid.UUID(hex_prefix + "0" * 16), id__lte=uuid.UUID(hex_prefix + "f" * 16)
        ).first()
        cursor.execute(SchemaManager._SCHEMAS_WITH_ROLE_GRANTS_SQL, {"role": role})
        schemas = {row[0] for row in cursor.fetchall()}
        cursor.execute(SchemaManager._SCHEMAS_WITH_ROLE_DEFAULT_ACLS_SQL, (role,))
        schemas |= {row[0] for row in cursor.fetchall()}
        tenant_ids = set(
            TenantSchema.objects.filter(schema_name__in=schemas).values_list("tenant_id", flat=True)
        )
        with contextlib.ExitStack() as stack:
            if workspace is not None:
                stack.enter_context(sync_workspace_data_lock(workspace.id))
                tenant_ids |= set(workspace.tenants.values_list("id", flat=True))
            stack.enter_context(sync_tenant_data_lock(tenant_ids))
            if workspace is not None:
                stack.enter_context(_serialize_view_build(workspace.id))
            yield

    def _log(self, message: str) -> None:
        logger.info(message)
        self.stdout.write(message)
