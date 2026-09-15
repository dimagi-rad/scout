"""Generate system-scoped TransformationAsset records from CommCare metadata.

Each asset holds the SQL for a dbt staging model — one per case type, one per
form xmlns, one per repeat group.  Assets are stored in the database but not
executed until the transform phase runs (Milestone 5).
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from apps.common.error_codes import ErrorCode
from apps.common.identifiers import dbt_column_alias, dbt_model_name, fit_identifier
from apps.common.localized import localized_str
from apps.transformations.models import TransformationAsset, TransformationScope

logger = logging.getLogger(__name__)


class CaseModelMigrationRequired(ValueError):
    """Existing ambiguous models cannot be regenerated without a reviewed migration."""

    code = ErrorCode.SCHEMA_BUILD_FAILED


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


def slugify_model_name(name: str) -> str:
    """Convert a form/case name to a valid dbt model name.

    - Lowercase
    - Replace spaces, hyphens, dots with underscores
    - Strip non-alphanumeric (except underscores)
    - Collapse consecutive underscores
    - Strip leading/trailing underscores

    Raises ValueError if the result is empty.
    """
    slug = name.lower()
    slug = re.sub(r"[\s\-\.]+", "_", slug)
    slug = re.sub(r"[^a-z0-9_]", "", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        raise ValueError(f"Cannot generate a valid model name from: {name!r}")
    return slug


def _slug_or_digest(name: object, *, identity: str) -> str:
    """Slug for *name*, or a stable digest of *identity* when no ASCII survives.

    Upstream identifiers (labels, case types, properties, question IDs) may be
    non-Latin, punctuation-only, or localized ``{"en": ...}`` dicts. The
    materializer catches one exception per tenant, so a single such identifier
    would silently drop every staging model for that tenant (SCOUT-DJANGO-3D).
    Key the fallback on source identity rather than the mutable name or
    enumeration order.
    """
    try:
        return slugify_model_name(localized_str(name))
    except ValueError:
        return fit_identifier("unnamed", unique_key=identity, always_hash=True)


def _question_path(question: dict) -> str:
    """The question's XForm ``value`` path, or ``""`` when it is not a usable string."""
    value = question.get("value")
    return value if isinstance(value, str) else ""


def _sql_escape(value: str) -> str:
    """Escape single quotes for safe interpolation into SQL string literals."""
    return value.replace("'", "''")


def _question_path_to_json_path(value_path: str) -> str:
    """Convert ``/data/patient_name`` → ``ARRAY['data','patient_name']::text[]``.

    Uses the ARRAY constructor instead of ``{...}`` array literal shorthand
    so that metacharacters (commas, braces) in path segments are safely
    handled as individually-quoted string elements.
    """
    parts = [f"'{_sql_escape(p)}'" for p in value_path.split("/") if p]
    return "ARRAY[" + ",".join(parts) + "]::text[]"


def _leaf_slug(path: str) -> str:
    """Slug the leaf segment of a question or repeat-group path.

    Latin leaves name by leaf alone, so the same question ID in two groups
    shares a slug and ``dbt_column_alias`` suffixes the second. The digest
    fallback keys on the full path because that is the question's XForm
    identity; keying on the leaf would tie the digest to enumeration order.
    """
    return _slug_or_digest(path.rsplit("/", 1)[-1], identity=path)


def _typed_expression(expr: str, question_type: str | None) -> str:
    """Wrap *expr* with a NULLIF + cast if the question type requires it."""
    cast = _TYPE_CAST.get(question_type or "")
    if cast is None:
        return expr
    return f"NULLIF({expr}, ''){cast}"


def _collect_case_properties(case_type_name: str, metadata: dict) -> list[str]:
    """Walk app_definitions to collect all properties for a case type."""
    props: set[str] = set()
    for app in metadata.get("app_definitions", []):
        for module in app.get("modules", []):
            if localized_str(module.get("case_type")) != case_type_name:
                continue
            case_props = module.get("case_properties", [])
            for prop in case_props:
                key = localized_str(prop.get("key") if isinstance(prop, dict) else prop)
                if key:
                    props.add(key)
    return sorted(props)


def _case_base_model_name(case_type: str) -> str:
    return dbt_model_name(f"stg_case_{_slug_or_digest(case_type, identity=f'case:{case_type}')}")


def _case_model_names(case_types: list[dict]) -> dict[str, str]:
    """Keep unambiguous names, disambiguating collisions by source identity.

    Case types are case-sensitive upstream, but PostgreSQL model slugs are not.
    Hash every member of a collision rather than letting metadata order choose
    which case type owns the old, ambiguous name. Reserve literal names too so
    a generated digest cannot overwrite an unrelated case type's model.
    """
    names = {
        name: _case_base_model_name(name)
        for item in case_types
        if (name := localized_str(item.get("name")))
    }
    counts = Counter(names.values())
    used = set(names.values())
    for case_type, base in sorted(names.items()):
        if counts[base] == 1:
            continue
        attempt = 0
        while True:
            identity = f"case:{case_type}" + (f"\0{attempt}" if attempt else "")
            candidate = fit_identifier(base, unique_key=identity, always_hash=True)
            if candidate not in used:
                break
            attempt += 1
        names[case_type] = candidate
        used.add(candidate)
    return names


def _generate_case_type_asset(
    tenant, case_type_name: str, properties: list[str], metadata: dict, *, model_name: str
) -> TransformationAsset:
    """Generate a staging asset for a single case type."""
    lines = ["SELECT"]
    select_parts: list[str] = []
    # Seed with core column names so custom properties that collide get a suffix.
    seen_aliases: dict[str, int] = {(alias or expr): 1 for expr, alias in _CASE_CORE_COLUMNS}
    property_columns = {prop: _slug_or_digest(prop, identity=f"prop:{prop}") for prop in properties}
    reserved_aliases = set(seen_aliases) | set(property_columns.values())

    for expr, alias in _CASE_CORE_COLUMNS:
        if alias:
            select_parts.append(f'    {expr} AS "{alias}"')
        else:
            select_parts.append(f"    {expr}")

    for prop in properties:
        col = dbt_column_alias(property_columns[prop], seen_aliases, reserved=reserved_aliases)
        select_parts.append(f"    properties->>'{_sql_escape(prop)}' AS \"{col}\"")

    lines.append(",\n".join(select_parts))
    lines.append("FROM raw_cases")
    lines.append(f"WHERE case_type = '{_sql_escape(case_type_name)}'")

    return TransformationAsset(
        name=model_name,
        description=f"Staging model for {case_type_name} cases",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


def _generate_form_asset(
    tenant, form_xmlns: str, form_def: dict, model_name_slug: str
) -> TransformationAsset:
    """Generate a staging asset for a single form."""
    questions = form_def.get("questions", [])

    lines = ["SELECT"]
    select_parts: list[str] = [
        "    form_id",
        "    xmlns",
        '    received_on::timestamp AS "received_on"',
        "    app_id",
        "    form_data",
    ]
    # Seed with fixed column names so question aliases that collide get a suffix.
    seen_aliases: dict[str, int] = {
        "form_id": 1,
        "xmlns": 1,
        "received_on": 1,
        "app_id": 1,
        "form_data": 1,
    }
    staged_questions = [q for q in questions if not q.get("repeat") and _question_path(q)]
    reserved_aliases = set(seen_aliases) | {_leaf_slug(q["value"]) for q in staged_questions}

    for q in staged_questions:
        value_path = q["value"]
        json_path = _question_path_to_json_path(value_path)
        col_name = dbt_column_alias(_leaf_slug(value_path), seen_aliases, reserved=reserved_aliases)
        raw_expr = f"form_data #>> {json_path}"
        q_type = q.get("type")
        select_parts.append(f'    {_typed_expression(raw_expr, q_type)} AS "{col_name}"')

    lines.append(",\n".join(select_parts))
    lines.append("FROM raw_forms")
    lines.append(f"WHERE xmlns = '{_sql_escape(form_xmlns)}'")

    model_name = dbt_model_name(f"stg_form_{model_name_slug}")
    return TransformationAsset(
        name=model_name,
        description=f"Staging model for form: {localized_str(form_def.get('name')) or form_xmlns}",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


def _generate_repeat_group_asset(
    tenant, form_name_slug: str, group_path: str, child_questions: list[dict]
) -> TransformationAsset:
    """Generate a staging asset for a repeat group child table."""
    group_json_path = _question_path_to_json_path(group_path)
    group_leaf = group_path.rsplit("/", 1)[-1]
    group_slug = _leaf_slug(group_path)
    # Must match the parent form asset's (possibly hash-bounded) name so ref()
    # resolves — both derive from the same slug via the same helper.
    parent_model = dbt_model_name(f"stg_form_{form_name_slug}")

    lines = ["SELECT"]
    select_parts: list[str] = [
        "    f.form_id",
        '    row_number() OVER (PARTITION BY f.form_id ORDER BY elem.ordinality) AS "repeat_index"',
    ]
    # Seed with fixed column names so child question aliases that collide get a suffix.
    seen_aliases: dict[str, int] = {"form_id": 1, "repeat_index": 1}
    staged_questions = [q for q in child_questions if _question_path(q)]
    reserved_aliases = set(seen_aliases) | {_leaf_slug(q["value"]) for q in staged_questions}

    for q in staged_questions:
        value_path = q["value"]
        leaf_name = value_path.rsplit("/", 1)[-1]
        col_name = dbt_column_alias(_leaf_slug(value_path), seen_aliases, reserved=reserved_aliases)
        raw_expr = f"elem.value->>'{_sql_escape(leaf_name)}'"
        q_type = q.get("type")
        select_parts.append(f'    {_typed_expression(raw_expr, q_type)} AS "{col_name}"')

    lines.append(",\n".join(select_parts))
    lines.append(f"FROM {{{{ ref('{parent_model}') }}}} f,")
    lines.append("LATERAL jsonb_array_elements(")
    lines.append(f"    f.form_data #> {group_json_path}")
    lines.append(") WITH ORDINALITY AS elem(value, ordinality)")
    lines.append(f"WHERE f.form_data #> {group_json_path} IS NOT NULL")

    model_name = dbt_model_name(f"{parent_model}__repeat_{group_slug}")
    return TransformationAsset(
        name=model_name,
        description=f"Repeat group '{group_leaf}' from {parent_model}",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="\n".join(lines),
        created_by=None,
    )


def generate_system_assets(tenant, metadata: dict) -> list[TransformationAsset]:
    """Generate unsaved TransformationAsset instances for all system staging models.

    Reads the metadata dict (from TenantMetadata.metadata) which has:
    - case_types: list of {"name": str, "app_id": str, "app_name": str, ...}
    - form_definitions: dict keyed by xmlns, each with "name", "questions", etc.
    - app_definitions: list of raw app JSON

    Returns unsaved TransformationAsset instances with scope=SYSTEM.
    """
    assets: list[TransformationAsset] = []

    for name, model_name in _case_model_names(metadata.get("case_types", [])).items():
        props = _collect_case_properties(name, metadata)
        assets.append(
            _generate_case_type_asset(tenant, name, props, metadata, model_name=model_name)
        )

    seen_form_slugs: dict[str, int] = {}  # slug → count for disambiguation
    form_definitions = metadata.get("form_definitions", {})

    for xmlns, form_def in form_definitions.items():
        app_name = localized_str(form_def.get("app_name"))
        base_slug = _slug_or_digest(form_def.get("name", xmlns), identity=f"form:{xmlns}")

        # Disambiguate duplicate form names across apps; always incorporate the
        # counter so 3+ collisions stay unique.
        if base_slug in seen_form_slugs:
            count = seen_form_slugs[base_slug]
            app_slug = (
                _slug_or_digest(app_name, identity=f"app:{form_def.get('app_id') or app_name}")
                if app_name
                else ""
            )
            app_suffix = f"_{app_slug}" if app_slug else ""
            slug = f"{base_slug}{app_suffix}_{count}"
        else:
            slug = base_slug
        seen_form_slugs[base_slug] = seen_form_slugs.get(base_slug, 0) + 1

        assets.append(_generate_form_asset(tenant, xmlns, form_def, slug))

        repeat_groups: dict[str, list[dict]] = {}
        for q in form_def.get("questions", []):
            repeat_path = q.get("repeat")
            if isinstance(repeat_path, str) and repeat_path:
                repeat_groups.setdefault(repeat_path, []).append(q)

        for group_path, child_qs in repeat_groups.items():
            assets.append(_generate_repeat_group_asset(tenant, slug, group_path, child_qs))

    return assets


def upsert_system_assets(tenant, tenant_metadata) -> dict:
    """Generate and upsert system staging TransformationAssets for a tenant.

    Calls generate_system_assets(), then update_or_create for each, and finally
    deletes any SYSTEM-scoped asset for this tenant whose model is no longer
    generated from the current metadata (issue #241, 04#5). Without this sweep a
    case type or form removed upstream would leave an orphaned asset that the
    transform phase keeps materializing into a stale table presented as fresh.

    Only SYSTEM-scoped assets for this tenant are swept — user-authored
    TENANT/WORKSPACE assets are never touched.

    Returns {"created": int, "updated": int, "deleted": int, "total": int}.
    """
    metadata = tenant_metadata.metadata
    assets = generate_system_assets(tenant, metadata)

    # A legacy collision may already have SQL/artifact references or `replaces`
    # links whose intended source case type cannot be inferred safely. The
    # ordinary orphan sweep below would delete it and SET_NULL those links.
    # Require an explicit migration before any writes; never guess an alias.
    renamed_case_models = {
        old_name
        for name, model_name in _case_model_names(metadata.get("case_types", [])).items()
        if (old_name := _case_base_model_name(name)) != model_name
    }
    if renamed_case_models:
        existing = list(
            TransformationAsset.objects.filter(
                tenant=tenant,
                scope=TransformationScope.SYSTEM,
                name__in=renamed_case_models,
            ).values_list("name", flat=True)
        )
        if existing:
            raise CaseModelMigrationRequired(
                "An explicit migration is required for existing ambiguous case-type models: "
                f"{', '.join(sorted(existing))}. Review SQL/artifact references and replaces "
                "links before rebuilding; no assets were changed."
            )

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
