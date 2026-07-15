"""
Schema Manager for the Scout-managed database.

Creates and tears down tenant-scoped PostgreSQL schemas.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import uuid

import psycopg
import psycopg.sql
from django.conf import settings
from django.utils import timezone

from apps.common.identifiers import (
    PG_MAX_IDENTIFIER_BYTES,
    dbt_role_name,
    readonly_role_name,
    refresh_schema_name,
    sanitize_identifier,
    tenant_schema_name,
)
from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema

logger = logging.getLogger(__name__)

# Cap the view-name prefix well below Postgres's 63-byte identifier limit, leaving
# budget for the ``__{table}`` suffix (the full name is hard-failed if it still
# exceeds the limit). Identifier minting lives in apps.common.identifiers (arch #235).
_MAX_VIEW_PREFIX_LEN = 32


def get_managed_db_connection():
    """Get a psycopg connection to the managed database."""
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise RuntimeError("MANAGED_DATABASE_URL is not configured")
    return psycopg.connect(url, autocommit=True)


async def aget_managed_db_connection():
    """Get an async psycopg connection to the managed database."""
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise RuntimeError("MANAGED_DATABASE_URL is not configured")
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


class SchemaManager:
    """Creates and manages tenant schemas in the managed database."""

    def provision(self, tenant) -> TenantSchema:
        """Get or create a schema for the tenant.

        Matches by the ``tenant`` FK, never by schema_name (arch #235): a
        sanitized-name collision (Connect ``123`` / OCS ``123``) would otherwise
        hand one tenant another's live ACTIVE schema.

        Resolution order, all scoped to this tenant:

        1. Current live schema (ACTIVE/MATERIALIZING), most-recently-accessed. A
           blue-green refresh's not-yet-promoted ``_r`` schema is PROVISIONING, so
           it is skipped until promoted (then sorts first).
        2. Else resurrect the most-recent EXPIRED record in place, reusing its name.
        3. Else mint a new collision-safe name via ``tenant_schema_name``.
        """
        from django.db import IntegrityError

        live = (
            TenantSchema.objects.filter(
                tenant=tenant,
                state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
            )
            .order_by("-last_accessed_at")
            .first()
        )
        if live:
            # The physical schema may have been dropped externally while the
            # Django record stayed ACTIVE.
            self._ensure_physical_schema(live.schema_name)
            live.touch()
            return live

        resurrectable = (
            TenantSchema.objects.filter(tenant=tenant, state=SchemaState.EXPIRED)
            .order_by("-last_accessed_at")
            .first()
        )
        if resurrectable:
            schema_name = resurrectable.schema_name
            ts = resurrectable
            created = False
        else:
            schema_name = tenant_schema_name(tenant.provider, tenant.external_id)
            created = True
            try:
                ts = TenantSchema.objects.create(
                    tenant=tenant,
                    schema_name=schema_name,
                    state=SchemaState.PROVISIONING,
                )
            except IntegrityError:
                # Race: another process created this tenant's (deterministically
                # named) record between lookup and create — re-fetch and return it.
                created = False
                ts = TenantSchema.objects.get(schema_name=schema_name)
                if ts.state in (SchemaState.ACTIVE, SchemaState.MATERIALIZING):
                    return ts
                # Not active yet: fall through to CREATE SCHEMA (IF NOT EXISTS is safe).

        try:
            conn = get_managed_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        psycopg.sql.Identifier(schema_name)
                    )
                )
                self._create_readonly_role(cursor, schema_name)
                cursor.close()
            finally:
                conn.close()
        except Exception:
            # Only delete a record WE created, so the next attempt can retry; a
            # resurrected pre-existing record is left in place.
            if created:
                ts.delete()
            raise

        # Reset the inactivity TTL on activation (covers fresh-create and
        # resurrect): otherwise a resurrected schema's stale last_accessed_at lets
        # expire_inactive_schemas drop it right after data is materialized.
        ts.state = SchemaState.ACTIVE
        ts.last_accessed_at = timezone.now()
        ts.save(update_fields=["state", "last_accessed_at"])

        logger.info(
            "Provisioned schema '%s' for tenant '%s'",
            schema_name,
            tenant.external_id,
        )
        return ts

    def _ensure_physical_schema(self, schema_name: str) -> None:
        """Ensure the physical PostgreSQL schema and readonly role exist.

        Idempotent — safe to call on every provision(). Handles the case where
        the physical schema was dropped externally but the Django record remains.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    psycopg.sql.Identifier(schema_name)
                )
            )
            self._create_readonly_role(cursor, schema_name)
            cursor.close()
        finally:
            conn.close()

    def create_physical_schema(self, tenant_schema: TenantSchema) -> None:
        """Create the physical PostgreSQL schema for an existing TenantSchema record.

        Idempotent — uses ``CREATE SCHEMA IF NOT EXISTS``. The caller is
        responsible for updating ``tenant_schema.state`` on success or failure.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    psycopg.sql.Identifier(tenant_schema.schema_name)
                )
            )
            self._create_readonly_role(cursor, tenant_schema.schema_name)
            cursor.close()
        finally:
            conn.close()

    def create_refresh_schema(self, tenant) -> TenantSchema:
        """Create a new TenantSchema record for a background refresh.

        Returns a PROVISIONING record with a unique schema name. The caller
        is responsible for creating the physical schema and dispatching the
        Celery task (refresh_tenant_schema) to run the materialization.
        """
        schema_name = refresh_schema_name(
            tenant.provider, tenant.external_id, token=uuid.uuid4().hex[:8]
        )
        return TenantSchema.objects.create(
            tenant=tenant,
            schema_name=schema_name,
            state=SchemaState.PROVISIONING,
        )

    def teardown(self, tenant_schema: TenantSchema) -> None:
        """Drop a tenant's schema from the managed database.

        Only performs the physical DROP SCHEMA — callers are responsible for
        updating the model state (EXPIRED or FAILED) after this returns.

        Role cleanup is best-effort: once DROP SCHEMA has succeeded the schema
        is gone, so a later failure in ``_drop_readonly_role`` must not surface
        as an exception (callers would otherwise incorrectly flip the record
        back to ACTIVE). A dangling role is logged for operator follow-up.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(tenant_schema.schema_name)
                )
            )
            try:
                self._drop_readonly_role(cursor, tenant_schema.schema_name)
                self._drop_dbt_role(cursor, tenant_schema.schema_name)
            except Exception:
                logger.exception(
                    "teardown: dropping derived roles for schema '%s' failed; "
                    "physical schema was dropped, role may be dangling",
                    tenant_schema.schema_name,
                )
            cursor.close()
        finally:
            conn.close()

    def _drop_dbt_role(self, cursor, schema_name: str) -> None:
        """Drop the low-privilege dbt role for a schema (issue #241).

        The role's only grants are on its own schema, which ``DROP SCHEMA
        CASCADE`` has already removed, so a plain ``DROP ROLE IF EXISTS`` is
        sufficient. Idempotent.
        """
        cursor.execute(
            psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
                psycopg.sql.Identifier(dbt_role_name(schema_name))
            )
        )

    def _view_schema_name(self, workspace_id) -> str:
        """Generate a PostgreSQL schema name for a workspace's view schema."""
        hex_id = str(workspace_id).replace("-", "")[:16]
        return f"ws_{hex_id}"

    def _view_prefix(self, tenant) -> str:
        """Derive the per-tenant ``{prefix}__{table}`` view-name prefix, capped to
        <= 32 chars so distinct long-named tenants don't truncate to the same
        identifier.

        Short sanitized names (<= 32) are used as-is; longer ones become 23
        sanitized chars + ``_`` + an 8-char digest of external_id (deterministic,
        so stable across rebuilds and distinct per tenant).
        """
        sanitized = self._sanitize_schema_name(tenant.canonical_name)
        if len(sanitized) <= _MAX_VIEW_PREFIX_LEN:
            return sanitized
        digest = hashlib.sha256(str(tenant.external_id).encode("utf-8")).hexdigest()[:8]
        # 23 (head) + 1 ("_") + 8 (digest) = 32
        return f"{sanitized[:23]}_{digest}"

    def build_view_schema(self, workspace) -> WorkspaceViewSchema:
        """(Re)build the PostgreSQL view schema for a multi-tenant workspace.

        Fetches all active TenantSchema objects for the workspace's tenants and
        creates one namespaced ``{prefix}__{table}`` view per tenant table in a
        dedicated schema. The build is idempotent: the view schema is dropped and
        recreated from scratch each call, so a rebuild after an underlying table's
        columns changed succeeds rather than failing on view-column mismatches.

        Raises ValueError if any tenant has no active schema, if two tenants
        produce the same view prefix or full view name, or if a composed view
        name would exceed PostgreSQL's 63-byte identifier limit.

        Returns the WorkspaceViewSchema model instance with state=ACTIVE on success.
        """
        # Create/reset the row FIRST so an early validation failure marks it FAILED
        # instead of leaving a resurrected row in PROVISIONING (arch #255 03#1/03#2).
        view_schema_name = self._view_schema_name(workspace.id)
        vs, _ = WorkspaceViewSchema.objects.get_or_create(
            workspace=workspace,
            defaults={"schema_name": view_schema_name, "state": SchemaState.PROVISIONING},
        )
        if vs.schema_name != view_schema_name:
            vs.schema_name = view_schema_name
        vs.state = SchemaState.PROVISIONING
        vs.save(update_fields=["schema_name", "state"])

        try:
            tenants = list(workspace.tenants.all())
            if not tenants:
                raise ValueError(f"Workspace {workspace.id} has no tenants")

            active_schemas = {
                ts.tenant_id: ts
                for ts in TenantSchema.objects.filter(tenant__in=tenants, state=SchemaState.ACTIVE)
            }
            tenant_schemas: list[tuple[str, Tenant]] = []  # (schema_name, tenant)
            for tenant in tenants:
                ts = active_schemas.get(tenant.id)
                if ts is None:
                    raise ValueError(
                        f"Tenant '{tenant.external_id}' has no active schema. "
                        "Run a data refresh for this tenant before building the view schema."
                    )
                tenant_schemas.append((ts.schema_name, tenant))
        except ValueError as exc:
            vs.state = SchemaState.FAILED
            vs.last_error = str(exc)[:500]
            vs.save(update_fields=["state", "last_error"])
            raise

        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()

            if not re.match(r"^ws_[a-f0-9]{16}$", view_schema_name):
                raise ValueError(f"Invalid view schema name: {view_schema_name!r}")

            # Detect collisions on the FINAL (bounded) prefixes.
            prefix_to_tenant: dict[str, str] = {}
            tenant_prefixes: list[
                tuple[str, str, str]
            ] = []  # (schema_name, tenant_external_id, prefix)
            for schema_name, tenant_obj in tenant_schemas:
                tenant_external_id = tenant_obj.external_id
                # Use the threaded tenant object, NOT a lookup by external_id —
                # that raises MultipleObjectsReturned across providers (arch #235).
                prefix = self._view_prefix(tenant_obj)
                if prefix in prefix_to_tenant:
                    raise ValueError(
                        f"Canonical name collision: tenants '{prefix_to_tenant[prefix]}' and "
                        f"'{tenant_external_id}' both sanitize to prefix '{prefix}'"
                    )
                prefix_to_tenant[prefix] = tenant_external_id
                tenant_prefixes.append((schema_name, tenant_external_id, prefix))

            # Check length + full-name collisions on FINAL names before any DDL.
            # The collision check catches ambiguous __ delimiters ("foo__bar"+"baz"
            # vs "foo"+"bar__baz"); the length check catches composed names that
            # would silently truncate past the 63-byte limit and collapse together.
            planned_views: list[tuple[str, str, str]] = []
            seen_view_names: dict[str, str] = {}  # view_name → tenant_external_id
            oversized_views: list[str] = []
            for schema_name, tenant_external_id, prefix in tenant_prefixes:
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')",
                    (schema_name,),
                )
                for (table_name,) in cursor.fetchall():
                    view_name = f"{prefix}__{table_name}"
                    if len(view_name.encode("utf-8")) > PG_MAX_IDENTIFIER_BYTES:
                        oversized_views.append(view_name)
                        continue
                    if view_name in seen_view_names:
                        raise ValueError(
                            f"View name collision: '{view_name}' produced by both "
                            f"tenant '{seen_view_names[view_name]}' and '{tenant_external_id}'"
                        )
                    seen_view_names[view_name] = tenant_external_id
                    planned_views.append((view_name, schema_name, table_name))

            if oversized_views:
                raise ValueError(
                    "View name(s) exceed PostgreSQL's 63-byte identifier limit and "
                    f"would be truncated: {', '.join(sorted(oversized_views))}"
                )

            # DROP + recreate (not CREATE OR REPLACE VIEW) so a rebuild after an
            # underlying column change never hits "cannot change name of view
            # column", and a duplicate name hard-errors instead of redefining.
            cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(view_schema_name)
                )
            )
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(view_schema_name))
            )

            for view_name, schema_name, table_name in planned_views:
                cursor.execute(
                    psycopg.sql.SQL("CREATE VIEW {}.{} AS SELECT * FROM {}.{}").format(
                        psycopg.sql.Identifier(view_schema_name),
                        psycopg.sql.Identifier(view_name),
                        psycopg.sql.Identifier(schema_name),
                        psycopg.sql.Identifier(table_name),
                    )
                )
            views_created = len(planned_views)

            self._create_readonly_role(cursor, view_schema_name)

            view_role = readonly_role_name(view_schema_name)

            # The _ro role is reused across rebuilds; revoke grants on tenants no
            # longer in the workspace, else a removed tenant's data stays reachable
            # (issue #244).
            current_schemas = {view_schema_name} | {s for s, _ in tenant_schemas}
            self._revoke_stale_view_role_grants(cursor, view_role, current_schemas)

            # ALTER DEFAULT PRIVILEGES only covers future tables, not the views
            # just created — grant SELECT on them explicitly.
            cursor.execute(
                psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    psycopg.sql.Identifier(view_schema_name),
                    psycopg.sql.Identifier(view_role),
                )
            )

            # Views reference the constituent tenant schemas directly.
            for tenant_schema_name, _ in tenant_schemas:
                cursor.execute(
                    psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        psycopg.sql.Identifier(tenant_schema_name),
                        psycopg.sql.Identifier(view_role),
                    )
                )
                cursor.execute(
                    psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                        psycopg.sql.Identifier(tenant_schema_name),
                        psycopg.sql.Identifier(view_role),
                    )
                )

            cursor.close()
        except Exception as exc:
            # Drop any partial schema before marking FAILED to avoid debris.
            try:
                if not conn.closed:
                    c = conn.cursor()
                    c.execute(
                        psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                            psycopg.sql.Identifier(view_schema_name)
                        )
                    )
                    c.close()
            except Exception:
                logger.exception(
                    "Failed to drop partial view schema '%s' during cleanup", view_schema_name
                )
            if not conn.closed:
                conn.close()
            # Persist the error text so the resume task, MCP get_schema_status, and
            # the status API can surface *why* the query layer is unavailable.
            vs.state = SchemaState.FAILED
            vs.last_error = str(exc)[:500]
            vs.save(update_fields=["state", "last_error"])
            raise
        finally:
            if not conn.closed:
                conn.close()

        # Reset the TTL on (re)build: a row resurrected from EXPIRED keeps its stale
        # last_accessed_at and expire_inactive_schemas would re-tear-down it (arch #255 03#2).
        vs.state = SchemaState.ACTIVE
        vs.last_error = ""
        vs.last_accessed_at = timezone.now()
        vs.save(update_fields=["state", "last_error", "last_accessed_at"])

        logger.info(
            "Built view schema '%s' for workspace '%s' (%d tenants, %d views)",
            view_schema_name,
            workspace.id,
            len(tenant_schemas),
            views_created,
        )
        return vs

    def teardown_view_schema(self, view_schema: WorkspaceViewSchema) -> None:
        """Drop the physical PostgreSQL schema for a WorkspaceViewSchema.

        Role cleanup is best-effort — see ``teardown`` for rationale.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(view_schema.schema_name)
                )
            )
            try:
                self._drop_readonly_role(cursor, view_schema.schema_name)
                self._drop_dbt_role(cursor, view_schema.schema_name)
            except Exception:
                logger.exception(
                    "teardown_view_schema: dropping derived roles for '%s' failed; "
                    "physical schema was dropped, role may be dangling",
                    view_schema.schema_name,
                )
            cursor.close()
        finally:
            conn.close()

    async def ateardown(self, tenant_schema: TenantSchema) -> None:
        """Async version of teardown — drop a tenant's schema from the managed database.

        Role cleanup is best-effort — see ``teardown`` for rationale.
        """
        async with await aget_managed_db_connection() as conn, conn.cursor() as cursor:
            await cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(tenant_schema.schema_name)
                )
            )
            try:
                await self._adrop_readonly_role(cursor, tenant_schema.schema_name)
                await cursor.execute(
                    psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
                        psycopg.sql.Identifier(dbt_role_name(tenant_schema.schema_name))
                    )
                )
            except Exception:
                logger.exception(
                    "ateardown: dropping derived roles for schema '%s' failed; "
                    "physical schema was dropped, role may be dangling",
                    tenant_schema.schema_name,
                )

    async def ateardown_view_schema(self, view_schema: WorkspaceViewSchema) -> None:
        """Async version of teardown_view_schema — drop the physical PostgreSQL schema.

        Role cleanup is best-effort — see ``teardown`` for rationale.
        """
        async with await aget_managed_db_connection() as conn, conn.cursor() as cursor:
            await cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(view_schema.schema_name)
                )
            )
            try:
                await self._adrop_readonly_role(cursor, view_schema.schema_name)
                await cursor.execute(
                    psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
                        psycopg.sql.Identifier(dbt_role_name(view_schema.schema_name))
                    )
                )
            except Exception:
                logger.exception(
                    "ateardown_view_schema: dropping derived roles for '%s' failed; "
                    "physical schema was dropped, role may be dangling",
                    view_schema.schema_name,
                )

    # Finds schemas where the role holds direct ACL entries — schema-level grants
    # are the only ones that survive DROP SCHEMA CASCADE and would block DROP ROLE.
    _SCHEMAS_WITH_ROLE_GRANTS_SQL = """
        SELECT DISTINCT n.nspname
        FROM pg_namespace n, aclexplode(n.nspacl) AS acl
        JOIN pg_roles r ON r.oid = acl.grantee
        WHERE r.rolname = %s
    """

    async def _adrop_readonly_role(self, cursor, schema_name: str) -> None:
        """Async version of _drop_readonly_role."""
        role_name = readonly_role_name(schema_name)
        await cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if not await cursor.fetchone():
            return
        await cursor.execute(self._SCHEMAS_WITH_ROLE_GRANTS_SQL, (role_name,))
        schemas_with_grants = [row[0] for row in await cursor.fetchall()]
        for schema in schemas_with_grants:
            await cursor.execute(
                psycopg.sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(role_name),
                )
            )
            await cursor.execute(
                psycopg.sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(role_name),
                )
            )
        await cursor.execute(
            psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role_name))
        )

    def _drop_readonly_role(self, cursor, schema_name: str) -> None:
        """Drop the read-only role, first revoking schema-scoped ACLs it still
        holds on other schemas (e.g. a view-schema role's grants on constituent
        tenant schemas).

        Avoids ``DROP OWNED BY`` — that needs privileges of the target role, which
        the managed-DB user does not reliably hold. Explicit REVOKE works because
        the current user issued the original GRANTs.
        """
        role_name = readonly_role_name(schema_name)
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if not cursor.fetchone():
            return
        cursor.execute(self._SCHEMAS_WITH_ROLE_GRANTS_SQL, (role_name,))
        schemas_with_grants = [row[0] for row in cursor.fetchall()]
        for schema in schemas_with_grants:
            cursor.execute(
                psycopg.sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(role_name),
                )
            )
            cursor.execute(
                psycopg.sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(role_name),
                )
            )
        cursor.execute(
            psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role_name))
        )

    def _create_dbt_role(self, cursor, schema_name: str) -> None:
        """Create a low-privilege dbt role confined to one schema (issue #241).

        dbt SET ROLEs to this so a TransformationAsset's free-text SQL runs with
        rights on THIS schema only, not as the managed-DB superuser. Grants USAGE +
        CREATE and SELECT (existing + default) on this schema and NOTHING on any
        other, so a cross-tenant read fails on missing USAGE. Granted TO the
        current user so it can SET ROLE. Idempotent.
        """
        role_name = dbt_role_name(schema_name)
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if not cursor.fetchone():
            with contextlib.suppress(psycopg.errors.DuplicateObject):
                cursor.execute(
                    psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(
                        psycopg.sql.Identifier(role_name)
                    )
                )
        cursor.execute(
            psycopg.sql.SQL("GRANT {} TO CURRENT_USER").format(psycopg.sql.Identifier(role_name))
        )
        cursor.execute(
            psycopg.sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )
        cursor.execute(
            psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )
        cursor.execute(
            psycopg.sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO {}"
            ).format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )

    def _create_readonly_role(self, cursor, schema_name: str) -> None:
        """Create a read-only PostgreSQL role for a schema.

        Idempotent — checks pg_roles before creating. Grants USAGE on the
        schema and sets ALTER DEFAULT PRIVILEGES so tables created later by
        the materializer are automatically readable.

        Also creates the low-privilege dbt role (issue #241) alongside the
        read-only role so every provisioned schema has its confinement role
        available before the transform phase runs.
        """
        role_name = readonly_role_name(schema_name)
        # pg has no CREATE ROLE IF NOT EXISTS.
        cursor.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (role_name,),
        )
        if not cursor.fetchone():
            # Another process may create the role between check and create.
            with contextlib.suppress(psycopg.errors.DuplicateObject):
                cursor.execute(
                    psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(
                        psycopg.sql.Identifier(role_name)
                    )
                )
        cursor.execute(
            psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )
        cursor.execute(
            psycopg.sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO {}"
            ).format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )
        self._create_dbt_role(cursor, schema_name)
        # dbt materializes staging tables while SET ROLE'd to the _dbt confinement
        # role (issue #241), so those tables are owned by _dbt, not CURRENT_USER —
        # the CURRENT_USER default-privilege grant above never reaches them, and the
        # _ro role gets "permission denied for table stg_visits" at query time. Set
        # default privileges FOR the dbt role too so its future tables are readable
        # by _ro. Safe because _create_dbt_role granted the dbt role TO CURRENT_USER,
        # which is the membership ALTER DEFAULT PRIVILEGES FOR ROLE requires.
        cursor.execute(
            psycopg.sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO {}"
            ).format(
                psycopg.sql.Identifier(dbt_role_name(schema_name)),
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )

    def _revoke_stale_view_role_grants(
        self, cursor, role_name: str, current_schemas: set[str]
    ) -> None:
        """Revoke the view-schema _ro role's grants on schemas it should no longer reach.

        On a view-schema rebuild the role is reused, so any tenant schema that
        was dropped from the workspace since the last build still carries
        SELECT/USAGE for this role. Find every schema where the role holds an
        ACL entry and revoke from those not in ``current_schemas`` (the view
        schema plus the constituent tenant schemas of the new membership).
        Best-effort per schema: a removed schema that has since been dropped
        from the database leaves no ACL to revoke.
        """
        cursor.execute(self._SCHEMAS_WITH_ROLE_GRANTS_SQL, (role_name,))
        granted_schemas = [row[0] for row in cursor.fetchall()]
        for schema in granted_schemas:
            if schema in current_schemas:
                continue
            cursor.execute(
                psycopg.sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(role_name),
                )
            )
            cursor.execute(
                psycopg.sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(role_name),
                )
            )

    def _sanitize_schema_name(self, tenant_id: str) -> str:
        """Sanitize an arbitrary string into a PostgreSQL identifier body.

        Thin delegate to the shared ``sanitize_identifier`` (arch #235). Retained
        because ``_view_prefix`` and tests reference it; collision-safe minting of
        full schema names goes through ``tenant_schema_name``.
        """
        return sanitize_identifier(tenant_id)
