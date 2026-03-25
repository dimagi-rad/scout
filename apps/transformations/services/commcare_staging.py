"""Generate system-scoped TransformationAsset records from CommCare metadata.

Each asset holds the SQL for a dbt staging model — one per case type, one per
form xmlns, one per repeat group.  Assets are stored in the database but not
executed until the transform phase runs (Milestone 5).
"""

from __future__ import annotations

import logging
import re

from apps.transformations.models import TransformationAsset, TransformationScope

logger = logging.getLogger(__name__)

# ── Type-casting map ────────────────────────────────────────────────────────
# CommCare question type → PostgreSQL cast suffix (None means TEXT / no cast).
_TYPE_CAST: dict[str, str | None] = {
    "Text": None,
    "Barcode": None,
    "PhoneNumber": None,
    "Select": None,
    "MultiSelect": None,
    "GeoPoint": None,
    "Int": "::integer",
    "Double": "::numeric",
    "Decimal": "::numeric",
    "Date": "::date",
    "DateTime": "::timestamp",
}

# CommCare core case columns that are always present on raw_cases.
_CASE_CORE_COLUMNS = [
    ("case_id", None),
    ("case_type", None),
    ("case_name", None),
    ("owner_id", None),
    ("date_opened::timestamp", "date_opened"),
    ("last_modified::timestamp", "last_modified"),
    ("closed", None),
]


# ── Helpers ─────────────────────────────────────────────────────────────────


def slugify_model_name(name: str) -> str:
    """Convert a form/case name to a valid dbt model name.

    - Lowercase
    - Replace spaces, hyphens, dots with underscores
    - Strip non-alphanumeric (except underscores)
    - Collapse consecutive underscores
    - Strip leading/trailing underscores
    """
    slug = name.lower()
    slug = re.sub(r"[\s\-\.]+", "_", slug)
    slug = re.sub(r"[^a-z0-9_]", "", slug)
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("_")


def _question_path_to_json_path(value_path: str) -> str:
    """Convert ``/data/patient_name`` → ``{data,patient_name}``."""
    parts = [p for p in value_path.split("/") if p]
    return "{" + ",".join(parts) + "}"


def _column_name_from_path(value_path: str) -> str:
    """Extract the leaf segment of a question path as a column alias."""
    return slugify_model_name(value_path.rsplit("/", 1)[-1])


def _sql_escape(value: str) -> str:
    """Escape single quotes for safe interpolation into SQL string literals."""
    return value.replace("'", "''")


def _typed_expression(expr: str, question_type: str | None) -> str:
    """Wrap *expr* with a NULLIF + cast if the question type requires it."""
    cast = _TYPE_CAST.get(question_type or "")
    if cast is None:
        return expr
    return f"NULLIF({expr}, ''){cast}"


# ── Case-type asset ────────────────────────────────────────────────────────


def _collect_case_properties(case_type_name: str, metadata: dict) -> list[str]:
    """Walk app_definitions to collect all properties for a case type."""
    props: set[str] = set()
    for app in metadata.get("app_definitions", []):
        for module in app.get("modules", []):
            if module.get("case_type") != case_type_name:
                continue
            case_props = module.get("case_properties", [])
            for prop in case_props:
                if isinstance(prop, dict):
                    key = prop.get("key", "")
                else:
                    key = str(prop)
                if key:
                    props.add(key)
    return sorted(props)


def _generate_case_type_asset(
    tenant, case_type_name: str, properties: list[str], metadata: dict
) -> TransformationAsset:
    """Generate a staging asset for a single case type."""
    lines = ["SELECT"]
    select_parts: list[str] = []

    for expr, alias in _CASE_CORE_COLUMNS:
        if alias:
            select_parts.append(f"    {expr} AS {alias}")
        else:
            select_parts.append(f"    {expr}")

    for prop in properties:
        col = slugify_model_name(prop)
        select_parts.append(f"    properties->>'{_sql_escape(prop)}' AS {col}")

    lines.append(",\n".join(select_parts))
    lines.append("FROM raw_cases")
    lines.append(f"WHERE case_type = '{_sql_escape(case_type_name)}'")

    model_name = f"stg_case_{slugify_model_name(case_type_name)}"
    return TransformationAsset(
        name=model_name,
        description=f"Staging model for {case_type_name} cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


# ── Form asset ──────────────────────────────────────────────────────────────


def _generate_form_asset(
    tenant, form_xmlns: str, form_def: dict, model_name_slug: str
) -> TransformationAsset:
    """Generate a staging asset for a single form."""
    questions = form_def.get("questions", [])

    lines = ["SELECT"]
    select_parts: list[str] = [
        "    form_id",
        "    xmlns",
        "    received_on::timestamp AS received_on",
        "    app_id",
        "    form_data",
    ]

    for q in questions:
        # Skip questions inside repeat groups — handled separately
        if q.get("repeat"):
            continue
        value_path = q.get("value", "")
        if not value_path:
            continue
        json_path = _question_path_to_json_path(value_path)
        col_name = _column_name_from_path(value_path)
        raw_expr = f"form_data #>> '{json_path}'"
        q_type = q.get("type")
        select_parts.append(f"    {_typed_expression(raw_expr, q_type)} AS {col_name}")

    lines.append(",\n".join(select_parts))
    lines.append("FROM raw_forms")
    lines.append(f"WHERE xmlns = '{_sql_escape(form_xmlns)}'")

    model_name = f"stg_form_{model_name_slug}"
    return TransformationAsset(
        name=model_name,
        description=f"Staging model for form: {form_def.get('name', form_xmlns)}",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


# ── Repeat-group asset ─────────────────────────────────────────────────────


def _generate_repeat_group_asset(
    tenant, form_name_slug: str, group_path: str, child_questions: list[dict]
) -> TransformationAsset:
    """Generate a staging asset for a repeat group child table."""
    group_json_path = _question_path_to_json_path(group_path)
    group_slug = slugify_model_name(group_path.rsplit("/", 1)[-1])
    parent_model = f"stg_form_{form_name_slug}"

    lines = ["SELECT"]
    select_parts: list[str] = [
        "    f.form_id",
        "    row_number() OVER (PARTITION BY f.form_id ORDER BY elem.ordinality) AS repeat_index",
    ]

    for q in child_questions:
        value_path = q.get("value", "")
        if not value_path:
            continue
        col_name = _column_name_from_path(value_path)
        raw_expr = f"elem.value->>'{col_name}'"
        q_type = q.get("type")
        select_parts.append(f"    {_typed_expression(raw_expr, q_type)} AS {col_name}")

    lines.append(",\n".join(select_parts))
    lines.append(f"FROM {{{{ ref('{parent_model}') }}}} f,")
    lines.append("LATERAL jsonb_array_elements(")
    lines.append(f"    f.form_data #> '{group_json_path}'")
    lines.append(") WITH ORDINALITY AS elem(value, ordinality)")
    lines.append(f"WHERE f.form_data #> '{group_json_path}' IS NOT NULL")

    model_name = f"{parent_model}__repeat_{group_slug}"
    return TransformationAsset(
        name=model_name,
        description=f"Repeat group '{group_slug}' from {parent_model}",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


# ── Public API ──────────────────────────────────────────────────────────────


def generate_system_assets(tenant, metadata: dict) -> list[TransformationAsset]:
    """Generate unsaved TransformationAsset instances for all system staging models.

    Reads the metadata dict (from TenantMetadata.metadata) which has:
    - case_types: list of {"name": str, "app_id": str, "app_name": str, ...}
    - form_definitions: dict keyed by xmlns, each with "name", "questions", etc.
    - app_definitions: list of raw app JSON

    Returns unsaved TransformationAsset instances with scope=SYSTEM.
    """
    assets: list[TransformationAsset] = []

    # ── Case types ──────────────────────────────────────────────────────────
    for ct in metadata.get("case_types", []):
        name = ct.get("name", "")
        if not name:
            continue
        props = _collect_case_properties(name, metadata)
        assets.append(_generate_case_type_asset(tenant, name, props, metadata))

    # ── Forms + repeat groups ───────────────────────────────────────────────
    seen_form_slugs: dict[str, int] = {}  # slug → count for disambiguation
    form_definitions = metadata.get("form_definitions", {})

    for xmlns, form_def in form_definitions.items():
        form_name = form_def.get("name", xmlns)
        app_name = form_def.get("app_name", "")
        base_slug = slugify_model_name(form_name)

        # Disambiguate duplicate form names across apps
        if base_slug in seen_form_slugs:
            slug = (
                f"{base_slug}_{slugify_model_name(app_name)}"
                if app_name
                else f"{base_slug}_{seen_form_slugs[base_slug]}"
            )
        else:
            slug = base_slug
        seen_form_slugs[base_slug] = seen_form_slugs.get(base_slug, 0) + 1

        assets.append(_generate_form_asset(tenant, xmlns, form_def, slug))

        # Collect repeat groups
        repeat_groups: dict[str, list[dict]] = {}
        for q in form_def.get("questions", []):
            repeat_path = q.get("repeat")
            if repeat_path:
                repeat_groups.setdefault(repeat_path, []).append(q)

        for group_path, child_qs in repeat_groups.items():
            assets.append(_generate_repeat_group_asset(tenant, slug, group_path, child_qs))

    return assets


def upsert_system_assets(tenant, tenant_metadata) -> dict:
    """Generate and upsert system staging TransformationAssets for a tenant.

    Calls generate_system_assets(), then update_or_create for each.
    Returns {"created": int, "updated": int, "total": int}.
    """
    metadata = tenant_metadata.metadata
    assets = generate_system_assets(tenant, metadata)

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

    return {"created": created, "updated": updated, "total": len(assets)}
