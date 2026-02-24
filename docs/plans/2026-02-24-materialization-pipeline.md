# Materialization Pipeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement a full three-phase (Discover → Load → Transform) materialization pipeline with a YAML-based pipeline registry, CommCare loaders (cases + forms), MCP progress notifications, and new MCP tools (list_pipelines, get_materialization_status, cancel_materialization).

**Architecture:** Pipeline definitions live in `pipelines/*.yml`. The existing `run_commcare_sync` in `mcp_server/services/materializer.py` is replaced by a `run_pipeline()` function that reads from the registry and runs three phases with progress callbacks. The `run_materialization` MCP tool in `server.py` gains a `ctx: Context` parameter and wires async progress notifications using `asyncio.run_coroutine_threadsafe`. A new generic `TenantMetadata` Django model (backed by `django-pydantic-field`) stores discovered provider metadata in the platform DB — it persists across schema teardowns. DBT is invoked via the `dbtRunner` Python API (no subprocess). Cancellation is basic (stub) in this plan.

**Key design notes:**
- `TenantMetadata` is provider-agnostic — CommCare metadata is one `dict[str, Any]` stored in a typed JSON field; the Pydantic schema lives with the loader, not the model.
- CommCare forms can create/update multiple cases, with case blocks nested anywhere in the form JSON. The forms loader extracts all case references from the nested structure.
- No users loader — CommCare user data is out of scope.
- DBT runs via `dbtRunner` (programmatic API), not subprocess.
- All CommCare loaders share `mcp_server/loaders/commcare_base.py` for auth, `HTTP_TIMEOUT`, and a `requests.Session`-based base class.
- Loaders expose `load_pages()` iterators; the materializer streams page-by-page so the full dataset is never held in memory.
- All source writes share a single psycopg2 connection and are committed in one transaction; a mid-run failure rolls back all sources atomically.
- Transform (DBT) failures are isolated — the run is marked COMPLETED even if transforms fail; errors are stored in `result["transform_error"]`.
- `dbtRunner` is not thread-safe; a module-level `threading.Lock` serialises all in-process invocations.
- Progress notification futures get a done-callback to log any silent delivery failures.

**Tech Stack:** Python, FastMCP (via `mcp.server.fastmcp`), Django 5, psycopg2, requests, pytest, PyYAML, django-pydantic-field, dbt-core + dbt-postgres

**Deferred:** subprocess-based cancellation, Celery background workers, network isolation for loaders.

**Test command:** `uv run pytest tests/ -x`

---

## Task 1: Pipeline Registry — YAML Definition + Loader Class

**Files:**
- Create: `pipelines/commcare_sync.yml`
- Create: `mcp_server/pipeline_registry.py`
- Test: `tests/test_pipeline_registry.py`

**Step 1: Write the failing test**

```python
# tests/test_pipeline_registry.py
import pytest
from mcp_server.pipeline_registry import PipelineRegistry, PipelineConfig, SourceConfig


class TestPipelineRegistry:
    def test_loads_commcare_sync_pipeline(self, tmp_path):
        yml = tmp_path / "commcare_sync.yml"
        yml.write_text("""
pipeline: commcare_sync
description: "Sync case and form data from CommCare HQ"
version: "1.0"
provider: commcare
sources:
  - name: cases
    loader: loaders/commcare/cases.py
    description: "CommCare case records"
  - name: forms
    loader: loaders/commcare/forms.py
    description: "CommCare form submission records"
metadata_discovery:
  loader: loaders/commcare/metadata.py
  description: "Extract application structure"
transforms:
  dbt_project: transforms/commcare
  models:
    - stg_cases
    - stg_forms
""")
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        config = registry.get("commcare_sync")
        assert config is not None
        assert config.name == "commcare_sync"
        assert config.description == "Sync case and form data from CommCare HQ"
        assert config.provider == "commcare"
        assert len(config.sources) == 2
        assert config.sources[0].name == "cases"
        assert config.sources[1].name == "forms"
        assert config.has_metadata_discovery is True
        assert config.dbt_models == ["stg_cases", "stg_forms"]

    def test_list_returns_all_pipelines(self, tmp_path):
        (tmp_path / "a.yml").write_text("pipeline: a\ndescription: A\nversion: '1.0'\nprovider: commcare\nsources: []\n")
        (tmp_path / "b.yml").write_text("pipeline: b\ndescription: B\nversion: '1.0'\nprovider: commcare\nsources: []\n")
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        names = [p.name for p in registry.list()]
        assert "a" in names and "b" in names

    def test_get_unknown_pipeline_returns_none(self, tmp_path):
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        assert registry.get("nonexistent") is None
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_pipeline_registry.py -v
```
Expected: `ModuleNotFoundError: No module named 'mcp_server.pipeline_registry'`

**Step 3: Create the YAML pipeline definition**

```yaml
# pipelines/commcare_sync.yml
pipeline: commcare_sync
description: "Sync case and form data from CommCare HQ"
version: "1.0"
provider: commcare

sources:
  - name: cases
    loader: loaders/commcare/cases.py
    description: "CommCare case records"
  - name: forms
    loader: loaders/commcare/forms.py
    description: "CommCare form submission records (includes nested case updates)"

metadata_discovery:
  loader: loaders/commcare/metadata.py
  description: "Extract application structure, case types, and form definitions"

transforms:
  dbt_project: transforms/commcare
  target_schema: "{{ schema_name }}"
  models:
    - stg_cases
    - stg_forms
```

**Step 4: Implement pipeline_registry.py**

```python
# mcp_server/pipeline_registry.py
"""YAML-based pipeline registry for materialization pipelines."""
from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PIPELINES_DIR = pathlib.Path(__file__).parent.parent / "pipelines"


@dataclass
class SourceConfig:
    name: str
    loader: str
    description: str = ""


@dataclass
class MetadataDiscoveryConfig:
    loader: str
    description: str = ""


@dataclass
class TransformConfig:
    dbt_project: str
    models: list[str] = field(default_factory=list)
    target_schema: str = "{{ schema_name }}"


@dataclass
class PipelineConfig:
    name: str
    description: str
    version: str
    provider: str
    sources: list[SourceConfig] = field(default_factory=list)
    metadata_discovery: MetadataDiscoveryConfig | None = None
    transforms: TransformConfig | None = None

    @property
    def has_metadata_discovery(self) -> bool:
        return self.metadata_discovery is not None

    @property
    def dbt_models(self) -> list[str]:
        return self.transforms.models if self.transforms else []


class PipelineRegistry:
    """Loads and caches pipeline definitions from YAML files."""

    def __init__(self, pipelines_dir: str | None = None) -> None:
        self._dir = pathlib.Path(pipelines_dir) if pipelines_dir else _DEFAULT_PIPELINES_DIR
        self._cache: dict[str, PipelineConfig] | None = None

    def _load_all(self) -> dict[str, PipelineConfig]:
        if self._cache is not None:
            return self._cache
        configs: dict[str, PipelineConfig] = {}
        for path in self._dir.glob("*.yml"):
            try:
                with path.open() as f:
                    data = yaml.safe_load(f)
                config = _parse_pipeline(data)
                configs[config.name] = config
            except Exception:
                logger.exception("Failed to load pipeline from %s", path)
        self._cache = configs
        return configs

    def get(self, name: str) -> PipelineConfig | None:
        return self._load_all().get(name)

    def list(self) -> list[PipelineConfig]:
        return list(self._load_all().values())


def _parse_pipeline(data: dict) -> PipelineConfig:
    sources = [
        SourceConfig(name=s["name"], loader=s["loader"], description=s.get("description", ""))
        for s in data.get("sources", [])
    ]
    md_raw = data.get("metadata_discovery")
    metadata_discovery = (
        MetadataDiscoveryConfig(loader=md_raw["loader"], description=md_raw.get("description", ""))
        if md_raw
        else None
    )
    tr_raw = data.get("transforms")
    transforms = (
        TransformConfig(
            dbt_project=tr_raw["dbt_project"],
            models=tr_raw.get("models", []),
            target_schema=tr_raw.get("target_schema", "{{ schema_name }}"),
        )
        if tr_raw
        else None
    )
    return PipelineConfig(
        name=data["pipeline"],
        description=data.get("description", ""),
        version=data.get("version", "1.0"),
        provider=data.get("provider", "commcare"),
        sources=sources,
        metadata_discovery=metadata_discovery,
        transforms=transforms,
    )


_registry: PipelineRegistry | None = None


def get_registry() -> PipelineRegistry:
    global _registry
    if _registry is None:
        _registry = PipelineRegistry()
    return _registry
```

**Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_pipeline_registry.py -v
```
Expected: all 3 tests PASS

**Step 6: Commit**

```bash
git add pipelines/commcare_sync.yml mcp_server/pipeline_registry.py tests/test_pipeline_registry.py
git commit -m "feat: YAML pipeline registry with PipelineConfig dataclasses"
```

---

## Task 2: `list_pipelines` MCP Tool

**Files:**
- Modify: `mcp_server/server.py`
- Test: `tests/test_mcp_tenant_tools.py`

**Step 1: Write the failing test**

Add at the end of `tests/test_mcp_tenant_tools.py`:

```python
class TestListPipelines:
    def test_returns_available_pipelines(self):
        import asyncio
        from unittest.mock import patch
        from mcp_server.pipeline_registry import PipelineConfig

        fake_pipelines = [
            PipelineConfig(
                name="commcare_sync",
                description="Sync case and form data from CommCare HQ",
                version="1.0",
                provider="commcare",
            )
        ]
        with patch("mcp_server.server.get_registry") as mock_reg:
            mock_reg.return_value.list.return_value = fake_pipelines
            from mcp_server.server import list_pipelines
            result = asyncio.run(list_pipelines())

        assert result["success"] is True
        assert len(result["data"]["pipelines"]) == 1
        assert result["data"]["pipelines"][0]["name"] == "commcare_sync"
        assert result["data"]["pipelines"][0]["provider"] == "commcare"
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestListPipelines -v
```
Expected: `AttributeError` — `list_pipelines` not defined on server

**Step 3: Add to server.py**

Add import near the top of `mcp_server/server.py`:
```python
from mcp_server.pipeline_registry import get_registry
```

Add the tool before `run_materialization`:

```python
@mcp.tool()
async def list_pipelines() -> dict:
    """List available materialization pipelines and their descriptions.

    Returns the registry of pipelines that can be run via run_materialization.
    Each entry includes the pipeline name, description, provider, sources, and DBT models.
    """
    async with tool_context("list_pipelines", "") as tc:
        registry = get_registry()
        pipelines = [
            {
                "name": p.name,
                "description": p.description,
                "provider": p.provider,
                "version": p.version,
                "sources": [{"name": s.name, "description": s.description} for s in p.sources],
                "has_metadata_discovery": p.has_metadata_discovery,
                "dbt_models": p.dbt_models,
            }
            for p in registry.list()
        ]
        tc["result"] = success_response(
            {"pipelines": pipelines},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestListPipelines tests/ -x -q
```
Expected: all pass

**Step 5: Commit**

```bash
git add mcp_server/server.py
git commit -m "feat: list_pipelines MCP tool — exposes pipeline registry to agents"
```

---

## Task 3: Generic TenantMetadata Model + django-pydantic-field

Provider metadata is stored as typed JSON. The model is completely provider-agnostic — CommCare structure is a concern of the loader, not the model.

**Files:**
- Install: `django-pydantic-field`
- Modify: `apps/projects/models.py`
- Create: `apps/projects/migrations/0014_tenantmetadata.py` (auto-generated)
- Test: `tests/test_models.py`

**Step 1: Install django-pydantic-field**

```bash
uv add django-pydantic-field
```

Verify it works:
```bash
uv run python -c "from django_pydantic_field import SchemaField; print('ok')"
```

**Step 2: Write the failing test**

Add to `tests/test_models.py`:

```python
@pytest.mark.django_db
class TestTenantMetadata:
    def test_create_and_retrieve_metadata(self, tenant_membership):
        from apps.projects.models import TenantMetadata
        from django.utils import timezone

        payload = {
            "case_types": ["patient", "household"],
            "app_definitions": [{"id": "abc", "name": "CHW App"}],
        }
        meta = TenantMetadata.objects.create(
            tenant_membership=tenant_membership,
            metadata=payload,
            discovered_at=timezone.now(),
        )
        retrieved = TenantMetadata.objects.get(pk=meta.pk)
        assert retrieved.metadata["case_types"] == ["patient", "household"]
        assert retrieved.metadata["app_definitions"][0]["id"] == "abc"

    def test_one_to_one_with_tenant_membership(self, tenant_membership):
        from apps.projects.models import TenantMetadata
        TenantMetadata.objects.create(tenant_membership=tenant_membership)
        with pytest.raises(Exception):
            TenantMetadata.objects.create(tenant_membership=tenant_membership)

    def test_metadata_defaults_to_empty_dict(self, tenant_membership):
        from apps.projects.models import TenantMetadata
        meta = TenantMetadata.objects.create(tenant_membership=tenant_membership)
        assert meta.metadata == {}
```

Check `tests/conftest.py` for an existing `tenant_membership` fixture. If it doesn't exist, add one:

```python
# In tests/conftest.py — add if missing:
@pytest.fixture
def tenant_membership(db):
    from apps.users.models import User, TenantMembership
    user = User.objects.create_user(email="test@example.com", password="pass")
    return TenantMembership.objects.create(
        user=user,
        provider="commcare",
        tenant_id="dimagi",
        tenant_name="Dimagi",
    )
```

**Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_models.py::TestTenantMetadata -v
```
Expected: `ImportError` — `TenantMetadata` not defined

**Step 4: Add TenantMetadata to models.py**

In `apps/projects/models.py`, add at the top:
```python
from django_pydantic_field import SchemaField
```

Add the model after `MaterializationRun`:

```python
class TenantMetadata(models.Model):
    """Generic provider metadata discovered during the materialize/discover phase.

    Completely provider-agnostic — each provider stores whatever structure it needs
    in the ``metadata`` JSON field. Survives schema teardown so re-provisioning can
    skip re-discovery if the data is still current.
    """

    tenant_membership = models.OneToOneField(
        "users.TenantMembership",
        on_delete=models.CASCADE,
        related_name="metadata",
    )
    metadata: dict = SchemaField(
        schema=dict,
        default=dict,
        help_text="Provider-specific metadata blob. Structure defined by the loader.",
    )
    discovered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this metadata was last successfully fetched from the provider",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tenant Metadata"
        verbose_name_plural = "Tenant Metadata"

    def __str__(self) -> str:
        return f"Metadata for {self.tenant_membership.tenant_id}"
```

**Step 5: Generate and apply migration**

```bash
uv run python manage.py makemigrations projects --name tenantmetadata
uv run python manage.py migrate
```

**Step 6: Run tests**

```bash
uv run pytest tests/test_models.py::TestTenantMetadata -v
```
Expected: all 3 PASS

**Step 7: Commit**

```bash
git add apps/projects/models.py apps/projects/migrations/0014_tenantmetadata.py pyproject.toml uv.lock
git commit -m "feat: generic TenantMetadata model with django-pydantic-field"
```

---

## Task 4: CommCare Base Loader + Update Cases Loader

Create `mcp_server/loaders/commcare_base.py` with shared auth, `HTTP_TIMEOUT`, and a `requests.Session`-based base class. Update `CommCareCaseLoader` to use it and expose a `load_pages()` iterator for streaming.

**Files:**
- Create: `mcp_server/loaders/commcare_base.py`
- Modify: `mcp_server/loaders/commcare_cases.py`
- Test: `tests/test_commcare_loader.py` (update + extend)

**Step 1: Write the failing test**

Add to `tests/test_commcare_loader.py`:

```python
class TestCommCareBaseLoader:
    def test_build_auth_header_api_key(self):
        from mcp_server.loaders.commcare_base import build_auth_header
        h = build_auth_header({"type": "api_key", "value": "user@example.com:abc"})
        assert h["Authorization"] == "ApiKey user@example.com:abc"

    def test_build_auth_header_oauth(self):
        from mcp_server.loaders.commcare_base import build_auth_header
        h = build_auth_header({"type": "oauth", "value": "tok123"})
        assert h["Authorization"] == "Bearer tok123"

    def test_http_timeout_is_tuple(self):
        from mcp_server.loaders.commcare_base import HTTP_TIMEOUT
        assert isinstance(HTTP_TIMEOUT, tuple)
        assert len(HTTP_TIMEOUT) == 2


class TestCaseLoaderLoadPages:
    def test_load_pages_yields_pages(self):
        from unittest.mock import MagicMock, patch
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "next": "https://www.commcarehq.org/a/dimagi/api/case/v2/?cursor=x",
            "cases": [{"case_id": "c1"}, {"case_id": "c2"}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {"next": None, "cases": [{"case_id": "c3"}]}

        with patch("mcp_server.loaders.commcare_base.requests.Session") as mock_session_cls:
            session = MagicMock()
            mock_session_cls.return_value = session
            session.get.side_effect = [page1, page2]

            loader = CommCareCaseLoader(
                domain="dimagi", credential={"type": "api_key", "value": "u:k"}
            )
            pages = list(loader.load_pages())

        assert len(pages) == 2
        assert len(pages[0]) == 2
        assert len(pages[1]) == 1

    def test_load_is_flat_list(self):
        from unittest.mock import MagicMock, patch
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "next": None,
            "cases": [{"case_id": "c1"}, {"case_id": "c2"}],
        }

        with patch("mcp_server.loaders.commcare_base.requests.Session") as mock_session_cls:
            session = MagicMock()
            mock_session_cls.return_value = session
            session.get.return_value = mock_resp

            loader = CommCareCaseLoader(
                domain="dimagi", credential={"type": "api_key", "value": "u:k"}
            )
            cases = loader.load()

        assert len(cases) == 2
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_commcare_loader.py::TestCommCareBaseLoader tests/test_commcare_loader.py::TestCaseLoaderLoadPages -v
```
Expected: `ModuleNotFoundError` for `commcare_base`

**Step 3: Create commcare_base.py**

```python
# mcp_server/loaders/commcare_base.py
"""Shared utilities for CommCare HQ API loaders.

All loaders should use CommCareBaseLoader as a base class so they share
a single requests.Session (HTTP connection pooling), consistent timeouts,
and a single auth-header builder.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

# (connect_timeout_seconds, read_timeout_seconds)
# Read timeout is generous: large CommCare domains may have slow API responses.
HTTP_TIMEOUT: tuple[int, int] = (10, 120)


class CommCareAuthError(Exception):
    """Raised when CommCare returns a 401 or 403 response."""


def build_auth_header(credential: dict[str, str]) -> dict[str, str]:
    """Return the Authorization header dict for a credential.

    Args:
        credential: {"type": "oauth"|"api_key", "value": str}
    """
    if credential.get("type") == "api_key":
        return {"Authorization": f"ApiKey {credential['value']}"}
    return {"Authorization": f"Bearer {credential['value']}"}


class CommCareBaseLoader:
    """Base class for CommCare HQ API loaders.

    Manages a persistent requests.Session (HTTP connection pooling) and
    applies consistent timeouts and auth headers to every request.
    """

    def __init__(self, domain: str, credential: dict[str, str]) -> None:
        self.domain = domain
        self._session = requests.Session()
        self._session.headers.update(build_auth_header(credential))

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET a URL, raising CommCareAuthError on 401/403."""
        resp = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise CommCareAuthError(
                f"CommCare auth failed for domain {self.domain}: HTTP {resp.status_code}"
            )
        resp.raise_for_status()
        return resp
```

**Step 4: Update commcare_cases.py**

Replace the existing `commcare_cases.py` with the version below. Key changes:
- Inherit from `CommCareBaseLoader` instead of managing auth inline.
- Re-export `CommCareAuthError` for backwards compatibility (other code imports it from here).
- Add `load_pages()` iterator; `load()` delegates to it.
- Use `self._get()` instead of `requests.get()`.

```python
# mcp_server/loaders/commcare_cases.py
"""Loader for CommCare case records (Case API v2)."""
from __future__ import annotations

import logging
from typing import Iterator

from mcp_server.loaders.commcare_base import CommCareAuthError, CommCareBaseLoader  # noqa: F401

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.commcarehq.org"
_DEFAULT_PAGE_SIZE = 1000


class CommCareCaseLoader(CommCareBaseLoader):
    """Loads CommCare case records from the Case API v2.

    Supports both ``load()`` (returns a flat list) and ``load_pages()``
    (yields one page at a time for streaming writes).
    """

    def __init__(
        self,
        domain: str,
        credential: dict[str, str] | None = None,
        access_token: str | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> None:
        # Support legacy ``access_token`` kwarg for backwards compatibility.
        if credential is None and access_token is not None:
            credential = {"type": "oauth", "value": access_token}
        elif credential is None:
            raise ValueError("Either credential or access_token is required")
        super().__init__(domain=domain, credential=credential)
        self.page_size = min(page_size, _DEFAULT_PAGE_SIZE)

    def load_pages(self) -> Iterator[list[dict]]:
        """Yield one page of cases at a time.

        Each page is a list of normalised case dicts. Prefer this over
        ``load()`` when writing to the DB to avoid holding all cases in memory.
        """
        url = f"{_BASE_URL}/a/{self.domain}/api/case/v2/"
        params: dict = {"limit": self.page_size}
        total_loaded = 0
        while url:
            data = self._get(url, params=params).json()
            cases = [_normalize_case(c) for c in data.get("cases", [])]
            if cases:
                total_loaded += len(cases)
                logger.info(
                    "Fetched %d cases (total so far: %d) for domain %s",
                    len(cases),
                    total_loaded,
                    self.domain,
                )
                yield cases
            url = data.get("next")
            params = {}

    def load(self) -> list[dict]:
        """Return all cases as a flat list (loads all pages into memory)."""
        return [case for page in self.load_pages() for case in page]


def _normalize_case(raw: dict) -> dict:
    return {
        "case_id": raw.get("case_id", ""),
        "case_type": raw.get("case_type", ""),
        "case_name": raw.get("case_name") or raw.get("properties", {}).get("case_name", ""),
        "external_id": raw.get("external_id", ""),
        "owner_id": raw.get("owner_id", ""),
        "date_opened": raw.get("date_opened", ""),
        "last_modified": raw.get("last_modified", ""),
        "server_last_modified": raw.get("server_last_modified", ""),
        "indexed_on": raw.get("indexed_on", ""),
        "closed": raw.get("closed", False),
        "date_closed": raw.get("date_closed") or "",
        "properties": raw.get("properties", {}),
        "indices": raw.get("indices", {}),
    }
```

**Step 5: Run all loader tests**

```bash
uv run pytest tests/test_commcare_loader.py -v
```
Expected: all pass (including the old `TestCommCareCaseLoader` tests — they should still work because `CommCareAuthError` is re-exported)

**Step 6: Commit**

```bash
git add mcp_server/loaders/commcare_base.py mcp_server/loaders/commcare_cases.py tests/test_commcare_loader.py
git commit -m "feat: CommCareBaseLoader with shared session, HTTP_TIMEOUT, build_auth_header"
```

---

## Task 5: CommCare Metadata Loader (Discover Phase)

Queries the CommCare Application API to discover case types, app structure, and form definitions. Returns a plain `dict` — the model stores it as-is.

**Files:**
- Create: `mcp_server/loaders/commcare_metadata.py`
- Test: `tests/test_commcare_metadata_loader.py`

**Step 1: Write the failing test**

```python
# tests/test_commcare_metadata_loader.py
from unittest.mock import MagicMock, patch
import pytest


def _make_app_response():
    return {
        "objects": [
            {
                "id": "app_abc",
                "name": "CHW App",
                "modules": [
                    {
                        "name": "Patient Registration",
                        "case_type": "patient",
                        "forms": [
                            {
                                "xmlns": "http://openrosa.org/formdesigner/form1",
                                "name": "Patient Registration",
                                "questions": [
                                    {"label": "Patient Name", "tag": "input", "value": "/data/name"},
                                    {"label": "Age", "tag": "input", "value": "/data/age"},
                                ],
                            }
                        ],
                    },
                    {
                        "name": "Household Visit",
                        "case_type": "household",
                        "forms": [],
                    },
                ],
            }
        ],
        "next": None,
    }


class TestCommCareMetadataLoader:
    def _mock_session(self, responses):
        """Return a patch context that intercepts Session().get() calls."""
        import unittest.mock as mock
        session = MagicMock()
        if isinstance(responses, list):
            session.get.side_effect = responses
        else:
            session.get.return_value = responses
        return mock.patch("mcp_server.loaders.commcare_base.requests.Session", return_value=session)

    def test_loads_app_definitions(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_app_response()

        with self._mock_session(mock_resp):
            loader = CommCareMetadataLoader(domain="dimagi", credential={"type": "api_key", "value": "user:key"})
            result = loader.load()

        assert result["app_definitions"][0]["id"] == "app_abc"
        assert result["app_definitions"][0]["name"] == "CHW App"

    def test_extracts_unique_case_types(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_app_response()

        with self._mock_session(mock_resp):
            loader = CommCareMetadataLoader(domain="dimagi", credential={"type": "api_key", "value": "user:key"})
            result = loader.load()

        case_type_names = [ct["name"] for ct in result["case_types"]]
        assert "patient" in case_type_names
        assert "household" in case_type_names
        assert len(case_type_names) == len(set(case_type_names))

    def test_extracts_form_definitions(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_app_response()

        with self._mock_session(mock_resp):
            loader = CommCareMetadataLoader(domain="dimagi", credential={"type": "api_key", "value": "user:key"})
            result = loader.load()

        form_defs = result["form_definitions"]
        assert "http://openrosa.org/formdesigner/form1" in form_defs
        assert form_defs["http://openrosa.org/formdesigner/form1"]["case_type"] == "patient"

    def test_raises_on_auth_failure(self):
        from mcp_server.loaders.commcare_base import CommCareAuthError
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with self._mock_session(mock_resp):
            # _get raises CommCareAuthError on 401/403
            with pytest.raises(CommCareAuthError):
                CommCareMetadataLoader(domain="dimagi", credential={"type": "api_key", "value": "bad"}).load()

    def test_paginates_apps(self):
        from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "objects": [{"id": "app1", "name": "App 1", "modules": []}],
            "next": "https://www.commcarehq.org/a/dimagi/api/v0.5/application/?offset=1",
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "objects": [{"id": "app2", "name": "App 2", "modules": []}],
            "next": None,
        }

        with self._mock_session([page1, page2]):
            loader = CommCareMetadataLoader(domain="dimagi", credential={"type": "api_key", "value": "user:key"})
            result = loader.load()

        assert len(result["app_definitions"]) == 2
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_commcare_metadata_loader.py -v
```

**Step 3: Implement CommCareMetadataLoader**

```python
# mcp_server/loaders/commcare_metadata.py
"""Metadata loader for CommCare HQ — discovers app structure, case types, form definitions."""
from __future__ import annotations

import logging

from mcp_server.loaders.commcare_base import CommCareBaseLoader

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.commcarehq.org"


class CommCareMetadataLoader(CommCareBaseLoader):
    """Discovers tenant metadata from CommCare HQ Application API.

    Returns a plain dict stored directly in TenantMetadata.metadata.
    Structure:
        {
            "app_definitions": [...],    # raw app JSON from CommCare API
            "case_types": [              # unique case types across all apps
                {"name": str, "app_id": str, "app_name": str, "module_name": str}
            ],
            "form_definitions": {        # keyed by xmlns
                "<xmlns>": {"name": str, "case_type": str, "questions": [...]}
            },
        }
    """

    def load(self) -> dict:
        apps = self._fetch_apps()
        case_types = _extract_case_types(apps)
        form_definitions = _extract_form_definitions(apps)
        logger.info(
            "Discovered %d apps, %d case types, %d forms for domain %s",
            len(apps),
            len(case_types),
            len(form_definitions),
            self.domain,
        )
        return {
            "app_definitions": apps,
            "case_types": case_types,
            "form_definitions": form_definitions,
        }

    def _fetch_apps(self) -> list[dict]:
        url = f"{_BASE_URL}/a/{self.domain}/api/v0.5/application/"
        params: dict = {"limit": 100}
        apps: list[dict] = []
        while url:
            data = self._get(url, params=params).json()
            apps.extend(data.get("objects", []))
            url = data.get("next")
            params = {}
        return apps


def _extract_case_types(apps: list[dict]) -> list[dict]:
    """Extract unique case types from application module definitions."""
    seen: set[str] = set()
    case_types: list[dict] = []
    for app in apps:
        for module in app.get("modules", []):
            ct = module.get("case_type", "")
            if ct and ct not in seen:
                seen.add(ct)
                case_types.append({
                    "name": ct,
                    "app_id": app.get("id", ""),
                    "app_name": app.get("name", ""),
                    "module_name": module.get("name", ""),
                })
    return case_types


def _extract_form_definitions(apps: list[dict]) -> dict[str, dict]:
    """Extract form definitions keyed by form xmlns."""
    forms: dict[str, dict] = {}
    for app in apps:
        for module in app.get("modules", []):
            for form in module.get("forms", []):
                xmlns = form.get("xmlns", "")
                if xmlns:
                    forms[xmlns] = {
                        "name": form.get("name", ""),
                        "case_type": module.get("case_type", ""),
                        "questions": form.get("questions", []),
                    }
    return forms
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_commcare_metadata_loader.py tests/test_commcare_loader.py -v
```
Expected: all pass

**Step 5: Commit**

```bash
git add mcp_server/loaders/commcare_metadata.py tests/test_commcare_metadata_loader.py
git commit -m "feat: CommCare metadata loader — app definitions, case types, form structure"
```

---

## Task 6: CommCare Forms Loader (with nested case extraction + streaming)

CommCare forms can create or update multiple cases. Case blocks are nested arbitrarily deep in the form JSON. The loader extracts all nested case references and exposes a `load_pages()` iterator for streaming writes.

**Files:**
- Create: `mcp_server/loaders/commcare_forms.py`
- Test: `tests/test_commcare_forms_loader.py`

**Step 1: Write the failing test**

```python
# tests/test_commcare_forms_loader.py
from unittest.mock import MagicMock, patch
import pytest


def _mock_session(responses):
    import unittest.mock as mock
    session = MagicMock()
    if isinstance(responses, list):
        session.get.side_effect = responses
    else:
        session.get.return_value = responses
    return mock.patch("mcp_server.loaders.commcare_base.requests.Session", return_value=session)


class TestCommCareFormLoader:
    def test_fetches_forms(self):
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "next": None,
            "meta": {"total_count": 2},
            "objects": [
                {"id": "f1", "form": {"@name": "Reg", "case": {"@case_id": "c1", "@action": "create"}}, "received_on": "2026-01-01"},
                {"id": "f2", "form": {"@name": "Follow"}, "received_on": "2026-01-02"},
            ],
        }

        with _mock_session(mock_resp):
            loader = CommCareFormLoader(domain="dimagi", credential={"type": "api_key", "value": "user:key"})
            forms = loader.load()

        assert len(forms) == 2
        assert forms[0]["form_id"] == "f1"

    def test_paginates(self):
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "next": "https://www.commcarehq.org/a/dimagi/api/v0.5/form/?cursor=x",
            "meta": {"total_count": 3},
            "objects": [{"id": "f1", "form": {}}, {"id": "f2", "form": {}}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "next": None, "meta": {"total_count": 3}, "objects": [{"id": "f3", "form": {}}]
        }

        with _mock_session([page1, page2]):
            forms = CommCareFormLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            ).load()

        assert len(forms) == 3

    def test_load_pages_yields_per_page(self):
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "next": "https://www.commcarehq.org/a/dimagi/api/v0.5/form/?cursor=x",
            "objects": [{"id": "f1", "form": {}}, {"id": "f2", "form": {}}],
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {"next": None, "objects": [{"id": "f3", "form": {}}]}

        with _mock_session([page1, page2]):
            pages = list(CommCareFormLoader(
                domain="dimagi", credential={"type": "api_key", "value": "user:key"}
            ).load_pages())

        assert len(pages) == 2
        assert len(pages[0]) == 2
        assert len(pages[1]) == 1

    def test_raises_on_auth_failure(self):
        from mcp_server.loaders.commcare_base import CommCareAuthError
        from mcp_server.loaders.commcare_forms import CommCareFormLoader

        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with _mock_session(mock_resp):
            with pytest.raises(CommCareAuthError):
                CommCareFormLoader(domain="dimagi", credential={"type": "api_key", "value": "bad"}).load()


class TestExtractCaseRefs:
    """Tests for the nested case-reference extractor."""

    def test_extracts_top_level_case(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs
        form_data = {"case": {"@case_id": "abc", "@action": "create", "update": {"name": "Alice"}}}
        refs = extract_case_refs(form_data)
        assert len(refs) == 1
        assert refs[0]["case_id"] == "abc"
        assert refs[0]["action"] == "create"

    def test_extracts_nested_case(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs
        form_data = {
            "name": "Alice",
            "child_group": {"case": {"@case_id": "child1", "@action": "update"}},
        }
        refs = extract_case_refs(form_data)
        assert len(refs) == 1
        assert refs[0]["case_id"] == "child1"

    def test_extracts_multiple_cases_from_repeat_group(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs
        form_data = {
            "repeat_item": [
                {"case": {"@case_id": "r1", "@action": "create"}},
                {"case": {"@case_id": "r2", "@action": "create"}},
            ]
        }
        refs = extract_case_refs(form_data)
        assert len(refs) == 2
        assert {r["case_id"] for r in refs} == {"r1", "r2"}

    def test_ignores_non_case_dicts(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs
        form_data = {"name": "test", "age": 30, "meta": {"timeEnd": "2026-01-01"}}
        assert extract_case_refs(form_data) == []

    def test_deduplicates_same_case_id(self):
        from mcp_server.loaders.commcare_forms import extract_case_refs
        form_data = {
            "case": {"@case_id": "same", "@action": "create"},
            "group": {"case": {"@case_id": "same", "@action": "update"}},
        }
        refs = extract_case_refs(form_data)
        assert [r["case_id"] for r in refs].count("same") == 1
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_commcare_forms_loader.py -v
```

**Step 3: Implement CommCareFormLoader**

```python
# mcp_server/loaders/commcare_forms.py
"""Loader for CommCare form submissions.

CommCare forms are complex: a single form submission can create or update multiple
cases. Case blocks may appear at any nesting depth in the form JSON (e.g.
``form.case``, ``form.group.case``, ``form.repeat[0].case``). The loader extracts
all case references from each form and stores them alongside the raw form data.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from mcp_server.loaders.commcare_base import CommCareBaseLoader

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.commcarehq.org"


class CommCareFormLoader(CommCareBaseLoader):
    """Loads form submission records from the CommCare HQ API.

    Supports both ``load()`` (returns flat list) and ``load_pages()``
    (yields one page at a time for streaming writes).

    Each returned record is a flat dict with:
        form_id, xmlns, received_on, server_modified_on, app_id,
        form_data (raw JSONB),
        case_ids (list of case IDs touched by this form)
    """

    def __init__(self, domain: str, credential: dict[str, str], page_size: int = 1000) -> None:
        super().__init__(domain=domain, credential=credential)
        self.page_size = min(page_size, 1000)

    def load_pages(self) -> Iterator[list[dict]]:
        """Yield one page of normalised form records at a time."""
        url = f"{_BASE_URL}/a/{self.domain}/api/v0.5/form/"
        params: dict = {"limit": self.page_size}
        total_loaded = 0
        while url:
            data = self._get(url, params=params).json()
            forms = [_normalize_form(raw) for raw in data.get("objects", [])]
            if forms:
                total_loaded += len(forms)
                logger.info(
                    "Fetched %d forms (total so far: %d/%s) for domain %s",
                    len(forms),
                    total_loaded,
                    data.get("meta", {}).get("total_count", "?"),
                    self.domain,
                )
                yield forms
            url = data.get("next")
            params = {}

    def load(self) -> list[dict]:
        """Return all forms as a flat list (loads all pages into memory)."""
        return [form for page in self.load_pages() for form in page]


def _normalize_form(raw: dict) -> dict:
    """Flatten a raw CommCare form API response into a loader record."""
    form_data = raw.get("form", {})
    case_refs = extract_case_refs(form_data)
    return {
        "form_id": raw.get("id", ""),
        "xmlns": form_data.get("@xmlns", ""),
        "received_on": raw.get("received_on", ""),
        "server_modified_on": raw.get("server_modified_on", ""),
        "app_id": raw.get("app_id", ""),
        "form_data": form_data,
        "case_ids": [r["case_id"] for r in case_refs],
    }


def extract_case_refs(form_data: Any, _seen: set[str] | None = None) -> list[dict]:
    """Recursively extract all case block references from a form's data dict.

    CommCare case blocks are identified by the presence of ``@case_id`` in a dict.
    They can be nested at any depth and may appear inside repeat groups (lists).

    Returns a deduplicated list of dicts with ``case_id`` and ``action`` keys.
    """
    if _seen is None:
        _seen = set()
    refs: list[dict] = []

    if isinstance(form_data, dict):
        if "@case_id" in form_data:
            case_id = form_data["@case_id"]
            if case_id and case_id not in _seen:
                _seen.add(case_id)
                refs.append({
                    "case_id": case_id,
                    "action": form_data.get("@action", ""),
                })
        else:
            for value in form_data.values():
                refs.extend(extract_case_refs(value, _seen))

    elif isinstance(form_data, list):
        for item in form_data:
            refs.extend(extract_case_refs(item, _seen))

    return refs
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_commcare_forms_loader.py -v
```
Expected: all pass

**Step 5: Commit**

```bash
git add mcp_server/loaders/commcare_forms.py tests/test_commcare_forms_loader.py
git commit -m "feat: CommCare forms loader with streaming load_pages() and recursive case-reference extraction"
```

---

## Task 7: Three-Phase Materializer Refactor

Replace `run_commcare_sync` with `run_pipeline()` — Discover → Load → Transform — with progress callbacks, streaming writes, batch inserts, shared transaction, and transform isolation.

**Files:**
- Modify: `apps/projects/models.py` (add `DISCOVERING` state)
- Rewrite: `mcp_server/services/materializer.py`
- Test: `tests/test_materializer.py`

**Step 1: Add DISCOVERING to RunState in models.py**

In `apps/projects/models.py`, find `RunState` and add `DISCOVERING`:

```python
class RunState(models.TextChoices):
    STARTED = "started"
    DISCOVERING = "discovering"   # NEW
    LOADING = "loading"
    TRANSFORMING = "transforming"
    COMPLETED = "completed"
    FAILED = "failed"
```

Check if a migration is needed:
```bash
uv run python manage.py makemigrations --check
```
If it flags a change, run `uv run python manage.py makemigrations projects --name add_discovering_state`.

**Step 2: Write the failing tests**

```python
# tests/test_materializer.py
from unittest.mock import MagicMock, patch, call
import pytest


class TestRunPipeline:
    def _make_schema(self, name="dimagi"):
        s = MagicMock()
        s.schema_name = name
        return s

    def _make_tm(self, tenant_id="dimagi"):
        tm = MagicMock()
        tm.tenant_id = tenant_id
        return tm

    def _setup_run_mock(self, mock_run_cls):
        run = MagicMock()
        run.id = "run-1"
        mock_run_cls.objects.create.return_value = run
        for attr in ("DISCOVERING", "LOADING", "TRANSFORMING", "COMPLETED", "FAILED"):
            setattr(mock_run_cls.RunState, attr, attr.lower())
        return run

    def test_returns_completed_result(self):
        from mcp_server.services.materializer import run_pipeline
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig

        pipeline = PipelineConfig(
            name="commcare_sync", description="", version="1.0", provider="commcare",
            sources=[SourceConfig(name="cases", loader="")],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {"app_definitions": [], "case_types": [], "form_definitions": {}}
            mock_cases.return_value.load_pages.return_value = iter([[{"case_id": "c1"}]])
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            result = run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        assert result["status"] == "completed"
        assert result["run_id"] == "run-1"
        assert "cases" in result["sources"]

    def test_progress_callback_called_full_sequence(self):
        """Progress callback must be called exactly total_steps times in order."""
        from mcp_server.services.materializer import run_pipeline
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig

        pipeline = PipelineConfig(
            name="commcare_sync", description="", version="1.0", provider="commcare",
            sources=[SourceConfig(name="cases", loader="")],
            # No metadata_discovery, no transforms — simplest pipeline
        )
        # total_steps = 1 (provision) + 1 (discover) + 1 (cases) + 1 (transform/skip) = 4

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {"app_definitions": [], "case_types": [], "form_definitions": {}}
            mock_cases.return_value.load_pages.return_value = iter([])
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            calls: list[tuple] = []
            run_pipeline(
                self._make_tm(), {"type": "api_key", "value": "x"}, pipeline,
                progress_callback=lambda cur, tot, msg: calls.append((cur, tot, msg)),
            )

        total = calls[0][1]  # total_steps from first call
        assert len(calls) == total  # exactly total_steps calls
        # Steps increment sequentially from 1 to total
        for i, (cur, tot, _msg) in enumerate(calls, start=1):
            assert cur == i
            assert tot == total
        # First step is provisioning, last step is transform/skip
        assert "provision" in calls[0][2].lower() or "schema" in calls[0][2].lower()
        assert "transform" in calls[-1][2].lower() or "skip" in calls[-1][2].lower()

    def test_no_metadata_discovery_skips_discover_phase(self):
        """Pipeline without metadata_discovery should not create TenantMetadata."""
        from mcp_server.services.materializer import run_pipeline
        from mcp_server.pipeline_registry import PipelineConfig

        pipeline = PipelineConfig(
            name="bare_sync", description="", version="1.0", provider="commcare",
            sources=[],  # no metadata_discovery
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata") as mock_meta_model,
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta_loader,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        mock_meta_loader.assert_not_called()
        mock_meta_model.objects.update_or_create.assert_not_called()

    def test_transform_failure_does_not_mark_run_failed(self):
        """A DBT transform failure should NOT change state to FAILED."""
        from mcp_server.services.materializer import run_pipeline
        from mcp_server.pipeline_registry import PipelineConfig, TransformConfig

        pipeline = PipelineConfig(
            name="commcare_sync", description="", version="1.0", provider="commcare",
            sources=[],
            transforms=TransformConfig(dbt_project="transforms/commcare", models=["stg_cases"]),
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer._run_transform_phase") as mock_transform,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {"app_definitions": [], "case_types": [], "form_definitions": {}}
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()
            mock_transform.side_effect = RuntimeError("dbt compilation error")

            result = run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        # Run should be COMPLETED, not FAILED
        assert run.state == "completed"
        assert result["status"] == "completed"
        # Transform error is recorded in result
        assert "transform_error" in result

    def test_unknown_source_raises(self):
        from mcp_server.services.materializer import _load_source
        conn = MagicMock()
        with pytest.raises(ValueError, match="Unknown source"):
            _load_source("nonexistent", MagicMock(), {}, "schema", conn)

    def test_failed_load_marks_run_failed(self):
        from mcp_server.services.materializer import run_pipeline
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig

        pipeline = PipelineConfig(
            name="commcare_sync", description="", version="1.0", provider="commcare",
            sources=[SourceConfig(name="cases", loader="")],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {"app_definitions": [], "case_types": [], "form_definitions": {}}
            mock_cases.return_value.load_pages.side_effect = RuntimeError("CommCare API down")
            conn = MagicMock()
            mock_conn.return_value = conn

            with pytest.raises(RuntimeError, match="CommCare API down"):
                run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        assert run.state == "failed"


@pytest.mark.django_db
class TestWriteCases:
    """Real DB tests for _write_cases using psycopg2."""

    def test_inserts_cases(self, django_db_setup, db):
        """_write_cases should insert rows into the named schema."""
        import os
        import psycopg2
        from mcp_server.services.materializer import _write_cases

        db_url = os.environ.get("MANAGED_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not db_url:
            pytest.skip("No MANAGED_DATABASE_URL/DATABASE_URL for writer test")

        test_schema = "test_write_cases"
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {test_schema}")
            conn.autocommit = False
            cases = [
                {"case_id": "c1", "case_type": "patient", "case_name": "Alice",
                 "external_id": "", "owner_id": "u1", "date_opened": "2026-01-01",
                 "last_modified": "2026-01-02", "server_last_modified": "", "indexed_on": "",
                 "closed": False, "date_closed": "", "properties": {"name": "Alice"}, "indices": {}},
            ]
            count = _write_cases(iter([cases]), test_schema, conn)
            conn.commit()
            assert count == 1
            with conn.cursor() as cur:
                cur.execute(f"SELECT case_id FROM {test_schema}.cases")
                rows = cur.fetchall()
            assert rows[0][0] == "c1"
        finally:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE")
            conn.close()


@pytest.mark.django_db
class TestWriteForms:
    def test_inserts_forms(self, django_db_setup, db):
        import os
        import psycopg2
        from mcp_server.services.materializer import _write_forms

        db_url = os.environ.get("MANAGED_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not db_url:
            pytest.skip("No MANAGED_DATABASE_URL/DATABASE_URL for writer test")

        test_schema = "test_write_forms"
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {test_schema}")
            conn.autocommit = False
            forms = [
                {"form_id": "f1", "xmlns": "http://example.com/form1",
                 "received_on": "2026-01-01", "server_modified_on": "",
                 "app_id": "app1", "form_data": {"@name": "Reg"}, "case_ids": ["c1"]},
            ]
            count = _write_forms(iter([forms]), test_schema, conn)
            conn.commit()
            assert count == 1
        finally:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE")
            conn.close()
```

**Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/test_materializer.py -v
```

**Step 4: Rewrite materializer.py**

```python
# mcp_server/services/materializer.py
"""Three-phase materialization orchestrator: Discover → Load → Transform.

Design notes:
- All source writes share a single psycopg2 connection, committed in one
  transaction. A mid-run failure rolls back all sources atomically.
- Loaders expose load_pages() iterators; rows are written page-by-page so the
  full dataset is never held in memory. Inserts use execute_values for efficiency.
- Transform failures are isolated — run is marked COMPLETED; error stored in result.
- An assertion at the end guards against total_steps / report() drift.
"""
from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from psycopg2 import sql as psql
from psycopg2.extras import execute_values

from apps.projects.services.schema_manager import SchemaManager, get_managed_db_connection
from mcp_server.loaders.commcare_cases import CommCareCaseLoader
from mcp_server.loaders.commcare_forms import CommCareFormLoader
from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader
from mcp_server.pipeline_registry import PipelineConfig

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


def run_pipeline(
    tenant_membership: Any,
    credential: dict[str, str],
    pipeline: PipelineConfig,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Run a three-phase materialization pipeline.

    Phases:
      1. DISCOVER — Fetch CommCare metadata, store in TenantMetadata (survives teardown).
      2. LOAD    — Execute loaders for each source, stream-write to tenant schema tables.
      3. TRANSFORM — Run DBT (if configured), or no-op. Failures are isolated.

    Args:
        tenant_membership: The TenantMembership to sync.
        credential: {"type": "oauth"|"api_key", "value": str}
        pipeline: Pipeline configuration from the registry.
        progress_callback: Optional callable(current, total, message).

    Returns a summary dict with run_id, status, and per-source row counts.
    """
    from apps.projects.models import MaterializationRun, TenantMetadata

    # total steps: provision + discover + N sources + transform/skip
    total_steps = 2 + len(pipeline.sources) + 1
    step = 0

    def report(message: str) -> None:
        nonlocal step
        step += 1
        if progress_callback:
            progress_callback(step, total_steps, message)

    # ── 1. PROVISION ──────────────────────────────────────────────────────────
    report(f"Provisioning schema for {tenant_membership.tenant_id}...")
    tenant_schema = SchemaManager().provision(tenant_membership)
    schema_name = tenant_schema.schema_name

    run = MaterializationRun.objects.create(
        tenant_schema=tenant_schema,
        pipeline=pipeline.name,
        state=MaterializationRun.RunState.DISCOVERING,
    )

    source_results: dict[str, dict] = {}

    try:
        # ── 2. DISCOVER ───────────────────────────────────────────────────────
        report("Discovering tenant metadata from CommCare...")
        _run_discover_phase(tenant_membership, credential, pipeline)

        # ── 3. LOAD ───────────────────────────────────────────────────────────
        run.state = MaterializationRun.RunState.LOADING
        run.save(update_fields=["state"])

        conn = get_managed_db_connection()
        conn.autocommit = False
        try:
            for source in pipeline.sources:
                report(f"Loading {source.name} from CommCare API...")
                rows = _load_source(source.name, tenant_membership, credential, schema_name, conn)
                source_results[source.name] = {"state": "loaded", "rows": rows}
                logger.info("Loaded %d rows into %s.%s", rows, schema_name, source.name)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    except Exception:
        run.state = MaterializationRun.RunState.FAILED
        run.completed_at = datetime.now(UTC)
        run.result = {"error": "Pipeline failed", "sources": source_results}
        run.save(update_fields=["state", "completed_at", "result"])
        raise

    # ── 4. TRANSFORM ──────────────────────────────────────────────────────────
    # Transform errors are isolated — failure here does NOT mark the run FAILED.
    run.state = MaterializationRun.RunState.TRANSFORMING
    run.save(update_fields=["state"])
    transform_result: dict = {}

    if pipeline.transforms and pipeline.dbt_models:
        report("Running DBT transforms...")
        try:
            transform_result = _run_transform_phase(pipeline, schema_name)
        except Exception as e:
            logger.error("Transform phase failed for schema %s: %s", schema_name, e)
            transform_result = {"error": str(e)}
    else:
        report("No DBT transforms configured — skipping")

    # ── 5. COMPLETE ───────────────────────────────────────────────────────────
    run.state = MaterializationRun.RunState.COMPLETED
    run.completed_at = datetime.now(UTC)
    run.result = {"sources": source_results, "pipeline": pipeline.name, "transforms": transform_result}
    run.save(update_fields=["state", "completed_at", "result"])

    tenant_schema.state = "active"
    tenant_schema.save(update_fields=["state", "last_accessed_at"])

    total_rows = sum(s.get("rows", 0) for s in source_results.values())
    logger.info("Pipeline '%s' complete for '%s': %d rows", pipeline.name, schema_name, total_rows)

    assert step == total_steps, (
        f"Progress step count mismatch: expected {total_steps}, got {step}. "
        "Update total_steps if you add/remove report() calls."
    )

    transform_error = transform_result.get("error")
    result: dict = {
        "status": "completed",
        "run_id": str(run.id),
        "schema": schema_name,
        "pipeline": pipeline.name,
        "sources": source_results,
        "rows_loaded": total_rows,
    }
    if transform_error:
        result["transform_error"] = transform_error
    return result


def _run_discover_phase(
    tenant_membership: Any, credential: dict[str, str], pipeline: PipelineConfig
) -> None:
    """Fetch CommCare metadata and upsert into TenantMetadata."""
    from apps.projects.models import TenantMetadata
    from django.utils import timezone

    if not pipeline.has_metadata_discovery:
        return

    loader = CommCareMetadataLoader(domain=tenant_membership.tenant_id, credential=credential)
    metadata = loader.load()

    TenantMetadata.objects.update_or_create(
        tenant_membership=tenant_membership,
        defaults={"metadata": metadata, "discovered_at": timezone.now()},
    )
    logger.info(
        "Stored metadata for tenant %s: %d apps, %d case types",
        tenant_membership.tenant_id,
        len(metadata.get("app_definitions", [])),
        len(metadata.get("case_types", [])),
    )


def _load_source(
    source_name: str,
    tenant_membership: Any,
    credential: dict[str, str],
    schema_name: str,
    conn: Any,
) -> int:
    domain = tenant_membership.tenant_id
    if source_name == "cases":
        loader = CommCareCaseLoader(domain=domain, credential=credential)
        return _write_cases(loader.load_pages(), schema_name, conn)
    if source_name == "forms":
        loader = CommCareFormLoader(domain=domain, credential=credential)
        return _write_forms(loader.load_pages(), schema_name, conn)
    raise ValueError(f"Unknown source '{source_name}'. Known sources: cases, forms")


def _run_transform_phase(pipeline: PipelineConfig, schema_name: str) -> dict:
    import tempfile

    from django.conf import settings

    from mcp_server.services.dbt_runner import generate_profiles_yml, run_dbt

    db_url = getattr(settings, "MANAGED_DATABASE_URL", "")
    repo_root = pathlib.Path(__file__).parent.parent.parent
    dbt_project_dir = str(repo_root / pipeline.transforms.dbt_project)

    with tempfile.TemporaryDirectory() as tmpdir:
        profiles_path = pathlib.Path(tmpdir) / "profiles.yml"
        generate_profiles_yml(output_path=profiles_path, schema_name=schema_name, db_url=db_url)
        return run_dbt(dbt_project_dir=dbt_project_dir, profiles_dir=tmpdir, models=pipeline.dbt_models)


# ── Table writers ──────────────────────────────────────────────────────────────
# Writers accept a shared psycopg2 connection managed by the caller.
# The caller owns commit/rollback; writers only cursor.execute.


def _write_cases(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the cases table and bulk-insert all pages. Returns total row count."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.cases CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.cases (
            case_id TEXT PRIMARY KEY,
            case_type TEXT,
            case_name TEXT,
            external_id TEXT,
            owner_id TEXT,
            date_opened TEXT,
            last_modified TEXT,
            server_last_modified TEXT,
            indexed_on TEXT,
            closed BOOLEAN DEFAULT FALSE,
            date_closed TEXT,
            properties JSONB DEFAULT '{{}}'::jsonb,
            indices JSONB DEFAULT '{{}}'::jsonb
        )
        """
        ).format(schema=sid)
    )

    ins_sql = psql.SQL(
        """
        INSERT INTO {schema}.cases
            (case_id, case_type, case_name, external_id, owner_id,
             date_opened, last_modified, server_last_modified, indexed_on,
             closed, date_closed, properties, indices)
        VALUES %s
        ON CONFLICT (case_id) DO UPDATE SET
            case_name=EXCLUDED.case_name, owner_id=EXCLUDED.owner_id,
            last_modified=EXCLUDED.last_modified,
            server_last_modified=EXCLUDED.server_last_modified,
            indexed_on=EXCLUDED.indexed_on, closed=EXCLUDED.closed,
            date_closed=EXCLUDED.date_closed, properties=EXCLUDED.properties,
            indices=EXCLUDED.indices
        """
    ).format(schema=sid).as_string(conn)

    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                c.get("case_id"), c.get("case_type", ""), c.get("case_name", ""),
                c.get("external_id", ""), c.get("owner_id", ""), c.get("date_opened", ""),
                c.get("last_modified", ""), c.get("server_last_modified", ""),
                c.get("indexed_on", ""), c.get("closed", False),
                c.get("date_closed") or "", json.dumps(c.get("properties", {})),
                json.dumps(c.get("indices", {})),
            )
            for c in page
        ]
        execute_values(cur, ins_sql, rows)
        total += len(page)

    return total


def _write_forms(pages: Iterator[list[dict]], schema_name: str, conn: Any) -> int:
    """Create the forms table and bulk-insert all pages. Returns total row count."""
    sid = psql.Identifier(schema_name)
    cur = conn.cursor()

    cur.execute(psql.SQL("DROP TABLE IF EXISTS {}.forms CASCADE").format(sid))
    cur.execute(
        psql.SQL(
            """
        CREATE TABLE {schema}.forms (
            form_id TEXT PRIMARY KEY,
            xmlns TEXT,
            received_on TEXT,
            server_modified_on TEXT,
            app_id TEXT,
            form_data JSONB DEFAULT '{{}}'::jsonb,
            case_ids JSONB DEFAULT '[]'::jsonb
        )
        """
        ).format(schema=sid)
    )

    ins_sql = psql.SQL(
        """
        INSERT INTO {schema}.forms
            (form_id, xmlns, received_on, server_modified_on, app_id, form_data, case_ids)
        VALUES %s
        ON CONFLICT (form_id) DO UPDATE SET
            received_on=EXCLUDED.received_on,
            server_modified_on=EXCLUDED.server_modified_on,
            form_data=EXCLUDED.form_data,
            case_ids=EXCLUDED.case_ids
        """
    ).format(schema=sid).as_string(conn)

    total = 0
    for page in pages:
        if not page:
            continue
        rows = [
            (
                f.get("form_id", ""), f.get("xmlns", ""),
                f.get("received_on", ""), f.get("server_modified_on", ""),
                f.get("app_id", ""), json.dumps(f.get("form_data", {})),
                json.dumps(f.get("case_ids", [])),
            )
            for f in page
        ]
        execute_values(cur, ins_sql, rows)
        total += len(page)

    return total


# ── Backwards-compatible shim ──────────────────────────────────────────────────

def run_commcare_sync(tenant_membership: Any, credential: dict[str, str]) -> dict:
    """Legacy entry point — delegates to run_pipeline with the default registry."""
    from mcp_server.pipeline_registry import get_registry
    pipeline = get_registry().get("commcare_sync")
    if pipeline is None:
        raise ValueError("commcare_sync pipeline not found in registry")
    return run_pipeline(tenant_membership, credential, pipeline)
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_materializer.py tests/test_commcare_loader.py tests/ -x -q
```
Expected: all pass

**Step 6: Commit**

```bash
git add apps/projects/models.py mcp_server/services/materializer.py tests/test_materializer.py
git commit -m "feat: three-phase materializer — streaming writes, shared txn, transform isolation"
```

---

## Task 8: `get_materialization_status` MCP Tool

**Files:**
- Modify: `mcp_server/server.py`
- Test: `tests/test_mcp_tenant_tools.py`

**Step 1: Write the failing test**

Add to `tests/test_mcp_tenant_tools.py`:

```python
class TestGetMaterializationStatus:
    def test_returns_run_status(self):
        import uuid, asyncio
        from unittest.mock import patch, MagicMock, AsyncMock

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.id = uuid.UUID(run_id)
        mock_run.pipeline = "commcare_sync"
        mock_run.state = "completed"
        mock_run.started_at.isoformat.return_value = "2026-02-24T10:00:00+00:00"
        mock_run.completed_at.isoformat.return_value = "2026-02-24T10:05:00+00:00"
        mock_run.result = {"sources": {"cases": {"rows": 100}}}
        mock_run.tenant_schema.tenant_membership.tenant_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            from mcp_server.server import get_materialization_status
            result = asyncio.run(get_materialization_status(run_id=run_id))

        assert result["success"] is True
        assert result["data"]["run_id"] == run_id
        assert result["data"]["state"] == "completed"

    def test_unknown_run_returns_not_found(self):
        import uuid, asyncio
        from unittest.mock import patch, AsyncMock
        from django.core.exceptions import ObjectDoesNotExist

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.DoesNotExist = ObjectDoesNotExist
            mock_cls.objects.select_related.return_value.aget = AsyncMock(
                side_effect=ObjectDoesNotExist
            )
            from mcp_server.server import get_materialization_status
            result = asyncio.run(get_materialization_status(run_id=str(uuid.uuid4())))

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestGetMaterializationStatus -v
```

**Step 3: Add the tool to server.py**

```python
@mcp.tool()
async def get_materialization_status(run_id: str) -> dict:
    """Retrieve the status of a materialization run by ID.

    Primarily a fallback for reconnection scenarios — live progress is delivered
    via MCP progress notifications during an active run_materialization call.

    Args:
        run_id: UUID of the MaterializationRun to look up.
    """
    from apps.projects.models import MaterializationRun

    async with tool_context("get_materialization_status", run_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related(
                "tenant_schema__tenant_membership"
            ).aget(id=run_id)
        except Exception as e:
            if "DoesNotExist" in type(e).__name__ or "invalid" in str(e).lower():
                tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
                return tc["result"]
            raise

        tenant_id = run.tenant_schema.tenant_membership.tenant_id
        schema = run.tenant_schema.schema_name

        tc["result"] = success_response(
            {
                "run_id": str(run.id),
                "pipeline": run.pipeline,
                "state": run.state,
                "result": run.result,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "tenant_id": tenant_id,
            },
            tenant_id=tenant_id,
            schema=schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestGetMaterializationStatus tests/ -x -q
```

**Step 5: Commit**

```bash
git add mcp_server/server.py
git commit -m "feat: get_materialization_status MCP tool — reconnect-and-poll fallback"
```

---

## Task 9: MCP Progress Notifications + Refactor run_materialization

Wire `ctx: Context` into `run_materialization`, switch from `run_commcare_sync` to `run_pipeline`, and emit `notifications/progress` using `asyncio.run_coroutine_threadsafe`. Log silently-failing notification futures via a done-callback.

**Files:**
- Modify: `mcp_server/server.py`

**Step 1: Update imports at top of server.py**

```python
import asyncio
import logging
from mcp.server.fastmcp import Context  # add to existing import line or separately

from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import run_pipeline
```

**Step 2: Replace the existing `run_materialization` function**

```python
@mcp.tool()
async def run_materialization(
    tenant_id: str,
    tenant_membership_id: str = "",
    pipeline: str = "commcare_sync",
    ctx: Context | None = None,
) -> dict:
    """Materialize data from CommCare into the tenant's schema.

    Runs a three-phase pipeline (Discover → Load → Transform). Creates the schema
    automatically if it doesn't exist. Streams progress via MCP notifications/progress
    when the caller provides a progressToken.

    Args:
        tenant_id: The tenant identifier (CommCare domain name).
        tenant_membership_id: UUID of the specific TenantMembership to use.
        pipeline: Pipeline to run (default: commcare_sync).
    """
    from asgiref.sync import sync_to_async

    from apps.users.models import TenantCredential, TenantMembership
    from mcp_server.loaders.commcare_base import CommCareAuthError

    async with tool_context("run_materialization", tenant_id, pipeline=pipeline) as tc:
        # ── Resolve TenantMembership ──────────────────────────────────────────
        try:
            qs = TenantMembership.objects.select_related("user")
            tm = (
                await qs.aget(id=tenant_membership_id, tenant_id=tenant_id)
                if tenant_membership_id
                else await qs.aget(tenant_id=tenant_id, provider="commcare")
            )
        except TenantMembership.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Tenant '{tenant_id}' not found")
            return tc["result"]

        # ── Resolve credential ────────────────────────────────────────────────
        try:
            cred_obj = await TenantCredential.objects.select_related("tenant_membership").aget(
                tenant_membership=tm
            )
        except TenantCredential.DoesNotExist:
            tc["result"] = error_response("AUTH_TOKEN_MISSING", "No credential configured for this tenant")
            return tc["result"]

        if cred_obj.credential_type == TenantCredential.API_KEY:
            from apps.users.adapters import decrypt_credential

            try:
                decrypted = await sync_to_async(decrypt_credential)(cred_obj.encrypted_credential)
            except Exception:
                logger.exception("Failed to decrypt API key for tenant %s", tenant_id)
                tc["result"] = error_response("AUTH_TOKEN_MISSING", "Failed to decrypt API key")
                return tc["result"]
            credential = {"type": "api_key", "value": decrypted}
        else:
            from allauth.socialaccount.models import SocialToken

            token_obj = (
                await SocialToken.objects.filter(
                    account__user=tm.user,
                    account__provider__startswith="commcare",
                )
                .exclude(account__provider__startswith="commcare_connect")
                .afirst()
            )
            if not token_obj:
                tc["result"] = error_response("AUTH_TOKEN_MISSING", "No CommCare OAuth token found")
                return tc["result"]
            credential = {"type": "oauth", "value": token_obj.token}

        # ── Resolve pipeline config ───────────────────────────────────────────
        registry = get_registry()
        pipeline_config = registry.get(pipeline)
        if pipeline_config is None:
            tc["result"] = error_response(NOT_FOUND, f"Pipeline '{pipeline}' not found in registry")
            return tc["result"]

        # ── Build progress callback ───────────────────────────────────────────
        # run_pipeline runs in a thread (via sync_to_async), so we bridge back
        # to the async event loop with run_coroutine_threadsafe.
        # A done-callback logs any silent delivery failures.
        progress_callback = None
        if ctx is not None:
            loop = asyncio.get_running_loop()

            def _on_progress_done(fut):
                exc = fut.exception()
                if exc is not None:
                    logger.warning("Progress notification delivery failed: %s", exc)

            def progress_callback(current: int, total: int, message: str) -> None:
                fut = asyncio.run_coroutine_threadsafe(
                    ctx.report_progress(current, total, message),
                    loop,
                )
                fut.add_done_callback(_on_progress_done)

        # ── Run pipeline ──────────────────────────────────────────────────────
        try:
            result = await sync_to_async(run_pipeline)(tm, credential, pipeline_config, progress_callback)
        except CommCareAuthError as e:
            logger.warning("CommCare auth failed for tenant %s: %s", tenant_id, e)
            tc["result"] = error_response(AUTH_TOKEN_EXPIRED, str(e))
            return tc["result"]
        except Exception:
            logger.exception("Pipeline '%s' failed for tenant %s", pipeline, tenant_id)
            tc["result"] = error_response(INTERNAL_ERROR, f"Pipeline '{pipeline}' failed")
            return tc["result"]

        tc["result"] = success_response(
            result,
            tenant_id=tenant_id,
            schema=result.get("schema", ""),
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Step 3: Run full test suite**

```bash
uv run pytest tests/ -x -q
```
Expected: all pass

**Step 4: Commit**

```bash
git add mcp_server/server.py
git commit -m "feat: MCP progress notifications via ctx.report_progress + done-callback logging"
```

---

## Task 10: DBT Runner (Programmatic API + Thread Safety)

Uses `dbtRunner` from `dbt.cli.main` — no subprocess. Generates a `profiles.yml` at runtime targeting the tenant's schema, then calls `dbt.invoke(["run", ...])`. A module-level `threading.Lock` serialises concurrent calls since dbtRunner is not thread-safe.

**Prerequisites:**

```bash
uv add dbt-core dbt-postgres
```

**Files:**
- Create: `mcp_server/services/dbt_runner.py`
- Test: `tests/test_dbt_runner.py`

**Step 1: Write the failing test**

```python
# tests/test_dbt_runner.py
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest


class TestGenerateProfilesYml:
    def test_generates_valid_yaml(self, tmp_path):
        from mcp_server.services.dbt_runner import generate_profiles_yml

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(
            output_path=path,
            schema_name="dimagi",
            db_url="postgresql://svc:pass@localhost:5432/managed_db",
        )

        assert path.exists()
        import yaml
        content = yaml.safe_load(path.read_text())
        profile = content["data_explorer"]["outputs"]["tenant_schema"]
        assert profile["schema"] == "dimagi"
        assert profile["host"] == "localhost"
        assert profile["dbname"] == "managed_db"
        assert profile["type"] == "postgres"

    def test_parses_url_components(self, tmp_path):
        from mcp_server.services.dbt_runner import generate_profiles_yml

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(
            output_path=path,
            schema_name="test_schema",
            db_url="postgresql://myuser:mypassword@db.host.com:5433/analytics",
        )

        import yaml
        content = yaml.safe_load(path.read_text())
        profile = content["data_explorer"]["outputs"]["tenant_schema"]
        assert profile["host"] == "db.host.com"
        assert profile["port"] == 5433
        assert profile["user"] == "myuser"
        assert profile["dbname"] == "analytics"


class TestRunDbt:
    def test_returns_success_result(self, tmp_path):
        from mcp_server.services.dbt_runner import run_dbt

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.result = [
            MagicMock(node=MagicMock(name="stg_cases"), status="success"),
            MagicMock(node=MagicMock(name="stg_forms"), status="success"),
        ]

        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            result = run_dbt(
                dbt_project_dir=str(tmp_path),
                profiles_dir=str(tmp_path),
                models=["stg_cases", "stg_forms"],
            )

        assert result["success"] is True
        assert result["models"]["stg_cases"] == "success"
        assert result["models"]["stg_forms"] == "success"

    def test_returns_failure_when_dbt_fails(self, tmp_path):
        from mcp_server.services.dbt_runner import run_dbt

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.result = []
        mock_result.exception = RuntimeError("dbt compilation error")

        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            result = run_dbt(
                dbt_project_dir=str(tmp_path),
                profiles_dir=str(tmp_path),
                models=["stg_cases"],
            )

        assert result["success"] is False
        assert "error" in result

    def test_passes_correct_cli_args(self, tmp_path):
        from mcp_server.services.dbt_runner import run_dbt

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.result = []

        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            run_dbt(
                dbt_project_dir="/path/to/project",
                profiles_dir="/path/to/profiles",
                models=["stg_cases", "stg_forms"],
            )

        call_args = mock_runner.invoke.call_args[0][0]
        assert "run" in call_args
        assert "--project-dir" in call_args
        assert "/path/to/project" in call_args
        assert "--profiles-dir" in call_args
        assert "/path/to/profiles" in call_args
        assert "stg_cases" in " ".join(call_args)

    def test_lock_is_acquired(self, tmp_path):
        """Verify the threading lock is acquired during a dbt run."""
        import threading
        from mcp_server.services.dbt_runner import run_dbt, _dbt_lock

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.result = []
        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        lock_was_held = []

        original_invoke = mock_runner.invoke
        def invoke_side_effect(*args, **kwargs):
            lock_was_held.append(_dbt_lock.locked())
            return original_invoke(*args, **kwargs)
        mock_runner.invoke.side_effect = invoke_side_effect

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            run_dbt(str(tmp_path), str(tmp_path), ["stg_cases"])

        assert lock_was_held == [True]
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dbt_runner.py -v
```

**Step 3: Implement dbt_runner.py**

```python
# mcp_server/services/dbt_runner.py
"""DBT runner using the programmatic Python API (dbtRunner).

Avoids subprocess overhead. Generates a runtime profiles.yml targeting the
tenant's schema, then invokes dbt via the Python API.

dbtRunner is NOT thread-safe — concurrent in-process invocations will corrupt
dbt's global state. A module-level lock serialises all calls.

Reference: https://docs.getdbt.com/reference/programmatic-invocations
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlparse

import yaml

logger = logging.getLogger(__name__)

# Serialise all dbt invocations — dbtRunner is not thread-safe.
_dbt_lock = threading.Lock()


def generate_profiles_yml(
    output_path: Path,
    schema_name: str,
    db_url: str,
    threads: int = 4,
) -> None:
    """Generate a dbt profiles.yml targeting the tenant's schema.

    Args:
        output_path: Where to write the profiles.yml.
        schema_name: PostgreSQL schema name for this tenant.
        db_url: PostgreSQL connection URL (postgresql://user:pass@host:port/dbname).
        threads: dbt parallelism (default 4).
    """
    parsed = urlparse(db_url)
    profile = {
        "data_explorer": {
            "target": "tenant_schema",
            "outputs": {
                "tenant_schema": {
                    "type": "postgres",
                    "host": parsed.hostname or "localhost",
                    "port": parsed.port or 5432,
                    "user": parsed.username or "",
                    "password": parsed.password or "",
                    "dbname": parsed.path.lstrip("/") if parsed.path else "",
                    "schema": schema_name,
                    "threads": threads,
                }
            },
        }
    }
    Path(output_path).write_text(yaml.dump(profile, default_flow_style=False))
    logger.debug("Generated profiles.yml at %s for schema '%s'", output_path, schema_name)


def run_dbt(
    dbt_project_dir: str,
    profiles_dir: str,
    models: list[str],
) -> dict:
    """Run dbt models via the programmatic Python API.

    Uses ``dbtRunner`` from ``dbt.cli.main`` — no subprocess needed.
    Acquires ``_dbt_lock`` before invoking to prevent concurrent in-process
    calls from corrupting dbt's global state.

    Args:
        dbt_project_dir: Directory containing dbt_project.yml.
        profiles_dir: Directory containing the generated profiles.yml.
        models: List of dbt model names to run.

    Returns:
        {"success": bool, "models": {name: status}, "error": str | None}
    """
    from dbt.cli.main import dbtRunner

    select_arg = " ".join(models)
    cli_args = [
        "run",
        "--project-dir", dbt_project_dir,
        "--profiles-dir", profiles_dir,
        "--select", select_arg,
    ]

    logger.info("Invoking dbt programmatically: %s", " ".join(cli_args))

    with _dbt_lock:
        dbt = dbtRunner()
        res = dbt.invoke(cli_args)

    if not res.success:
        error_msg = str(res.exception) if res.exception else "dbt run failed"
        logger.error("dbt run failed: %s", error_msg)
        return {"success": False, "error": error_msg, "models": {}}

    model_results = {
        r.node.name: str(r.status)
        for r in (res.result or [])
        if hasattr(r, "node") and hasattr(r, "status")
    }

    for model in models:
        if model not in model_results:
            model_results[model] = "unknown"

    logger.info("dbt run complete: %s", model_results)
    return {"success": True, "models": model_results}
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_dbt_runner.py tests/test_materializer.py -v
```
Expected: all pass

**Step 5: Commit**

```bash
git add mcp_server/services/dbt_runner.py tests/test_dbt_runner.py pyproject.toml uv.lock
git commit -m "feat: DBT runner via programmatic dbtRunner API with threading.Lock for concurrency safety"
```

---

## Task 11: `cancel_materialization` MCP Tool (Basic)

**Files:**
- Modify: `mcp_server/server.py`
- Test: `tests/test_mcp_tenant_tools.py`

**Step 1: Write the failing test**

```python
class TestCancelMaterialization:
    def test_cancel_in_progress_run(self):
        import uuid, asyncio
        from unittest.mock import patch, MagicMock, AsyncMock

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.id = uuid.UUID(run_id)
        mock_run.state = "loading"
        mock_run.result = {}
        mock_run.tenant_schema.tenant_membership.tenant_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            mock_cls.RunState.STARTED = "started"
            mock_cls.RunState.DISCOVERING = "discovering"
            mock_cls.RunState.LOADING = "loading"
            mock_cls.RunState.TRANSFORMING = "transforming"
            mock_cls.RunState.FAILED = "failed"
            from mcp_server.server import cancel_materialization
            result = asyncio.run(cancel_materialization(run_id=run_id))

        assert result["success"] is True
        assert result["data"]["cancelled"] is True
        assert result["data"]["run_id"] == run_id

    def test_cancel_completed_run_returns_error(self):
        import uuid, asyncio
        from unittest.mock import patch, MagicMock, AsyncMock

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.state = "completed"
        mock_run.tenant_schema.tenant_membership.tenant_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            mock_cls.RunState.STARTED = "started"
            mock_cls.RunState.DISCOVERING = "discovering"
            mock_cls.RunState.LOADING = "loading"
            mock_cls.RunState.TRANSFORMING = "transforming"
            mock_cls.RunState.FAILED = "failed"
            from mcp_server.server import cancel_materialization
            result = asyncio.run(cancel_materialization(run_id=run_id))

        assert result["success"] is False
        assert "not in progress" in result["error"]["message"].lower()
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_mcp_tenant_tools.py::TestCancelMaterialization -v
```

**Step 3: Add the tool to server.py**

```python
@mcp.tool()
async def cancel_materialization(run_id: str) -> dict:
    """Cancel a running materialization pipeline.

    Marks the run as failed in the database. This is a best-effort cancellation —
    in-flight loader operations may not terminate immediately. Full subprocess
    cancellation is a future feature.

    Args:
        run_id: UUID of the MaterializationRun to cancel.
    """
    from datetime import UTC, datetime

    from asgiref.sync import sync_to_async

    from apps.projects.models import MaterializationRun

    async with tool_context("cancel_materialization", run_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related(
                "tenant_schema__tenant_membership"
            ).aget(id=run_id)
        except Exception as e:
            if "DoesNotExist" in type(e).__name__ or "invalid" in str(e).lower():
                tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
                return tc["result"]
            raise

        in_progress = {
            MaterializationRun.RunState.STARTED,
            MaterializationRun.RunState.DISCOVERING,
            MaterializationRun.RunState.LOADING,
            MaterializationRun.RunState.TRANSFORMING,
        }
        if run.state not in in_progress:
            tc["result"] = error_response(
                VALIDATION_ERROR,
                f"Run '{run_id}' is not in progress (state: {run.state})",
            )
            return tc["result"]

        previous_state = run.state
        run.state = MaterializationRun.RunState.FAILED
        run.completed_at = datetime.now(UTC)
        run.result = {**(run.result or {}), "cancelled": True}
        await sync_to_async(run.save)(update_fields=["state", "completed_at", "result"])

        tenant_id = run.tenant_schema.tenant_membership.tenant_id
        schema = run.tenant_schema.schema_name
        logger.info("Cancelled run %s for tenant %s (was: %s)", run_id, tenant_id, previous_state)

        tc["result"] = success_response(
            {"run_id": run_id, "cancelled": True, "previous_state": previous_state},
            tenant_id=tenant_id,
            schema=schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

**Step 4: Run full test suite**

```bash
uv run pytest tests/ -x -q
```
Expected: all pass

**Step 5: Run linting**

```bash
uv run ruff check mcp_server/ apps/projects/ && uv run ruff format --check mcp_server/ apps/projects/
```

**Step 6: Commit**

```bash
git add mcp_server/server.py
git commit -m "feat: cancel_materialization MCP tool — marks in-progress runs as failed"
```

---

## Task 12: Update TODO.md + Final Verification

**Step 1: Mark completed items in TODO.md**

```markdown
- [x] get_materialization_status tool
- [x] list_pipelines tool
- [x] cancel_materialization tool
- [x] Pipeline Registry
- [x] Three-phase structure (Discover → Load → Transform)
- [x] Discover phase (generic TenantMetadata, CommCare metadata loader)
- [x] Forms loader (with nested case-reference extraction)
- [x] DBT integration (programmatic dbtRunner API)
- [x] MCP progress notifications
```

**Step 2: Final test run**

```bash
uv run pytest tests/ -v --tb=short
```

**Step 3: Commit**

```bash
git add TODO.md
git commit -m "docs: mark materialization pipeline tasks complete"
```

---

## Summary of Changes

| File | Action |
|------|--------|
| `pipelines/commcare_sync.yml` | Create — cases + forms sources, no users |
| `mcp_server/pipeline_registry.py` | Create — YAML loader + PipelineConfig |
| `mcp_server/loaders/commcare_base.py` | Create — CommCareAuthError, build_auth_header, HTTP_TIMEOUT, CommCareBaseLoader (requests.Session) |
| `mcp_server/loaders/commcare_cases.py` | Modify — extend CommCareBaseLoader, add load_pages() iterator |
| `mcp_server/loaders/commcare_metadata.py` | Create — app/case-type/form discovery, uses base |
| `mcp_server/loaders/commcare_forms.py` | Create — forms with load_pages() + extract_case_refs, uses base |
| `mcp_server/services/materializer.py` | Rewrite — three-phase orchestrator, shared conn, single txn, streaming, execute_values, transform isolation, step assertion |
| `mcp_server/services/dbt_runner.py` | Create — generate_profiles_yml + dbtRunner with threading.Lock |
| `mcp_server/server.py` | Modify — list_pipelines, get_materialization_status, cancel_materialization; progress notifications with done-callback |
| `apps/projects/models.py` | Modify — generic TenantMetadata (django-pydantic-field), DISCOVERING state |
| `apps/projects/migrations/0014_tenantmetadata.py` | Auto-generated |
