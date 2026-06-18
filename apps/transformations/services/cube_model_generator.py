"""
Cube model generator: staged schema + form definitions + knowledge → validated YAML.

Produces per-workspace Cube data model files where every cube uses
COMPILE_CONTEXT.security_context.schema_name for multi-tenant schema isolation.

Public API
----------
    files = await generate_cube_model(
        schema_name="t_42",
        staged_tables=[{"name": "stg_visits", "columns": [...]}],
        form_definitions={"muac_visit": {"questions": [...]}},
        knowledge="...",
        relationships=[{"from_cube": "visits", "to_cube": "flws", ...}],
    )
    # files: list of CubeFile dicts with "path" and "yaml" keys.

Each generated file is written to cube/model/<schema_name>/ (or write_dir).
The Pydantic schema (cube_model_schema.CubeModel) validates the LLM output.
On validation failure a single repair round-trip re-prompts the LLM with the
error message before raising.

LLM client
----------
The default client is ChatAnthropic using settings.DEFAULT_LLM_MODEL (inheriting
the pattern from apps/agents/graph/base.py). Pass a custom client via
model_client= for tests — the interface is `client.invoke(messages) -> response`
where response.content is a string.

Async conventions
-----------------
The LLM call (model_client.invoke / ainvoke) is the external async-capable call.
File I/O uses pathlib (sync) in a threadpool via asyncio.to_thread so the async
caller is not blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from apps.transformations.services.cube_model_schema import CubeModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class CubeFile:
    """A single Cube model file to write to disk."""

    path: str  # relative path e.g. "cube/model/t_42/visits.yml"
    yaml: str  # validated YAML content


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert Cube.js data model engineer. You generate valid Cube YAML
data model files from schema information, form definitions, and business knowledge.

CRITICAL RULES:
1. Every cube MUST use the multi-tenant sql_table template:
   sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.<table_name>"
   Do NOT hard-code a schema name.

2. Output ONLY a single YAML document. No prose, no markdown fences.
   Start your response with the YAML key "cubes:" or "views:".

3. YAML structure per Cube docs:
   cubes:
     - name: <cube_name>
       sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.<table>"
       dimensions:
         - name: <dim_name>
           sql: "<col_name>"
           type: string|number|time|boolean
           title: "<human label>"   # from form question label
       measures:
         - name: count
           type: count
         - name: <kpi_name>
           type: number
           sql: "<aggregate expression>"
       joins:
         - name: <other_cube_name>
           relationship: many_to_one|one_to_many|one_to_one
           sql: "{visits}.username = {flws.username}"
   views:
     - name: program_health
       cubes:
         - join_path: visits
           includes: "*"

4. Valid dimension types: string, number, time, boolean, geo
5. Valid measure types: count, count_distinct, sum, avg, min, max, number, string, time, boolean
6. Valid join relationships: many_to_one, one_to_many, one_to_one
7. Include a 'count' measure on every cube.
8. Seed KPI measures: muac_confirmation_rate, approval_rate, flag_rate (as type: number).
9. Include a 'program_health' view at the end of the YAML referencing key cubes.
10. Carry the form question's 'label' as the dimension 'title' field.
"""


def _build_user_prompt(
    schema_name: str,
    staged_tables: list[dict],
    form_definitions: dict,
    knowledge: str,
    relationships: list[dict] | None,
) -> str:
    """Assemble the user-facing prompt from inputs."""
    parts: list[str] = []

    parts.append(f"Generate a Cube data model for workspace schema: {schema_name!r}\n")

    # Staged tables and their columns
    parts.append("## Staged tables\n")
    for table in staged_tables:
        tname = table.get("name", "unknown")
        cols = table.get("columns", [])
        parts.append(f"### {tname}")
        if cols:
            col_lines = []
            for col in cols:
                if isinstance(col, dict):
                    cname = col.get("name", "?")
                    ctype = col.get("type", "text")
                    col_lines.append(f"  - {cname} ({ctype})")
                else:
                    col_lines.append(f"  - {col}")
            parts.append("\n".join(col_lines))
        parts.append("")

    # Form definitions (labels for dimension titles)
    if form_definitions:
        parts.append("## Form definitions (use 'label' as dimension title)\n")
        try:
            parts.append(json.dumps(form_definitions, indent=2)[:4000])
        except (TypeError, ValueError):
            parts.append(str(form_definitions)[:4000])
        parts.append("")

    # Business knowledge / KPI hints
    if knowledge:
        parts.append("## Business knowledge & KPI definitions\n")
        parts.append(knowledge[:3000])
        parts.append("")

    # Relationships → joins
    if relationships:
        parts.append("## Relationships (use as joins)\n")
        for rel in relationships:
            parts.append(f"  - {rel}")
        parts.append("")

    parts.append(
        "Produce a single YAML document covering all cubes and a program_health view. "
        "Remember: sql_table MUST use {COMPILE_CONTEXT.security_context.schema_name}.<table>."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# YAML parsing helpers
# ---------------------------------------------------------------------------


def _strip_markdown_fences(text: str) -> str:
    """Remove ```yaml ... ``` or ``` ... ``` fences if present."""
    text = text.strip()
    # Match opening fence with optional language tag
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _parse_yaml(text: str) -> dict:
    """Parse YAML text, stripping fences first. Returns parsed dict."""
    cleaned = _strip_markdown_fences(text)
    return yaml.safe_load(cleaned) or {}


def _validate_cube_model(data: dict) -> CubeModel:
    """Validate parsed YAML dict against CubeModel. Raises ValidationError on failure."""
    return CubeModel.model_validate(data)


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------


def _write_files_sync(files: list[CubeFile], write_dir: str) -> None:
    """Write CubeFile list to disk synchronously (called via asyncio.to_thread)."""
    base = Path(write_dir)
    base.mkdir(parents=True, exist_ok=True)
    for f in files:
        dest = Path(f.path)
        if not dest.is_absolute():
            dest = base / dest.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.yaml, encoding="utf-8")
        logger.debug("Wrote Cube model file: %s", dest)


# ---------------------------------------------------------------------------
# Default LLM client factory
# ---------------------------------------------------------------------------


def _default_model_client() -> Any:
    """Build the default ChatAnthropic client matching apps/agents convention."""
    return ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL,
        max_tokens=4096,
    )


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------


async def _call_model(client: Any, messages: list) -> str:
    """Invoke model_client with messages; supports sync .invoke and async .ainvoke."""
    if hasattr(client, "ainvoke"):
        response = await client.ainvoke(messages)
    else:
        # Fallback: wrap sync invoke in a thread
        response = await asyncio.to_thread(client.invoke, messages)
    # Support both LangChain message objects and plain strings/callables
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, list):
            # Handle list-of-blocks responses (tool-use etc.) — join text parts
            return " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(content)
    return str(response)


# ---------------------------------------------------------------------------
# CubeFile assembly from validated model
# ---------------------------------------------------------------------------


def _model_to_files(model: CubeModel, schema_name: str, write_dir: str) -> list[CubeFile]:
    """Convert validated CubeModel into CubeFile list (one file per cube + views file)."""
    base = Path(write_dir)
    files: list[CubeFile] = []

    # Emit one YAML file per cube
    for cube in model.cubes:
        cube_data = {"cubes": [cube.model_dump(exclude_none=True)]}
        cube_yaml = yaml.dump(
            cube_data, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        path = str(base / f"{cube.name}.yml")
        files.append(CubeFile(path=path, yaml=cube_yaml))

    # Emit views in a separate file
    if model.views:
        views_data = {"views": [v.model_dump(exclude_none=True) for v in model.views]}
        views_yaml = yaml.dump(
            views_data, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        path = str(base / "views.yml")
        files.append(CubeFile(path=path, yaml=views_yaml))

    return files


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_cube_model(
    *,
    schema_name: str,
    staged_tables: list[dict],
    form_definitions: dict,
    knowledge: str = "",
    relationships: list[dict] | None = None,
    model_client: Any = None,
    write_dir: str | None = None,
) -> list[CubeFile]:
    """Generate a validated, multi-tenant Cube data model from schema + form defs.

    Parameters
    ----------
    schema_name:
        PostgreSQL schema name for this workspace (e.g. "t_42").
    staged_tables:
        List of dicts with "name" and "columns" (list of {"name", "type"} dicts).
    form_definitions:
        Connect form definitions keyed by deliver_unit slug; each value has
        {"questions": [{"label", "value", "type", "options", "repeat"}, ...]}.
    knowledge:
        Optional free-text business knowledge / KPI metric definitions.
    relationships:
        Optional list of relationship dicts e.g.
        {"from_cube": "visits", "to_cube": "flws",
         "sql": "{visits}.username = {flws.username}",
         "relationship": "many_to_one"}.
    model_client:
        Injectable LLM client. Default: ChatAnthropic(DEFAULT_LLM_MODEL).
        Must support .invoke(messages) or .ainvoke(messages) returning an object
        with a .content attribute (str or list of blocks).
    write_dir:
        Directory to write YAML files into.
        Default: "cube/model/<schema_name>/" (relative to CWD).

    Returns
    -------
    list[CubeFile]
        Each CubeFile has .path and .yaml. Files are also written to write_dir.
    """
    if model_client is None:
        model_client = _default_model_client()

    if write_dir is None:
        write_dir = f"cube/model/{schema_name}"

    system_msg = SystemMessage(content=_SYSTEM_PROMPT)
    user_prompt = _build_user_prompt(
        schema_name=schema_name,
        staged_tables=staged_tables,
        form_definitions=form_definitions,
        knowledge=knowledge,
        relationships=relationships,
    )
    user_msg = HumanMessage(content=user_prompt)

    # --- First call ---
    raw_yaml = await _call_model(model_client, [system_msg, user_msg])

    # --- Parse + validate ---
    try:
        data = _parse_yaml(raw_yaml)
        model = _validate_cube_model(data)
    except (yaml.YAMLError, ValidationError, ValueError) as exc:
        logger.warning("Cube model validation failed on first attempt: %s", exc)
        # --- Repair round-trip ---
        # Include the bad output as an AIMessage so the LLM sees the full
        # prior turn (human → ai → human) rather than two consecutive human
        # messages, which violates the Anthropic turn-alternation contract.
        bad_raw_yaml = raw_yaml
        repair_msg = HumanMessage(
            content=(
                f"The YAML you returned failed validation with this error:\n\n{exc}\n\n"
                "Please fix the YAML and return a corrected version. Output ONLY valid YAML, "
                "no prose or markdown fences. Remember the multi-tenant sql_table rule."
            )
        )
        raw_yaml = await _call_model(
            model_client,
            [system_msg, user_msg, AIMessage(content=bad_raw_yaml), repair_msg],
        )
        try:
            data = _parse_yaml(raw_yaml)
            model = _validate_cube_model(data)
        except (yaml.YAMLError, ValidationError, ValueError) as repair_exc:
            raise ValueError(
                f"Cube model generation failed after repair attempt: {repair_exc}"
            ) from repair_exc

    # --- Assemble CubeFile list ---
    files = _model_to_files(model, schema_name=schema_name, write_dir=write_dir)

    # --- Write files ---
    await asyncio.to_thread(_write_files_sync, files, write_dir)

    # Observability: log generation summary.
    # NOTE: a dedicated CubeModelRun audit model is deferred to pipeline-wiring.
    logger.info(
        "Generated Cube model: schema=%r cubes=%d write_dir=%r",
        schema_name,
        len(model.cubes),
        write_dir,
    )

    return files
