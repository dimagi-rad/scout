"""
Deterministic tests for the Cube measure proposer.

No live LLM — uses fake model_client objects returning canned YAML.

Tests:
  1. Happy path: gap signal + agent learning seeded; fake client returns new measure;
     existing model already has 'count'. Assert proposed result:
       - validates as CubeModel
       - INCLUDES the novel measure ('revenue_sum')
       - EXCLUDES the already-existing 'count' measure (dedupe)

  2. Full-dedupe path: fake client returns a measure whose name already exists in
     the existing model → result is an empty list.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from apps.knowledge.models import AgentLearning, ModelGapSignal
from apps.transformations.services.cube_model_schema import CubeModel
from apps.transformations.services.measure_proposer import propose_measures

# ---------------------------------------------------------------------------
# Existing model YAML (already has 'count' on the 'orders' cube)
# ---------------------------------------------------------------------------

_EXISTING_MODEL_YAML = """\
cubes:
  - name: orders
    sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.orders"
    measures:
      - name: count
        type: count
    dimensions:
      - name: order_id
        sql: order_id
        type: string
"""

# ---------------------------------------------------------------------------
# Proposed YAML returned by fake client (adds 'revenue_sum' — a novel measure)
# ---------------------------------------------------------------------------

_PROPOSED_YAML_NOVEL = """\
cubes:
  - name: orders
    sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.orders"
    measures:
      - name: revenue_sum
        type: sum
        sql: amount
        title: "Total Revenue"
    dimensions: []
"""

# ---------------------------------------------------------------------------
# Proposed YAML where the measure already exists (count)
# ---------------------------------------------------------------------------

_PROPOSED_YAML_DUPLICATE = """\
cubes:
  - name: orders
    sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.orders"
    measures:
      - name: count
        type: count
    dimensions: []
"""

# ---------------------------------------------------------------------------
# Fake LLM clients
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeClient:
    """Returns a fixed YAML string on every call."""

    def __init__(self, yaml_response: str) -> None:
        self._yaml = yaml_response
        self.call_count = 0

    def invoke(self, messages: list) -> Any:
        self.call_count += 1
        return _FakeResponse(self._yaml)

    async def ainvoke(self, messages: list) -> Any:
        self.call_count += 1
        return _FakeResponse(self._yaml)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_propose_measures_novel(workspace) -> None:
    """
    Happy path:
    - A ModelGapSignal and a high-signal AgentLearning are seeded for the workspace.
    - Fake client returns YAML with a new 'revenue_sum' measure.
    - Existing model already has 'count'.

    Assertions:
    - Result is a non-empty list of CubeFile.
    - The returned YAML validates as CubeModel.
    - 'revenue_sum' IS present (novel measure included).
    - 'count' is NOT present (dedupe removed the already-existing measure).
    """
    from asgiref.sync import sync_to_async

    # Seed a ModelGapSignal
    await sync_to_async(ModelGapSignal.objects.create)(
        workspace=workspace,
        question="What is the total revenue per order?",
        sql="SELECT SUM(amount) FROM orders",
    )

    # Seed a high-signal AgentLearning (aggregation category)
    await sync_to_async(AgentLearning.objects.create)(
        workspace=workspace,
        description="Use SUM(amount) for revenue; amount is in cents.",
        category="aggregation",
        applies_to_tables=["orders"],
        corrected_sql="SELECT SUM(amount) / 100.0 AS revenue FROM orders",
        confidence_score=0.9,
        is_active=True,
    )

    client = _FakeClient(_PROPOSED_YAML_NOVEL)

    files = await propose_measures(
        workspace,
        model_client=client,
        existing_model_yaml=_EXISTING_MODEL_YAML,
    )

    # Non-empty result
    assert len(files) > 0, "Expected at least one proposed CubeFile"

    # Collect all proposed measures
    proposed_measure_names: set[str] = set()
    for f in files:
        data = yaml.safe_load(f.yaml) or {}
        # Must validate
        CubeModel.model_validate(data)
        for cube in data.get("cubes", []):
            for m in cube.get("measures", []):
                proposed_measure_names.add(m["name"])

    # Novel measure present
    assert "revenue_sum" in proposed_measure_names, (
        f"Expected 'revenue_sum' in proposed measures; got: {proposed_measure_names}"
    )

    # Already-existing 'count' deduplicated out
    assert "count" not in proposed_measure_names, (
        f"'count' should have been deduped out; got: {proposed_measure_names}"
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_propose_measures_fully_deduped(workspace) -> None:
    """
    Full-dedupe path:
    - Fake client returns only a 'count' measure (already in existing model).
    - Result should be an empty list.
    """
    from asgiref.sync import sync_to_async

    # Seed a gap signal so the proposer has something to work with
    await sync_to_async(ModelGapSignal.objects.create)(
        workspace=workspace,
        question="How many orders were placed?",
        sql="SELECT COUNT(*) FROM orders",
    )

    client = _FakeClient(_PROPOSED_YAML_DUPLICATE)

    files = await propose_measures(
        workspace,
        model_client=client,
        existing_model_yaml=_EXISTING_MODEL_YAML,
    )

    # Fully deduped — no novel measures
    assert files == [], (
        f"Expected empty list when all proposed measures already exist; got: {files}"
    )
