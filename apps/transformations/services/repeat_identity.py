"""Preserve repeat model identity across generated-name changes.

Current descriptors come from the generator. For older rows, only canonical
generated SQL establishes provenance; a name or description never selects a source.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from apps.common.identifiers import fit_identifier
from apps.transformations.models import TransformationAsset
from apps.transformations.services.staging_identity import RepeatModelMigrationRequired
from mcp_server.event_time import event_time_sql

_REF = re.compile(r"\{\{\s*ref\('([a-z][a-z0-9_]*)'\)\s*\}\}")
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_CAST_TYPES = tuple(
    exp.DataType.build(name, dialect="postgres")
    for name in ("integer", "numeric", "date", "timestamp")
)


@dataclass(frozen=True)
class GeneratedRepeat:
    asset: TransformationAsset
    parent_model: str
    group_path: str


@dataclass(frozen=True)
class RepeatSource:
    parent_model: str
    path: tuple[str, ...]


def _parse(sql: str) -> exp.Select | None:
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except SqlglotError:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return None
    tree = statements[0]
    return None if any(node.comments for node in tree.walk()) else tree


def _without_projection(tree: exp.Select) -> exp.Select:
    source = tree.copy()
    source.set("expressions", [])
    return source


def _literal(value: str) -> str:
    return exp.Literal.string(value).sql(dialect="postgres")


def _json_path(path: tuple[str, ...]) -> str:
    return f"ARRAY[{','.join(_literal(part) for part in path)}]::text[]"


def _array_path(node: exp.Expression | None) -> tuple[str, ...] | None:
    if not isinstance(node, exp.Cast) or not isinstance(node.this, exp.Array):
        return None
    parts = node.this.expressions
    if not all(isinstance(part, exp.Literal) and part.is_string for part in parts):
        return None
    return tuple(part.this for part in parts)


def _generated_projection(
    column: exp.Alias, *, json_column: str | None, scalar_column: str, allow_cast: bool
) -> bool:
    expression = column.this
    cast = None
    if isinstance(expression, exp.Cast):
        cast = expression.args.get("to")
        if not allow_cast or cast not in _CAST_TYPES or not isinstance(expression.this, exp.Nullif):
            return False
        expression = expression.this.this
    if json_column:
        if not isinstance(expression, exp.JSONBExtractScalar):
            return False
        path = _array_path(expression.expression)
        if path is None:
            return False
        raw = f"{json_column} #>> {_json_path(path)}"
    else:
        if not isinstance(expression, exp.JSONExtractScalar):
            return False
        path = expression.expression
        if (
            not isinstance(path, exp.JSONPath)
            or len(path.expressions) != 2
            or not isinstance(path.expressions[0], exp.JSONPathRoot)
            or not isinstance(path.expressions[1], exp.JSONPathKey)
        ):
            return False
        raw = f"{scalar_column}->>{_literal(path.expressions[1].this)}"
    if cast is not None:
        raw = f"NULLIF({raw}, '')::{cast.sql(dialect='postgres')}"
    expected = sqlglot.parse_one(f"SELECT {raw}", read="postgres").expressions[0]
    return column.this == expected


def _canonical_query(
    tree: exp.Select,
    expected: exp.Select,
    *,
    core_count: int,
    json_column: str | None = None,
    scalar_column: str = "elem.value",
    allow_cast: bool = True,
) -> bool:
    if (
        _without_projection(tree) != _without_projection(expected)
        or tree.expressions[:core_count] != expected.expressions
    ):
        return False
    # Added, removed or reordered questions may change projections, but never
    # admit arbitrary SQL or changes to the source, row filter, or repeat index.
    # Alias text is inert and unconstrained: generators before #235 (CommCare),
    # cede159 (Connect) and #426 persisted overlong, digit-led and duplicate
    # aliases (SCOUT-DJANGO-3F).
    for column in tree.expressions[core_count:]:
        if not isinstance(column, exp.Alias):
            return False
        alias = column.args.get("alias")
        if (
            not isinstance(alias, exp.Identifier)
            or not alias.args.get("quoted")
            or not _generated_projection(
                column,
                json_column=json_column,
                scalar_column=scalar_column,
                allow_cast=allow_cast,
            )
        ):
            return False
    return True


def repeat_source(sql: str, *, provider: str) -> RepeatSource | None:
    """Recognize a generated repeat's exact parent and lateral JSON source."""
    refs = list(_REF.finditer(sql))
    if len(refs) != 1:
        return None
    parent = refs[0].group(1)
    sql = _REF.sub(parent, sql)
    if "{{" in sql or "{%" in sql:
        return None
    tree = _parse(sql)
    if tree is None:
        return None
    json_column, id_column = (
        ("form_data", "form_id") if provider == "commcare" else ("form_json", "visit_id")
    )
    where = tree.args.get("where")
    if not isinstance(where, exp.Where) or not isinstance(where.this, exp.Is):
        return None
    extract = where.this.this
    if not isinstance(extract, exp.JSONBExtract):
        return None
    path = _array_path(extract.expression)
    if path is None:
        return None
    # Parser-only template: identifiers are validated and path literals quoted.
    expected = sqlglot.parse_one(
        f"SELECT f.{id_column}, row_number() OVER (PARTITION BY f.{id_column} "  # noqa: S608
        f'ORDER BY elem.ordinality) AS "repeat_index" '
        f"FROM {parent} f, LATERAL jsonb_array_elements("
        f"f.{json_column} #> {_json_path(path)}) WITH ORDINALITY AS elem(value, ordinality) "
        f"WHERE f.{json_column} #> {_json_path(path)} IS NOT NULL",
        read="postgres",
    )
    return RepeatSource(parent, path) if _canonical_query(tree, expected, core_count=2) else None


def _parent_source(sql: str, *, provider: str) -> str | None:
    tree = _parse(sql)
    if tree is None:
        return None
    if provider == "commcare":
        where = tree.args.get("where")
        if not isinstance(where, exp.Where) or not isinstance(where.this, exp.EQ):
            return None
        xmlns = where.this.expression
        if not isinstance(xmlns, exp.Literal) or not xmlns.is_string:
            return None
        # This quoted literal is parsed for comparison, never sent to a database.
        expected = sqlglot.parse_one(
            'SELECT form_id, xmlns, received_on::timestamp AS "received_on", app_id, form_data '  # noqa: S608
            f"FROM raw_forms WHERE xmlns = {_literal(xmlns.this)}",
            read="postgres",
        )
        core_count, json_column = 5, "form_data"
    else:
        expected = sqlglot.parse_one(
            "SELECT visit_id, opportunity_id, username, entity_id, status, deliver_unit_id, "
            "form_json FROM raw_visits",
            read="postgres",
        )
        core_count, json_column = 7, "form_json"
    templates = [expected]
    if provider == "commcare":
        typed_time = expected.copy()
        next(p for p in typed_time.expressions if p.alias_or_name == "received_on").set(
            "this", sqlglot.parse_one(event_time_sql("received_on"), read="postgres")
        )
        templates.append(typed_time)
    if provider == "commcare_connect":
        # Before 03771cc the generator emitted user_id instead of username.
        # That known projection bug does not change raw_visits source identity;
        # regeneration fixes it while preserving existing repeat consumers.
        historical = expected.copy()
        historical.expressions[2].set("this", exp.to_identifier("user_id"))
        templates.append(historical)
    if not any(
        _canonical_query(tree, template, core_count=core_count, json_column=json_column)
        for template in templates
    ):
        return None
    return _without_projection(expected).sql(dialect="postgres")


def _canonical_case(sql: str) -> bool:
    tree = _parse(sql)
    if tree is None:
        return False
    where = tree.args.get("where")
    if not isinstance(where, exp.Where) or not isinstance(where.this, exp.EQ):
        return False
    case_type = where.this.expression
    if not isinstance(case_type, exp.Literal) or not case_type.is_string:
        return False
    # Parser-only template with a quoted literal, never executed as SQL.
    expected = sqlglot.parse_one(
        'SELECT case_id, case_type, case_name, owner_id, date_opened::timestamp AS "date_opened", '  # noqa: S608
        'last_modified::timestamp AS "last_modified", closed FROM raw_cases '
        f"WHERE case_type = {_literal(case_type.this)}",
        read="postgres",
    )
    typed_times = expected.copy()
    for column in ("date_opened", "last_modified"):
        next(p for p in typed_times.expressions if p.alias_or_name == column).set(
            "this", sqlglot.parse_one(event_time_sql(column), read="postgres")
        )
    return any(
        _canonical_query(tree, template, core_count=7, scalar_column="properties", allow_cast=False)
        for template in [expected, typed_times]
    )


def _migration(name: str, reason: str) -> RepeatModelMigrationRequired:
    return RepeatModelMigrationRequired(
        f"An explicit migration is required for staging model {name!r}: {reason}. "
        "Review its source SQL before rebuilding. No staging assets, dependent models, "
        "or replaces links were changed."
    )


def preserve_repeat_names(
    assets: list[TransformationAsset],
    repeats: list[GeneratedRepeat],
    existing: list[TransformationAsset],
    *,
    provider: str,
) -> None:
    """Reserve proven existing identities before allocating names for new sources.

    This only updates unsaved generated instances. Callers must hold the tenant
    lock until the complete asset upsert and orphan sweep commit atomically.
    """
    if provider not in {"commcare", "commcare_connect"}:
        raise ValueError("Unsupported staging provider")
    current = {asset.name: asset for asset in assets}
    old = {asset.name: asset for asset in existing}
    if len(current) != len(assets):
        raise _migration("generated models", "multiple sources require the same model name")
    planned: dict[tuple[str, tuple[str, ...]], GeneratedRepeat] = {}
    parents = {}
    for repeat in repeats:
        parent = current[repeat.parent_model]
        if repeat.parent_model not in parents:
            parents[repeat.parent_model] = _parent_source(parent.sql_content, provider=provider)
        parent_source = parents[repeat.parent_model]
        if parent_source is None:
            raise _migration(repeat.asset.name, "the generated parent source is not canonical")
        identity = (parent_source, tuple(part for part in repeat.group_path.split("/") if part))
        if identity in planned:
            raise _migration(repeat.asset.name, "multiple generated models read the same source")
        planned[identity] = repeat

    owners = defaultdict(list)
    repeat_names = {repeat.asset.name for repeat in repeats}
    other_assets = []
    old_parents = {}
    for asset in existing:
        source = repeat_source(asset.sql_content, provider=provider)
        if source is None:
            other_assets.append(asset)
            continue
        if source.parent_model not in old_parents:
            parent = old.get(source.parent_model)
            old_parents[source.parent_model] = (
                _parent_source(parent.sql_content, provider=provider) if parent else None
            )
        parent_source = old_parents[source.parent_model]
        if parent_source is None:
            raise _migration(asset.name, "its original parent source cannot be established")
        identity = (parent_source, source.path)
        if identity in planned:
            owners[identity].append(asset.name)
        elif source.parent_model in current:
            next_parent = _parent_source(
                current[source.parent_model].sql_content, provider=provider
            )
            if next_parent != parent_source:
                raise _migration(asset.name, "its parent model now identifies a different source")
        if asset.name in current and asset.name not in repeat_names:
            raise _migration(asset.name, "its name is now required by a non-repeat model")
        # A canonical source absent from the current plan is an ordinary orphan.

    for names in owners.values():
        if len(names) != 1:
            raise _migration(names[0], "multiple existing models own the same source")
        name = names[0]
        if not _NAME.fullmatch(name) or len(name.encode()) > 63:
            raise _migration(name, "its existing name is not a supported PostgreSQL model identity")
    reserved = set(current) | set(old)
    for identity, repeat in sorted(planned.items()):
        if identity in owners:
            repeat.asset.name = owners[identity][0]
            continue
        if repeat.asset.name not in old:
            continue
        # Reserve removed names too: a different source must not inherit SQL
        # consumers merely because its preferred name became available today.
        attempt = 0
        while True:
            name = fit_identifier(
                repeat.asset.name,
                unique_key=f"repeat-upgrade:{identity!r}\0{attempt}",
                always_hash=True,
            )
            if name not in reserved:
                break
            attempt += 1
        repeat.asset.name = name
        reserved.add(name)

    # The final plan contains preserved legacy names, not just today's preferred
    # names. Absence alone is never evidence that an unrecognized old model is safe to delete.
    final_names = {asset.name for asset in assets}
    for asset in other_assets:
        if asset.name in final_names:
            continue
        if _parent_source(asset.sql_content, provider=provider) is not None:
            continue
        if provider == "commcare" and _canonical_case(asset.sql_content):
            continue
        raise _migration(asset.name, "its source cannot be proven safe to remove")
