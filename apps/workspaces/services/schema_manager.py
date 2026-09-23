"""
Schema Manager for the Scout-managed database.

Creates and tears down tenant-scoped PostgreSQL schemas.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import uuid

import psycopg
import psycopg.sql
from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.common.identifiers import (
    dbt_role_name,
    readonly_role_name,
    refresh_schema_name,
    sanitize_identifier,
    tenant_schema_name,
    view_name,
)
from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema
from apps.workspaces.services.data_operation import (
    LockOrderError,
    sync_tenant_data_lock,
    sync_workspace_data_lock,
)
from apps.workspaces.services.view_sources import VIEW_SOURCES_VERSION

logger = logging.getLogger(__name__)

# Cap the view-name prefix well below Postgres's 63-byte identifier limit, leaving
# budget for the ``__{table}`` suffix (the full name is digest-fitted by
# ``view_name`` if it still exceeds the limit). Identifier minting lives in
# apps.common.identifiers (arch #235).
_MAX_VIEW_PREFIX_LEN = 32

# Schema comment written inside the publication transaction. It is the only
# evidence a *committed* physical publication leaves behind: the control row is
# saved afterwards in a different database, so a worker that dies in between
# leaves a marker no row claims (see ``reconcile_view_publication``).
_PUBLICATION_MARKER_VERSION = 1

# Retirement takes ACCESS EXCLUSIVE locks on every relation of the schema; a
# reader that never ends would otherwise pin the worker forever. Exceeding this
# fails the retirement visibly and it is retried with backoff.
_RETIRE_LOCK_TIMEOUT = "30s"

# The publication transaction holds W, T and the view lock; never let it sit
# behind a long in-place load indefinitely. A timeout rolls back to last-good.
_PUBLICATION_LOCK_TIMEOUT = "30s"

_VIEW_BUILD_LOCK_NAMESPACE = 0x53435642
_view_build_context = threading.local()
_publication_context = threading.local()


@contextlib.contextmanager
def _serialize_view_publication(workspace):
    with sync_workspace_data_lock(workspace.id):
        tenant_ids = frozenset(workspace.tenants.values_list("id", flat=True))
        with sync_tenant_data_lock(tenant_ids), _serialize_view_build(workspace.id):
            _publication_context.owned = (workspace.id, tenant_ids)
            try:
                yield
            finally:
                _publication_context.owned = None


def _assert_publication_owned(workspace, tenant_ids=None):
    owned = getattr(_publication_context, "owned", None)
    if owned is None or owned[0] != workspace.id:
        raise LockOrderError("View publication requires workspace and tenant ownership")
    if tenant_ids is not None and not set(tenant_ids) <= owned[1]:
        raise LockOrderError("Workspace sources changed after acquiring tenant locks; retry")


class SchemaStillReferenced(Exception):
    """A schema cannot be retired: objects outside it still depend on its relations."""

    def __init__(self, schema_name: str, dependents: list[dict], detail: str = ""):
        self.schema_name = schema_name
        self.dependents = dependents
        self.detail = detail
        listed = ", ".join(f"{d['schema']}.{d['name']}" for d in dependents[:5])
        reason = listed or detail or "unknown dependents"
        super().__init__(f"Schema '{schema_name}' is still referenced by {reason}")


@contextlib.contextmanager
def _serialize_view_build(workspace_id):
    workspace_key = str(workspace_id)
    active_builds = getattr(_view_build_context, "workspaces", None)
    if active_builds is None:
        active_builds = _view_build_context.workspaces = set()
    if workspace_key in active_builds:
        raise RuntimeError(f"Recursive view build for workspace {workspace_key}")
    lock_key = int.from_bytes(hashlib.sha256(workspace_key.encode()).digest()[:4], signed=True)
    active_builds.add(workspace_key)
    try:
        # A session lock serializes plan capture, DDL, and publication while
        # autocommit keeps PROVISIONING visible to readers in other processes.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(%s, %s)", [_VIEW_BUILD_LOCK_NAMESPACE, lock_key]
            )
            try:
                yield
            finally:
                try:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(%s, %s)", [_VIEW_BUILD_LOCK_NAMESPACE, lock_key]
                    )
                except Exception:
                    logger.exception(
                        "Failed to release workspace view-build lock for %s", workspace_key
                    )
                    connection.close()
    finally:
        active_builds.remove(workspace_key)


def get_managed_db_connection():
    """Get a psycopg connection to the managed database."""
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise RuntimeError("MANAGED_DATABASE_URL is not configured")
    return psycopg.connect(url, autocommit=True)


def get_managed_db_transaction():
    """Managed connection with autocommit off, for DDL that must publish atomically.

    Routed through ``get_managed_db_connection`` so a caller (or a test) that
    substitutes the managed connection sees this connection too.
    """
    conn = get_managed_db_connection()
    conn.autocommit = False
    return conn


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

    # View rewrite rules are how one schema's views keep reading another schema's
    # relations. A view's own rule depends on the view itself, so restricting the
    # dependent namespace to a *different* schema leaves exactly the outside
    # readers — normally sibling workspace ``ws_*`` views.
    _EXTERNAL_DEPENDENTS_SQL = """
        SELECT DISTINCT dn.nspname, dc.relname, dc.relkind
        FROM pg_depend d
        JOIN pg_rewrite rw ON rw.oid = d.objid AND d.classid = 'pg_rewrite'::regclass
        JOIN pg_class dc ON dc.oid = rw.ev_class
        JOIN pg_namespace dn ON dn.oid = dc.relnamespace
        JOIN pg_class rc ON rc.oid = d.refobjid AND d.refclassid = 'pg_class'::regclass
        JOIN pg_namespace rn ON rn.oid = rc.relnamespace
        WHERE rn.nspname = %(schema)s AND dn.nspname <> %(schema)s
        ORDER BY 1, 2
    """

    _SCHEMA_RELATIONS_SQL = """
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
        ORDER BY c.relname
    """

    # DROP TABLE on a sequence (and vice versa) is an error, so each relkind needs
    # its own statement. Sequences cannot be LOCK TABLE'd at all.
    _DROP_KEYWORD_BY_RELKIND = {
        "r": "TABLE",
        "p": "TABLE",
        "f": "FOREIGN TABLE",
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "S": "SEQUENCE",
    }

    def external_dependents(self, schema_name: str) -> list[dict]:
        """Objects outside ``schema_name`` whose views read relations inside it."""
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            try:
                return self._external_dependents(cursor, schema_name)
            finally:
                cursor.close()
        finally:
            conn.close()

    def _external_dependents(self, cursor, schema_name: str) -> list[dict]:
        cursor.execute(self._EXTERNAL_DEPENDENTS_SQL, {"schema": schema_name})
        return [
            {"schema": schema, "name": name, "relkind": relkind}
            for schema, name, relkind in cursor.fetchall()
        ]

    def retire_tenant_schema(self, tenant_schema: TenantSchema) -> None:
        """Drop a tenant schema only if nothing outside it still reads its data.

        Unlike ``teardown`` this never cascades: a sibling workspace whose views
        still point at this schema must keep its query layer, so the whole
        retirement is one transaction that

        1. takes ACCESS EXCLUSIVE locks on the schema's relations, which blocks a
           ``CREATE VIEW`` that would otherwise appear between check and drop,
        2. re-checks external dependents under those locks and raises
           ``SchemaStillReferenced`` (dropping nothing) if any remain,
        3. empties the schema with RESTRICT drops only, retrying until a pass makes
           no progress — so an in-schema dbt view is dropped before the raw table it
           reads, and an unexpected dependency fails the transaction instead of
           silently taking someone else's object with it.

        Role cleanup after the commit is best-effort, as in ``teardown``.
        """
        schema_name = tenant_schema.schema_name
        conn = get_managed_db_transaction()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("SET LOCAL lock_timeout = {}").format(
                    psycopg.sql.Literal(_RETIRE_LOCK_TIMEOUT)
                )
            )
            cursor.execute(self._SCHEMA_RELATIONS_SQL, (schema_name,))
            relations = sorted(
                cursor.fetchall(), key=lambda rel: (rel[1] not in ("v", "m"), rel[0])
            )
            for relname, relkind in relations:
                # LOCK TABLE rejects sequences, materialized views and foreign tables.
                if relkind not in ("r", "p", "v"):
                    continue
                cursor.execute(
                    psycopg.sql.SQL("LOCK TABLE {}.{} IN ACCESS EXCLUSIVE MODE").format(
                        psycopg.sql.Identifier(schema_name),
                        psycopg.sql.Identifier(relname),
                    )
                )

            dependents = self._external_dependents(cursor, schema_name)
            if dependents:
                conn.rollback()
                raise SchemaStillReferenced(schema_name, dependents)

            self._drop_relations_restrict(conn, cursor, schema_name, relations)
            try:
                cursor.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} RESTRICT").format(
                        psycopg.sql.Identifier(schema_name)
                    )
                )
            except psycopg.errors.DependentObjectsStillExist as exc:
                # Something that is not a relation (a type, function, or a relation
                # created after the listing) is still in the schema.
                conn.rollback()
                raise SchemaStillReferenced(schema_name, [], detail=str(exc)) from exc
            cursor.close()
            conn.commit()
        except Exception:
            if not conn.closed:
                with contextlib.suppress(Exception):
                    conn.rollback()
            raise
        finally:
            if not conn.closed:
                conn.close()

        self._drop_schema_roles(schema_name)
        logger.info("Retired schema '%s' (no external dependents)", schema_name)

    def _drop_relations_restrict(self, conn, cursor, schema_name: str, relations) -> None:
        remaining = list(relations)
        while remaining:
            blocked: list[tuple[str, str]] = []
            detail = ""
            for relname, relkind in remaining:
                keyword = self._DROP_KEYWORD_BY_RELKIND[relkind]
                try:
                    # A savepoint keeps one blocked relation from aborting the
                    # transaction that still has to drop the others.
                    with conn.transaction():
                        cursor.execute(
                            psycopg.sql.SQL("DROP {} IF EXISTS {}.{} RESTRICT").format(
                                psycopg.sql.SQL(keyword),
                                psycopg.sql.Identifier(schema_name),
                                psycopg.sql.Identifier(relname),
                            )
                        )
                except psycopg.errors.DependentObjectsStillExist as exc:
                    blocked.append((relname, relkind))
                    detail = str(exc)
            if len(blocked) == len(remaining):
                dependents = self._external_dependents(cursor, schema_name)
                conn.rollback()
                raise SchemaStillReferenced(schema_name, dependents, detail=detail)
            remaining = blocked

    def _drop_schema_roles(self, schema_name: str) -> None:
        # Best effort all the way down: the schema is already dropped, and an
        # escaping error would make the caller revert the row to ACTIVE.
        conn = None
        try:
            conn = get_managed_db_connection()
            cursor = conn.cursor()
            self._drop_readonly_role(cursor, schema_name)
            self._drop_dbt_role(cursor, schema_name)
            cursor.close()
        except Exception:
            logger.exception(
                "Dropping derived roles for schema '%s' failed; the physical schema "
                "was dropped, the role may be dangling",
                schema_name,
            )
        finally:
            if conn is not None:
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
        """Publish one coherent physical view schema and its coverage per workspace."""
        with _serialize_view_publication(workspace):
            return self._build_view_schema(workspace)

    def tenant_ids_for_view(self, view_name: str, tenants) -> tuple[str, ...]:
        """Recover a legacy view's owner only when the canonical plan is unambiguous.

        Use the same bounded prefix as publication, never labels guessed from a
        semantic member. New catalogs persist the result before names can change.
        """
        candidates = [
            str(tenant.id)
            for tenant in tenants
            if view_name.startswith(f"{self._view_prefix(tenant)}__")
            and len(view_name) > len(self._view_prefix(tenant)) + 2
        ]
        return tuple(candidates) if len(candidates) == 1 else ()

    def _build_view_schema(self, workspace) -> WorkspaceViewSchema:
        """(Re)build the PostgreSQL view schema for a multi-tenant workspace.

        Fetches all active TenantSchema objects for the workspace's tenants and
        creates one namespaced ``{prefix}__{table}`` view per tenant table in a
        dedicated schema. The build is idempotent: the view schema is dropped and
        recreated from scratch each call, so a rebuild after an underlying table's
        columns changed succeeds rather than failing on view-column mismatches.

        A workspace with at least one active tenant schema remains queryable: tenants
        without one are recorded in ``tenant_coverage`` and omitted from this build.
        Raises ValueError if no tenant has an active schema, if two included tenants
        produce the same view prefix or full view name. A composed view name that
        would exceed PostgreSQL's 63-byte identifier limit is digest-fitted rather
        than truncated (SCOUT-DJANGO-3C).

        Every physical statement — plan reads, DROP/CREATE SCHEMA, CREATE VIEW,
        role creation, grants and the commit marker — runs in ONE managed
        transaction, so a failure rolls back to the previously published views
        instead of leaving the workspace unqueryable. Coverage, provenance and the
        build token are written to the control row only after that commit, and a
        row that was ACTIVE keeps its state and its last-good metadata for the whole
        rebuild: its old views stay readable and stay truthfully described.

        Returns the WorkspaceViewSchema model instance with state=ACTIVE on success.
        """
        _assert_publication_owned(workspace)
        tenants = sorted(
            workspace.tenants.all().iterator(),
            key=lambda tenant: (tenant.provider, tenant.external_id, str(tenant.id)),
        )
        _assert_publication_owned(workspace, (tenant.id for tenant in tenants))

        # Create/reset the row FIRST so an early validation failure marks it FAILED
        # instead of leaving a resurrected row in PROVISIONING (arch #255 03#1/03#2).
        view_schema_name = self._view_schema_name(workspace.id)
        vs, _ = WorkspaceViewSchema.objects.get_or_create(
            workspace=workspace,
            defaults={"schema_name": view_schema_name, "state": SchemaState.PROVISIONING},
        )
        was_active = vs.state == SchemaState.ACTIVE
        vs.schema_name = view_schema_name
        entry_fields = ["schema_name"]
        if not was_active:
            # An ACTIVE row is still serving readable views; advertising
            # PROVISIONING would make a working query layer look unavailable.
            vs.state = SchemaState.PROVISIONING
            entry_fields.append("state")
        vs.save(update_fields=entry_fields)

        coverage = {"included_tenants": [], "excluded_tenants": []}
        try:
            if not tenants:
                raise ValueError(f"Workspace {workspace.id} has no tenants")

            active_schemas = {
                ts.tenant_id: ts
                for ts in TenantSchema.objects.filter(tenant__in=tenants, state=SchemaState.ACTIVE)
            }
            included_tenants = [tenant for tenant in tenants if tenant.id in active_schemas]
            excluded_tenants = [tenant for tenant in tenants if tenant.id not in active_schemas]
            tenant_schemas: list[tuple[str, Tenant]] = [
                (active_schemas[tenant.id].schema_name, tenant) for tenant in included_tenants
            ]
            coverage = {
                "included_tenants": [self._tenant_coverage_entry(t) for t in included_tenants],
                "excluded_tenants": [self._tenant_coverage_entry(t) for t in excluded_tenants],
            }

            if not tenant_schemas:
                raise ValueError(
                    f"Workspace {workspace.id} has no active schema for any tenant. "
                    "Run a data refresh before building the view schema."
                )
        except ValueError as exc:
            # Nothing can be served: the sources these views read are gone or going,
            # so they can never be valid again and must not keep a dependency-guarded
            # tenant-schema retirement blocked. Dropping them is the one failure path
            # that touches the physical layer.
            self._drop_view_schema_physically(view_schema_name)
            vs.state = SchemaState.FAILED
            vs.last_error = str(exc)[:500]
            vs.tenant_coverage = coverage
            vs.physical_build_token = ""
            vs.save(
                update_fields=["state", "last_error", "tenant_coverage", "physical_build_token"]
            )
            raise

        build_token = uuid.uuid4().hex
        conn = get_managed_db_transaction()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("SET LOCAL lock_timeout = {}").format(
                    psycopg.sql.Literal(_PUBLICATION_LOCK_TIMEOUT)
                )
            )

            if not re.match(r"^ws_[a-f0-9]{16}$", view_schema_name):
                raise ValueError(f"Invalid view schema name: {view_schema_name!r}")

            # Detect collisions on the FINAL (bounded) prefixes.
            prefix_to_tenant: dict[str, str] = {}
            tenant_prefixes: list[tuple[str, Tenant, str]] = []
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
                tenant_prefixes.append((schema_name, tenant_obj, prefix))

            # Check full-name collisions on FINAL (fitted) names before any DDL.
            # The collision check catches ambiguous __ delimiters ("foo__bar"+"baz"
            # vs "foo"+"bar__baz"); view_name keeps every name within the
            # 63-byte limit so Postgres never silently truncates two into one.
            planned_views: list[tuple[str, str, str]] = []
            planned_sources: dict[str, dict[str, str]] = {}
            seen_view_names: dict[str, str] = {}  # fitted view name → tenant_external_id
            for schema_name, tenant_obj, prefix in tenant_prefixes:
                tenant_external_id = tenant_obj.external_id
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')",
                    (schema_name,),
                )
                for (table_name,) in cursor.fetchall():
                    name = view_name(prefix, table_name)
                    if name in seen_view_names:
                        raise ValueError(
                            f"View name collision: '{name}' produced by both "
                            f"tenant '{seen_view_names[name]}' and '{tenant_external_id}'"
                        )
                    seen_view_names[name] = tenant_external_id
                    planned_views.append((name, schema_name, table_name))
                    planned_sources[name] = {
                        "tenant_id": str(tenant_obj.id),
                        "source_table_name": table_name,
                    }

            # Lock the sources before touching our own views. An in-place load
            # holds its raw table exclusively and then cascades into every view
            # schema reading it; taking the tables first (in one global order) keeps
            # that from forming a cycle with our DROP SCHEMA below.
            for schema_name, table_name in sorted({(v[1], v[2]) for v in planned_views}):
                cursor.execute(
                    psycopg.sql.SQL("LOCK TABLE {}.{} IN ACCESS SHARE MODE").format(
                        psycopg.sql.Identifier(schema_name),
                        psycopg.sql.Identifier(table_name),
                    )
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

            for name, schema_name, table_name in planned_views:
                cursor.execute(
                    psycopg.sql.SQL("CREATE VIEW {}.{} AS SELECT * FROM {}.{}").format(
                        psycopg.sql.Identifier(view_schema_name),
                        psycopg.sql.Identifier(name),
                        psycopg.sql.Identifier(schema_name),
                        psycopg.sql.Identifier(table_name),
                    )
                )
            views_created = len(planned_views)

            self._create_readonly_role(cursor, view_schema_name)

            view_role = readonly_role_name(view_schema_name)

            # The view role needs access to the view schema ONLY. The views are
            # owned by CURRENT_USER and created with plain CREATE VIEW (no
            # security_invoker), so reads through them resolve the underlying
            # tenant tables with the OWNER's privileges — the view role never
            # touches the raw tenant schemas directly. Granting it USAGE/SELECT
            # there was therefore unnecessary and widened cross-tenant reach (a
            # SET ROLE {view_role} session could read raw tenant tables), and the
            # tenant-schema default-ACL entries it left behind blocked DROP ROLE
            # at teardown. Revoke any such grants left by earlier versions by
            # scoping the "keep" set to the view schema alone (issue #244 cleanup
            # of removed tenants still holds — they're revoked here too).
            self._revoke_stale_view_role_grants(cursor, view_role, {view_schema_name})

            # ALTER DEFAULT PRIVILEGES only covers future tables, not the views
            # just created — grant SELECT on them explicitly.
            cursor.execute(
                psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    psycopg.sql.Identifier(view_schema_name),
                    psycopg.sql.Identifier(view_role),
                )
            )

            self._write_publication_marker(cursor, view_schema_name, build_token)

            cursor.close()
            conn.commit()
        except Exception as exc:
            # Rollback restores the previous schema, views and grants; a DROP here
            # would destroy exactly the last-good serving layer it just brought back.
            self._rollback_publication(conn, view_schema_name)
            # Persist the error text so the resume task, MCP get_schema_status, and
            # the status API can surface *why* the query layer is unavailable.
            vs.last_error = str(exc)[:500]
            if was_active and not self._published_views_missing(vs):
                # The restored views still serve, and coverage/provenance/token still
                # describe them truthfully — only the error is new.
                vs.save(update_fields=["last_error"])
            else:
                # Either nothing served before, or something outside this build
                # (an in-place load's DROP ... CASCADE) already removed views the
                # row still lists; rolling back cannot bring those back.
                vs.state = SchemaState.FAILED
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
        # Coverage, provenance and the build token describe what is physically on
        # disk, so they are published only once that DDL has committed. A failure of
        # this save leaves the managed marker ahead of the row —
        # ``reconcile_view_publication`` is what closes that cross-database window.
        vs.tenant_coverage = coverage
        vs.view_sources = {"version": VIEW_SOURCES_VERSION, "views": planned_sources}
        vs.physical_build_token = build_token
        vs.save(
            update_fields=[
                "state",
                "last_error",
                "last_accessed_at",
                "tenant_coverage",
                "view_sources",
                "physical_build_token",
            ]
        )

        logger.info(
            "Built view schema '%s' for workspace '%s' (%d tenants, %d views)",
            view_schema_name,
            workspace.id,
            len(tenant_schemas),
            views_created,
        )
        return vs

    @staticmethod
    def _write_publication_marker(cursor, schema_name: str, build_token: str) -> None:
        """Stamp the build token on the schema inside the publication transaction.

        COMMENT is a utility statement and takes no bind parameters, hence the
        composed literal.
        """
        payload = json.dumps({"build_token": build_token, "version": _PUBLICATION_MARKER_VERSION})
        cursor.execute(
            psycopg.sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Literal(payload),
            )
        )

    @staticmethod
    def _rollback_publication(conn, view_schema_name: str) -> None:
        try:
            if not conn.closed:
                conn.rollback()
        except Exception:
            logger.exception("Rolling back the view publication for '%s' failed", view_schema_name)

    def _drop_view_schema_physically(self, view_schema_name: str) -> None:
        """Drop a view schema in its own short transaction.

        Best-effort: the caller is already failing on a condition the operator has
        to act on, and a managed-database outage must not replace that message.
        """
        try:
            conn = get_managed_db_transaction()
        except Exception:
            logger.exception("Could not connect to drop view schema '%s'", view_schema_name)
            return
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(view_schema_name)
                )
            )
            cursor.close()
            conn.commit()
        except Exception:
            logger.exception("Failed to drop view schema '%s'", view_schema_name)
            self._rollback_publication(conn, view_schema_name)
        finally:
            if not conn.closed:
                conn.close()

    @staticmethod
    def _missing_views(cursor, vs) -> list[str]:
        expected = set(((vs.view_sources or {}).get("views") or {}).keys())
        if not expected:
            return []
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'v'",
            (vs.schema_name,),
        )
        return sorted(expected - {row[0] for row in cursor.fetchall()})

    def _published_views_missing(self, vs) -> bool:
        """True unless every view the row records is physically present.

        An unverifiable answer counts as missing: claiming a serving layer we
        cannot see is worse than reporting it unavailable.
        """
        try:
            conn = get_managed_db_connection()
        except Exception:
            logger.exception("Could not verify published views for '%s'", vs.schema_name)
            return True
        try:
            cursor = conn.cursor()
            try:
                return bool(self._missing_views(cursor, vs))
            finally:
                cursor.close()
        except Exception:
            logger.exception("Could not verify published views for '%s'", vs.schema_name)
            return True
        finally:
            conn.close()

    def read_publication_marker(self, schema_name: str) -> str | None:
        """Return the build token committed on ``schema_name``, or None.

        None means "no committed publication evidence": the schema is absent, has
        no comment, or carries something this version cannot parse.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            try:
                return self._read_publication_marker(cursor, schema_name)
            finally:
                cursor.close()
        finally:
            conn.close()

    @staticmethod
    def _read_publication_marker(cursor, schema_name: str) -> str | None:
        cursor.execute(
            "SELECT obj_description(n.oid, 'pg_namespace') FROM pg_namespace n "
            "WHERE n.nspname = %s",
            (schema_name,),
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        token = payload.get("build_token")
        return token if isinstance(token, str) and token else None

    @staticmethod
    def _schema_exists(cursor, schema_name: str) -> bool:
        cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema_name,))
        return cursor.fetchone() is not None

    def reconcile_view_publication(self, workspace) -> dict:
        with _serialize_view_publication(workspace):
            return self._reconcile_view_publication(workspace)

    def _reconcile_view_publication(self, workspace) -> dict:
        """Reconcile the managed publication marker with the control row.

        The physical publication and the row that describes it commit in different
        databases, so a worker can die in between. The marker is the authority on
        what is physically published: when it does not match the row, the recorded
        coverage/provenance describe views that are not the ones on disk, and the
        only honest repair is to republish (the build is idempotent). Reports what
        actually happened — a republish is never described as a rollback.
        """
        _assert_publication_owned(workspace)
        vs = WorkspaceViewSchema.objects.filter(workspace=workspace).first()
        if vs is None:
            return {"status": "no_row"}

        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            try:
                exists = self._schema_exists(cursor, vs.schema_name)
                marker = self._read_publication_marker(cursor, vs.schema_name) if exists else None
                missing = self._missing_views(cursor, vs) if exists else []
            finally:
                cursor.close()
        finally:
            conn.close()

        marker_matches = (marker or "") == (vs.physical_build_token or "")
        if exists and marker_matches and not missing:
            return {"status": "consistent"}
        if exists and marker_matches:
            # The marker survives a view being dropped from under it (an in-place
            # load cascades into every schema reading its raw tables).
            if vs.state != SchemaState.ACTIVE:
                return {"status": "consistent"}
            reason = "views_missing"
        elif not exists:
            if vs.state != SchemaState.ACTIVE:
                return {"status": "no_physical_schema"}
            reason = "physical_missing"
        else:
            reason = "marker_mismatch"

        logger.warning(
            "View publication for workspace '%s' is out of sync (%s); republishing",
            workspace.id,
            reason,
        )
        try:
            self._build_view_schema(workspace)
        except Exception as exc:
            # The build already recorded its outcome on the row; report rather
            # than raise so callers using this as a pre-check still run.
            logger.exception("Republishing the view schema for workspace '%s' failed", workspace.id)
            return {"status": "republish_failed", "reason": reason, "error": str(exc)[:500]}
        return {"status": "republished", "reason": reason}

    @staticmethod
    def _tenant_coverage_entry(tenant: Tenant) -> dict[str, str]:
        return {
            "tenant_id": str(tenant.id),
            "provider": tenant.provider,
            "external_id": tenant.external_id,
        }

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

    # Finds schemas where the role holds direct ACL entries that survive DROP SCHEMA
    # CASCADE and would block DROP ROLE. Relation-level ACLs must be searched
    # independently of schema-level ones: a leftover pg_default_acl entry stamps
    # SELECT onto every table created later in that schema, so a schema whose
    # nspacl grant was already revoked can still accumulate table ACLs and strand
    # the role. Only the relkinds that "REVOKE ... ON ALL TABLES" can actually
    # revoke are considered; the role is never granted anything else.
    _SCHEMAS_WITH_ROLE_GRANTS_SQL = """
        SELECT DISTINCT nspname FROM (
            SELECT n.nspname
            FROM pg_namespace n, aclexplode(n.nspacl) AS acl
            JOIN pg_roles r ON r.oid = acl.grantee
            WHERE r.rolname = %(role)s
            UNION
            SELECT n.nspname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace,
                 aclexplode(c.relacl) AS acl
            JOIN pg_roles r ON r.oid = acl.grantee
            WHERE r.rolname = %(role)s AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
        ) s
    """

    # Finds (schema, owning-role) pairs where the role is a grantee in a schema's
    # DEFAULT PRIVILEGES. These pg_default_acl entries also survive DROP SCHEMA
    # CASCADE (the schema they target may be a *different* one that still exists)
    # and, like direct ACLs, block DROP ROLE. Earlier versions set such entries on
    # the constituent tenant schemas for the workspace view role.
    _SCHEMAS_WITH_ROLE_DEFAULT_ACLS_SQL = """
        SELECT DISTINCT n.nspname, pg_get_userbyid(d.defaclrole) AS owner_role
        FROM pg_default_acl d
        JOIN pg_namespace n ON n.oid = d.defaclnamespace
        CROSS JOIN aclexplode(d.defaclacl) AS acl
        JOIN pg_roles r ON r.oid = acl.grantee
        WHERE r.rolname = %s AND d.defaclobjtype = 'r'
    """

    async def _adrop_readonly_role(self, cursor, schema_name: str) -> None:
        """Async version of _drop_readonly_role."""
        role_name = readonly_role_name(schema_name)
        await cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if not await cursor.fetchone():
            return
        await cursor.execute(self._SCHEMAS_WITH_ROLE_GRANTS_SQL, {"role": role_name})
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
        await cursor.execute(self._SCHEMAS_WITH_ROLE_DEFAULT_ACLS_SQL, (role_name,))
        for schema, owner_role in await cursor.fetchall():
            await cursor.execute(
                psycopg.sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE ALL ON TABLES FROM {}"
                ).format(
                    psycopg.sql.Identifier(owner_role),
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
        cursor.execute(self._SCHEMAS_WITH_ROLE_GRANTS_SQL, {"role": role_name})
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
        cursor.execute(self._SCHEMAS_WITH_ROLE_DEFAULT_ACLS_SQL, (role_name,))
        for schema, owner_role in cursor.fetchall():
            cursor.execute(
                psycopg.sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE ALL ON TABLES FROM {}"
                ).format(
                    psycopg.sql.Identifier(owner_role),
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
            with (
                contextlib.suppress(psycopg.errors.DuplicateObject),
                cursor.connection.transaction(),
            ):
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
        schema, grants SELECT on existing tables, and sets ALTER DEFAULT
        PRIVILEGES so tables created later by both the materializer and dbt
        are automatically readable.

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
            # Another process may create the role between check and create. The
            # savepoint matters inside the view-publication transaction: a suppressed
            # error would otherwise leave that whole transaction aborted.
            with (
                contextlib.suppress(psycopg.errors.DuplicateObject),
                cursor.connection.transaction(),
            ):
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
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
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
        cursor.execute(self._SCHEMAS_WITH_ROLE_GRANTS_SQL, {"role": role_name})
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

    def _revoke_stale_view_role_default_acls(
        self, cursor, role_name: str, current_schemas: set[str]
    ) -> None:
        """Revoke pg_default_acl entries naming the view role outside ``current_schemas``.

        Revoking the direct grants is not enough: an earlier version's
        ``ALTER DEFAULT PRIVILEGES`` in a tenant schema keeps stamping SELECT onto
        every table created there afterwards, so the role re-accumulates ACLs and
        strands itself at teardown. Kept out of ``build_view_schema`` on purpose —
        ``ALTER DEFAULT PRIVILEGES FOR ROLE`` needs membership in the owning role,
        so a failure here must not fail a rebuild.
        """
        cursor.execute(self._SCHEMAS_WITH_ROLE_DEFAULT_ACLS_SQL, (role_name,))
        for schema, owner_role in cursor.fetchall():
            if schema in current_schemas:
                continue
            cursor.execute(
                psycopg.sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE ALL ON TABLES FROM {}"
                ).format(
                    psycopg.sql.Identifier(owner_role),
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
