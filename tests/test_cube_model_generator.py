"""
Deterministic tests for the Cube model generator.

No live LLM — uses fake model_client objects returning canned YAML.

Assertions (per the task brief):
  - Result validates against cube_model_schema.CubeModel
  - A 'visits' cube exists
  - Every cube's sql_table contains 'COMPILE_CONTEXT' (multi-tenant templating)
  - A 'count' measure exists on the visits cube
  - A seeded KPI measure (muac_confirmation_rate) exists
  - A join exists on the visits cube
  - Files are written under the tmp write_dir

Repair round-trip test:
  - Fake client returns INVALID YAML first, valid YAML on retry
  - The generator calls the client exactly twice and produces a valid model
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from apps.transformations.services.cube_model_generator import CubeFile, generate_cube_model
from apps.transformations.services.cube_model_schema import CubeModel

# ---------------------------------------------------------------------------
# Canned YAML — valid model
# ---------------------------------------------------------------------------

_SCHEMA_NAME = "t_test"

_VALID_YAML = """\
cubes:
  - name: visits
    sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.stg_visits"
    dimensions:
      - name: visit_id
        sql: visit_id
        type: string
        title: "Visit ID"
      - name: username
        sql: username
        type: string
        title: "Username"
      - name: muac
        sql: muac
        type: number
        title: "MUAC (cm)"
      - name: muac_confirmed
        sql: muac_confirmed
        type: string
        title: "Confirmed"
      - name: visited_on
        sql: visited_on
        type: time
        title: "Visit Date"
    measures:
      - name: count
        type: count
      - name: muac_confirmation_rate
        type: number
        sql: "AVG(CASE WHEN muac_confirmed = 'yes' THEN 1.0 ELSE 0 END)"
        title: "MUAC Confirmation Rate"
      - name: approval_rate
        type: number
        sql: "AVG(CASE WHEN status = 'approved' THEN 1.0 ELSE 0 END)"
        title: "Approval Rate"
      - name: flag_rate
        type: number
        sql: "AVG(CASE WHEN flagged = 'yes' THEN 1.0 ELSE 0 END)"
        title: "Flag Rate"
    joins:
      - name: flws
        relationship: many_to_one
        sql: "{visits}.username = {flws.username}"
  - name: flws
    sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.raw_users"
    dimensions:
      - name: username
        sql: username
        type: string
        title: "Username"
      - name: name
        sql: name
        type: string
        title: "FLW Name"
    measures:
      - name: count
        type: count
views:
  - name: program_health
    cubes:
      - join_path: visits
        includes: "*"
      - join_path: visits.flws
        includes:
          - username
          - name
        prefix: true
"""

_INVALID_YAML_FIRST = "this is not valid: yaml: at: all: {{{"

# ---------------------------------------------------------------------------
# Staged tables & form definitions fixtures
# ---------------------------------------------------------------------------

_STAGED_TABLES = [
    {
        "name": "stg_visits",
        "columns": [
            {"name": "visit_id", "type": "text"},
            {"name": "username", "type": "text"},
            {"name": "muac", "type": "numeric"},
            {"name": "muac_confirmed", "type": "text"},
            {"name": "visited_on", "type": "date"},
            {"name": "status", "type": "text"},
            {"name": "flagged", "type": "text"},
        ],
    },
    {
        "name": "raw_users",
        "columns": [
            {"name": "username", "type": "text"},
            {"name": "name", "type": "text"},
        ],
    },
]

_FORM_DEFINITIONS = {
    "muac_visit": {
        "questions": [
            {
                "label": "MUAC (cm)",
                "value": "/data/muac",
                "type": "Decimal",
                "options": None,
                "repeat": False,
            },
            {
                "label": "Confirmed",
                "value": "/data/muac_confirmed",
                "type": "Select",
                "options": ["yes", "no"],
                "repeat": False,
            },
        ]
    }
}

_RELATIONSHIPS = [
    {
        "from_cube": "visits",
        "to_cube": "flws",
        "sql": "{visits}.username = {flws.username}",
        "relationship": "many_to_one",
    }
]

_KNOWLEDGE = (
    "muac_confirmation_rate: percentage of visits where MUAC was confirmed.\n"
    "approval_rate: percentage of visits approved by supervisor.\n"
    "flag_rate: percentage of visits flagged for review.\n"
)

# ---------------------------------------------------------------------------
# Fake model clients
# ---------------------------------------------------------------------------


class _FakeClient:
    """Returns canned YAML unconditionally."""

    def __init__(self, yaml_response: str) -> None:
        self._yaml = yaml_response
        self.call_count = 0

    def invoke(self, messages: list) -> Any:
        self.call_count += 1
        return _FakeResponse(self._yaml)

    async def ainvoke(self, messages: list) -> Any:
        self.call_count += 1
        return _FakeResponse(self._yaml)


class _FakeClientSequence:
    """Returns different YAML on successive calls (for repair round-trip test)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def invoke(self, messages: list) -> Any:
        idx = min(self.call_count, len(self._responses) - 1)
        self.call_count += 1
        return _FakeResponse(self._responses[idx])

    async def ainvoke(self, messages: list) -> Any:
        idx = min(self.call_count, len(self._responses) - 1)
        self.call_count += 1
        return _FakeResponse(self._responses[idx])


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_cube_model_valid(tmp_path: Path) -> None:
    """Happy-path: fake client returns valid YAML; generator produces valid model."""
    client = _FakeClient(_VALID_YAML)
    write_dir = str(tmp_path / _SCHEMA_NAME)

    files = await generate_cube_model(
        schema_name=_SCHEMA_NAME,
        staged_tables=_STAGED_TABLES,
        form_definitions=_FORM_DEFINITIONS,
        knowledge=_KNOWLEDGE,
        relationships=_RELATIONSHIPS,
        model_client=client,
        write_dir=write_dir,
    )

    # --- Result type ---
    assert isinstance(files, list)
    assert len(files) > 0
    for f in files:
        assert isinstance(f, CubeFile)
        assert f.path
        assert f.yaml

    # --- All file paths live under write_dir ---
    write_path = tmp_path / _SCHEMA_NAME
    for f in files:
        assert str(f.path).startswith(str(write_path)), (
            f"File path {f.path!r} should be under {write_path}"
        )

    # --- Files actually written to disk ---
    written = list(write_path.glob("*.yml"))
    assert len(written) > 0, "No YAML files written to disk"

    # --- Parse all written files and collect all cubes ---
    all_cube_names: set[str] = set()
    all_cubes: list[dict] = []
    for yml_path in written:
        data = yaml.safe_load(yml_path.read_text())
        for cube in data.get("cubes", []):
            all_cube_names.add(cube["name"])
            all_cubes.append(cube)

    # --- 'visits' cube exists ---
    assert "visits" in all_cube_names, f"Expected 'visits' cube; got: {all_cube_names}"

    # --- Multi-tenant sql_table: every cube uses COMPILE_CONTEXT ---
    for cube in all_cubes:
        sql_table = cube.get("sql_table", "")
        assert "COMPILE_CONTEXT" in sql_table, (
            f"Cube '{cube['name']}' sql_table {sql_table!r} is missing COMPILE_CONTEXT templating"
        )

    # --- visits cube: count measure present ---
    visits_cube = next(c for c in all_cubes if c["name"] == "visits")
    measure_names = {m["name"] for m in visits_cube.get("measures", [])}
    assert "count" in measure_names, f"Expected 'count' measure in visits; got: {measure_names}"

    # --- visits cube: seeded KPI measure present ---
    assert "muac_confirmation_rate" in measure_names, (
        f"Expected 'muac_confirmation_rate' KPI measure; got: {measure_names}"
    )

    # --- visits cube: join exists ---
    joins = visits_cube.get("joins", [])
    assert len(joins) > 0, "Expected at least one join on the visits cube"
    join_names = {j["name"] for j in joins}
    assert "flws" in join_names, f"Expected join to 'flws'; got: {join_names}"

    # --- LLM called exactly once (no repair needed) ---
    assert client.call_count == 1, f"Expected 1 LLM call; got {client.call_count}"


@pytest.mark.asyncio
async def test_generate_cube_model_validates_against_schema(tmp_path: Path) -> None:
    """Generated model content must validate against CubeModel Pydantic schema."""
    client = _FakeClient(_VALID_YAML)
    write_dir = str(tmp_path / _SCHEMA_NAME)

    await generate_cube_model(
        schema_name=_SCHEMA_NAME,
        staged_tables=_STAGED_TABLES,
        form_definitions=_FORM_DEFINITIONS,
        model_client=client,
        write_dir=write_dir,
    )

    # Reconstruct full model from written files
    write_path = tmp_path / _SCHEMA_NAME
    merged: dict = {"cubes": [], "views": []}
    for yml_path in write_path.glob("*.yml"):
        data = yaml.safe_load(yml_path.read_text()) or {}
        merged["cubes"].extend(data.get("cubes", []))
        merged["views"].extend(data.get("views", []))

    # Must validate without error
    model = CubeModel.model_validate(merged)
    assert len(model.cubes) >= 1


@pytest.mark.asyncio
async def test_generate_cube_model_repair_round_trip(tmp_path: Path) -> None:
    """If first LLM response is invalid YAML, the generator retries and succeeds."""
    client = _FakeClientSequence([_INVALID_YAML_FIRST, _VALID_YAML])
    write_dir = str(tmp_path / _SCHEMA_NAME)

    files = await generate_cube_model(
        schema_name=_SCHEMA_NAME,
        staged_tables=_STAGED_TABLES,
        form_definitions=_FORM_DEFINITIONS,
        model_client=client,
        write_dir=write_dir,
    )

    # Generator must have called the client twice (first bad, then repaired)
    assert client.call_count == 2, (
        f"Expected 2 LLM calls for repair round-trip; got {client.call_count}"
    )

    # Result must still be valid
    assert isinstance(files, list)
    assert len(files) > 0

    # Files written successfully after repair
    write_path = tmp_path / _SCHEMA_NAME
    written = list(write_path.glob("*.yml"))
    assert len(written) > 0

    # visits cube still present with COMPILE_CONTEXT
    for yml_path in written:
        data = yaml.safe_load(yml_path.read_text()) or {}
        for cube in data.get("cubes", []):
            sql_table = cube.get("sql_table", "")
            assert "COMPILE_CONTEXT" in sql_table, (
                f"After repair, cube '{cube['name']}' is still missing COMPILE_CONTEXT"
            )


def test_cube_model_sql_table_without_compile_context_raises():
    with pytest.raises(ValueError, match="COMPILE_CONTEXT"):
        CubeModel(
            cubes=[
                {"name": "test", "sql_table": "myschema.visits", "measures": [], "dimensions": []}
            ]
        )
