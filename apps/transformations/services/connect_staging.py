"""Generate system-scoped TransformationAsset records from Connect deliver-app metadata.

Each asset holds the SQL for a dbt staging model that flattens
``raw_visits.form_json`` into typed, human-labeled columns.  One ``stg_visits``
asset is produced (non-repeat questions), plus one
``stg_visits__repeat_<group>`` asset per repeat group.

Assets are unsaved — callers must persist them.
"""

from __future__ import annotations

import logging

from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.commcare_staging import (
    _column_name_from_path,
    _question_path_to_json_path,
    _sql_escape,
    _typed_expression,
    _unique_alias,
    slugify_model_name,
)

logger = logging.getLogger(__name__)

# Base columns always present on raw_visits.
_VISIT_BASE_COLUMNS = [
    "visit_id",
    "opportunity_id",
    "user_id",
    "entity_id",
    "status",
    "deliver_unit_id",
    "form_json",
]


# ── Single source of truth for stg_visits column naming ──────────────────────


def visit_column_map(form_definitions: dict) -> list[tuple[dict, str]]:
    """Return an ordered list of (question, final_column_name) for stg_visits.

    Applies the same base-column seeding and :func:`_unique_alias` deduplication
    that :func:`_generate_stg_visits` uses, guaranteeing that the column names
    returned here are byte-for-byte identical to those emitted in the staging SQL.

    Only non-repeat questions with a non-empty ``value`` path are included —
    repeat-group children are staged in separate tables and excluded here.
    """
    seen_aliases: dict[str, int] = {col: 1 for col in _VISIT_BASE_COLUMNS}
    result: list[tuple[dict, str]] = []
    for _deliver_unit, form_def in form_definitions.items():
        for q in form_def.get("questions", []):
            if q.get("repeat"):
                continue
            value_path = q.get("value", "")
            if not value_path:
                continue
            col_name = _unique_alias(_column_name_from_path(value_path), seen_aliases)
            result.append((q, col_name))
    return result


# ── Visit staging asset ──────────────────────────────────────────────────────


def _generate_stg_visits(tenant, form_definitions: dict) -> TransformationAsset:
    """Generate the ``stg_visits`` staging asset for all deliver-app forms.

    Non-repeat questions from every form definition contribute a typed, aliased
    column extracted from ``form_json``.  Repeat questions are skipped here and
    handled by :func:`_generate_repeat_group_asset`.
    """
    lines = ["SELECT"]
    select_parts: list[str] = [f"    {col}" for col in _VISIT_BASE_COLUMNS]

    for q, col_name in visit_column_map(form_definitions):
        value_path = q.get("value", "")
        json_path = _question_path_to_json_path(value_path)
        raw_expr = f"form_json #>> {json_path}"
        q_type = q.get("type")
        select_parts.append(f'    {_typed_expression(raw_expr, q_type)} AS "{col_name}"')

    lines.append(",\n".join(select_parts))
    lines.append("FROM raw_visits")

    return TransformationAsset(
        name="stg_visits",
        description="Staging model for Connect raw_visits with typed form_json columns",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


# ── Repeat-group asset ───────────────────────────────────────────────────────


def _generate_connect_repeat_group_asset(
    tenant, group_path: str, child_questions: list[dict]
) -> TransformationAsset:
    """Generate a ``stg_visits__repeat_<group>`` asset for a repeat group.

    Mirrors ``commcare_staging._generate_repeat_group_asset`` but targets
    ``stg_visits`` as the parent and ``form_json`` as the JSON column.
    """
    group_json_path = _question_path_to_json_path(group_path)
    group_slug = slugify_model_name(group_path.rsplit("/", 1)[-1])
    parent_model = "stg_visits"

    lines = ["SELECT"]
    select_parts: list[str] = [
        "    f.visit_id",
        '    row_number() OVER (PARTITION BY f.visit_id ORDER BY elem.ordinality) AS "repeat_index"',
    ]
    seen_aliases: dict[str, int] = {"visit_id": 1, "repeat_index": 1}

    for q in child_questions:
        value_path = q.get("value", "")
        if not value_path:
            continue
        leaf_name = value_path.rsplit("/", 1)[-1]
        col_name = _unique_alias(_column_name_from_path(value_path), seen_aliases)
        raw_expr = f"elem.value->>'{_sql_escape(leaf_name)}'"
        q_type = q.get("type")
        select_parts.append(f'    {_typed_expression(raw_expr, q_type)} AS "{col_name}"')

    lines.append(",\n".join(select_parts))
    lines.append(f"FROM {{{{ ref('{parent_model}') }}}} f,")
    lines.append("LATERAL jsonb_array_elements(")
    lines.append(f"    f.form_json #> {group_json_path}")
    lines.append(") WITH ORDINALITY AS elem(value, ordinality)")
    lines.append(f"WHERE f.form_json #> {group_json_path} IS NOT NULL")

    model_name = f"{parent_model}__repeat_{group_slug}"
    return TransformationAsset(
        name=model_name,
        description=f"Repeat group '{group_slug}' from {parent_model}",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


# ── Public API ───────────────────────────────────────────────────────────────


def upsert_connect_assets(tenant, tenant_metadata) -> dict:
    """Generate and upsert system staging TransformationAssets for a Connect tenant.

    Mirrors :func:`~apps.transformations.services.commcare_staging.upsert_system_assets`
    exactly but for the Connect deliver-app staging models (``stg_visits`` +
    repeat-group children).

    Calls :func:`generate_connect_assets`, then ``update_or_create`` for each asset,
    and finally deletes any SYSTEM-scoped asset for this tenant whose model is no
    longer generated from the current metadata (orphan sweep).

    Only SYSTEM-scoped assets for this tenant are swept — user-authored
    TENANT/WORKSPACE assets are never touched.

    Returns ``{"created": int, "updated": int, "deleted": int, "total": int}``.
    """
    form_definitions = (tenant_metadata.metadata or {}).get("form_definitions", {})
    assets = generate_connect_assets(form_definitions, tenant)

    created = 0
    updated = 0

    for asset in assets:
        _, was_created = TransformationAsset.objects.update_or_create(
            name=asset.name,
            scope=TransformationScope.SYSTEM,
            tenant=tenant,
            defaults={
                "description": asset.description,
                "sql_content": asset.sql_content,
                "created_by": None,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    current_names = {a.name for a in assets}
    deleted, _ = (
        TransformationAsset.objects.filter(tenant=tenant, scope=TransformationScope.SYSTEM)
        .exclude(name__in=current_names)
        .delete()
    )

    return {"created": created, "updated": updated, "deleted": deleted, "total": len(assets)}


def generate_connect_assets(form_definitions: dict, tenant) -> list[TransformationAsset]:
    """Generate unsaved TransformationAsset instances for Connect staging.

    Args:
        form_definitions: dict keyed by deliver_unit slug, each value having
            ``name``, ``deliver_unit``, and ``questions`` (list of dicts with
            ``label``, ``value``, ``type``, ``repeat``, ``options`` keys).
            Shape matches what ``_extract_form_definitions`` produces for the
            Connect deliver app.
        tenant: a ``users.Tenant`` instance with provider ``commcare_connect``.

    Returns:
        Unsaved :class:`~apps.transformations.models.TransformationAsset`
        instances:
        - one ``stg_visits`` asset (non-repeat questions only)
        - one ``stg_visits__repeat_<group>`` asset per repeat group
    """
    assets: list[TransformationAsset] = []

    # Main stg_visits asset (non-repeat columns only)
    assets.append(_generate_stg_visits(tenant, form_definitions))

    # Collect repeat groups across all form definitions
    repeat_groups: dict[str, list[dict]] = {}
    for _deliver_unit, form_def in form_definitions.items():
        for q in form_def.get("questions", []):
            if not q.get("repeat"):
                continue
            value_path = q.get("value", "")
            if not value_path:
                continue
            # Group path is everything up to (but not including) the leaf segment
            group_path = value_path.rsplit("/", 1)[0]
            repeat_groups.setdefault(group_path, []).append(q)

    for group_path, child_qs in repeat_groups.items():
        assets.append(_generate_connect_repeat_group_asset(tenant, group_path, child_qs))

    return assets
