# Auto-modeled Cube Semantic Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an auto-modeled Cube.dev semantic layer over Scout's Connect data, let the agent query governed metrics (falling back to raw SQL), and prove via an eval whether governed querying beats free-SQL.

**Architecture:** Scout's existing Connect pipeline materializes `raw_*` tables; the deliver-app form schema is fetched into `TenantMetadata.metadata`. An auto-modeling engine stages `raw_visits.form_json` into typed/labeled columns and generates a version-controlled Cube model. Cube Core (Docker) fronts the managed Postgres and exposes a pg-wire SQL API; a new `semantic_query` MCP tool lets the agent query governed measures. An eval framework compares free-SQL vs via-Cube answers. A self-improving loop promotes learnings into the model behind a PR-based curation gate.

**Tech Stack:** Django 5 (async ORM), psycopg 3, FastMCP, dbt, Cube Core (Docker), Postgres, pytest/pytest-asyncio, React (eval report viewing only, later).

**Spec:** `docs/superpowers/specs/2026-06-18-cube-semantic-layer-auto-modeling-design.md`

## Global Constraints

- **Async-first** (CLAUDE.md): new views `async def` with native async ORM (`.aget()`, `.afirst()`, `acreate()`, `async for`); never call sync ORM from async. `await request.auser()`. `sync_to_async` only for external API calls / dbt / atomic write blocks.
- **Imports at module level only** (CLAUDE.md). Exceptions: optional deps guarded by `try/except ImportError`; code before `django.setup()`. When moving an inline import to module level, update `mock.patch()` targets to the consuming module.
- **Python style:** ruff (line-length=100, target py311, rules E/F/I/UP/B/ASYNC/DJ/S/SIM/TRY/RUF/PTH). Run `uv run ruff check .` and `uv run ruff format .` before each commit.
- **Async tests** require `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)`; use `AsyncClient`; fixtures stay sync. Run tests with `uv run pytest`. If port 5432 is taken, the test DB is on **5433** (see memory: scout-test-db-port-conflict).
- **data-testid** on new interactive UI elements (`{component}-{element}`, kebab-case) — only relevant to M4 report UI if built.
- **Commit cadence:** one commit per task (TDD: failing test → impl → passing → commit). Co-author trailer per repo convention.
- **No secrets in code.** Cube/DB creds via env; PATs via Fernet-encrypted `TenantConnection`.
- **Cube model lives in** `cube/model/*.yml`, version-controlled (the durable knowledge artifact).

---

# PHASE M1 — Real Connect ingestion + deliver-app app_structure

**Outcome:** A real Connect opportunity materializes `raw_*` tables AND its deliver-app form schema lands in `TenantMetadata.metadata` under `form_definitions`, ready for auto-modeling.

### Task 1: Fetch deliver-app `app_structure` in ConnectMetadataLoader

**Files:**
- Modify: `mcp_server/loaders/connect_metadata.py` (currently lines 1-45: `ConnectMetadataLoader` with `load()`, `_fetch_org_data()`, `_fetch_opportunity_detail()`)
- Test: `tests/test_connect_metadata_loader.py` (create if absent)

**Interfaces:**
- Consumes: `ConnectBaseLoader._get(url)` (returns `requests.Response`, raises `ConnectAuthError` on 401/403); `self.base_url`, `self.opportunity_id`. **Reuses** `mcp_server.loaders.commcare_metadata._extract_form_definitions` (module-level fn taking a list of HQ app JSON dicts → `form_definitions` keyed by xmlns) and `_extract_case_types`.
- Produces: `ConnectMetadataLoader.load()` returns a dict that now ALSO contains `form_definitions: dict[str, FormDef]` **in the exact CommCare shape** produced by `_extract_form_definitions` — keyed by xmlns, each `FormDef = {"name", "app_name", "module_name", "case_type", "questions": [...]}`. This is identical to what `_build_jsonb_annotations` and CommCare staging already consume, maximizing reuse.

**Connect endpoint (verified in `dimagi/commcare-connect` `data_export` app):** `GET {base_url}/export/opportunity/{id}/app_structure/?app_type=both` returns `{"learn_app": <HQ app JSON | null>, "deliver_app": <HQ app JSON | null>}`. Each app is the full CommCare HQ application structure (`{"id", "name", "modules": [{"name", "forms": [{"xmlns", "name", "questions": [...]}]}]}`). **Connect proxies to HQ server-side via the opportunity's stored `api_key`** — the caller needs only the Connect OAuth `export` scope; NO CommCare HQ credentials. Normalize by passing the non-null apps as a list straight into the existing `_extract_form_definitions([...])`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connect_metadata_loader.py
from unittest import mock

from mcp_server.loaders.connect_metadata import ConnectMetadataLoader

# Real Connect /app_structure/ shape: {"learn_app": <HQ app JSON>, "deliver_app": <HQ app JSON>}
# Each app is HQ application JSON: app -> modules -> forms -> questions.
APP_STRUCTURE_PAYLOAD = {
    "learn_app": None,
    "deliver_app": {
        "id": "app_deliver",
        "name": "MUAC Deliver",
        "modules": [
            {
                "name": "Delivery",
                "case_type": "beneficiary",
                "forms": [
                    {
                        "xmlns": "http://openrosa.org/formdesigner/muac1",
                        "name": "MUAC Visit",
                        "questions": [
                            {"label": "MUAC (cm)", "value": "/data/muac_group/muac",
                             "tag": "input", "type": "Decimal", "repeat": False},
                            {"label": "MUAC confirmed", "value": "/data/muac_group/muac_confirmed",
                             "tag": "select1", "type": "Select", "repeat": False},
                        ],
                    }
                ],
            }
        ],
    },
}


def _loader():
    return ConnectMetadataLoader(
        opportunity_id=1237,
        credential={"type": "api_key", "value": "tok"},
        base_url="https://connect.example",
    )


def test_load_includes_form_definitions_from_app_structure():
    loader = _loader()
    with mock.patch.object(loader, "_fetch_org_data", return_value={"organizations": [], "programs": [], "opportunities": []}), \
         mock.patch.object(loader, "_fetch_opportunity_detail", return_value={"name": "Demo", "id": 1237}), \
         mock.patch.object(loader, "_fetch_app_structure", return_value=APP_STRUCTURE_PAYLOAD):
        result = loader.load()

    # _extract_form_definitions keys by xmlns (CommCare shape):
    fd = result["form_definitions"]
    assert "http://openrosa.org/formdesigner/muac1" in fd
    form = fd["http://openrosa.org/formdesigner/muac1"]
    q = {item["value"]: item for item in form["questions"]}
    assert q["/data/muac_group/muac"]["type"] == "Decimal"
    assert q["/data/muac_group/muac_confirmed"]["label"] == "MUAC confirmed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_connect_metadata_loader.py -v`
Expected: FAIL — `ConnectMetadataLoader` has no `_fetch_app_structure`, and `load()` returns no `form_definitions`.

- [ ] **Step 3: Implement `_fetch_app_structure` + normalization, wire into `load()`**

```python
# mcp_server/loaders/connect_metadata.py
import logging

from mcp_server.loaders.commcare_metadata import _extract_case_types, _extract_form_definitions
from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


class ConnectMetadataLoader(ConnectBaseLoader):
    """Fetch metadata for a Connect opportunity."""

    def load(self) -> dict:
        org_data = self._fetch_org_data()
        opp_detail = self._fetch_opportunity_detail()
        form_definitions: dict = {}
        case_types: list = []
        try:
            app_structure = self._fetch_app_structure()
            # Real Connect returns {"learn_app": <HQ app JSON|null>, "deliver_app": ...}.
            # Each app is HQ application JSON; reuse the CommCare extractors verbatim.
            apps = [a for a in (app_structure.get("deliver_app"), app_structure.get("learn_app")) if a]
            form_definitions = _extract_form_definitions(apps)
            case_types = _extract_case_types(apps)
        except Exception:
            logger.exception(
                "Failed to fetch app_structure for opportunity %s; continuing without form_definitions",
                self.opportunity_id,
            )

        logger.info(
            "Loaded metadata for Connect opportunity %s: %s (%d forms)",
            self.opportunity_id,
            opp_detail.get("name", "unknown"),
            len(form_definitions),
        )
        return {
            "opportunity": opp_detail,
            "organizations": org_data.get("organizations", []),
            "programs": org_data.get("programs", []),
            "all_opportunities": org_data.get("opportunities", []),
            "form_definitions": form_definitions,
            "case_types": case_types,
        }

    def _fetch_org_data(self) -> dict:
        url = f"{self.base_url}/export/opp_org_program_list/"
        return self._get(url).json()

    def _fetch_opportunity_detail(self) -> dict:
        url = f"{self.base_url}/export/opportunity/{self.opportunity_id}/"
        return self._get(url).json()

    def _fetch_app_structure(self) -> dict:
        url = f"{self.base_url}/export/opportunity/{self.opportunity_id}/app_structure/"
        return self._get(url, params={"app_type": "both"}).json()
```

> **Verify at task start:** confirm `_extract_form_definitions` / `_extract_case_types` are importable module-level names in `mcp_server/loaders/commcare_metadata.py` (the verbatim extract shows they are called from `CommCareMetadataLoader.load`). Confirm `ConnectBaseLoader._get` accepts a `params=` kwarg; if it does not, add the query string to the URL instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_connect_metadata_loader.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check mcp_server/loaders/connect_metadata.py tests/test_connect_metadata_loader.py
uv run ruff format mcp_server/loaders/connect_metadata.py tests/test_connect_metadata_loader.py
git add mcp_server/loaders/connect_metadata.py tests/test_connect_metadata_loader.py
git commit -m "feat(connect): fetch deliver-app app_structure into form_definitions"
```

> **Note on graceful degradation:** `load()` swallows app_structure errors so existing real-Connect ingestion never regresses if the endpoint is missing for a given opp. The discover phase (`materializer._run_discover_phase`, lines 525-558) already stores `loader.load()` verbatim into `TenantMetadata.metadata`, so `form_definitions` lands automatically — no materializer change needed for M1.

### Task 2: Verify end-to-end discover persists form_definitions (integration)

**Files:**
- Test: `tests/test_connect_discover_integration.py` (create)

**Interfaces:**
- Consumes: `mcp_server.services.materializer._run_discover_phase(tenant_membership, credential, pipeline)`; `PipelineRegistry.get_by_provider("commcare_connect")`; `TenantMetadata`.

- [ ] **Step 1: Write the failing test** — patch `ConnectMetadataLoader.load` to return a dict containing `form_definitions`, call `_run_discover_phase` for a `commcare_connect` membership, assert `TenantMetadata.metadata["form_definitions"]` persisted.

```python
# tests/test_connect_discover_integration.py
import pytest
from unittest import mock

from mcp_server.services import materializer
from mcp_server.pipeline_registry import PipelineRegistry


@pytest.mark.django_db(transaction=True)
def test_discover_persists_form_definitions(connect_tenant_membership):
    pipeline = PipelineRegistry().get_by_provider("commcare_connect")
    fake = {"opportunity": {"name": "Demo"}, "organizations": [], "programs": [],
            "all_opportunities": [], "form_definitions": {"muac_visit": {"questions": []}}}
    with mock.patch.object(materializer.ConnectMetadataLoader, "load", return_value=fake):
        result = materializer._run_discover_phase(
            connect_tenant_membership, {"type": "api_key", "value": "t"}, pipeline
        )
    from apps.workspaces.models import TenantMetadata
    tm = TenantMetadata.objects.get(tenant_membership=connect_tenant_membership)
    assert "muac_visit" in tm.metadata["form_definitions"]
    assert result["form_definitions"]["muac_visit"] == {"questions": []}
```

(Add a `connect_tenant_membership` fixture in `tests/conftest.py` creating a `Tenant(provider="commcare_connect", external_id="1237")`, a `User`, and a `TenantMembership` — follow existing tenant fixtures in the suite.)

- [ ] **Step 2:** Run → FAIL (fixture/behavior missing). `uv run pytest tests/test_connect_discover_integration.py -v`
- [ ] **Step 3:** Add the fixture; no production change expected (Task 1 already wired it).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

```bash
git add tests/test_connect_discover_integration.py tests/conftest.py
git commit -m "test(connect): verify discover persists form_definitions"
```

---

# PHASE M2 — Connect form_json staging + auto column_notes

**Outcome:** `raw_visits.form_json` is staged into typed, human-labeled columns (with repeat-group child tables and choice-list awareness) driven by `form_definitions`; `TableKnowledge.column_notes` is auto-populated. The raw-SQL agent immediately improves; the eval baseline lifts.

### Task 3: Connect staging generator (`connect_staging.py`)

**Files:**
- Create: `apps/transformations/services/connect_staging.py`
- Reuse (import from): `apps/transformations/services/commcare_staging.py` helpers — `_question_path_to_json_path`, `_column_name_from_path`, `_typed_expression`, `slugify_model_name`, `_unique_alias` (verify exact names/signatures in that file before importing; if private, lift the needed helpers into a shared `apps/transformations/services/_form_staging.py` module and have both import from it).
- Test: `tests/test_connect_staging.py`

**Interfaces:**
- Consumes: `form_definitions` (shape from Task 1); `TransformationAsset` model (scope/tenant/name/sql_content/test_yaml).
- Produces: `generate_connect_assets(form_definitions: dict, tenant) -> list[TransformationAsset]` — one staging asset `stg_visits` flattening `raw_visits.form_json` into typed columns (one per non-repeat question, labeled by column name derived from the question path), plus one `stg_visits__repeat_<group>` asset per repeat group. SQL conventions mirror `commcare_staging` form/repeat SQL (Postgres `#>>`/`jsonb_array_elements ... WITH ORDINALITY`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connect_staging.py
import pytest

from apps.transformations.services.connect_staging import generate_connect_assets

FORM_DEFS = {
    "muac_visit": {
        "name": "MUAC Visit",
        "deliver_unit": "muac_visit",
        "questions": [
            {"label": "MUAC (cm)", "value": "/data/muac_group/muac", "type": "Decimal", "repeat": False, "options": None},
            {"label": "Confirmed", "value": "/data/muac_group/muac_confirmed", "type": "Select", "repeat": False, "options": ["yes", "no"]},
            {"label": "Child name", "value": "/data/children/child_name", "type": "Text", "repeat": True, "options": None},
        ],
    }
}


@pytest.mark.django_db(transaction=True)
def test_generates_visit_staging_with_typed_columns(connect_tenant):
    assets = generate_connect_assets(FORM_DEFS, connect_tenant)
    by_name = {a.name: a for a in assets}

    assert "stg_visits" in by_name
    sql = by_name["stg_visits"].sql_content
    # Non-repeat questions become typed, aliased columns from form_json:
    assert "form_json" in sql
    assert "muac" in sql                 # column derived from /data/muac_group/muac
    assert "muac_confirmed" in sql
    # Repeat question is NOT inlined into stg_visits:
    assert "child_name" not in sql

    # Repeat group becomes its own child asset:
    assert "stg_visits__repeat_children" in by_name
    rsql = by_name["stg_visits__repeat_children"].sql_content
    assert "jsonb_array_elements" in rsql
    assert "child_name" in rsql
```

- [ ] **Step 2:** Run → FAIL (`connect_staging` missing). `uv run pytest tests/test_connect_staging.py -v`

- [ ] **Step 3: Implement `generate_connect_assets`** — adapt `commcare_staging`'s form/repeat generators to read from `form_definitions` and target `raw_visits.form_json` (instead of `raw_forms.form_data` keyed by xmlns). Group questions by repeat status; non-repeat → typed columns on `stg_visits`; each repeat path prefix → a `stg_visits__repeat_<group>` asset using `jsonb_array_elements(... form_json #> ...) WITH ORDINALITY`. Use the shared typed-expression helper so `Decimal/Int/Date` map to the same casts as CommCare staging. Set `scope=TransformationScope.SYSTEM`, `tenant=tenant`.

(Full implementation mirrors `_generate_form_asset` / `_generate_repeat_group_asset` from `commcare_staging.py`; reuse those helpers rather than re-deriving the JSON-path and casting logic.)

- [ ] **Step 4:** Run → PASS. `uv run pytest tests/test_connect_staging.py -v`
- [ ] **Step 5:** Lint + commit.

```bash
git add apps/transformations/services/connect_staging.py tests/test_connect_staging.py
# include _form_staging.py + commcare_staging.py if helpers were extracted
git commit -m "feat(transform): stage raw_visits.form_json into typed/labeled columns"
```

### Task 4: Auto-populate `TableKnowledge.column_notes` from form_definitions

**Files:**
- Create: `apps/knowledge/services/column_note_generator.py`
- Test: `tests/test_column_note_generator.py`

**Interfaces:**
- Consumes: `form_definitions` (Task 1 shape); `TableKnowledge` model (`workspace`, `table_name`, `column_notes` dict, `unique_together=[workspace, table_name]`).
- Produces: `async def sync_column_notes(workspace, table_name: str, form_definitions: dict) -> TableKnowledge` — upserts `TableKnowledge` and merges per-column notes derived from question label+type+options, e.g. `{"muac": "MUAC (cm) — Decimal", "muac_confirmed": "Confirmed — Select; values: yes, no"}`. Column name derived via the same path→column helper used in Task 3.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_column_note_generator.py
import pytest

from apps.knowledge.services.column_note_generator import sync_column_notes

FORM_DEFS = {
    "muac_visit": {"questions": [
        {"label": "MUAC (cm)", "value": "/data/muac_group/muac", "type": "Decimal", "options": None, "repeat": False},
        {"label": "Confirmed", "value": "/data/muac_group/muac_confirmed", "type": "Select", "options": ["yes", "no"], "repeat": False},
    ]}
}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_column_notes_populates_from_form_defs(workspace):
    tk = await sync_column_notes(workspace, "stg_visits", FORM_DEFS)
    assert "Decimal" in tk.column_notes["muac"]
    assert "yes, no" in tk.column_notes["muac_confirmed"]
```

- [ ] **Step 2:** Run → FAIL. `uv run pytest tests/test_column_note_generator.py -v`
- [ ] **Step 3: Implement** `sync_column_notes` using `await TableKnowledge.objects.aupdate_or_create(workspace=..., table_name=..., defaults={"column_notes": merged, "description": ...})`. Build notes by iterating non-repeat questions, deriving column name from path, formatting `f"{label} — {type}"` (+ `; values: ...` when `options`). Async ORM only.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

```bash
git add apps/knowledge/services/column_note_generator.py tests/test_column_note_generator.py
git commit -m "feat(knowledge): auto-populate column_notes from Connect form_definitions"
```

> `KnowledgeRetriever._format_table_knowledge()` (retriever.py lines ~70-113) already renders `column_notes` into the agent prompt — no retriever change needed. This is why M2 lifts the raw-SQL baseline for free.

### Task 5: Wire Connect staging + column notes into the materializer transform phase

**Files:**
- Modify: `mcp_server/services/materializer.py` — the TRANSFORM phase, where CommCare staging is invoked (find the call to `upsert_system_assets`/`generate_system_assets`; add a `commcare_connect` branch).
- Test: `tests/test_connect_transform_wiring.py`

**Interfaces:**
- Consumes: discovered `metadata["form_definitions"]`; `generate_connect_assets` (Task 3); `sync_column_notes` (Task 4); existing dbt asset upsert/run path.

- [ ] **Step 1:** Write a failing test asserting that, for a `commcare_connect` pipeline with `form_definitions` present, the transform phase creates a `stg_visits` `TransformationAsset` and a `TableKnowledge` row with column notes. Patch dbt execution.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add a branch in the transform phase: when `pipeline.provider == "commcare_connect"`, call `generate_connect_assets(metadata["form_definitions"], tenant)`, upsert via the existing asset-upsert helper, and call `sync_column_notes(...)` for `stg_visits`. Follow the existing CommCare staging invocation pattern exactly (same scope/tenant, same dbt run trigger).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

```bash
git add mcp_server/services/materializer.py tests/test_connect_transform_wiring.py
git commit -m "feat(transform): generate Connect staging + column notes during materialization"
```

**M1+M2 acceptance:** materialize a real Connect opportunity → `raw_visits` present, `stg_visits` (+ repeat children) generated, `TableKnowledge.column_notes` populated, and the chat agent answers a form-field question (e.g. "average MUAC by status") using the labeled staging columns — all WITHOUT Cube yet. This is the baseline the eval (M4) measures against.

---

# PHASE M3 — Cube Core + generated model + `semantic_query`

> **Architecture mandate (user decision):** M3 is built **fully multi-tenant** — Cube must map to Scout's per-tenant/per-workspace schema routing, not a single-schema shortcut. The design (single source of truth = Scout):
> 1. **Scout resolves** `workspace_id → schema_name` via the existing `mcp_server/context.py::load_workspace_context()` (single-tenant → `t_<id>`; multi-tenant → `ws_<hash>`). Cube never re-implements this routing.
> 2. **Scout mints a JWT** signed with `CUBEJS_API_SECRET`, carrying `securityContext = {workspace_id, schema_name}`, and connects to Cube's pg-wire SQL API passing the JWT as the password.
> 3. **Cube's `cube.js`**: `checkSqlAuth` verifies the JWT and returns its `securityContext`; `contextToAppId`/`contextToOrchestratorId` key compilation + cache per `workspace_id`; the generated data model uses `sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.stg_visits"` so ONE model definition resolves to the correct schema per request. One managed DB, dynamic schema via `COMPILE_CONTEXT` (no per-tenant `driverFactory` needed).
>
> The model generator (Task 7) drives an LLM, so its steps test the output **contract** (incl. the `COMPILE_CONTEXT` schema templating), not literal YAML.

### Task 6: Cube Core service + multi-tenant `cube.js` config

**Files:**
- Create: `cube/model/.gitkeep`, `cube/cube.js` (multi-tenant config), `cube/README.md`
- Modify: `docker-compose.yml` (add `cube` service), `.env.example` (Cube env vars)
- Test: `tests/test_cube_service_smoke.py` (skipped unless `CUBE_URL`/`CUBEJS_API_SECRET` set — a connectivity smoke test that mints a JWT and runs a trivial query)

**Interfaces:**
- Produces: a running Cube Core container reading `cube/model/` + `cube/cube.js`, connected to the **managed DB** (same DB Scout's tenant schemas live in), exposing the SQL API (pg-wire) and REST API. Multi-tenant: schema is selected per request from the JWT `securityContext.schema_name`. Env: `CUBEJS_DB_TYPE=postgres`, `CUBEJS_DB_*` (managed DB), `CUBEJS_API_SECRET` (JWT signing — MUST match what Scout signs with), `CUBEJS_PG_SQL_PORT`.

- [ ] **Step 1:** Add the `cube` service to `docker-compose.yml`, pointed at the managed DB (here the same `platform-db`/managed Postgres; use the managed-DB env). Mount `./cube`:

```yaml
  cube:
    image: cubejs/cube:latest
    ports:
      - "4000:4000"      # REST/playground/meta
      - "15432:15432"    # SQL API (pg-wire)
    environment:
      CUBEJS_DEV_MODE: "true"
      CUBEJS_DB_TYPE: postgres
      CUBEJS_DB_HOST: platform-db
      CUBEJS_DB_PORT: "5432"
      CUBEJS_DB_NAME: agent_platform
      CUBEJS_DB_USER: platform
      CUBEJS_DB_PASS: ${PLATFORM_DB_PASSWORD:-devpassword}
      CUBEJS_API_SECRET: ${CUBEJS_API_SECRET}
      CUBEJS_PG_SQL_PORT: "15432"
    volumes:
      - ./cube:/cube/conf
    depends_on:
      platform-db:
        condition: service_healthy
```
(Note: `MANAGED_DATABASE_URL` may differ from `DATABASE_URL` in real deploys — read `mcp_server/context.py` for how the managed DB is resolved and wire Cube to the SAME managed DB. Confirm at task start.)

- [ ] **Step 2:** Create `cube/cube.js` implementing the multi-tenant security model (verify against current Cube docs at task start):

```javascript
const jwt = require("jsonwebtoken");

module.exports = {
  // Verify the JWT Scout passes as the SQL-API password; return its securityContext.
  checkSqlAuth: (query, username, password) => {
    const decoded = jwt.verify(password, process.env.CUBEJS_API_SECRET);
    return {
      password,
      securityContext: {
        workspace_id: decoded.workspace_id,
        schema_name: decoded.schema_name,
      },
    };
  },
  // Per-workspace compilation + cache isolation (also drives COMPILE_CONTEXT).
  contextToAppId: ({ securityContext }) => `ws_${securityContext.schema_name}`,
  contextToOrchestratorId: ({ securityContext }) => `ws_${securityContext.schema_name}`,
};
```

- [ ] **Step 3:** `cube/model/.gitkeep` (generated models land here) + `cube/README.md` documenting: run via `docker compose up cube`, the JWT `securityContext` shape (`{workspace_id, schema_name}`), that models use `{COMPILE_CONTEXT.security_context.schema_name}`, and that `CUBEJS_API_SECRET` MUST equal the secret Scout signs JWTs with (Task 8). Add `CUBEJS_API_SECRET` + cube ports to `.env.example`.
- [ ] **Step 4:** `tests/test_cube_service_smoke.py` — skipped unless `CUBE_URL` and `CUBEJS_API_SECRET` are set; when set, mint a JWT (`{workspace_id, schema_name}`), connect to the SQL API, and assert a trivial `SELECT 1`. (CI-safe: skips by default.)
- [ ] **Step 5:** Verify locally if Docker available: `docker compose config` parses; `docker compose up cube` boots and `http://localhost:4000` serves. If Docker isn't available in the environment, validate `cube.js` syntax (`node --check cube/cube.js`) and `docker compose config`, and note the live-boot check as a manual step. Commit.

```bash
git add docker-compose.yml .env.example cube/
git commit -m "feat(cube): add multi-tenant Cube Core service + cube.js security config"
```

### Task 7: Cube model generator (schema + form_definitions + knowledge → YAML)

**Files:**
- Create: `apps/transformations/services/cube_model_generator.py`
- Create: `apps/transformations/services/cube_model_schema.py` (Pydantic models validating generated YAML)
- Test: `tests/test_cube_model_generator.py`

**Interfaces:**
- Consumes: staged-schema column list (from `pipeline_describe_table`/information_schema for `stg_visits` & children), `form_definitions`, `KnowledgeEntry` metric definitions, declared `RelationshipConfig`s, plus seed KPI hints (`muac_confirmation_rate`, `approval_rate`, `flag_rate`).
- Produces: `async def generate_cube_model(tenant, staged_tables: list[dict], form_definitions: dict, knowledge: str) -> list[CubeFile]` where `CubeFile = {"path": "cube/model/visits.yml", "yaml": str}`. Each generated cube validates against `cube_model_schema.CubeModel` (cubes with `name`, `sql_table`/`sql`, `dimensions[]`, `measures[]`, `joins[]`; optional `views[]`). Writes files to `cube/model/` and records a `TransformationAssetRun`-style audit row.

**Generated-YAML contract** (what tests assert):
- One cube per staged table (`visits` ← `stg_visits`, `flws` ← `raw_users`, etc.).
- **Multi-tenant `sql_table`:** every cube uses `sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.<table>"` (e.g. `…schema_name}.stg_visits`) so the model resolves to the correct per-workspace schema at compile time. Tests MUST assert this templating is present (not a hard-coded schema).
- Dimensions: every staged column → dimension with `type` mapped from question type; choice-list questions → `type: string` dimensions; the question `label` carried into a `title`/`description`.
- Joins: from `RelationshipConfig` (e.g. `visits.username = flws.username`, `many_to_one`).
- Measures: `count`; plus seeded measures from KPI hints (e.g. `muac_confirmation_rate` as `type: number, sql: AVG(CASE WHEN muac_confirmed='yes' THEN 1.0 ELSE 0 END)`), and any `KnowledgeEntry` tagged `metric` translated to a measure.
- A `program_health` view including curated measures/dimensions.

- [ ] **Step 1:** Write tests asserting the **contract** on a generated model for a fixed input (validate with `cube_model_schema`, assert presence of the `visits` cube, a `count` measure, a `muac_confirmation_rate` measure, and a `visits→flws` join). Use a stubbed/mock LLM so the test is deterministic — the generator takes an injectable `model_client` and the test passes a fake returning canned YAML; the generator's job under test is: prompt assembly, YAML parse, schema validation, file emission.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement: assemble prompt from inputs; call `model_client`; parse YAML; validate against `cube_model_schema`; on validation failure, one repair round-trip; write `CubeFile`s; return them. Use `langchain-anthropic` consistent with `apps/agents` (default to the latest Claude model). LLM call wrapped per async conventions.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

```bash
git add apps/transformations/services/cube_model_generator.py apps/transformations/services/cube_model_schema.py tests/test_cube_model_generator.py
git commit -m "feat(cube): generate validated Cube model from schema + form defs + knowledge"
```

### Task 8: `semantic_query` + `semantic_catalog` MCP tools

**Files:**
- Create: `mcp_server/services/semantic.py` (Cube SQL-API connection + catalog fetch)
- Modify: `mcp_server/server.py` (register two `@mcp.tool()` tools, mirroring the `query` tool at lines 335-378)
- Test: `tests/test_semantic_query.py`

**Multi-tenant auth (the core of this task):** `semantic_query` must scope to the caller's workspace exactly as the raw `query` tool does — by reusing `load_workspace_context(workspace_id)` (single source of truth) to resolve the `schema_name`, then minting a short-lived JWT signed with `CUBEJS_API_SECRET` carrying `securityContext = {workspace_id, schema_name}`, and passing it as the Cube SQL-API password. Cube's `cube.js` (Task 6) verifies it and selects the schema via `COMPILE_CONTEXT`. The agent NEVER sees or sets the schema — it's injected server-side, same as the raw query path.

**Files:**
- Create: `mcp_server/services/semantic.py` (resolve schema → mint JWT → Cube pg-wire connect; REST `/v1/meta` with the JWT)
- Modify: `mcp_server/server.py` (register `semantic_query` + `semantic_catalog` tools, mirroring `query` at lines 335-378); settings for `CUBEJS_API_SECRET`, `CUBE_SQL_HOST/PORT`, `CUBE_REST_URL`
- Dependency: `PyJWT` (confirm present in `pyproject.toml`; add if missing)
- Test: `tests/test_semantic_query.py`

**Interfaces:**
- Consumes: `load_workspace_context(workspace_id)` (→ `schema_name`); `settings.CUBEJS_API_SECRET`; `success_response`/`error_response`/`tool_context` envelope helpers.
- Produces:
  - `async def mint_cube_jwt(workspace_id: str, schema_name: str) -> str` — short-TTL JWT signed with `CUBEJS_API_SECRET`, claims `{workspace_id, schema_name, exp}`.
  - `semantic_query(sql: str, workspace_id: str = "") -> dict` — resolves context, mints JWT, connects to Cube's pg-wire SQL API (`psycopg`, password=JWT), runs the Semantic SQL (`MEASURE(...)`), returns the same envelope shape as `query` (`columns`, `rows`, `row_count`, `sql_executed`).
  - `semantic_catalog(workspace_id: str = "") -> dict` — `GET {CUBE_REST_URL}/v1/meta` with `Authorization: Bearer <JWT>`; returns available cubes/views + their measures & dimensions so the agent knows what it can ask for.

- [ ] **Step 1:** Test (no live Cube): assert `mint_cube_jwt` produces a token that decodes (with `CUBEJS_API_SECRET`) to `{workspace_id, schema_name}`. Patch `load_workspace_context` and the Cube pg-wire connection to assert `semantic_query` (a) resolves the schema, (b) connects with the JWT as password, (c) returns the standard success envelope. Patch httpx for `semantic_catalog` `/v1/meta`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `semantic.py` (JWT mint via PyJWT; psycopg connect to Cube pg-wire with password=JWT; httpx GET `/v1/meta`); register both tools in `server.py` using the existing `tool_context` + `success_response` pattern verbatim from the `query` tool. Keep the schema injection server-side (agent never passes schema/JWT).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

```bash
git add mcp_server/services/semantic.py mcp_server/server.py tests/test_semantic_query.py
git commit -m "feat(mcp): add semantic_query + semantic_catalog tools backed by Cube"
```

### Task 9: Agent routing — prefer governed measures, fall back to raw SQL, record fallbacks

**Files:**
- Modify: `apps/agents/graph/base.py` (tool binding/prompt) and the agent system prompt source.
- Create: `apps/agents/services/fallback_log.py` (record an "unmodeled question" signal when the agent uses raw `query` for a metric-style question).
- Test: `tests/test_agent_semantic_routing.py`

**Interfaces:**
- Consumes: `semantic_catalog` output (inject available measures into the prompt); existing tool-binding mechanism (`INJECTED_TOOL_PARAMS`, `@mcp.tool` registration).
- Produces: prompt guidance "prefer `semantic_query` for governed metrics; use `query` only when no measure fits"; a `record_fallback(workspace, question, sql)` hook persisting a lightweight `ModelGapSignal` row (define minimal model) consumed by M5.

- [ ] **Step 1:** Test: given a workspace with a populated `semantic_catalog`, the assembled system prompt contains the catalog and the routing instruction; `record_fallback` writes a `ModelGapSignal`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement prompt injection + `ModelGapSignal` model/migration + `record_fallback`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

```bash
git add apps/agents/ tests/test_agent_semantic_routing.py
git commit -m "feat(agent): route to governed measures with raw-SQL fallback + gap logging"
```

**M3 acceptance:** with Cube running and a generated model, the agent answers "MUAC confirmation rate by FLW archetype" via `semantic_query` (governed measure), and a question with no matching measure falls back to raw SQL and logs a `ModelGapSignal`.

---

# PHASE M4 — Eval framework (the adoption verdict)

### Task 10: `apps/evals` models — `GoldenQuery` + `EvalRun`

**Files:**
- Create: `apps/evals/__init__.py`, `apps/evals/models.py`, `apps/evals/apps.py`, migration
- Modify: `config/settings/base.py` (add `apps.evals`)
- Test: `tests/test_evals_models.py`

**Interfaces:**
- Produces:
  - `GoldenQuery(workspace, title, question, reference_sql, expected_summary, source)`
  - `EvalRun(workspace, golden_query, free_sql, free_sql_result(JSON), free_sql_ms, cube_query, cube_result(JSON), cube_ms, result_match(bool), match_confidence(float), semantic_equivalence(choices: exact/approximate/failed), used_preaggregation(bool), created_at)`

- [ ] Steps 1-5: failing test for model creation/fields → add models + migration (`uv run python manage.py makemigrations evals`) → passing test → commit.

```bash
git add apps/evals/ config/settings/base.py tests/test_evals_models.py
git commit -m "feat(evals): GoldenQuery + EvalRun models"
```

### Task 11: Eval runner — answer both ways, compare, judge

**Files:**
- Create: `apps/evals/services/runner.py`, `apps/evals/services/judge.py`
- Test: `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: the agent (free-SQL path via raw `query`; Cube path via `semantic_query`), `GoldenQuery`, an LLM judge.
- Produces: `async def run_eval(golden_query, *, runs: int = 3, use_preagg: bool = False) -> list[EvalRun]` — executes each path `runs` times, compares result sets deterministically, calls `judge.semantic_equivalence(free_result, cube_result)` for an LLM verdict, records latency and `used_preaggregation`, persists `EvalRun`s.

- [ ] Steps 1-5: failing test (mock both agent paths + judge; assert `EvalRun` rows with `result_match` and latency) → implement → passing → commit.

```bash
git add apps/evals/services/ tests/test_eval_runner.py
git commit -m "feat(evals): runner comparing free-SQL vs Cube with LLM judge"
```

### Task 12: Seed golden questions + scorecard command

**Files:**
- Create: `apps/evals/management/commands/run_eval.py`, `apps/evals/fixtures/golden_questions.yml`
- Create: `apps/evals/services/scorecard.py`
- Test: `tests/test_eval_scorecard.py`

**Interfaces:**
- Produces: `python manage.py run_eval --workspace <id> [--preagg]` → loads ~8-15 seed `GoldenQuery`s, runs the runner, prints a scorecard (correctness %, consistency/variance, mean latency, # Cube-answerable, preagg speedup) and writes a JSON report under `docs/superpowers/evals/`.

- [ ] Steps 1-5: failing test for `scorecard.summarize(eval_runs)` → implement command + scorecard → passing → commit.

```bash
git add apps/evals/management/ apps/evals/fixtures/ apps/evals/services/scorecard.py tests/test_eval_scorecard.py
git commit -m "feat(evals): seed golden questions + scorecard command (incl. pre-agg dimension)"
```

> **Pre-aggregations dimension:** Task 12 runs the suite twice (`--preagg` off/on). Add a pre-aggregation declaration to the generated `program_health` view (Task 7 can emit a default rollup) so the scorecard reports the caching speedup — completing the adoption verdict (governance *and* performance).

**M4 acceptance:** `run_eval` produces a scorecard comparing free-SQL vs Cube on correctness, consistency, and latency (with/without pre-aggregations) — the go/no-go evidence.

---

# PHASE M5 — Self-improving loop + PR-based curation gate

### Task 13: Promote model gaps + learnings into candidate measures

**Files:**
- Create: `apps/transformations/services/measure_proposer.py`
- Test: `tests/test_measure_proposer.py`

**Interfaces:**
- Consumes: `ModelGapSignal` rows (Task 9), `AgentLearning` (category `aggregation`/`join_pattern`, high confidence), the current Cube model.
- Produces: `async def propose_measures(workspace) -> list[CubeFile]` — LLM proposes new measures/dimensions as a YAML diff over the existing model, validated against `cube_model_schema`; deduped against existing measures.

- [ ] Steps 1-5: failing test (mock LLM + seeded gaps/learnings; assert proposed measure validates and is novel) → implement → passing → commit.

```bash
git add apps/transformations/services/measure_proposer.py tests/test_measure_proposer.py
git commit -m "feat(cube): propose new measures from model gaps + agent learnings"
```

### Task 14: PR-based curation gate

**Files:**
- Create: `apps/transformations/services/cube_curation_pr.py`
- Create: `apps/transformations/management/commands/propose_cube_measures.py`
- Test: `tests/test_cube_curation_pr.py`

**Interfaces:**
- Consumes: `propose_measures` output.
- Produces: `python manage.py propose_cube_measures --workspace <id>` → writes proposed YAML into a branch and opens a PR via `gh` (the human approves/merges = the curation gate). On merge, `cube/model/` updates and Cube reloads. Never auto-merges.

- [ ] Steps 1-5: failing test (mock `gh`/git; assert a branch+PR are created with the proposed diff and a descriptive body citing the originating gap/learning) → implement (shell out to `git`/`gh`, `sync_to_async` for the subprocess) → passing → commit.

```bash
git add apps/transformations/services/cube_curation_pr.py apps/transformations/management/commands/propose_cube_measures.py tests/test_cube_curation_pr.py
git commit -m "feat(cube): PR-based curation gate for proposed measures"
```

**M5 acceptance:** a logged model gap produces a candidate measure, which opens a PR for human review; merging it grows the governed model — the "persist knowledge over time" loop, closed.

---

# PHASE M1b — connect-labs synthetic source (parallel, after connect-labs#637)

> Non-blocking. Reuses all of M2-M5. Only adds a second data source.

### Task 15: connect-labs credential + base-URL override

**Files:**
- Modify: `apps/users/models.py` (`PROVIDER_CHOICES` — add `commcare_connect_labs` or reuse `commcare_connect` with a stored base URL), credential resolver if needed.
- Modify: `mcp_server/loaders/connect_base.py` — already accepts `base_url`; ensure it is threaded from a per-tenant setting (the synthetic base URL) rather than only `settings.CONNECT_API_URL`.
- Test: `tests/test_connect_labs_credential.py`

- [ ] Steps 1-5: failing test (a connect-labs membership resolves a PAT credential and a synthetic base URL) → implement → passing → commit.

### Task 16: `connect_labs_sync` pipeline + discovery

**Files:**
- Create: `pipelines/connect_labs_sync.yml` (same sources as `connect_sync.yml`, provider routes to Connect loaders with the synthetic base URL).
- Modify: `mcp_server/services/materializer.py::_load_connect_source` to pass the per-tenant `base_url` into loaders.
- Test: `tests/test_connect_labs_pipeline.py`

- [ ] Steps 1-5: failing test (synthetic opp materializes `raw_visits` + `app_structure` from `/api/export/...`) → implement → passing → commit.

**M1b acceptance:** a synthetic opportunity from connect-labs#637 flows through the identical M2-M5 machinery; the M4 eval gains a ground-truth dataset (manifest KPIs/anomalies) for a higher-confidence verdict.

---

## Self-Review

**Spec coverage:**
- §4.1a real Connect + app_structure → M1 (Tasks 1-2). ✓
- §4.1b synthetic → M1b (Tasks 15-16). ✓
- §4.2a enriched staging + column_notes → M2 (Tasks 3-5). ✓
- §4.2b Cube model generation → M3 (Task 7). ✓
- §4.3 Cube Core deployment → M3 (Task 6). ✓
- §4.4 agent↔Cube interface 1 + fallback → M3 (Tasks 8-9). ✓
- §4.5 eval framework (+ ground-truth note) → M4 (Tasks 10-12). ✓
- §4.6 self-improving loop + PR curation → M5 (Tasks 13-14). ✓
- §7 pre-aggregations eval dimension → M4 Task 12 note + Task 7 rollup. ✓
- §7 curation-gate mechanism (PR-based) → M5 Task 14. ✓

**Placeholder scan:** M1/M2 steps carry complete, grounded code. M3-M5 tasks are contract-defined where they drive an LLM (Tasks 7, 11, 13) — tests assert output contracts, which is the correct way to plan non-deterministic steps; not placeholders. Literal infra/model/tool code (Tasks 6, 8, 10, 14) is concrete.

**Type consistency:** `form_definitions` shape is defined once in Task 1 and consumed identically in Tasks 3, 4, 7. `CubeFile`/`CubeModel` defined in Task 7, reused in Tasks 13-14. Envelope helpers (`success_response`/`tool_context`) reused verbatim from the existing `query` tool in Task 8.

**Known verification points (resolve at task start, not plan-time):**
- Exact helper names/visibility in `commcare_staging.py` before importing in Task 3 (extract to `_form_staging.py` if private).
- The real Connect `/export/opportunity/{id}/app_structure/` payload shape (Task 1 normalizer assumes `deliver_units[].questions[]`; adjust to the actual response).
- Where `upsert_system_assets` is invoked in the transform phase (Task 5).
