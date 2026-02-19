# Data Explorer Vertical Slice Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** End-to-end flow where a CommCare user logs in, selects a domain, materializes case data from CommCare API into a Scout-managed schema, and queries it via the agent.

**Architecture:** Replace project-based scoping with tenant-based scoping. TenantMembership (populated from CommCare domain API) replaces ProjectMembership. MCP tools receive tenant context via `_meta` instead of `project_id` parameter. Frontend domain selector replaces project selector.

**Tech Stack:** Django 5, django-allauth, FastMCP, React 19, Zustand, Tailwind CSS 4, psycopg2, CommCare REST API v2

---

## Task 1: TenantMembership Django Model

**Files:**
- Create: `apps/users/models.py` (add TenantMembership class)
- Create: `apps/users/migrations/XXXX_add_tenantmembership.py` (auto-generated)
- Test: `tests/test_tenant_models.py`

**Step 1: Write the failing test**

```python
# tests/test_tenant_models.py
import pytest
from apps.users.models import TenantMembership


@pytest.mark.django_db
class TestTenantMembership:
    def test_create_membership(self, user):
        tm = TenantMembership.objects.create(
            user=user,
            provider="commcare",
            tenant_id="dimagi",
            tenant_name="Dimagi",
        )
        assert tm.tenant_id == "dimagi"
        assert tm.provider == "commcare"
        assert str(tm) == f"{user.email} - commcare:dimagi"

    def test_unique_constraint(self, user):
        TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        with pytest.raises(Exception):  # IntegrityError
            TenantMembership.objects.create(
                user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
            )

    def test_last_selected_at_nullable(self, user):
        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        assert tm.last_selected_at is None
```

Note: The `user` fixture should already exist in `conftest.py`. If not, add:
```python
# conftest.py
@pytest.fixture
def user(db):
    from apps.users.models import User
    return User.objects.create_user(email="test@example.com", password="testpass123")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tenant_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'TenantMembership'`

**Step 3: Write the model**

Add to `apps/users/models.py`:

```python
class TenantMembership(models.Model):
    """
    Links users to tenants discovered from OAuth providers.
    Replaces ProjectMembership for tenant-scoped access.
    """

    PROVIDER_CHOICES = [
        ("commcare", "CommCare HQ"),
        ("commcare_connect", "CommCare Connect"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    tenant_id = models.CharField(
        max_length=255,
        help_text="Domain name (CommCare) or organization ID (Connect)",
    )
    tenant_name = models.CharField(
        max_length=255,
        help_text="Human-readable tenant name",
    )
    last_selected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "provider", "tenant_id"]
        ordering = ["-last_selected_at", "tenant_name"]

    def __str__(self):
        return f"{self.user.email} - {self.provider}:{self.tenant_id}"
```

You'll also need `import uuid` and `from django.conf import settings` if not already imported in the file.

**Step 4: Generate and apply migration**

Run: `uv run python manage.py makemigrations users && uv run python manage.py migrate`

**Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_tenant_models.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add apps/users/models.py apps/users/migrations/ tests/test_tenant_models.py
git commit -m "feat: add TenantMembership model"
```

---

## Task 2: Tenant Resolution Service (CommCare Domain API)

**Files:**
- Create: `apps/users/services/tenant_resolution.py`
- Test: `tests/test_tenant_resolution.py`

**Step 1: Write the failing test**

```python
# tests/test_tenant_resolution.py
import pytest
from unittest.mock import patch, MagicMock
from apps.users.services.tenant_resolution import resolve_commcare_domains


@pytest.mark.django_db
class TestResolveCommcareDomains:
    def test_fetches_and_stores_domains(self, user):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "meta": {"limit": 20, "offset": 0, "total_count": 2, "next": None},
            "objects": [
                {"domain_name": "dimagi", "project_name": "Dimagi"},
                {"domain_name": "test-project", "project_name": "Test Project"},
            ],
        }

        with patch("apps.users.services.tenant_resolution.requests.get", return_value=mock_response):
            memberships = resolve_commcare_domains(user, "fake-token")

        assert len(memberships) == 2
        assert memberships[0].tenant_id == "dimagi"
        assert memberships[1].tenant_id == "test-project"

        from apps.users.models import TenantMembership
        assert TenantMembership.objects.filter(user=user).count() == 2

    def test_updates_existing_memberships(self, user):
        from apps.users.models import TenantMembership
        TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Old Name"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "meta": {"limit": 20, "offset": 0, "total_count": 1, "next": None},
            "objects": [{"domain_name": "dimagi", "project_name": "New Name"}],
        }

        with patch("apps.users.services.tenant_resolution.requests.get", return_value=mock_response):
            resolve_commcare_domains(user, "fake-token")

        tm = TenantMembership.objects.get(user=user, tenant_id="dimagi")
        assert tm.tenant_name == "New Name"

    def test_api_error_raises(self, user):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status.side_effect = Exception("Unauthorized")

        with patch("apps.users.services.tenant_resolution.requests.get", return_value=mock_response):
            with pytest.raises(Exception):
                resolve_commcare_domains(user, "fake-token")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tenant_resolution.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write the service**

```python
# apps/users/services/tenant_resolution.py
"""
Tenant resolution for OAuth providers.

After a user authenticates, this service queries the provider's API
to discover which tenants (domains/organizations) the user belongs to,
and stores them as TenantMembership records.
"""
from __future__ import annotations

import logging

import requests

from apps.users.models import TenantMembership

logger = logging.getLogger(__name__)

COMMCARE_DOMAIN_API = "https://www.commcarehq.org/api/user_domains/v1/"


def resolve_commcare_domains(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's CommCare domains and upsert TenantMembership records.

    Args:
        user: Django User instance.
        access_token: CommCare OAuth access token.

    Returns:
        List of TenantMembership instances (created or updated).
    """
    domains = _fetch_all_domains(access_token)
    memberships = []

    for domain in domains:
        tm, _created = TenantMembership.objects.update_or_create(
            user=user,
            provider="commcare",
            tenant_id=domain["domain_name"],
            defaults={"tenant_name": domain["project_name"]},
        )
        memberships.append(tm)

    logger.info(
        "Resolved %d CommCare domains for user %s",
        len(memberships),
        user.email,
    )
    return memberships


def _fetch_all_domains(access_token: str) -> list[dict]:
    """Paginate through the CommCare user_domains API."""
    results = []
    url = COMMCARE_DOMAIN_API
    while url:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("objects", []))
        url = data.get("meta", {}).get("next")
    return results
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tenant_resolution.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add apps/users/services/tenant_resolution.py tests/test_tenant_resolution.py
git commit -m "feat: add CommCare domain resolution service"
```

---

## Task 3: Tenant REST API Endpoint

**Files:**
- Create: `apps/users/views.py` (add tenant views)
- Modify: `config/urls.py` (add route)
- Test: `tests/test_tenant_api.py`

**Step 1: Write the failing test**

```python
# tests/test_tenant_api.py
import pytest
from django.test import AsyncClient
from apps.users.models import TenantMembership


@pytest.mark.django_db
class TestTenantListAPI:
    @pytest.mark.asyncio
    async def test_list_tenants(self, user):
        TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        client = AsyncClient()
        client.force_login(user)
        response = await client.get("/api/auth/tenants/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["tenant_id"] == "dimagi"

    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        client = AsyncClient()
        response = await client.get("/api/auth/tenants/")
        assert response.status_code == 401


@pytest.mark.django_db
class TestTenantSelectAPI:
    @pytest.mark.asyncio
    async def test_select_tenant(self, user):
        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        client = AsyncClient()
        client.force_login(user)
        response = await client.post(
            "/api/auth/tenants/select/",
            data={"tenant_id": str(tm.id)},
            content_type="application/json",
        )
        assert response.status_code == 200
        tm.refresh_from_db()
        assert tm.last_selected_at is not None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tenant_api.py -v`
Expected: FAIL — 404

**Step 3: Write the views and wire up URLs**

Add to `apps/users/views.py` (or create if it doesn't exist as a views file for auth endpoints — check where existing auth views live in `apps/chat/views.py` and follow the same pattern):

```python
# In whichever file has the auth views (likely apps/chat/views.py or a new apps/users/views.py)
from django.utils import timezone


async def tenant_list_view(request):
    """GET /api/auth/tenants/ — List the user's tenant memberships."""
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    from apps.users.models import TenantMembership
    memberships = []
    async for tm in TenantMembership.objects.filter(user=user):
        memberships.append({
            "id": str(tm.id),
            "provider": tm.provider,
            "tenant_id": tm.tenant_id,
            "tenant_name": tm.tenant_name,
            "last_selected_at": tm.last_selected_at.isoformat() if tm.last_selected_at else None,
        })

    return JsonResponse(memberships, safe=False)


async def tenant_select_view(request):
    """POST /api/auth/tenants/select/ — Mark a tenant as the active selection."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    import json
    body = json.loads(request.body)
    tenant_membership_id = body.get("tenant_id")

    from apps.users.models import TenantMembership
    try:
        tm = await TenantMembership.objects.aget(id=tenant_membership_id, user=user)
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found"}, status=404)

    tm.last_selected_at = timezone.now()
    await tm.asave(update_fields=["last_selected_at"])

    return JsonResponse({"status": "ok", "tenant_id": tm.tenant_id})
```

Add URL routes in the same file that defines auth URL patterns (check `config/urls.py`):
```python
path("api/auth/tenants/", tenant_list_view, name="tenant-list"),
path("api/auth/tenants/select/", tenant_select_view, name="tenant-select"),
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tenant_api.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add apps/users/views.py config/urls.py tests/test_tenant_api.py
# (or whichever files were modified)
git commit -m "feat: add tenant list and select API endpoints"
```

---

## Task 4: Scout-Managed Database Connection

**Files:**
- Modify: `config/settings/base.py` (add MANAGED_DB_URL setting)
- Modify: `config/settings/development.py` (dev default)
- Modify: `config/settings/test.py` (test default)

**Step 1: Add the setting**

In `config/settings/base.py`, add near the DATABASE section:
```python
# Scout-managed database for materialized tenant data.
# Separate from the application database to allow future migration to Snowflake etc.
MANAGED_DATABASE_URL = env("MANAGED_DATABASE_URL", default="")
```

In `config/settings/development.py`:
```python
# For dev, default to the same database as the app (separate schema isolation still applies)
if not MANAGED_DATABASE_URL:
    MANAGED_DATABASE_URL = DATABASES["default"]["NAME"]  # noqa: F405
```

Note: The actual connection to the managed DB will be made by the Schema Manager service (Task 5), not through Django's DATABASES dict. This keeps it decoupled.

**Step 2: Commit**

```bash
git add config/settings/base.py config/settings/development.py
git commit -m "feat: add MANAGED_DATABASE_URL setting"
```

---

## Task 5: TenantSchema Model + Schema Manager

**Files:**
- Create: `apps/projects/models.py` (add TenantSchema, MaterializationRun)
- Create: `apps/projects/services/schema_manager.py`
- Test: `tests/test_schema_manager.py`

Note: We put these in the `projects` app for now since it already handles DB concerns. Could be a new app later.

**Step 1: Write the models**

Add to `apps/projects/models.py`:

```python
class SchemaState(models.TextChoices):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    MATERIALIZING = "materializing"
    EXPIRED = "expired"
    TEARDOWN = "teardown"


class TenantSchema(models.Model):
    """Tracks a tenant's provisioned schema in the managed database."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_membership = models.ForeignKey(
        "users.TenantMembership",
        on_delete=models.CASCADE,
        related_name="schemas",
    )
    schema_name = models.CharField(max_length=255, unique=True)
    state = models.CharField(
        max_length=20,
        choices=SchemaState.choices,
        default=SchemaState.PROVISIONING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_accessed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_accessed_at"]

    def __str__(self):
        return f"{self.schema_name} ({self.state})"


class MaterializationRun(models.Model):
    """Records a materialization pipeline execution."""

    class RunState(models.TextChoices):
        STARTED = "started"
        LOADING = "loading"
        TRANSFORMING = "transforming"
        COMPLETED = "completed"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_schema = models.ForeignKey(
        TenantSchema,
        on_delete=models.CASCADE,
        related_name="materialization_runs",
    )
    pipeline = models.CharField(max_length=255)
    state = models.CharField(max_length=20, choices=RunState.choices, default=RunState.STARTED)
    result = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.pipeline} - {self.state}"
```

**Step 2: Generate migration**

Run: `uv run python manage.py makemigrations projects && uv run python manage.py migrate`

**Step 3: Write the Schema Manager test**

```python
# tests/test_schema_manager.py
import pytest
from unittest.mock import patch, MagicMock
from apps.projects.services.schema_manager import SchemaManager
from apps.projects.models import TenantSchema


@pytest.mark.django_db
class TestSchemaManager:
    def test_provision_creates_schema(self, tenant_membership):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("apps.projects.services.schema_manager.get_managed_db_connection", return_value=mock_conn):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership)

        assert ts.schema_name == tenant_membership.tenant_id
        assert ts.state == "active"
        assert TenantSchema.objects.count() == 1
        # Verify DDL was executed
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("CREATE SCHEMA" in c for c in calls)

    def test_provision_returns_existing(self, tenant_membership):
        TenantSchema.objects.create(
            tenant_membership=tenant_membership,
            schema_name=tenant_membership.tenant_id,
            state="active",
        )

        with patch("apps.projects.services.schema_manager.get_managed_db_connection"):
            mgr = SchemaManager()
            ts = mgr.provision(tenant_membership)

        assert TenantSchema.objects.count() == 1  # no duplicate
```

Add fixture to `conftest.py`:
```python
@pytest.fixture
def tenant_membership(user):
    from apps.users.models import TenantMembership
    return TenantMembership.objects.create(
        user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
    )
```

**Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_schema_manager.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 5: Write the Schema Manager**

```python
# apps/projects/services/schema_manager.py
"""
Schema Manager for the Scout-managed database.

Creates and tears down tenant-scoped PostgreSQL schemas.
Uses PG role-based access control for isolation.
"""
from __future__ import annotations

import logging

import psycopg2
from django.conf import settings

from apps.projects.models import SchemaState, TenantSchema

logger = logging.getLogger(__name__)


def get_managed_db_connection():
    """Get a psycopg2 connection to the managed database."""
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise RuntimeError("MANAGED_DATABASE_URL is not configured")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


class SchemaManager:
    """Creates and manages tenant schemas in the managed database."""

    def provision(self, tenant_membership) -> TenantSchema:
        """Get or create a schema for the tenant.

        Returns an existing active schema if one exists,
        otherwise creates a new one.
        """
        existing = TenantSchema.objects.filter(
            tenant_membership=tenant_membership,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).first()

        if existing:
            existing.save(update_fields=["last_accessed_at"])  # touch
            return existing

        schema_name = self._sanitize_schema_name(tenant_membership.tenant_id)

        ts = TenantSchema.objects.create(
            tenant_membership=tenant_membership,
            schema_name=schema_name,
            state=SchemaState.PROVISIONING,
        )

        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {psycopg2.extensions.quote_ident(schema_name, cursor)}")
            cursor.close()
        finally:
            conn.close()

        ts.state = SchemaState.ACTIVE
        ts.save(update_fields=["state"])

        logger.info("Provisioned schema '%s' for tenant '%s'", schema_name, tenant_membership.tenant_id)
        return ts

    def teardown(self, tenant_schema: TenantSchema) -> None:
        """Drop a tenant's schema and mark it as torn down."""
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"DROP SCHEMA IF EXISTS {psycopg2.extensions.quote_ident(tenant_schema.schema_name, cursor)} CASCADE"
            )
            cursor.close()
        finally:
            conn.close()

        tenant_schema.state = SchemaState.TEARDOWN
        tenant_schema.save(update_fields=["state"])

    def _sanitize_schema_name(self, tenant_id: str) -> str:
        """Convert a tenant_id to a valid PostgreSQL schema name."""
        # Replace hyphens with underscores, lowercase
        name = tenant_id.lower().replace("-", "_")
        # Strip non-alphanumeric/underscore chars
        name = "".join(c for c in name if c.isalnum() or c == "_")
        # Ensure starts with letter/underscore
        if name and name[0].isdigit():
            name = f"t_{name}"
        return name or "unknown"
```

**Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_schema_manager.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add apps/projects/models.py apps/projects/migrations/ apps/projects/services/schema_manager.py tests/test_schema_manager.py conftest.py
git commit -m "feat: add TenantSchema model and SchemaManager service"
```

---

## Task 6: MCP Server — Replace project_id with Tenant Context

**Files:**
- Modify: `mcp_server/context.py` (replace ProjectContext with TenantContext)
- Modify: `mcp_server/server.py` (update all tools)
- Modify: `mcp_server/envelope.py` (update envelope to use tenant_id)
- Test: `tests/test_mcp_server_tools.py`

**Step 1: Rewrite context.py**

Replace `mcp_server/context.py` with:

```python
"""Tenant context for the MCP server.

Holds tenant configuration as an immutable snapshot. Extracted from
the _meta field injected by the Django chat view on each tool call.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    """Immutable snapshot of tenant context for tool handlers."""

    tenant_id: str
    user_id: str
    provider: str
    schema_name: str
    oauth_tokens: dict[str, str]
    max_rows_per_query: int = 500
    max_query_timeout_seconds: int = 30
```

**Step 2: Update server.py tools**

Replace all tools in `mcp_server/server.py` to accept no parameters (tenant context comes from session/meta). For the vertical slice, update `list_tables`, `describe_table`, `get_metadata`, and `query` to work with tenant context.

Since FastMCP doesn't directly expose `_meta` to tool functions, the tenant context will need to be injected via a different mechanism. For the vertical slice, add a `tenant_id` parameter to each tool (the agent passes it based on the system prompt, similar to how `project_id` works today). The `_meta` injection can be refined later.

Update each tool to accept `tenant_id: str` instead of `project_id: str`, and load the TenantSchema from the Django DB instead of loading a Project.

**Step 3: Add `run_materialization` tool stub**

```python
@mcp.tool()
async def run_materialization(tenant_id: str, pipeline: str = "commcare_sync") -> dict:
    """Materialize data from CommCare into the tenant's schema.

    Loads case data from the CommCare API and writes it to the tenant's
    schema in the managed database. Creates the schema if it doesn't exist.

    Args:
        tenant_id: The tenant identifier (CommCare domain name).
        pipeline: Pipeline to run (default: commcare_sync).
    """
    # Implementation in Task 7
    pass
```

**Step 4: Update envelope.py**

In `success_response`, change `project_id` parameter to `tenant_id`:

```python
def success_response(
    data: dict[str, Any],
    *,
    tenant_id: str,
    schema: str,
    timing_ms: int | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "success": True,
        "data": data,
        "tenant_id": tenant_id,
        "schema": schema,
    }
    # ... rest unchanged
```

**Step 5: Update tool_context in envelope.py**

Change `project_id` parameter to `tenant_id` in the `tool_context` context manager signature and audit log format.

**Step 6: Run tests and fix**

Run: `uv run pytest tests/ -k mcp -v`
Update any existing MCP tests that reference `project_id` to use `tenant_id`.

**Step 7: Commit**

```bash
git add mcp_server/
git commit -m "feat: replace project_id with tenant_id in MCP tools"
```

---

## Task 7: CommCare Case Loader + Materialization

**Files:**
- Create: `mcp_server/loaders/__init__.py`
- Create: `mcp_server/loaders/commcare_cases.py`
- Create: `mcp_server/services/materializer.py`
- Test: `tests/test_commcare_loader.py`

**Step 1: Write the loader test**

```python
# tests/test_commcare_loader.py
import pytest
from unittest.mock import patch, MagicMock
from mcp_server.loaders.commcare_cases import CommCareCaseLoader


class TestCommCareCaseLoader:
    def test_fetches_and_returns_cases(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "meta": {"next": None, "total_count": 2},
            "objects": [
                {"case_id": "abc", "case_type": "patient", "properties": {"name": "Alice"}},
                {"case_id": "def", "case_type": "patient", "properties": {"name": "Bob"}},
            ],
        }

        with patch("mcp_server.loaders.commcare_cases.requests.get", return_value=mock_response):
            loader = CommCareCaseLoader(domain="dimagi", access_token="fake-token")
            cases = loader.load()

        assert len(cases) == 2
        assert cases[0]["case_id"] == "abc"

    def test_paginates(self):
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "meta": {"next": "https://www.commcarehq.org/api/v0.5/case/?offset=2", "total_count": 3},
            "objects": [{"case_id": "1"}, {"case_id": "2"}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "meta": {"next": None, "total_count": 3},
            "objects": [{"case_id": "3"}],
        }

        with patch("mcp_server.loaders.commcare_cases.requests.get", side_effect=[page1, page2]):
            loader = CommCareCaseLoader(domain="dimagi", access_token="fake-token")
            cases = loader.load()

        assert len(cases) == 3
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_commcare_loader.py -v`

**Step 3: Write the loader**

```python
# mcp_server/loaders/__init__.py
# (empty)

# mcp_server/loaders/commcare_cases.py
"""CommCare case loader — fetches case data from the CommCare HQ REST API."""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareCaseLoader:
    """Loads case records from CommCare HQ for a given domain."""

    def __init__(self, domain: str, access_token: str):
        self.domain = domain
        self.access_token = access_token
        self.base_url = f"{COMMCARE_API_BASE}/a/{domain}/api/v0.5/case/"

    def load(self) -> list[dict]:
        """Fetch all cases from the CommCare API (paginated)."""
        results = []
        url = self.base_url
        params = {"format": "json", "limit": 100}

        while url:
            resp = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("objects", []))

            url = data.get("meta", {}).get("next")
            params = {}  # next URL includes params

            logger.info(
                "Loaded %d/%d cases for domain %s",
                len(results),
                data.get("meta", {}).get("total_count", "?"),
                self.domain,
            )

        return results
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_commcare_loader.py -v`

**Step 5: Write the materializer service**

```python
# mcp_server/services/materializer.py
"""
Simplified materializer for the vertical slice.

Loads CommCare case data and writes it to raw tables in the tenant's schema.
No DBT transforms — the raw table IS the queryable table for now.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import psycopg2
from psycopg2 import sql as psql

from apps.projects.services.schema_manager import SchemaManager, get_managed_db_connection
from mcp_server.loaders.commcare_cases import CommCareCaseLoader

logger = logging.getLogger(__name__)


def run_commcare_sync(tenant_membership, access_token: str) -> dict:
    """Load CommCare cases into the tenant's schema.

    Returns a summary dict with row counts and status.
    """
    # 1. Provision schema
    mgr = SchemaManager()
    tenant_schema = mgr.provision(tenant_membership)
    schema_name = tenant_schema.schema_name

    # 2. Load cases from CommCare
    loader = CommCareCaseLoader(
        domain=tenant_membership.tenant_id,
        access_token=access_token,
    )
    cases = loader.load()

    if not cases:
        return {"status": "completed", "rows_loaded": 0, "schema": schema_name}

    # 3. Write to managed DB
    conn = get_managed_db_connection()
    try:
        cursor = conn.cursor()
        schema_id = psql.Identifier(schema_name)

        # Create cases table (replace if exists)
        cursor.execute(psql.SQL("DROP TABLE IF EXISTS {}.cases").format(schema_id))
        cursor.execute(psql.SQL("""
            CREATE TABLE {schema}.cases (
                case_id TEXT PRIMARY KEY,
                case_type TEXT,
                owner_id TEXT,
                date_opened TEXT,
                date_modified TEXT,
                closed BOOLEAN DEFAULT FALSE,
                properties JSONB DEFAULT '{{}}'::jsonb
            )
        """).format(schema=schema_id))

        # Insert rows
        for case in cases:
            props = case.get("properties", {})
            cursor.execute(
                psql.SQL("""
                    INSERT INTO {schema}.cases (case_id, case_type, owner_id, date_opened, date_modified, closed, properties)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (case_id) DO UPDATE SET
                        properties = EXCLUDED.properties,
                        date_modified = EXCLUDED.date_modified,
                        closed = EXCLUDED.closed
                """).format(schema=schema_id),
                (
                    case.get("case_id"),
                    case.get("case_type", ""),
                    case.get("owner_id", ""),
                    case.get("date_opened", ""),
                    case.get("date_modified", ""),
                    case.get("closed", False),
                    json.dumps(props),
                ),
            )

        cursor.close()
    finally:
        conn.close()

    # 4. Update materialization record
    from apps.projects.models import MaterializationRun
    run = MaterializationRun.objects.create(
        tenant_schema=tenant_schema,
        pipeline="commcare_sync",
        state="completed",
        completed_at=datetime.now(UTC),
        result={"rows_loaded": len(cases), "table": "cases"},
    )

    tenant_schema.state = "active"
    tenant_schema.save(update_fields=["state", "last_accessed_at"])

    logger.info("Materialized %d cases into schema '%s'", len(cases), schema_name)

    return {
        "status": "completed",
        "run_id": str(run.id),
        "rows_loaded": len(cases),
        "schema": schema_name,
        "table": "cases",
    }
```

**Step 6: Commit**

```bash
git add mcp_server/loaders/ mcp_server/services/materializer.py tests/test_commcare_loader.py
git commit -m "feat: add CommCare case loader and materializer"
```

---

## Task 8: Wire run_materialization MCP Tool

**Files:**
- Modify: `mcp_server/server.py` (implement run_materialization)
- Test: `tests/test_mcp_materialization.py`

**Step 1: Implement the tool in server.py**

```python
@mcp.tool()
async def run_materialization(tenant_id: str, pipeline: str = "commcare_sync") -> dict:
    """Materialize data from CommCare into the tenant's schema.

    Loads case data from the CommCare API and writes it to the tenant's
    schema in the managed database. Creates the schema if it doesn't exist.

    Args:
        tenant_id: The tenant identifier (CommCare domain name).
        pipeline: Pipeline to run (default: commcare_sync).
    """
    async with tool_context("run_materialization", tenant_id, pipeline=pipeline) as tc:
        from apps.users.models import TenantMembership
        from apps.agents.mcp_client import get_user_oauth_tokens

        # Find tenant membership
        try:
            tm = await TenantMembership.objects.aget(tenant_id=tenant_id, provider="commcare")
        except TenantMembership.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Tenant '{tenant_id}' not found")
            return tc["result"]

        # Get OAuth token — for now, use the token from the first matching social account
        from allauth.socialaccount.models import SocialToken
        token_obj = await SocialToken.objects.filter(
            account__user=tm.user,
            account__provider="commcare",
        ).afirst()
        if not token_obj:
            tc["result"] = error_response("AUTH_TOKEN_MISSING", "No CommCare OAuth token found")
            return tc["result"]

        # Run materialization (sync, wrapped in sync_to_async)
        from asgiref.sync import sync_to_async
        from mcp_server.services.materializer import run_commcare_sync

        try:
            result = await sync_to_async(run_commcare_sync)(tm, token_obj.token)
        except Exception as e:
            logger.exception("Materialization failed for tenant %s", tenant_id)
            tc["result"] = error_response(INTERNAL_ERROR, f"Materialization failed: {e}")
            return tc["result"]

        tc["result"] = success_response(
            result,
            tenant_id=tenant_id,
            schema=result.get("schema", ""),
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Step 2: Commit**

```bash
git add mcp_server/server.py
git commit -m "feat: implement run_materialization MCP tool"
```

---

## Task 9: Update Chat View for Tenant Context

**Files:**
- Modify: `apps/chat/views.py` (replace project resolution with tenant resolution)
- Modify: `apps/agents/graph/base.py` (update build_agent_graph signature)
- Modify: `apps/agents/graph/state.py` (replace project_id with tenant_id in AgentState)

**Step 1: Update AgentState**

In `apps/agents/graph/state.py`, replace project fields:

```python
# Replace these fields:
#     project_id: str
#     project_name: str
# With:
    tenant_id: str
    tenant_name: str
```

Keep `user_id` and `user_role` (set role to "analyst" for all tenant users for now).

**Step 2: Update build_agent_graph**

In `apps/agents/graph/base.py`:
- Change function signature: replace `project` parameter with `tenant_membership`
- Update `_build_system_prompt` to use tenant info instead of project
- Update the Query Configuration section in the system prompt to reference `tenant_id` instead of `project_id`

**Step 3: Update chat_view**

In `apps/chat/views.py`:
- Replace `project_id` extraction with `tenantId` from request body
- Replace `_get_membership()` with tenant membership lookup
- Update `input_state` to use `tenant_id`/`tenant_name`

Key change in `chat_view`:
```python
# Replace:
#     project_id = data.get("projectId") or body.get("projectId")
# With:
    tenant_id = data.get("tenantId") or body.get("tenantId")

# Replace membership check with:
    from apps.users.models import TenantMembership
    try:
        tenant_membership = await TenantMembership.objects.aget(
            id=tenant_id, user=user
        )
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found or access denied"}, status=403)
```

Update `input_state`:
```python
    input_state = {
        "messages": [HumanMessage(content=user_content)],
        "tenant_id": tenant_membership.tenant_id,
        "tenant_name": tenant_membership.tenant_name,
        "user_id": str(user.id),
        "user_role": "analyst",
        ...
    }
```

**Step 4: Run tests and fix**

Run: `uv run pytest tests/ -v`
Fix any tests that reference the old project-based flow.

**Step 5: Commit**

```bash
git add apps/chat/views.py apps/agents/graph/base.py apps/agents/graph/state.py
git commit -m "feat: update chat view and agent to use tenant context"
```

---

## Task 10: Frontend — Domain Selector

**Files:**
- Create: `frontend/src/store/domainSlice.ts`
- Modify: `frontend/src/store/store.ts` (add domain slice)
- Modify: `frontend/src/components/Sidebar/Sidebar.tsx` (replace project selector)
- Modify: `frontend/src/components/ChatPanel/ChatPanel.tsx` (send tenantId)

**Step 1: Create domainSlice**

```typescript
// frontend/src/store/domainSlice.ts
import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface TenantMembership {
  id: string
  provider: string
  tenant_id: string
  tenant_name: string
  last_selected_at: string | null
}

export type DomainsStatus = "idle" | "loading" | "loaded" | "error"

export interface DomainSlice {
  domains: TenantMembership[]
  activeDomainId: string | null
  domainsStatus: DomainsStatus
  domainsError: string | null
  domainActions: {
    fetchDomains: () => Promise<void>
    setActiveDomain: (id: string) => void
  }
}

export const createDomainSlice: StateCreator<DomainSlice, [], [], DomainSlice> = (set, get) => ({
  domains: [],
  activeDomainId: null,
  domainsStatus: "idle",
  domainsError: null,
  domainActions: {
    fetchDomains: async () => {
      set({ domainsStatus: "loading", domainsError: null })
      try {
        const domains = await api.get<TenantMembership[]>("/api/auth/tenants/")
        const activeDomainId = get().activeDomainId
        set({
          domains,
          domainsStatus: "loaded",
          domainsError: null,
          activeDomainId: activeDomainId ?? (domains[0]?.id ?? null),
        })
        // Mark as selected on backend
        const selected = activeDomainId ?? domains[0]?.id
        if (selected) {
          api.post("/api/auth/tenants/select/", { tenant_id: selected }).catch(() => {})
        }
      } catch (error) {
        set({
          domainsStatus: "error",
          domainsError: error instanceof Error ? error.message : "Failed to load domains",
        })
      }
    },

    setActiveDomain: (id: string) => {
      set({ activeDomainId: id })
      api.post("/api/auth/tenants/select/", { tenant_id: id }).catch(() => {})
    },
  },
})
```

**Step 2: Add to store.ts**

```typescript
// Add import
import { createDomainSlice, type DomainSlice } from "./domainSlice"

// Update AppStore type
export type AppStore = AuthSlice & ProjectSlice & UiSlice & DictionarySlice & KnowledgeSlice & RecipeSlice & DomainSlice

// Add to create call
export const useAppStore = create<AppStore>()((...a) => ({
  ...createAuthSlice(...a),
  ...createProjectSlice(...a),
  ...createUiSlice(...a),
  ...createDictionarySlice(...a),
  ...createKnowledgeSlice(...a),
  ...createRecipeSlice(...a),
  ...createDomainSlice(...a),
}))
```

**Step 3: Update Sidebar.tsx**

Replace the Project Selector section (lines 53-80) with:

```tsx
{/* Domain Selector */}
<div className="border-b p-4">
  <label className="text-xs font-medium text-muted-foreground">
    Domain
  </label>
  <Select
    value={activeDomainId ?? ""}
    onValueChange={(value) => { setActiveDomain(value); newThread() }}
  >
    <SelectTrigger className="mt-1 w-full" data-testid="domain-selector">
      <SelectValue placeholder="Select domain" />
    </SelectTrigger>
    <SelectContent>
      {domains.map((d) => (
        <SelectItem key={d.id} value={d.id} data-testid={`domain-item-${d.tenant_id}`}>
          {d.tenant_name}
        </SelectItem>
      ))}
    </SelectContent>
  </Select>
</div>
```

Update the store selectors at the top of the component to use domain slice:
```tsx
const domains = useAppStore((s) => s.domains)
const activeDomainId = useAppStore((s) => s.activeDomainId)
const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
```

Add useEffect to fetch domains on mount:
```tsx
useEffect(() => {
  fetchDomains()
}, [fetchDomains])
```

Update thread fetching to use activeDomainId:
```tsx
useEffect(() => {
  if (activeDomainId) {
    fetchThreads(activeDomainId)
  }
}, [activeDomainId, fetchThreads])
```

**Step 4: Update ChatPanel.tsx**

Replace `activeProjectId` with `activeDomainId`:

```tsx
const activeDomainId = useAppStore((s) => s.activeDomainId)
// ...
const contextRef = useRef({ tenantId: activeDomainId, threadId })
contextRef.current = { tenantId: activeDomainId, threadId }
```

The transport body already sends `data: contextRef.current`, so it will now send `{ tenantId, threadId }` instead of `{ projectId, threadId }`.

Update the "no selection" guard:
```tsx
if (!activeDomainId) {
  return (
    <div className="flex-1 flex items-center justify-center text-muted-foreground">
      Select a domain to start chatting
    </div>
  )
}
```

**Step 5: Run frontend lint**

Run: `cd frontend && bun run lint`

**Step 6: Commit**

```bash
git add frontend/src/store/domainSlice.ts frontend/src/store/store.ts frontend/src/components/Sidebar/Sidebar.tsx frontend/src/components/ChatPanel/ChatPanel.tsx
git commit -m "feat: add domain selector UI, replace project selector"
```

---

## Task 11: Integration Test

**Files:**
- Create: `tests/test_tenant_flow_integration.py`

Write a test that exercises the full flow:
1. Create a user with a TenantMembership
2. POST to `/api/chat/` with `tenantId`
3. Verify the agent receives tenant context

```python
# tests/test_tenant_flow_integration.py
import pytest
from django.test import AsyncClient
from apps.users.models import TenantMembership


@pytest.mark.django_db
class TestTenantChatFlow:
    @pytest.mark.asyncio
    async def test_chat_with_tenant_context(self, user):
        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )

        client = AsyncClient()
        client.force_login(user)

        response = await client.post(
            "/api/chat/",
            data={
                "messages": [{"role": "user", "content": "Hello"}],
                "data": {"tenantId": str(tm.id)},
            },
            content_type="application/json",
        )

        # Should get a streaming response (200) or specific error,
        # but NOT "projectId is required" (old behavior)
        assert response.status_code != 400 or "projectId" not in response.json().get("error", "")
```

Run: `uv run pytest tests/test_tenant_flow_integration.py -v`

Commit:
```bash
git add tests/test_tenant_flow_integration.py
git commit -m "test: add tenant chat flow integration test"
```

---

## Summary

| Task | What | Key Files |
|------|------|-----------|
| 1 | TenantMembership model | `apps/users/models.py` |
| 2 | CommCare domain resolution | `apps/users/services/tenant_resolution.py` |
| 3 | Tenant REST API | `apps/users/views.py`, URL config |
| 4 | Managed DB config | `config/settings/base.py` |
| 5 | TenantSchema + SchemaManager | `apps/projects/models.py`, `apps/projects/services/schema_manager.py` |
| 6 | MCP tools → tenant context | `mcp_server/context.py`, `mcp_server/server.py` |
| 7 | CommCare loader + materializer | `mcp_server/loaders/`, `mcp_server/services/materializer.py` |
| 8 | run_materialization tool | `mcp_server/server.py` |
| 9 | Chat view → tenant context | `apps/chat/views.py`, `apps/agents/graph/` |
| 10 | Frontend domain selector | `frontend/src/store/domainSlice.ts`, Sidebar, ChatPanel |
| 11 | Integration test | `tests/test_tenant_flow_integration.py` |
