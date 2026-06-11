from datetime import timedelta
from unittest.mock import MagicMock, patch

import psycopg.errors
import psycopg.sql
import pytest
from django.utils import timezone

from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema
from apps.workspaces.services.schema_manager import SchemaManager, readonly_role_name


@pytest.mark.django_db
class TestSchemaManager:
    def test_provision_creates_schema(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership.tenant)

        assert ts.schema_name == mgr._sanitize_schema_name(tenant_membership.tenant.external_id)
        assert ts.state == "active"
        assert TenantSchema.objects.count() == 1
        # Verify DDL was executed
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE SCHEMA" in c for c in calls)

    def test_provision_fresh_sets_last_accessed_at(self, tenant_membership):
        """A freshly provisioned schema must start with a populated
        last_accessed_at so the inactivity TTL begins from now, not null."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        before = timezone.now()
        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            ts = SchemaManager().provision(tenant_membership.tenant)

        ts.refresh_from_db()
        assert ts.state == SchemaState.ACTIVE
        assert ts.last_accessed_at is not None
        assert ts.last_accessed_at >= before

    def test_provision_returns_existing(self, tenant_membership):
        mgr = SchemaManager()
        schema_name = mgr._sanitize_schema_name(tenant_membership.tenant.external_id)
        TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name=schema_name,
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)  # role already exists

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            ts = mgr.provision(tenant_membership.tenant)

        assert TenantSchema.objects.count() == 1  # no duplicate
        assert ts.schema_name == schema_name
        # Verify physical schema was ensured even for existing record
        mock_cursor.execute.assert_any_call(
            psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                psycopg.sql.Identifier(schema_name)
            )
        )


@pytest.mark.django_db(transaction=True)
def test_provision_resurrects_expired_schema_and_refreshes_ttl(tenant_membership):
    """Provisioning over an EXPIRED record (the resurrect/fall-through path)
    re-activates it AND refreshes last_accessed_at — otherwise the janitor
    would drop the freshly resurrected schema using its stale timestamp.

    Uses transaction=True so the IntegrityError raised by objects.create (the
    schema_name already exists) does not poison an enclosing atomic block —
    mirroring production, where provision() runs outside a transaction.
    """
    mgr = SchemaManager()
    schema_name = mgr._sanitize_schema_name(tenant_membership.tenant.external_id)
    stale = timezone.now() - timedelta(days=20)
    TenantSchema.objects.create(
        tenant=tenant_membership.tenant,
        schema_name=schema_name,
        state=SchemaState.EXPIRED,
        last_accessed_at=stale,
    )

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = None  # role doesn't exist yet

    before = timezone.now()
    with patch(
        "apps.workspaces.services.schema_manager.get_managed_db_connection",
        return_value=mock_conn,
    ):
        ts = mgr.provision(tenant_membership.tenant)

    # No duplicate row; the existing one was resurrected in place.
    assert TenantSchema.objects.count() == 1
    ts.refresh_from_db()
    assert ts.state == SchemaState.ACTIVE
    assert ts.last_accessed_at is not None
    assert ts.last_accessed_at >= before
    # The stale timestamp must be gone.
    assert ts.last_accessed_at > stale


@pytest.mark.django_db
class TestSchemaManagerRoleCreation:
    def test_provision_creates_readonly_role(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # role doesn't exist yet

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership.tenant)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls), (
            f"Expected CREATE ROLE for {role_name} in DDL calls"
        )
        assert any("GRANT USAGE ON SCHEMA" in c for c in calls)
        assert any("ALTER DEFAULT PRIVILEGES" in c for c in calls)

    def test_create_physical_schema_creates_readonly_role(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # role doesn't exist yet

        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain_r1a2b3c4",
            state="provisioning",
        )

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.create_physical_schema(ts)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE ROLE" in c and role_name in c for c in calls)


@pytest.mark.django_db
class TestSchemaManagerRoleTeardown:
    def test_teardown_drops_readonly_role(self, tenant_membership):
        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)  # role exists
        mock_cursor.fetchall.return_value = []  # no schemas with residual grants

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.teardown(ts)

        role_name = readonly_role_name(ts.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("DROP ROLE IF EXISTS" in c and role_name in c for c in calls)

    def test_teardown_view_schema_drops_readonly_role(self, workspace):
        vs = WorkspaceViewSchema.objects.create(
            workspace=workspace,
            schema_name="ws_abc1234def56789",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)  # role exists
        mock_cursor.fetchall.return_value = []  # no cross-schema grants

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            mgr.teardown_view_schema(vs)

        role_name = readonly_role_name(vs.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("DROP ROLE IF EXISTS" in c and role_name in c for c in calls)

    def test_drop_readonly_role_does_not_use_drop_owned_by(self, tenant_membership):
        """DROP OWNED BY requires membership in the target role, which a non-superuser
        creator may not have. Use explicit REVOKE instead."""
        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = []

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            SchemaManager().teardown(ts)

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert not any("DROP OWNED BY" in c for c in calls), (
            "DROP OWNED BY should not be emitted; it requires role membership"
        )

    def test_drop_readonly_role_revokes_cross_schema_grants(self, workspace):
        """View-schema _ro roles hold USAGE/SELECT on constituent tenant schemas that
        survive DROP SCHEMA CASCADE. _drop_readonly_role must revoke those explicitly."""
        vs = WorkspaceViewSchema.objects.create(
            workspace=workspace,
            schema_name="ws_abc1234def56789",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)  # role exists
        # Simulate: role holds grants on two tenant schemas
        mock_cursor.fetchall.return_value = [("tenant_alpha",), ("tenant_beta",)]

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            SchemaManager().teardown_view_schema(vs)

        role_name = readonly_role_name(vs.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        for schema in ("tenant_alpha", "tenant_beta"):
            assert any(
                "REVOKE" in c and "SCHEMA" in c and schema in c and role_name in c for c in calls
            ), f"Expected REVOKE on schema {schema} from {role_name}"
            assert any(
                "REVOKE" in c and "ALL TABLES IN SCHEMA" in c and schema in c and role_name in c
                for c in calls
            ), f"Expected REVOKE ALL TABLES in {schema} from {role_name}"

    def test_drop_readonly_role_is_noop_when_role_missing(self, tenant_membership):
        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # role does not exist

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            SchemaManager().teardown(ts)

        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        role_name = readonly_role_name(ts.schema_name)
        # DROP SCHEMA still emitted; role commands skipped
        assert any("DROP SCHEMA" in c for c in calls)
        assert not any("DROP ROLE" in c and role_name in c for c in calls)
        assert not any("REVOKE" in c and role_name in c for c in calls)

    def test_teardown_tolerates_role_cleanup_failure(self, tenant_membership):
        """If DROP SCHEMA succeeds but role cleanup raises, teardown() should log and
        return successfully — the physical schema is already gone, so failing here
        would make callers incorrectly flip state back to ACTIVE."""
        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # DROP SCHEMA succeeds. Role cleanup queries raise.
        drop_schema_seen = {"v": False}

        def execute(stmt, *args, **kwargs):
            s = str(stmt)
            if "DROP SCHEMA" in s:
                drop_schema_seen["v"] = True
                return None
            # Every role-related query raises InsufficientPrivilege
            raise psycopg.errors.InsufficientPrivilege("permission denied")

        mock_cursor.execute.side_effect = execute

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            SchemaManager().teardown(ts)

        assert drop_schema_seen["v"], "DROP SCHEMA must be attempted before role cleanup"

    def test_teardown_reraises_when_drop_schema_fails(self, tenant_membership):
        """If DROP SCHEMA itself fails, teardown() must re-raise so the caller can
        roll the record state back to ACTIVE."""
        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = RuntimeError("boom")

        with (
            patch(
                "apps.workspaces.services.schema_manager.get_managed_db_connection",
                return_value=mock_conn,
            ),
            pytest.raises(RuntimeError),
        ):
            SchemaManager().teardown(ts)


@pytest.mark.django_db
class TestViewSchemaRoleCreation:
    def test_build_view_schema_creates_readonly_role_with_tenant_grants(
        self, workspace, tenant_membership
    ):
        ts = TenantSchema.objects.create(
            tenant=tenant_membership.tenant,
            schema_name="test_domain",
            state="active",
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.closed = False
        # Return empty columns result for information_schema query
        mock_cursor.fetchall.return_value = []
        # fetchone returns None so _create_readonly_role creates the role
        mock_cursor.fetchone.return_value = None

        with patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=mock_conn,
        ):
            mgr = SchemaManager()
            vs = mgr.build_view_schema(workspace)

        view_role_name = readonly_role_name(vs.schema_name)
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        # View schema role should be created
        assert any("CREATE ROLE" in c and view_role_name in c for c in calls), (
            f"Expected CREATE ROLE for {view_role_name}"
        )
        # Should grant USAGE on view schema
        assert any("GRANT USAGE ON SCHEMA" in c and vs.schema_name in c for c in calls)
        # Should grant SELECT on constituent tenant schema tables
        assert any(
            "GRANT SELECT ON ALL TABLES IN SCHEMA" in c and ts.schema_name in c for c in calls
        )
        # Should grant USAGE on constituent tenant schema
        assert any("GRANT USAGE ON SCHEMA" in c and ts.schema_name in c for c in calls)


class _AsyncCursor:
    """Minimal async cursor double for testing ateardown paths."""

    def __init__(self, fetchone_value=(1,), fetchall_value=()):
        self.executed: list[str] = []
        self._fetchone = fetchone_value
        self._fetchall = list(fetchall_value)
        self.execute_side_effect = None

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.executed.append(s)
        if self.execute_side_effect is not None:
            raise self.execute_side_effect

    async def fetchone(self):
        return self._fetchone

    async def fetchall(self):
        return self._fetchall

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AsyncConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.django_db(transaction=True)
class TestSchemaManagerAsyncTeardown:
    @pytest.mark.asyncio
    async def test_ateardown_does_not_use_drop_owned_by(self, tenant_membership):
        ts = await TenantSchema.objects.acreate(
            tenant=tenant_membership.tenant,
            schema_name="test_async_schema",
            state="active",
        )

        cursor = _AsyncCursor(fetchone_value=(1,), fetchall_value=[])

        async def _aconn():
            return _AsyncConn(cursor)

        with patch(
            "apps.workspaces.services.schema_manager.aget_managed_db_connection",
            side_effect=_aconn,
        ):
            await SchemaManager().ateardown(ts)

        assert not any("DROP OWNED BY" in s for s in cursor.executed)
        role_name = readonly_role_name(ts.schema_name)
        assert any("DROP ROLE IF EXISTS" in s and role_name in s for s in cursor.executed)

    @pytest.mark.asyncio
    async def test_ateardown_revokes_cross_schema_grants(self, workspace):
        vs = await WorkspaceViewSchema.objects.acreate(
            workspace=workspace,
            schema_name="ws_aaaa1111bbbb2222",
            state="active",
        )

        cursor = _AsyncCursor(
            fetchone_value=(1,),
            fetchall_value=[("tenant_gamma",)],
        )

        async def _aconn():
            return _AsyncConn(cursor)

        with patch(
            "apps.workspaces.services.schema_manager.aget_managed_db_connection",
            side_effect=_aconn,
        ):
            await SchemaManager().ateardown_view_schema(vs)

        role_name = readonly_role_name(vs.schema_name)
        assert any(
            "REVOKE" in s and "SCHEMA" in s and "tenant_gamma" in s and role_name in s
            for s in cursor.executed
        )


class TestReadonlyRoleName:
    def test_basic(self):
        assert readonly_role_name("tenant_abc123") == "tenant_abc123_ro"

    def test_view_schema(self):
        assert readonly_role_name("ws_abc1234def56789") == "ws_abc1234def56789_ro"

    def test_refresh_schema(self):
        assert readonly_role_name("test_domain_r1a2b3c4") == "test_domain_r1a2b3c4_ro"


class _FakeTenant:
    """Lightweight stand-in for a Tenant — _view_prefix only reads two attrs."""

    def __init__(self, canonical_name: str, external_id: str):
        self.canonical_name = canonical_name
        self.external_id = external_id


class TestViewPrefix:
    """Pure-logic coverage for the bounded per-tenant view prefix (no DB)."""

    PIPN_NAME = "Kangaroo Mother Care- Preterm Infants Parents Network (PIPN)"

    def test_short_name_used_verbatim(self):
        mgr = SchemaManager()
        # _sanitize_schema_name lowercases, maps "-" -> "_", strips other
        # non-alphanumerics (spaces dropped). "domain_a" stays verbatim.
        t = _FakeTenant("domain_a", "ext-a")
        assert mgr._view_prefix(t) == "domain_a"

    def test_name_at_32_char_boundary_used_verbatim(self):
        mgr = SchemaManager()
        # 32 sanitized chars exactly -> used as-is (no digest)
        name = "a" * 32
        t = _FakeTenant(name, "ext-32")
        prefix = mgr._view_prefix(t)
        assert prefix == name
        assert len(prefix) == 32

    def test_long_name_is_bounded_and_hashed(self):
        mgr = SchemaManager()
        t = _FakeTenant(self.PIPN_NAME, "pipn-001")
        prefix = mgr._view_prefix(t)
        assert len(prefix) == 32
        # 23-char sanitized head + "_" + 8 hex chars
        assert prefix == "kangaroomothercare_pret_" + prefix[-8:]
        assert all(c in "0123456789abcdef" for c in prefix[-8:])

    def test_long_name_prefix_is_deterministic_across_calls(self):
        mgr = SchemaManager()
        t = _FakeTenant(self.PIPN_NAME, "pipn-001")
        assert mgr._view_prefix(t) == mgr._view_prefix(t)

    def test_pipn_views_distinct_and_within_byte_limit(self):
        """The production regression: raw_completed_works / raw_completed_modules
        previously truncated to the same 63-byte identifier."""
        mgr = SchemaManager()
        t = _FakeTenant(self.PIPN_NAME, "pipn-001")
        prefix = mgr._view_prefix(t)
        works = f"{prefix}__raw_completed_works"
        modules = f"{prefix}__raw_completed_modules"
        assert works != modules
        assert len(works.encode("utf-8")) <= 63
        assert len(modules.encode("utf-8")) <= 63

    def test_two_long_names_sharing_head_get_distinct_prefixes(self):
        mgr = SchemaManager()
        head = "Maternal Child Health Program "
        t1 = _FakeTenant(head + "Northern Region Implementation", "mch-north-1")
        t2 = _FakeTenant(head + "Southern Region Implementation", "mch-south-1")
        p1 = mgr._view_prefix(t1)
        p2 = mgr._view_prefix(t2)
        assert p1[:23] == p2[:23]
        assert p1 != p2
        assert len(p1) <= 32
        assert len(p2) <= 32

    def test_long_name_prefix_distinct_for_distinct_external_ids(self):
        mgr = SchemaManager()
        # Identical long canonical names, different external_ids -> different digests
        t1 = _FakeTenant(self.PIPN_NAME, "pipn-001")
        t2 = _FakeTenant(self.PIPN_NAME, "pipn-002")
        assert mgr._view_prefix(t1) != mgr._view_prefix(t2)
