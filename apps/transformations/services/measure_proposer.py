"""
Measure proposer: promote model gaps + agent learnings into candidate Cube measures.

Reads ModelGapSignal and AgentLearning rows for a workspace, prompts the LLM to
propose new Cube measures/dimensions that would close those gaps, validates the
proposed YAML against the CubeModel schema, and returns only NOVEL additions
(deduped against an existing model YAML string).

Public API
----------
    files = await propose_measures(
        workspace,
        model_client=...,          # optional; default ChatAnthropic(DEFAULT_LLM_MODEL)
        existing_model_yaml="...", # current Cube model YAML (for deduplication)
    )
    # files: list of CubeFile dicts with "path" and "yaml" keys.
    # Empty list if every proposed measure already exists in the current model.

LLM client
----------
The default client is ChatAnthropic using settings.DEFAULT_LLM_MODEL, matching
the pattern from apps/agents/graph/base.py and cube_model_generator.py.
Pass a custom client via model_client= for tests.

Async conventions
-----------------
All ORM access uses async methods (aget, afilter, async for). The LLM call
uses ainvoke when available, falling back to asyncio.to_thread(invoke, ...).
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from apps.transformations.services.cube_model_generator import CubeFile, _call_model, _parse_yaml
from apps.transformations.services.cube_model_schema import CubeModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert Cube.js data model engineer. You propose new Cube measures and
dimensions to fill gaps in an existing semantic layer.

CRITICAL RULES:
1. Every cube MUST use the multi-tenant sql_table template:
   sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.<table_name>"
   Do NOT hard-code a schema name.

2. Output ONLY a single YAML document. No prose, no markdown fences.
   Start your response with the YAML key "cubes:".

3. Propose ONLY NEW measures/dimensions not already present in the existing model.
   Focus on measures and dimensions that would directly answer the gap questions
   or encode the agent learnings provided.

4. YAML structure:
   cubes:
     - name: <cube_name>
       sql_table: "{COMPILE_CONTEXT.security_context.schema_name}.<table>"
       measures:
         - name: <measure_name>
           type: count|count_distinct|sum|avg|min|max|number
           sql: "<aggregate expression>"
           title: "<human label>"
       dimensions:
         - name: <dim_name>
           sql: "<col_name>"
           type: string|number|time|boolean
           title: "<human label>"

5. Valid dimension types: string, number, time, boolean, geo
6. Valid measure types: count, count_distinct, sum, avg, min, max, number, string, time, boolean
"""


def _build_proposal_prompt(
    gap_signals: list[dict],
    learnings: list[dict],
    existing_model_yaml: str,
) -> str:
    """Build the user-facing prompt from gap signals, learnings, and existing model."""
    parts: list[str] = []

    parts.append("Propose new Cube measures and dimensions to fill the following gaps.\n")

    if gap_signals:
        parts.append("## Unanswered questions (fell back to raw SQL — no matching measure existed)")
        for sig in gap_signals:
            parts.append(f"- Question: {sig['question']}")
            if sig.get("sql"):
                parts.append(f"  Raw SQL used: {sig['sql'][:500]}")
        parts.append("")

    if learnings:
        parts.append("## Agent learnings (aggregation / join / business logic patterns to encode)")
        for lrn in learnings:
            parts.append(f"- {lrn['description']}")
            if lrn.get("corrected_sql"):
                parts.append(f"  Corrected SQL: {lrn['corrected_sql'][:500]}")
            if lrn.get("applies_to_tables"):
                parts.append(f"  Applies to tables: {lrn['applies_to_tables']}")
        parts.append("")

    if existing_model_yaml:
        parts.append("## Existing Cube model (DO NOT repeat any measure/dimension already here)")
        parts.append(existing_model_yaml[:4000])
        parts.append("")

    parts.append(
        "Produce a YAML document with cubes containing ONLY the new measures/dimensions. "
        "Each cube must include sql_table using COMPILE_CONTEXT. "
        "If an appropriate cube already exists in the model above, reuse the same cube name "
        "but include ONLY the new members (not the existing ones). "
        "Return cubes: [...] YAML only."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------


def _extract_measure_names(model_yaml: str) -> dict[str, set[str]]:
    """
    Parse a Cube YAML string and return {cube_name: {measure_name, ...}}.

    Returns an empty dict if the YAML is empty or unparseable.
    """
    if not model_yaml or not model_yaml.strip():
        return {}
    try:
        data = yaml.safe_load(model_yaml) or {}
    except yaml.YAMLError:
        logger.warning("Could not parse existing model YAML for deduplication")
        return {}

    result: dict[str, set[str]] = {}
    for cube in data.get("cubes", []):
        cube_name = cube.get("name", "")
        if not cube_name:
            continue
        names: set[str] = set()
        for m in cube.get("measures", []):
            if m.get("name"):
                names.add(m["name"])
        result[cube_name] = names
    return result


def _dedupe_model(proposed: CubeModel, existing_names: dict[str, set[str]]) -> CubeModel:
    """
    Remove measures from proposed that already exist in existing_names.

    Returns a new CubeModel containing only cubes that still have at least one
    novel measure or dimension after deduplication. Cubes with no novel members
    are dropped entirely.
    """
    novel_cubes = []
    for cube in proposed.cubes:
        existing_for_cube = existing_names.get(cube.name, set())
        novel_measures = [m for m in cube.measures if m.name not in existing_for_cube]
        # Dimensions are always kept as novel (we only dedupe on measure names per spec)
        if novel_measures or cube.dimensions:
            # Rebuild cube with only novel measures
            cube_copy = cube.model_copy(update={"measures": novel_measures})
            novel_cubes.append(cube_copy)

    return proposed.model_copy(update={"cubes": novel_cubes, "views": []})


# ---------------------------------------------------------------------------
# Default LLM client
# ---------------------------------------------------------------------------


def _default_model_client() -> Any:
    """Build the default ChatAnthropic client matching the project convention."""
    return ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL,
        max_tokens=4096,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def propose_measures(
    workspace: Any,
    *,
    model_client: Any = None,
    existing_model_yaml: str = "",
) -> list[CubeFile]:
    """Propose new Cube measures from accumulated model gaps and agent learnings.

    Parameters
    ----------
    workspace:
        A Workspace instance. Used to filter ModelGapSignal and AgentLearning rows.
    model_client:
        Injectable LLM client. Default: ChatAnthropic(DEFAULT_LLM_MODEL).
        Must support .invoke(messages) or .ainvoke(messages).
    existing_model_yaml:
        YAML string of the current Cube model. New proposals are deduped against
        it — any measure whose name already appears in this model is dropped.

    Returns
    -------
    list[CubeFile]
        CubeFile items containing only NOVEL proposed additions.
        Empty list if every proposed measure already exists.
    """
    if model_client is None:
        model_client = _default_model_client()

    # --- Gather inputs via async ORM ---
    from apps.knowledge.models import AgentLearning, ModelGapSignal

    gap_signals: list[dict] = []
    async for sig in ModelGapSignal.objects.filter(workspace=workspace).order_by("-created_at"):
        gap_signals.append(
            {
                "question": sig.question,
                "sql": sig.sql,
                "created_at": str(sig.created_at),
            }
        )

    high_signal_categories = ("aggregation", "join_pattern", "business_logic")
    learnings: list[dict] = []
    async for lrn in AgentLearning.objects.filter(
        workspace=workspace,
        is_active=True,
        category__in=high_signal_categories,
    ).order_by("-confidence_score"):
        learnings.append(
            {
                "description": lrn.description,
                "corrected_sql": lrn.corrected_sql,
                "applies_to_tables": lrn.applies_to_tables,
                "category": lrn.category,
            }
        )

    if not gap_signals and not learnings:
        logger.info("No gap signals or learnings for workspace %s; skipping proposal", workspace)
        return []

    # --- Build existing measure names for deduplication ---
    existing_names = _extract_measure_names(existing_model_yaml)

    # --- Prompt LLM ---
    system_msg = SystemMessage(content=_SYSTEM_PROMPT)
    user_prompt = _build_proposal_prompt(
        gap_signals=gap_signals,
        learnings=learnings,
        existing_model_yaml=existing_model_yaml,
    )
    user_msg = HumanMessage(content=user_prompt)

    raw_yaml = await _call_model(model_client, [system_msg, user_msg])

    # --- Parse + validate (with one repair round-trip on failure) ---
    def _try_parse_validate(text: str) -> CubeModel:
        data = _parse_yaml(text)
        return CubeModel.model_validate(data)

    try:
        proposed_model = _try_parse_validate(raw_yaml)
    except (yaml.YAMLError, ValidationError, ValueError) as exc:
        logger.warning("Measure proposal validation failed on first attempt: %s", exc)
        bad_raw = raw_yaml
        repair_msg = HumanMessage(
            content=(
                f"The YAML you returned failed validation with this error:\n\n{exc}\n\n"
                "Please fix the YAML and return a corrected version. Output ONLY valid YAML, "
                "no prose or markdown fences. Remember the multi-tenant sql_table rule."
            )
        )
        raw_yaml = await _call_model(
            model_client,
            [system_msg, user_msg, AIMessage(content=bad_raw), repair_msg],
        )
        try:
            proposed_model = _try_parse_validate(raw_yaml)
        except (yaml.YAMLError, ValidationError, ValueError) as repair_exc:
            raise ValueError(
                f"Measure proposal failed after repair attempt: {repair_exc}"
            ) from repair_exc

    # --- Deduplicate against existing model ---
    novel_model = _dedupe_model(proposed_model, existing_names)

    if not novel_model.cubes:
        logger.info(
            "All proposed measures already exist in the current model for workspace %s", workspace
        )
        return []

    # --- Convert to CubeFile list (one per cube) ---
    files: list[CubeFile] = []
    for cube in novel_model.cubes:
        cube_data = {"cubes": [cube.model_dump(exclude_none=True)]}
        cube_yaml = yaml.dump(
            cube_data, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        path = f"cube/model/proposals/{cube.name}.yml"
        files.append(CubeFile(path=path, yaml=cube_yaml))

    logger.info(
        "Proposed %d novel cube(s) for workspace %s",
        len(files),
        workspace,
    )
    return files
