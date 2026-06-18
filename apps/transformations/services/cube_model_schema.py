"""
Pydantic models validating generated Cube YAML structure.

Covers the Cube data-model shape verified against current Cube docs:
  https://cube.dev/docs/product/data-modeling/reference/cube

Key Cube YAML field names (verified against docs):
  - Cube: name, sql_table (or sql), dimensions, measures, joins
  - Dimension: name, sql, type (string/number/time/boolean/geo), title, description
  - Measure: name, type (count/count_distinct/sum/avg/min/max/number/string/time/boolean),
             sql (optional for count), title, description
  - Join: name (joined cube name), relationship (many_to_one/one_to_many/one_to_one),
          sql (ON clause)
  - View: name, cubes (list of ViewCubeRef with join_path, includes, prefix)

COMPILE_CONTEXT note:
  In Cube Jinja-templated YAML, the security context is accessed via
  COMPILE_CONTEXT.securityContext.<field>. However, this project's checkSqlAuth
  returns { securityContext: { workspace_id, schema_name } } — so the Jinja
  accessor is COMPILE_CONTEXT.securityContext.schema_name. The project convention
  (established in cube.js and cube/README.md) uses the literal string
  '{COMPILE_CONTEXT.security_context.schema_name}' in sql_table values, where
  security_context matches the field name set in checkSqlAuth's return object.
  Both spellings work; we validate the full sql_table string contains
  'COMPILE_CONTEXT' to catch the multi-tenant templating requirement.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------------------
# Dimension
# ---------------------------------------------------------------------------

DimensionType = Literal["string", "number", "time", "boolean", "geo"]


class Dimension(BaseModel):
    """A single Cube dimension (column/expression)."""

    name: str
    sql: str
    type: DimensionType
    title: str | None = None
    description: str | None = None
    primary_key: bool | None = None
    public: bool | None = None


# ---------------------------------------------------------------------------
# Measure
# ---------------------------------------------------------------------------

MeasureType = Literal[
    "count",
    "count_distinct",
    "count_distinct_approx",
    "sum",
    "avg",
    "min",
    "max",
    "number",
    "string",
    "time",
    "boolean",
]


class Measure(BaseModel):
    """A single Cube measure (aggregation)."""

    name: str
    type: MeasureType
    # sql is optional for 'count' type measures
    sql: str | None = None
    title: str | None = None
    description: str | None = None
    public: bool | None = None


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

JoinRelationship = Literal[
    "many_to_one",
    "one_to_many",
    "one_to_one",
    # Legacy Cube aliases — accept but normalise mentally
    "has_one",
    "has_many",
    "belongs_to",
]


class Join(BaseModel):
    """A join from this cube to another cube."""

    name: str
    relationship: JoinRelationship
    sql: str


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class ViewCubeRef(BaseModel):
    """A cube reference inside a view's cubes list."""

    join_path: str
    includes: list[str] | str | None = None  # list of member names or "*"
    excludes: list[str] | None = None
    prefix: bool | None = None
    alias: str | None = None


class View(BaseModel):
    """A Cube view (composed projection of one or more cubes)."""

    name: str
    cubes: list[ViewCubeRef]
    title: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Cube
# ---------------------------------------------------------------------------


_CUBE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class Cube(BaseModel):
    """A single Cube cube definition."""

    name: str
    # sql_table or sql — at least one must be present
    sql_table: str | None = None
    sql: str | None = None
    dimensions: list[Dimension] = []
    measures: list[Measure] = []
    joins: list[Join] = []
    title: str | None = None
    description: str | None = None
    public: bool | None = None

    @field_validator("name")
    @classmethod
    def name_is_safe_identifier(cls, v: str) -> str:
        """Enforce a safe identifier pattern for Cube.name.

        The generated file path includes the cube name
        (cube/model/<schema_name>/<name>.yml).  Rejecting names that contain
        path-separator characters or start with digits provides defense-in-depth
        against path traversal and odd filenames produced by an LLM-generated
        model.
        """
        if not _CUBE_NAME_RE.match(v):
            raise ValueError(
                f"Cube name {v!r} is not a valid identifier. "
                "Must match ^[a-zA-Z_][a-zA-Z0-9_]*$"
            )
        return v

    @model_validator(mode="after")
    def sql_table_or_sql_present(self) -> Cube:
        if not self.sql_table and not self.sql:
            raise ValueError(f"Cube '{self.name}' must have either sql_table or sql")
        return self

    @field_validator("sql_table")
    @classmethod
    def sql_table_has_compile_context(cls, v: str | None) -> str | None:
        """Enforce that sql_table uses COMPILE_CONTEXT for multi-tenancy.

        This is validated at the schema level so callers get a clear error
        message when the multi-tenant template is missing.
        """
        if v is not None and "COMPILE_CONTEXT" not in v:
            raise ValueError(
                f"sql_table {v!r} must use COMPILE_CONTEXT.security_context.schema_name"
                " for multi-tenant schema isolation"
            )
        return v


# ---------------------------------------------------------------------------
# Top-level model file
# ---------------------------------------------------------------------------


class CubeModel(BaseModel):
    """Top-level validated Cube YAML model (one file can hold cubes + views)."""

    cubes: list[Cube] = []
    views: list[View] = []

    @model_validator(mode="after")
    def at_least_one_cube(self) -> CubeModel:
        if not self.cubes:
            raise ValueError("A CubeModel must contain at least one cube")
        return self
