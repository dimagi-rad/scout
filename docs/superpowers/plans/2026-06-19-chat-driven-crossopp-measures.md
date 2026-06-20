# Chat-Driven Cross-Opp Measure Creation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the chat agent create cross-opp canonical measures on demand — resolve a plain-language measure across every opp, pause for approval only when uncertain, commit it additively to the Cube model + lineage, and answer with expandable per-opp SQL. Plus an app-driven proposer that feeds the SAME engine.

**Architecture:** One trigger-agnostic engine (`crossopp_measure_service`) takes a `CanonicalMeasureSpec` → resolves across the workspace's opps (existing `measure_resolver`) → classifies doubt → commits (additive model regen + lineage + Cube reload) or drafts. Two spec sources feed it: the `define_crossopp_measure` agent tool (question-driven) and the `crossopp_measure_proposer` (app-driven). Doubt pauses via the proven fire-and-resume pattern (`ThreadJob` + `aupdate_state`), with an inline approval card rendered from the tool output. No native LangGraph `interrupt()`.

**Tech Stack:** Django 5 async ORM, DRF (sync `APIView`), Procrastinate tasks, LangGraph + langchain-anthropic, Cube.dev (dev-mode hot-reload), React 19 + Recharts/Tailwind, pytest + pytest-asyncio.

## Global Constraints

- **Async-first:** new tenant/streaming endpoints are `async def`; DRF `APIView` stays sync. Use async ORM (`aget`/`acreate`/`aupdate_or_create`/`async for`) in async contexts; never sync ORM from async. Wrap multi-write atomic blocks in `@sync_to_async` with `transaction.atomic()`. (enforced by `tests/test_async_conventions.py`)
- **Imports at module level**, except: optional deps guarded by try/except, and **agent tool factories** which import models inside the function to break circular deps (match `artifact_tool.py`).
- **ruff:** line-length=100, target py311, rules E/F/I/UP/B/ASYNC/DJ/S/SIM/TRY/RUF/PTH. Cube-SQL-composing modules carry a file-level `# ruff: noqa: S608`.
- **Async tests:** `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)` + `AsyncClient`; fixtures stay sync.
- **Measure-identity stability (#303):** every commit is additive — regenerating the model preserves existing measures' ids and SQL byte-for-byte except the one being added/updated.
- **Cube reload:** `CUBEJS_DEV_MODE=true` with `./cube` mounted → writing `cube/model/<ws_hash>/canonical.yml` hot-reloads. Commit polls `/cubejs-api/v1/meta` for the new measure; restart is the documented fallback.
- **data-testid** on every new interactive UI element, `{component}-{element}` kebab-case.
- **DEFAULT_LLM_MODEL** for all LLM calls (resolver + proposer).
- Run backend tests with `uv run pytest`; frontend lint with `cd frontend && bun run lint`.

---

## File Structure

**Backend — new:**
- `apps/transformations/services/crossopp_measure_service.py` — the shared engine (resolve / classify / add_measure / reload).
- `apps/transformations/services/crossopp_measure_proposer.py` — LLM app→specs proposer.
- `apps/agents/tools/crossopp_measure_tool.py` — `define_crossopp_measure` + `propose_crossopp_measures` tool factories.
- migrations under `apps/transformations/migrations/`.

**Backend — modified:**
- `apps/transformations/models.py` — add `CrossOppMeasure`, `CrossOppMeasureDraft`.
- `apps/agents/graph/base.py` — register the new tools in `_build_tools`.
- `apps/agents/prompts/base_system.py` (or a new `crossopp_prompt.py`) — guidance.
- `apps/workspaces/api/crossopp_views.py` + `apps/workspaces/api/urls.py` — approve endpoint.
- `apps/workspaces/tasks.py` — `resume_thread_after_measure_approval`.
- `apps/workspaces/management/commands/build_crossopp_workspace.py` — call the service.

**Frontend — new:**
- `frontend/src/components/ChatMessage/CrossOppMeasureOutput.tsx` — committed lineage card + approval card.

**Frontend — modified:**
- `frontend/src/components/ChatMessage/ChatMessage.tsx` — `renderToolOutput` case + `AUTO_EXPAND_TOOLS`.
- `frontend/src/api/crossopp.ts` — `approveMeasure()`.

**Tests — new:** `tests/test_crossopp_measure_service.py`, `tests/test_crossopp_measure_tool.py`, `tests/test_crossopp_measure_proposer.py`, `tests/test_crossopp_approve_api.py`, `tests/e2e/test_crossopp_chat_loop_live.py`, `frontend/src/components/ChatMessage/CrossOppMeasureOutput.test.tsx`.

---

## Phase 1 — Models

### Task 1: `CrossOppMeasure` spec-catalog model

**Files:**
- Modify: `apps/transformations/models.py` (after `CrossOppMeasureLineage`)
- Migration: `apps/transformations/migrations/000X_crossoppmeasure.py` (via makemigrations)
- Test: `tests/test_crossopp_measure_service.py`

**Interfaces:**
- Produces: `CrossOppMeasure(workspace, name, description, kind)` with `kind in {"numeric","rate"}`; `unique_together=(workspace, name)`. Reconstructs a `CanonicalMeasureSpec` via `.to_spec()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crossopp_measure_service.py
import pytest
from apps.transformations.models import CrossOppMeasure
from apps.transformations.services.measure_resolver import CanonicalMeasureSpec

@pytest.mark.django_db
def test_crossopp_measure_persists_and_roundtrips_spec(workspace):
    m = CrossOppMeasure.objects.create(
        workspace=workspace, name="birth_weight",
        description="newborn weight in grams", kind="numeric",
    )
    spec = m.to_spec()
    assert isinstance(spec, CanonicalMeasureSpec)
    assert (spec.name, spec.description, spec.kind) == ("birth_weight", "newborn weight in grams", "numeric")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_crossopp_measure_persists_and_roundtrips_spec -v`
Expected: FAIL (ImportError: cannot import name `CrossOppMeasure`)

- [ ] **Step 3: Add the model**

```python
# apps/transformations/models.py — append after CrossOppMeasureLineage
class CrossOppMeasure(models.Model):
    """The human spec for a cross-opp canonical measure (the catalog of intent).

    Cube holds the resolved per-opp SQL; this holds the name/description/kind a person
    (or the proposer) chose, which we need to regenerate the model additively. One row
    per (workspace, measure).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="measures"
    )
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=16, default="numeric")  # numeric | rate
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["workspace", "name"]
        ordering = ["name"]

    def to_spec(self):
        from apps.transformations.services.measure_resolver import CanonicalMeasureSpec
        return CanonicalMeasureSpec(name=self.name, description=self.description, kind=self.kind)

    def __str__(self):
        return f"{self.name} ({self.kind})"
```

- [ ] **Step 4: Make + run migration, run test**

Run: `uv run python manage.py makemigrations transformations && uv run pytest tests/test_crossopp_measure_service.py::test_crossopp_measure_persists_and_roundtrips_spec -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/models.py apps/transformations/migrations/ tests/test_crossopp_measure_service.py
git commit -m "feat(crossopp): CrossOppMeasure spec-catalog model"
```

---

### Task 2: `CrossOppMeasureDraft` model

**Files:**
- Modify: `apps/transformations/models.py`
- Migration: via makemigrations
- Test: `tests/test_crossopp_measure_service.py`

**Interfaces:**
- Produces: `CrossOppMeasureDraft(workspace, name, description, kind, resolutions, flagged, shortlists, status, thread_id, created_by)`. `resolutions`/`flagged`/`shortlists` are JSON. `status in {"pending","committed","cancelled"}`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.django_db
def test_measure_draft_holds_resolutions_and_flagged(workspace, user):
    from apps.transformations.models import CrossOppMeasureDraft
    d = CrossOppMeasureDraft.objects.create(
        workspace=workspace, name="length_of_stay", description="days in care",
        kind="numeric", thread_id="t-1", created_by=user,
        resolutions={"10012": {"column": "los_days", "status": "resolved", "confidence": 0.9}},
        flagged=["10013"],
        shortlists={"10013": [{"column": "stay_len", "label": "Length of stay (days)", "type": "Int"}]},
        status="pending",
    )
    assert d.flagged == ["10013"]
    assert d.shortlists["10013"][0]["column"] == "stay_len"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_measure_draft_holds_resolutions_and_flagged -v`
Expected: FAIL (cannot import `CrossOppMeasureDraft`)

- [ ] **Step 3: Add the model**

```python
# apps/transformations/models.py — append
class CrossOppMeasureDraft(models.Model):
    """A measure pending user approval because the resolver had doubt on >=1 opp.

    Holds the full per-opp resolutions + the flagged opps + per-flagged-opp candidate
    shortlists, so the approval card can offer confirm / pick / reject. Committed on approve.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="measure_drafts"
    )
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=16, default="numeric")
    resolutions = models.JSONField(default=dict)   # opp_id -> serialized MeasureResolution
    flagged = models.JSONField(default=list)       # [opp_id, ...] low_confidence/absent
    shortlists = models.JSONField(default=dict)    # opp_id -> [ {column,label,type}, ... ]
    status = models.CharField(max_length=16, default="pending")  # pending|committed|cancelled
    thread_id = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"draft:{self.name} ({self.status})"
```

- [ ] **Step 4: Make + run migration, run test**

Run: `uv run python manage.py makemigrations transformations && uv run pytest tests/test_crossopp_measure_service.py::test_measure_draft_holds_resolutions_and_flagged -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/models.py apps/transformations/migrations/ tests/test_crossopp_measure_service.py
git commit -m "feat(crossopp): CrossOppMeasureDraft model for approval gate"
```

---

## Phase 2 — The shared engine (`crossopp_measure_service.py`)

### Task 3: doubt classification + serialization helpers

**Files:**
- Create: `apps/transformations/services/crossopp_measure_service.py`
- Test: `tests/test_crossopp_measure_service.py`

**Interfaces:**
- Produces:
  - `serialize_resolution(r: MeasureResolution) -> dict` and `deserialize_resolution(d: dict) -> MeasureResolution`
  - `classify_doubt(resolutions: dict[str, MeasureResolution]) -> tuple[bool, list[str]]` — doubt is any opp whose status is `low_confidence` or `absent`; returns `(has_doubt, flagged_opp_ids)`.

- [ ] **Step 1: Write the failing test**

```python
def test_classify_doubt_flags_low_confidence_and_absent():
    from apps.transformations.services.crossopp_measure_service import classify_doubt
    from apps.transformations.services.measure_resolver import MeasureResolution
    def R(status, conf): return MeasureResolution("m", "c", "p", "c=1", conf, status, "lbl", "why")
    res = {"a": R("resolved", 0.9), "b": R("low_confidence", 0.3), "c": R("absent", 0.0)}
    has_doubt, flagged = classify_doubt(res)
    assert has_doubt is True
    assert sorted(flagged) == ["b", "c"]
    assert classify_doubt({"a": R("resolved", 0.9)}) == (False, [])
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_classify_doubt_flags_low_confidence_and_absent -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement helpers**

```python
# apps/transformations/services/crossopp_measure_service.py
"""The trigger-agnostic engine for cross-opp canonical measures.

Spec in -> resolve across the workspace's opps -> classify doubt -> commit (additive
model regen + lineage + Cube reload) or hand back for approval. Fed by both the
on-demand agent tool and the app-driven proposer.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from apps.transformations.services.measure_resolver import MeasureResolution

_DOUBT_STATUSES = frozenset({"low_confidence", "absent"})


def serialize_resolution(r: MeasureResolution) -> dict:
    return asdict(r)


def deserialize_resolution(d: dict) -> MeasureResolution:
    return MeasureResolution(**d)


def classify_doubt(resolutions: dict[str, MeasureResolution]) -> tuple[bool, list[str]]:
    """Doubt = any opp the resolver was unsure about (low_confidence) or found absent."""
    flagged = [opp for opp, r in resolutions.items() if r.status in _DOUBT_STATUSES]
    return (bool(flagged), flagged)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_classify_doubt_flags_low_confidence_and_absent -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/services/crossopp_measure_service.py tests/test_crossopp_measure_service.py
git commit -m "feat(crossopp): doubt classification + resolution serialization"
```

---

### Task 4: `add_measure` — additive commit (the stability-critical core)

**Files:**
- Modify: `apps/transformations/services/crossopp_measure_service.py`
- Test: `tests/test_crossopp_measure_service.py`

**Interfaces:**
- Consumes: `OppRef`, `render_crossopp_model` (`crossopp_cube_builder`), `CrossOppMeasure`, `CrossOppMeasureLineage`, `SchemaManager._view_schema_name`.
- Produces: `add_measure(workspace, spec, resolutions, opps, *, model_root="cube/model") -> list[dict]` — upserts `CrossOppMeasure`, upserts lineage, regenerates the FULL model (all existing measures + this one), writes `cube/model/<ws_hash>/canonical.yml`. Returns the lineage list (inspector shape). And `load_workspace_specs_and_resolutions(workspace) -> tuple[list[CanonicalMeasureSpec], dict[str, dict[str, MeasureResolution]]]`.

- [ ] **Step 1: Write the failing test (additive + stability)**

```python
@pytest.mark.django_db
def test_add_measure_is_additive_and_preserves_existing(workspace, tmp_path):
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    from apps.transformations.services.measure_resolver import CanonicalMeasureSpec, MeasureResolution
    opps = [OppRef("10012", "t_10012_x"), OppRef("10013", "t_10013_y")]
    def R(col): return MeasureResolution("m", col, "p", col, 0.9, "resolved", "lbl", "why")
    # First measure
    svc.add_measure(workspace, CanonicalMeasureSpec("birth_weight", "g", "numeric"),
                    {"10012": R("child_weight_birth"), "10013": R("birth_weight")},
                    opps, model_root=str(tmp_path))
    model_after_first = (tmp_path / svc._ws_hash(workspace) / "canonical.yml").read_text()
    # Second measure
    svc.add_measure(workspace, CanonicalMeasureSpec("kmc_hours", "hrs", "numeric"),
                    {"10012": R("kmc_hours"), "10013": R("kmc_hours")},
                    opps, model_root=str(tmp_path))
    model_after_second = (tmp_path / svc._ws_hash(workspace) / "canonical.yml").read_text()
    # birth_weight's per-opp SELECT terms are still present, unchanged (stability)
    assert "child_weight_birth AS birth_weight" in model_after_first
    assert "child_weight_birth AS birth_weight" in model_after_second
    assert "AS kmc_hours" in model_after_second
    # Both measures present in the blended cube
    from apps.transformations.models import CrossOppMeasure, CrossOppMeasureLineage
    assert set(CrossOppMeasure.objects.filter(workspace=workspace).values_list("name", flat=True)) == {"birth_weight", "kmc_hours"}
    assert CrossOppMeasureLineage.objects.filter(workspace=workspace, measure="kmc_hours").count() == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_add_measure_is_additive_and_preserves_existing -v`
Expected: FAIL (`add_measure` not defined)

- [ ] **Step 3: Implement `add_measure` + loaders**

```python
# append to crossopp_measure_service.py
from asgiref.sync import sync_to_async  # noqa: E402
from django.db import transaction  # noqa: E402

from apps.transformations.models import CrossOppMeasure, CrossOppMeasureLineage  # noqa: E402
from apps.transformations.services.crossopp_cube_builder import (  # noqa: E402
    OppRef, render_crossopp_model,
)
from apps.transformations.services.measure_resolver import CanonicalMeasureSpec  # noqa: E402
from apps.workspaces.services.schema_manager import SchemaManager  # noqa: E402

BLENDED_CUBE = "kmc_cross_opp"


def _ws_hash(workspace) -> str:
    return SchemaManager()._view_schema_name(workspace.id)


def load_workspace_specs_and_resolutions(workspace):
    """Reconstruct (specs, resolutions_by_opp) from the persisted catalog + lineage.

    Lets a single add be additive: re-render the whole model from what already exists
    plus the new measure.
    """
    specs = [m.to_spec() for m in CrossOppMeasure.objects.filter(workspace=workspace)]
    res: dict[str, dict] = {}
    for row in CrossOppMeasureLineage.objects.filter(workspace=workspace):
        res.setdefault(row.opportunity_id, {})[row.measure] = MeasureResolution(
            measure=row.measure, column=row.column or None, source_path=row.source_path or None,
            sql_expression=row.sql_expression or None, confidence=row.confidence,
            status=row.status, matched_label=row.matched_label, reason="",
        )
    return specs, res


def add_measure(workspace, spec, resolutions, opps, *, model_root="cube/model"):
    """Commit ONE measure: upsert spec + lineage, regenerate the full model additively, write it.

    Returns the inspector-shaped lineage list for this measure.
    """
    with transaction.atomic():
        CrossOppMeasure.objects.update_or_create(
            workspace=workspace, name=spec.name,
            defaults={"description": spec.description, "kind": spec.kind},
        )
        for opp_id, r in resolutions.items():
            CrossOppMeasureLineage.objects.update_or_create(
                workspace=workspace, opportunity_id=opp_id, measure=spec.name,
                defaults={
                    "column": r.column or "", "source_path": r.source_path or "",
                    "matched_label": r.matched_label or "", "sql_expression": r.sql_expression or "",
                    "confidence": r.confidence, "status": r.status,
                },
            )

    specs, res_by_opp = load_workspace_specs_and_resolutions(workspace)
    model_yaml = render_crossopp_model(BLENDED_CUBE, opps, specs, res_by_opp)
    ws_hash = _ws_hash(workspace)
    path = Path(model_root) / ws_hash / "canonical.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model_yaml)

    return [
        {
            "opportunity_id": opp_id, "status": r.status, "confidence": r.confidence,
            "column": r.column, "matched_label": r.matched_label, "sql_expression": r.sql_expression,
        }
        for opp_id, r in resolutions.items()
    ]


aadd_measure = sync_to_async(add_measure)
```

NOTE: `render_crossopp_model` already lists measures in the order `specs` are passed; `load_workspace_specs_and_resolutions` returns them in `CrossOppMeasure.Meta.ordering` (name) order, which is stable — so an added measure never reorders/changes existing per-opp SELECT terms (only appends its own alias).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_add_measure_is_additive_and_preserves_existing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/services/crossopp_measure_service.py tests/test_crossopp_measure_service.py
git commit -m "feat(crossopp): additive add_measure (regenerate model, persist lineage, stability)"
```

---

### Task 5: resolve-across-opps + per-opp candidate shortlist

**Files:**
- Modify: `apps/transformations/services/crossopp_measure_service.py`
- Test: `tests/test_crossopp_measure_service.py`

**Interfaces:**
- Consumes: `gather_measure_candidates`, `resolve_measure`, `_clinical_entry_candidates` (measure_resolver), `Tenant`/`TenantSchema`/`TenantMembership`/`WorkspaceTenant`.
- Produces:
  - `workspace_opps(workspace) -> tuple[list[OppRef], dict[str, list[FieldCandidate]]]` — the active opps + candidates per opp (the command's collection logic, extracted).
  - `async resolve_across_opps(workspace, spec, candidates_by_opp, *, model_client=None) -> dict[str, MeasureResolution]`.
  - `shortlist_for_opp(candidates) -> list[dict]` — `[{column,label,type}]` for the approval picker (reuse `_clinical_entry_candidates`).

- [ ] **Step 1: Write the failing test (fake resolver)**

```python
@pytest.mark.django_db
def test_resolve_across_opps_uses_resolver_per_opp():
    import asyncio
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.measure_resolver import CanonicalMeasureSpec, MeasureResolution
    spec = CanonicalMeasureSpec("mortality", "child died", "rate")
    cands = {"10012": [], "10013": []}
    class FakeClient:  # resolve_measure(model_client=...) path returns absent for [] candidates
        pass
    # With empty candidates resolve_measure returns 'absent' without an LLM call.
    res = asyncio.run(svc.resolve_across_opps_from_candidates(spec, cands))
    assert set(res) == {"10012", "10013"}
    assert all(r.status == "absent" for r in res.values())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_resolve_across_opps_uses_resolver_per_opp -v`
Expected: FAIL (`resolve_across_opps_from_candidates` not defined)

- [ ] **Step 3: Implement**

```python
# append to crossopp_measure_service.py
from apps.transformations.services.measure_resolver import (  # noqa: E402
    gather_measure_candidates, resolve_measure, _clinical_entry_candidates,
)
from apps.users.models import Tenant, TenantMembership  # noqa: E402
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant  # noqa: E402

LABS_PROVIDER = "commcare_connect_labs"


def workspace_opps(workspace):
    """Active opps in the workspace + their stg_visits field candidates (sync ORM)."""
    opps, cands = [], {}
    for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant"):
        tenant = wt.tenant
        schema = TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).first()
        if schema is None:
            continue
        tm = TenantMembership.objects.filter(tenant=tenant).first()
        form_defs = (getattr(getattr(tm, "metadata", None), "metadata", None) or {}).get(
            "form_definitions", {}
        )
        opps.append(OppRef(tenant.external_id, schema.schema_name))
        cands[tenant.external_id] = gather_measure_candidates(form_defs)
    return opps, cands


async def resolve_across_opps_from_candidates(spec, candidates_by_opp, *, model_client=None):
    out = {}
    for opp_id, cands in candidates_by_opp.items():
        out[opp_id] = await resolve_measure(spec, cands, model_client=model_client)
    return out


def shortlist_for_opp(candidates) -> list[dict]:
    return [
        {"column": c.column, "label": c.label, "type": c.type}
        for c in _clinical_entry_candidates(candidates)
    ]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_resolve_across_opps_uses_resolver_per_opp -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/services/crossopp_measure_service.py tests/test_crossopp_measure_service.py
git commit -m "feat(crossopp): resolve-across-opps + approval shortlist helpers"
```

---

### Task 6: Cube reload + queryability poll

**Files:**
- Modify: `apps/transformations/services/crossopp_measure_service.py`
- Test: `tests/test_crossopp_measure_service.py`

**Interfaces:**
- Produces: `await ensure_measure_queryable(workspace, measure_name, *, timeout_s=15) -> bool` — polls `<CUBE>/cubejs-api/v1/meta` until `kmc_cross_opp.<measure>` appears (dev-mode hot-reload) or timeout; returns True if visible. Cube base URL from `settings.CUBE_URL`/env (`http://localhost:4000`), token via the existing cube-auth helper used by `semantic_query`.

- [ ] **Step 1: Write the failing test (mock HTTP)**

```python
def test_ensure_measure_queryable_polls_meta(monkeypatch):
    import asyncio
    from apps.transformations.services import crossopp_measure_service as svc
    calls = {"n": 0}
    async def fake_meta():
        calls["n"] += 1
        return {"cubes": [{"name": "kmc_cross_opp", "measures": [{"name": "kmc_cross_opp.kmc_hours"}]}]} if calls["n"] >= 2 else {"cubes": []}
    monkeypatch.setattr(svc, "_fetch_cube_meta", fake_meta)
    ok = asyncio.run(svc.ensure_measure_queryable_meta("kmc_hours", timeout_s=2, interval_s=0.01))
    assert ok is True and calls["n"] >= 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_ensure_measure_queryable_polls_meta -v`
Expected: FAIL

- [ ] **Step 3: Implement (reuse the cube auth/url helper from semantic.py)**

```python
# append to crossopp_measure_service.py
import asyncio  # noqa: E402

from mcp_server.services.semantic import _cube_base_url, _cube_headers  # reuse existing helpers


async def _fetch_cube_meta() -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{_cube_base_url()}/cubejs-api/v1/meta", headers=_cube_headers())
        resp.raise_for_status()
        return resp.json()


async def ensure_measure_queryable_meta(measure_name, *, timeout_s=15, interval_s=0.5) -> bool:
    """Poll Cube /v1/meta until the new measure is compiled (dev-mode hot-reload)."""
    target = f"{BLENDED_CUBE}.{measure_name}"
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        meta = await _fetch_cube_meta()
        for cube in meta.get("cubes", []):
            if any(m.get("name") == target for m in cube.get("measures", [])):
                return True
        await asyncio.sleep(interval_s)
    return False
```

If `_cube_base_url`/`_cube_headers` do not exist with those names in `mcp_server/services/semantic.py`, grep that file for how `semantic_query` builds the Cube URL + auth header and mirror it; do not hardcode the secret.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_measure_service.py::test_ensure_measure_queryable_polls_meta -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/transformations/services/crossopp_measure_service.py tests/test_crossopp_measure_service.py
git commit -m "feat(crossopp): poll Cube /v1/meta for measure queryability after commit"
```

---

### Task 7: Refactor `build_crossopp_workspace` onto the service

**Files:**
- Modify: `apps/workspaces/management/commands/build_crossopp_workspace.py`
- Test: existing `tests/` for the command (run to confirm no regression) + manual smoke

**Interfaces:** the command keeps its CLI but its resolve→persist→render block now calls `crossopp_measure_service.workspace_opps` + `resolve_across_opps_from_candidates` + `add_measure` per starter measure.

- [ ] **Step 1: Replace the inline resolve/persist/render block**

```python
# in handle(), after opps are added to the workspace + view-schema row + ro-role:
import asyncio
from apps.transformations.services import crossopp_measure_service as svc

opps, candidates_by_opp = svc.workspace_opps(workspace)
for m in STARTER_MEASURES:
    resolutions = asyncio.run(svc.resolve_across_opps_from_candidates(m, candidates_by_opp))
    svc.add_measure(workspace, m, resolutions, opps)
self.stdout.write(self.style.SUCCESS(f"  committed {len(STARTER_MEASURES)} measures via service"))
```

(Delete the now-dead `_resolve_all`, the manual lineage loop, and the manual `render_crossopp_model`/write block — the service owns them. Keep the coverage report by reading lineage back.)

- [ ] **Step 2: Run the command against the live KMC workspace (smoke)**

Run: `uv run python manage.py build_crossopp_workspace --name "KMC Cross-Opp" --opps 10012 10013 10014 10015 10016 10017 10018 10019 10020 10021 10022`
Expected: completes; `cube/model/ws_b357b50f17764536/canonical.yml` regenerated with the same 4 measures; `/crossopp` still renders (re-run the browse check from earlier).

- [ ] **Step 3: Commit**

```bash
git add apps/workspaces/management/commands/build_crossopp_workspace.py
git commit -m "refactor(crossopp): build command delegates to the shared measure engine"
```

---

## Phase 3 — Agent tool + approval + resume

### Task 8: `define_crossopp_measure` tool

**Files:**
- Create: `apps/agents/tools/crossopp_measure_tool.py`
- Test: `tests/test_crossopp_measure_tool.py`

**Interfaces:**
- Consumes: the service (Task 3-6), `CrossOppMeasure`, `CrossOppMeasureDraft`.
- Produces: `create_crossopp_measure_tools(workspace, user, conversation_id) -> list` containing `define_crossopp_measure(name, description, kind="numeric")`. Returns one of:
  - `{"status": "committed", "measure": name, "lineage": [...], "message": str}`
  - `{"status": "needs_approval", "draft_id": str, "measure": name, "flagged": [{opp_id, guess, shortlist}], "resolved": [{opp_id, column, confidence}], "message": str}`
  - `{"status": "exists", "measure": name, "message": "already defined"}`

- [ ] **Step 1: Write the failing tests (fake resolver via monkeypatch on the service)**

```python
# tests/test_crossopp_measure_tool.py
import pytest

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_measure_commits_when_no_doubt(workspace, user, monkeypatch):
    from apps.agents.tools import crossopp_measure_tool as t
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    from apps.transformations.services.measure_resolver import MeasureResolution
    def R(): return MeasureResolution("m","c","p","c",0.9,"resolved","lbl","why")
    monkeypatch.setattr(svc, "workspace_opps", lambda ws: ([OppRef("10012","s")], {"10012": []}))
    async def fake_resolve(spec, cands, **k): return {"10012": R()}
    monkeypatch.setattr(svc, "resolve_across_opps_from_candidates", fake_resolve)
    monkeypatch.setattr(svc, "add_measure", lambda *a, **k: [{"opportunity_id":"10012","status":"resolved"}])
    async def ok(*a, **k): return True
    monkeypatch.setattr(svc, "ensure_measure_queryable_meta", ok)
    [define] = t.create_crossopp_measure_tools(workspace, user, "thread-1")
    out = await define.ainvoke({"name":"birth_weight","description":"g","kind":"numeric"})
    assert out["status"] == "committed" and out["measure"] == "birth_weight"

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_measure_drafts_when_doubt(workspace, user, monkeypatch):
    from apps.agents.tools import crossopp_measure_tool as t
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    from apps.transformations.services.measure_resolver import MeasureResolution
    monkeypatch.setattr(svc, "workspace_opps", lambda ws: ([OppRef("10012","s"),OppRef("10013","s2")], {"10012": [], "10013": []}))
    async def fake_resolve(spec, cands, **k):
        return {"10012": MeasureResolution("m","c","p","c",0.9,"resolved","lbl","y"),
                "10013": MeasureResolution("m",None,None,None,0.2,"low_confidence","","y")}
    monkeypatch.setattr(svc, "resolve_across_opps_from_candidates", fake_resolve)
    monkeypatch.setattr(svc, "shortlist_for_opp", lambda c: [{"column":"x","label":"X","type":"Int"}])
    [define] = t.create_crossopp_measure_tools(workspace, user, "thread-1")
    out = await define.ainvoke({"name":"los","description":"days","kind":"numeric"})
    assert out["status"] == "needs_approval"
    assert out["flagged"][0]["opp_id"] == "10013"
    from apps.transformations.models import CrossOppMeasureDraft
    assert await CrossOppMeasureDraft.objects.filter(id=out["draft_id"]).aexists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_crossopp_measure_tool.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement the tool**

```python
# apps/agents/tools/crossopp_measure_tool.py
"""Agent tool: define a cross-opp canonical measure on demand (the chat-driven auto-model)."""
from __future__ import annotations

from langchain_core.tools import tool


def create_crossopp_measure_tools(workspace, user, conversation_id=None) -> list:
    @tool
    async def define_crossopp_measure(name: str, description: str, kind: str = "numeric") -> dict:
        """Define a NEW cross-opp canonical measure so it can be compared across every
        opportunity. Use when a cross-opp question needs a measure that semantic_catalog
        does not list. `name` is a snake_case slug; `description` is one plain-language
        line of what it means; `kind` is 'numeric' (a value to average) or 'rate' (a
        boolean event averaged into a 0..1 rate). If the resolver is unsure for any opp,
        this returns needs_approval and the user is asked to confirm before it commits."""
        from asgiref.sync import sync_to_async

        from apps.transformations.models import CrossOppMeasure, CrossOppMeasureDraft
        from apps.transformations.services import crossopp_measure_service as svc
        from apps.transformations.services.measure_resolver import CanonicalMeasureSpec

        spec = CanonicalMeasureSpec(name=name.strip(), description=description.strip(), kind=kind)

        if await CrossOppMeasure.objects.filter(workspace=workspace, name=spec.name).aexists():
            return {"status": "exists", "measure": spec.name, "message": f"'{spec.name}' is already defined."}

        opps, cands = await sync_to_async(svc.workspace_opps)(workspace)
        resolutions = await svc.resolve_across_opps_from_candidates(spec, cands)
        has_doubt, flagged = svc.classify_doubt(resolutions)

        if not has_doubt:
            lineage = await svc.aadd_measure(workspace, spec, resolutions, opps)
            await svc.ensure_measure_queryable_meta(spec.name)
            return {"status": "committed", "measure": spec.name, "lineage": lineage,
                    "message": f"Defined '{spec.name}' across {len(opps)} opportunities."}

        draft = await CrossOppMeasureDraft.objects.acreate(
            workspace=workspace, name=spec.name, description=spec.description, kind=spec.kind,
            thread_id=conversation_id or "", created_by=user, status="pending",
            resolutions={o: svc.serialize_resolution(r) for o, r in resolutions.items()},
            flagged=flagged,
            shortlists={o: svc.shortlist_for_opp(cands.get(o, [])) for o in flagged},
        )
        return {
            "status": "needs_approval", "draft_id": str(draft.id), "measure": spec.name,
            "flagged": [
                {"opp_id": o, "guess": resolutions[o].column,
                 "confidence": resolutions[o].confidence, "shortlist": draft.shortlists[o]}
                for o in flagged
            ],
            "resolved": [
                {"opp_id": o, "column": r.column, "confidence": r.confidence}
                for o, r in resolutions.items() if o not in flagged
            ],
            "message": f"Resolved '{spec.name}' but {len(flagged)} opp(s) need your confirmation.",
        }

    define_crossopp_measure.name = "define_crossopp_measure"
    return [define_crossopp_measure]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_crossopp_measure_tool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agents/tools/crossopp_measure_tool.py tests/test_crossopp_measure_tool.py
git commit -m "feat(crossopp): define_crossopp_measure agent tool (commit or draft-on-doubt)"
```

---

### Task 9: Register the tool + system-prompt guidance

**Files:**
- Modify: `apps/agents/graph/base.py` (`_build_tools`)
- Modify: `apps/agents/prompts/base_system.py`
- Test: `tests/test_crossopp_measure_tool.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_tool_registered_in_agent(workspace, user):
    from apps.agents.graph.base import _build_tools
    tools = _build_tools(workspace, user, mcp_tools=[], conversation_id="t-1")
    assert any(getattr(t, "name", "") == "define_crossopp_measure" for t in tools)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_tool.py::test_define_tool_registered_in_agent -v`
Expected: FAIL

- [ ] **Step 3: Register + prompt**

```python
# apps/agents/graph/base.py — in _build_tools, alongside the other local tools:
from apps.agents.tools.crossopp_measure_tool import create_crossopp_measure_tools
tools.extend(create_crossopp_measure_tools(workspace, user, conversation_id=conversation_id))
```

```python
# apps/agents/prompts/base_system.py — append a guidance block:
CROSSOPP_GUIDANCE = """
## Cross-opportunity measures
This workspace can compare a measure across multiple opportunities. When a question needs a
domain measure (e.g. "birth weight", "referral rate"):
1. Call `semantic_catalog` to see which measures already exist on the `kmc_cross_opp` cube.
2. If a needed measure is MISSING, call `define_crossopp_measure(name, description, kind)`
   BEFORE querying. Name it as a snake_case slug; kind is 'numeric' or 'rate'.
3. If it returns status=needs_approval, STOP and tell the user you need their confirmation on
   the flagged opportunities (the UI shows an approval card); do not retry or invent values.
4. Once committed (or after approval), use `semantic_query` against `kmc_cross_opp`.
"""
# ...and include CROSSOPP_GUIDANCE in the assembled system prompt string.
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_measure_tool.py::test_define_tool_registered_in_agent -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agents/graph/base.py apps/agents/prompts/base_system.py tests/test_crossopp_measure_tool.py
git commit -m "feat(crossopp): register define tool + cross-opp agent guidance"
```

---

### Task 10: Approval API endpoint

**Files:**
- Modify: `apps/workspaces/api/crossopp_views.py`, `apps/workspaces/api/urls.py`
- Test: `tests/test_crossopp_approve_api.py`

**Interfaces:**
- Produces: `POST /api/workspaces/<id>/crossopp/measures/<draft_id>/approve/` body `{"overrides": {opp_id: {"action": "confirm"|"pick"|"reject", "column": str?}}}`. Applies overrides to the draft's resolutions, calls `add_measure`, marks draft `committed`, defers `resume_thread_after_measure_approval`. Returns `{"status": "committed", "measure": name, "lineage": [...]}`.

- [ ] **Step 1: Write the failing API test**

```python
# tests/test_crossopp_approve_api.py
import pytest
from rest_framework.test import APIClient

@pytest.mark.django_db(transaction=True)
def test_approve_applies_overrides_and_commits(workspace, user, monkeypatch):
    from apps.transformations.models import CrossOppMeasureDraft, CrossOppMeasure
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    monkeypatch.setattr(svc, "workspace_opps", lambda ws: ([OppRef("10012","s"),OppRef("10013","s2")], {}))
    captured = {}
    def fake_add(ws, spec, resolutions, opps, **k):
        captured["res"] = resolutions
        return [{"opportunity_id": o, "status": resolutions[o].status} for o in resolutions]
    monkeypatch.setattr(svc, "add_measure", fake_add)
    monkeypatch.setattr("apps.workspaces.api.crossopp_views._defer_measure_resume", lambda *a, **k: None)
    draft = CrossOppMeasureDraft.objects.create(
        workspace=workspace, name="los", description="d", kind="numeric", thread_id="t1",
        created_by=user, status="pending",
        resolutions={"10012": {"measure":"los","column":"a","source_path":"","sql_expression":"a","confidence":0.9,"status":"resolved","matched_label":"","reason":""},
                     "10013": {"measure":"los","column":None,"source_path":"","sql_expression":None,"confidence":0.2,"status":"low_confidence","matched_label":"","reason":""}},
        flagged=["10013"], shortlists={"10013":[{"column":"stay_len","label":"Stay","type":"Int"}]},
    )
    c = APIClient(); c.force_authenticate(user=user)
    resp = c.post(f"/api/workspaces/{workspace.id}/crossopp/measures/{draft.id}/approve/",
                  {"overrides": {"10013": {"action": "pick", "column": "stay_len"}}}, format="json")
    assert resp.status_code == 200 and resp.data["status"] == "committed"
    assert captured["res"]["10013"].column == "stay_len"
    assert captured["res"]["10013"].status == "resolved"
    draft.refresh_from_db(); assert draft.status == "committed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_approve_api.py -v`
Expected: FAIL (404 — route missing)

- [ ] **Step 3: Implement the view + override logic + route**

```python
# apps/workspaces/api/crossopp_views.py — add
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from apps.transformations.models import CrossOppMeasureDraft
from apps.transformations.services import crossopp_measure_service as svc


def _apply_overrides(draft, overrides, opps_cands):
    """Return resolutions dict (opp -> MeasureResolution) with the user's per-opp choices."""
    res = {o: svc.deserialize_resolution(d) for o, d in draft.resolutions.items()}
    for opp_id, choice in (overrides or {}).items():
        action = choice.get("action")
        if action == "reject":
            r = res[opp_id]
            res[opp_id] = r.__class__(measure=draft.name, column=None, source_path=None,
                                      sql_expression=None, confidence=r.confidence,
                                      status="absent", matched_label="", reason="user rejected")
        elif action == "pick":
            col = choice["column"]
            res[opp_id] = res[opp_id].__class__(
                measure=draft.name, column=col, source_path=None,
                sql_expression=col if draft.kind == "numeric" else f"({col} = 'yes')",
                confidence=1.0, status="resolved", matched_label="(user)", reason="user picked")
        elif action == "confirm":
            r = res[opp_id]
            res[opp_id] = r.__class__(measure=draft.name, column=r.column, source_path=r.source_path,
                                      sql_expression=r.sql_expression, confidence=1.0,
                                      status="resolved", matched_label=r.matched_label, reason="user confirmed")
    return res


def _defer_measure_resume(workspace, thread_id, measure_name):
    from apps.workspaces.tasks import resume_thread_after_measure_approval
    from asgiref.sync import async_to_sync
    async_to_sync(resume_thread_after_measure_approval.defer_async)(
        workspace_id=str(workspace.id), thread_id=thread_id, measure_name=measure_name)


class CrossOppMeasureApproveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, draft_id):
        workspace = get_object_or_404(Workspace, id=workspace_id, memberships__user=request.user)
        draft = get_object_or_404(CrossOppMeasureDraft, id=draft_id, workspace=workspace)
        if draft.status != "pending":
            return Response({"error": f"draft already {draft.status}"}, status=409)
        opps, _ = svc.workspace_opps(workspace)
        resolutions = _apply_overrides(draft, request.data.get("overrides", {}), None)
        lineage = svc.add_measure(workspace, draft.to_spec_like(), resolutions, opps)
        draft.status = "committed"; draft.save(update_fields=["status"])
        _defer_measure_resume(workspace, draft.thread_id, draft.name)
        return Response({"status": "committed", "measure": draft.name, "lineage": lineage})
```

Add `to_spec_like()` to `CrossOppMeasureDraft` (returns a `CanonicalMeasureSpec` from its name/description/kind — same body as `CrossOppMeasure.to_spec`). Register the route:

```python
# apps/workspaces/api/urls.py
from apps.workspaces.api.crossopp_views import CrossOppMeasureApproveView
path("crossopp/measures/<uuid:draft_id>/approve/", CrossOppMeasureApproveView.as_view(), name="crossopp_measure_approve"),
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_approve_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/api/crossopp_views.py apps/workspaces/api/urls.py apps/transformations/models.py tests/test_crossopp_approve_api.py
git commit -m "feat(crossopp): approval endpoint applies per-opp overrides and commits"
```

---

### Task 11: `resume_thread_after_measure_approval` task

**Files:**
- Modify: `apps/workspaces/tasks.py`
- Test: `tests/test_crossopp_approve_api.py` (mock the agent build)

**Interfaces:**
- Produces: `@task resume_thread_after_measure_approval(context, workspace_id, thread_id, measure_name)` — builds the agent for the thread (`_build_agent_for_resume`), injects a `HumanMessage(f"{SYSTEM_RESUME_MARKER} Measure '<name>' is now defined and queryable. Continue the user's request using semantic_query.")`, and `ainvoke`s with the thread config. Mirrors `resume_thread_after_materialization` minus the ThreadJob aggregation.

- [ ] **Step 1: Write the failing test (mock `_build_agent_for_resume`)**

```python
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_after_approval_reinvokes_agent(workspace, user, monkeypatch):
    from apps.workspaces import tasks
    seen = {}
    class FakeAgent:
        async def ainvoke(self, state, config): seen["state"] = state; seen["config"] = config
    async def fake_build(ws, u, conversation_id=None): return FakeAgent(), {}
    monkeypatch.setattr(tasks, "_build_agent_for_resume", fake_build)
    # resolve workspace/user lookups the task does:
    out = await tasks.resume_thread_after_measure_approval.func(
        None, workspace_id=str(workspace.id), thread_id="thread-1", measure_name="los")
    assert "los" in seen["state"]["messages"][0].content
    assert seen["config"]["configurable"]["thread_id"] == "thread-1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_approve_api.py::test_resume_after_approval_reinvokes_agent -v`
Expected: FAIL

- [ ] **Step 3: Implement the task**

```python
# apps/workspaces/tasks.py — add (near resume_thread_after_materialization)
@task(pass_context=True)
async def resume_thread_after_measure_approval(context, workspace_id: str, thread_id: str, measure_name: str) -> dict:
    """After a measure is approved+committed, nudge the agent to continue the user's request."""
    from langchain_core.messages import HumanMessage
    from apps.workspaces.models import Workspace
    workspace = await Workspace.objects.aget(id=workspace_id)
    user = await sync_to_async(lambda: workspace.created_by)()
    agent, oauth_tokens = await _build_agent_for_resume(workspace, user, conversation_id=thread_id)
    body = (f"{SYSTEM_RESUME_MARKER} Measure '{measure_name}' is now defined and queryable across "
            f"the workspace's opportunities. Continue the user's original request using semantic_query.")
    input_state = {"messages": [HumanMessage(content=body)], "workspace_id": workspace_id,
                   "user_id": str(user.id), "user_role": "analyst", "thread_id": thread_id}
    config = {"configurable": {"thread_id": thread_id},
              "recursion_limit": settings.AGENT_RESUME_RECURSION_LIMIT}
    await agent.ainvoke(input_state, config)
    return {"status": "resumed", "measure": measure_name}
```

(If `user` resolution needs the actual requester, store `created_by` on the draft and pass the user id through the approve call instead of `workspace.created_by`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_approve_api.py::test_resume_after_approval_reinvokes_agent -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/tasks.py tests/test_crossopp_approve_api.py
git commit -m "feat(crossopp): resume thread after measure approval"
```

---

## Phase 4 — Proposer (the both/and)

### Task 12: `crossopp_measure_proposer` + `propose_crossopp_measures` tool

**Files:**
- Create: `apps/transformations/services/crossopp_measure_proposer.py`
- Modify: `apps/agents/tools/crossopp_measure_tool.py` (add the propose tool to the factory)
- Test: `tests/test_crossopp_measure_proposer.py`

**Interfaces:**
- Produces: `async propose_measures(candidates_by_opp, *, model_client=None, limit=8) -> list[CanonicalMeasureSpec]` — LLM reads the union of candidate labels and proposes likely measures. `propose_crossopp_measures()` tool: proposes, then routes each spec through the SAME `define`-style commit/draft path (confident → commit, doubtful → one draft each), returns a summary list.

- [ ] **Step 1: Write the failing test (fake LLM)**

```python
# tests/test_crossopp_measure_proposer.py
import pytest

@pytest.mark.asyncio
async def test_proposer_emits_specs_from_candidates():
    from apps.transformations.services import crossopp_measure_proposer as p
    from apps.transformations.services.measure_resolver import FieldCandidate
    fc = FieldCandidate("/d/w","w","child_weight_birth","Birth weight (g)","Double","Reg","Visit","child", True)
    class FakeLLM:
        async def ainvoke(self, messages):
            from apps.transformations.services.crossopp_measure_proposer import _ProposedList, _Proposed
            return _ProposedList(measures=[_Proposed(name="birth_weight", description="g", kind="numeric")])
    specs = await p.propose_measures({"10012": [fc]}, model_client=FakeLLM(), limit=5)
    assert specs[0].name == "birth_weight" and specs[0].kind == "numeric"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_crossopp_measure_proposer.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the proposer**

```python
# apps/transformations/services/crossopp_measure_proposer.py
"""Propose likely cross-opp measures from the apps ("what analysis is most likely").

Emits the SAME CanonicalMeasureSpec the on-demand path uses, so the downstream engine
(resolve -> doubt-gate -> commit) is identical. This replaces the hardcoded STARTER_MEASURES.
"""
from __future__ import annotations

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from apps.transformations.services.measure_resolver import (
    CanonicalMeasureSpec, _clinical_entry_candidates,
)

class _Proposed(BaseModel):
    name: str = Field(description="snake_case slug")
    description: str
    kind: str = Field(description="'numeric' or 'rate'")

class _ProposedList(BaseModel):
    measures: list[_Proposed]

_SYSTEM = (
    "You design a starter analytics catalog for a clinical program spanning several apps. "
    "Given candidate fields (label + type) common across the apps, propose the measures an "
    "operations expert is most likely to want to compare across sites. Prefer clinical "
    "outcomes and delivery quality. Each measure: a snake_case name, a one-line description, "
    "and kind='numeric' (a value to average) or 'rate' (a boolean event averaged 0..1). "
    "Do not invent fields that aren't represented; propose only what the labels support."
)

def _default_client():
    return ChatAnthropic(model=settings.DEFAULT_LLM_MODEL, temperature=0).with_structured_output(_ProposedList)

async def propose_measures(candidates_by_opp, *, model_client=None, limit=8):
    # Union of distinct clinical entry labels across opps (dedupe by column).
    seen, lines = set(), []
    for cands in candidates_by_opp.values():
        for c in _clinical_entry_candidates(cands):
            if c.column in seen:
                continue
            seen.add(c.column)
            lines.append(f"- column={c.column} | type={c.type} | label={c.label!r}")
    client = model_client or _default_client()
    msgs = [SystemMessage(content=_SYSTEM),
            HumanMessage(content=f"Candidate fields:\n" + "\n".join(lines) + f"\n\nPropose up to {limit} measures.")]
    result: _ProposedList = await client.ainvoke(msgs)
    return [CanonicalMeasureSpec(name=m.name, description=m.description,
                                 kind=("rate" if m.kind == "rate" else "numeric"))
            for m in result.measures[:limit]]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_crossopp_measure_proposer.py -v`
Expected: PASS

- [ ] **Step 5: Add the `propose_crossopp_measures` tool + commit**

```python
# apps/agents/tools/crossopp_measure_tool.py — add inside create_crossopp_measure_tools, then return [define, propose]
    @tool
    async def propose_crossopp_measures(limit: int = 8) -> dict:
        """Propose the measures most worth comparing across these opportunities (reads the
        apps). Commits the confident ones and returns any that need your approval."""
        from asgiref.sync import sync_to_async
        from apps.transformations.services import crossopp_measure_service as svc
        from apps.transformations.services import crossopp_measure_proposer as prop
        from apps.transformations.models import CrossOppMeasure, CrossOppMeasureDraft
        opps, cands = await sync_to_async(svc.workspace_opps)(workspace)
        specs = await prop.propose_measures(cands, limit=limit)
        committed, pending = [], []
        for spec in specs:
            if await CrossOppMeasure.objects.filter(workspace=workspace, name=spec.name).aexists():
                continue
            resolutions = await svc.resolve_across_opps_from_candidates(spec, cands)
            has_doubt, flagged = svc.classify_doubt(resolutions)
            if not has_doubt:
                await svc.aadd_measure(workspace, spec, resolutions, opps); committed.append(spec.name)
            else:
                d = await CrossOppMeasureDraft.objects.acreate(
                    workspace=workspace, name=spec.name, description=spec.description, kind=spec.kind,
                    thread_id=conversation_id or "", created_by=user, status="pending",
                    resolutions={o: svc.serialize_resolution(r) for o, r in resolutions.items()},
                    flagged=flagged, shortlists={o: svc.shortlist_for_opp(cands.get(o, [])) for o in flagged})
                pending.append({"measure": spec.name, "draft_id": str(d.id), "flagged": flagged})
        if committed:
            await svc.ensure_measure_queryable_meta(committed[-1])
        return {"status": "proposed", "committed": committed, "needs_approval": pending,
                "message": f"Committed {len(committed)}; {len(pending)} need approval."}
    propose_crossopp_measures.name = "propose_crossopp_measures"
    return [define_crossopp_measure, propose_crossopp_measures]
```

```bash
git add apps/transformations/services/crossopp_measure_proposer.py apps/agents/tools/crossopp_measure_tool.py tests/test_crossopp_measure_proposer.py
git commit -m "feat(crossopp): app-driven measure proposer feeding the same engine (both/and)"
```

---

## Phase 5 — Frontend

### Task 13: `CrossOppMeasureOutput` renderer + approval card + wiring

**Files:**
- Create: `frontend/src/components/ChatMessage/CrossOppMeasureOutput.tsx`
- Modify: `frontend/src/components/ChatMessage/ChatMessage.tsx` (switch + AUTO_EXPAND_TOOLS)
- Modify: `frontend/src/api/crossopp.ts`
- Test: `frontend/src/components/ChatMessage/CrossOppMeasureOutput.test.tsx`

**Interfaces:**
- Consumes: tool output shapes from Task 8 (`committed` / `needs_approval` / `exists`).
- Produces: a renderer that for `committed` shows an expandable per-opp lineage table (column / label / confidence / SQL via `SqlHighlighter`), and for `needs_approval` shows per-flagged-opp controls (confirm / pick-from-shortlist / reject) + a Submit button calling `crossOppApi.approveMeasure(workspaceId, draftId, overrides)`.

- [ ] **Step 1: Add the API client method**

```tsx
// frontend/src/api/crossopp.ts — add
export interface ApproveResponse { status: string; measure: string; lineage: unknown[] }
export const approveMeasure = (
  workspaceId: string, draftId: string,
  overrides: Record<string, { action: "confirm" | "pick" | "reject"; column?: string }>,
) => api.post<ApproveResponse>(
  `/api/workspaces/${workspaceId}/crossopp/measures/${draftId}/approve/`, { overrides })
// add `approveMeasure` to the crossOppApi object too
```

- [ ] **Step 2: Write the failing component test**

```tsx
// CrossOppMeasureOutput.test.tsx
import { render, screen } from "@testing-library/react"
import { CrossOppMeasureOutput } from "./CrossOppMeasureOutput"

it("renders committed lineage", () => {
  render(<CrossOppMeasureOutput workspaceId="w1" output={{ status: "committed", measure: "birth_weight",
    lineage: [{ opportunity_id: "10012", status: "resolved", confidence: 0.97, column: "child_weight_birth",
                matched_label: "Birth weight (g)", sql_expression: "CAST(child_weight_birth AS NUMERIC)" }] }} />)
  expect(screen.getByTestId("crossopp-measure-output-birth_weight")).toBeInTheDocument()
  expect(screen.getByText("child_weight_birth")).toBeInTheDocument()
})

it("renders approval controls when needs_approval", () => {
  render(<CrossOppMeasureOutput workspaceId="w1" output={{ status: "needs_approval", draft_id: "d1",
    measure: "los", flagged: [{ opp_id: "10013", guess: null, confidence: 0.2,
      shortlist: [{ column: "stay_len", label: "Stay (days)", type: "Int" }] }], resolved: [] }} />)
  expect(screen.getByTestId("crossopp-approval-d1")).toBeInTheDocument()
  expect(screen.getByTestId("crossopp-approve-reject-10013")).toBeInTheDocument()
})
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd frontend && bunx vitest run src/components/ChatMessage/CrossOppMeasureOutput.test.tsx`
Expected: FAIL (module not found)

- [ ] **Step 4: Implement the component** (mirror `QueryToolOutput` + the collapsible pattern; use `SqlHighlighter`; `data-testid` on every control)

```tsx
// frontend/src/components/ChatMessage/CrossOppMeasureOutput.tsx
import { useState } from "react"
import { SqlHighlighter } from "./SqlHighlighter"
import { approveMeasure } from "@/api/crossopp"

type Lineage = { opportunity_id: string; status: string; confidence: number; column: string | null; matched_label: string; sql_expression: string | null }
type Flagged = { opp_id: string; guess: string | null; confidence: number; shortlist: { column: string; label: string; type: string }[] }
export type MeasureOutput =
  | { status: "committed"; measure: string; lineage: Lineage[] }
  | { status: "needs_approval"; draft_id: string; measure: string; flagged: Flagged[]; resolved: { opp_id: string; column: string | null; confidence: number }[] }
  | { status: "exists"; measure: string; message?: string }

export function CrossOppMeasureOutput({ workspaceId, output }: { workspaceId: string; output: MeasureOutput }) {
  if (output.status === "exists")
    return <div className="text-xs text-muted-foreground">Measure “{output.measure}” already defined.</div>
  if (output.status === "committed")
    return <LineageTable measure={output.measure} rows={output.lineage} testid={`crossopp-measure-output-${output.measure}`} />
  return <ApprovalCard workspaceId={workspaceId} output={output} />
}

function LineageTable({ measure, rows, testid }: { measure: string; rows: Lineage[]; testid: string }) {
  return (
    <div data-testid={testid} className="space-y-2">
      <div className="text-xs font-medium">{measure} — per-opportunity mapping</div>
      <div className="overflow-x-auto rounded border border-border/50">
        <table className="w-full text-xs"><thead><tr className="bg-muted/40">
          <th className="px-2 py-1 text-left">opp</th><th className="px-2 py-1 text-left">field</th>
          <th className="px-2 py-1 text-left">label</th><th className="px-2 py-1 text-right">conf</th>
          <th className="px-2 py-1 text-left">SQL</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.opportunity_id} className="border-t align-top">
              <td className="px-2 py-1 font-mono">{r.opportunity_id}</td>
              <td className="px-2 py-1 font-mono">{r.column || "—"}</td>
              <td className="px-2 py-1">{r.matched_label || "—"}</td>
              <td className="px-2 py-1 text-right tabular-nums">{r.status === "absent" ? "—" : r.confidence.toFixed(2)}</td>
              <td className="px-2 py-1 font-mono text-muted-foreground">{r.sql_expression ? <SqlHighlighter sql={r.sql_expression} /> : "—"}</td>
            </tr>))}</tbody></table>
      </div>
    </div>
  )
}

function ApprovalCard({ workspaceId, output }: { workspaceId: string; output: Extract<MeasureOutput, { status: "needs_approval" }> }) {
  const [choices, setChoices] = useState<Record<string, { action: "confirm" | "pick" | "reject"; column?: string }>>({})
  const [done, setDone] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const submit = async () => {
    try { const r = await approveMeasure(workspaceId, output.draft_id, choices); setDone(r.measure) }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }
  if (done) return <div data-testid={`crossopp-approved-${output.draft_id}`} className="text-xs text-emerald-600">Defined “{done}”. Ask your question again to see it.</div>
  return (
    <div data-testid={`crossopp-approval-${output.draft_id}`} className="space-y-2 rounded border border-amber-300 bg-amber-50/50 p-2">
      <div className="text-xs font-medium">“{output.measure}” needs your confirmation on {output.flagged.length} opp(s)</div>
      {output.flagged.map((f) => (
        <div key={f.opp_id} className="rounded border px-2 py-1.5 text-xs space-y-1">
          <div className="font-mono">{f.opp_id} <span className="text-muted-foreground">(guess: {f.guess || "absent"}, conf {f.confidence.toFixed(2)})</span></div>
          <div className="flex gap-1 flex-wrap items-center">
            <button data-testid={`crossopp-approve-confirm-${f.opp_id}`} onClick={() => setChoices((c) => ({ ...c, [f.opp_id]: { action: "confirm" } }))} className="rounded border px-1.5">Confirm</button>
            <select data-testid={`crossopp-approve-pick-${f.opp_id}`} onChange={(e) => setChoices((c) => ({ ...c, [f.opp_id]: { action: "pick", column: e.target.value } }))} className="rounded border px-1">
              <option value="">pick field…</option>
              {f.shortlist.map((s) => <option key={s.column} value={s.column}>{s.column} — {s.label}</option>)}
            </select>
            <button data-testid={`crossopp-approve-reject-${f.opp_id}`} onClick={() => setChoices((c) => ({ ...c, [f.opp_id]: { action: "reject" } }))} className="rounded border px-1.5">Reject</button>
            {choices[f.opp_id] && <span className="text-emerald-600">✓ {choices[f.opp_id].action}{choices[f.opp_id].column ? `: ${choices[f.opp_id].column}` : ""}</span>}
          </div>
        </div>
      ))}
      {err && <div className="text-red-600">{err}</div>}
      <button data-testid={`crossopp-approve-submit-${output.draft_id}`} onClick={submit}
        disabled={Object.keys(choices).length < output.flagged.length}
        className="rounded bg-foreground px-2 py-1 text-xs text-background disabled:opacity-50">Commit measure</button>
    </div>
  )
}
```

- [ ] **Step 5: Wire into the switch + auto-expand**

```tsx
// ChatMessage.tsx renderToolOutput — needs workspaceId in scope (the active workspace from the store)
case "define_crossopp_measure":
case "propose_crossopp_measures":
  return <CrossOppMeasureOutput workspaceId={activeWorkspaceId} output={output as MeasureOutput} />
// AUTO_EXPAND_TOOLS — add:
"define_crossopp_measure", "propose_crossopp_measures",
```

If `renderToolOutput` has no workspace in scope, read `activeDomainId` from the app store at the call site (it's already imported in ChatMessage/ChatPanel) and thread it into `renderToolOutput`.

- [ ] **Step 6: Run tests + lint**

Run: `cd frontend && bunx vitest run src/components/ChatMessage/CrossOppMeasureOutput.test.tsx && bun run lint`
Expected: PASS, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/ChatMessage/CrossOppMeasureOutput.tsx frontend/src/components/ChatMessage/ChatMessage.tsx frontend/src/api/crossopp.ts frontend/src/components/ChatMessage/CrossOppMeasureOutput.test.tsx
git commit -m "feat(crossopp): inline measure lineage + approval card in chat"
```

---

## Phase 6 — End-to-end

### Task 14: live chat loop e2e (cube_e2e)

**Files:**
- Create: `tests/e2e/test_crossopp_chat_loop_live.py`

**Interfaces:** exercises the full loop against the live KMC Cross-Opp workspace + Cube (like `tests/e2e/test_tenant_isolation_live.py`). Marked so it only runs when the live stack is up.

- [ ] **Step 1: Write the e2e test**

```python
# tests/e2e/test_crossopp_chat_loop_live.py
import pytest
pytestmark = [pytest.mark.e2e]

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_then_query_new_measure(live_kmc_workspace, admin_user):
    """define_crossopp_measure on a fresh measure -> committed -> semantic_query returns it per opp."""
    from apps.agents.tools.crossopp_measure_tool import create_crossopp_measure_tools
    from mcp_server.services.semantic import semantic_query
    [define, _propose] = create_crossopp_measure_tools(live_kmc_workspace, admin_user, "e2e-thread")
    out = await define.ainvoke({"name": "successful_feeds", "kind": "numeric",
        "description": "successful feeds in the last 24 hours"})
    assert out["status"] in {"committed", "needs_approval"}
    if out["status"] == "needs_approval":
        pytest.skip("resolver had doubt on this measure; approval path covered by API test")
    res = await semantic_query(
        "SELECT opportunity_id, MEASURE(kmc_cross_opp.successful_feeds) "
        "FROM kmc_cross_opp GROUP BY 1", workspace_id=str(live_kmc_workspace.id))
    assert res.get("rows")
```

- [ ] **Step 2: Run against the live stack**

Run: `uv run pytest tests/e2e/test_crossopp_chat_loop_live.py -v -m e2e`
Expected: PASS (or skip if doubt). If the measure isn't queryable, confirm `ensure_measure_queryable_meta` succeeded (dev-mode reload); fall back to `docker compose restart cube` and document.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_crossopp_chat_loop_live.py
git commit -m "test(crossopp): live e2e for chat-driven define -> query loop"
```

---

## Self-Review (completed by author)

- **Spec coverage:** engine (T3-6), on-demand tool (T8-9), approval UI+API+resume (T10,11,13), proposer/both-and (T12), stability test (T4), isolation preserved (no schema-access change; T14 notes negative isolation still holds), Cube-reload risk (T6). All spec sections map to a task.
- **Type consistency:** `MeasureResolution`/`CanonicalMeasureSpec` reused verbatim from `measure_resolver`; `OppRef`/`render_crossopp_model` from `crossopp_cube_builder`; tool output shapes (`committed`/`needs_approval`/`exists`) are identical in T8 (backend) and T13 (frontend types). `add_measure` signature identical in T4 definition and T8/T10/T12 callers. `ensure_measure_queryable_meta` name consistent T6/T8/T12.
- **Placeholders:** none — every code step has real code. Two explicit "verify the helper name in semantic.py / thread workspaceId from the store" notes are grounded fallbacks, not TBDs.
- **Risk note:** if Cube dev-mode does NOT hot-reload model files in this deployment, T6's poll will time out; the fallback (restart cube in the commit path, or a small `CUBEJS`-reload call) is documented in T6/T14 — resolve during T6.
