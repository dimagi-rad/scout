"""The FROM clause a generated staging model reads its raw table through."""

from __future__ import annotations


def detoasted_source(
    table: str, columns: list[str], json_column: str, where: str | None = None
) -> str:
    """Read *table* with *json_column* decompressed once per row, not once per field.

    PostgreSQL detoasts a compressed jsonb value for every expression that reads
    it, so a model extracting 864 fields from a 6.5 KB ``form_json`` decompressed
    each row 864 times: ~100s for 7.4k visits against <1s this way (#918).
    ``#> '{}'`` returns the value unchanged (JSON null included) but in memory, and
    ``OFFSET 0`` stops the planner from inlining it back into each reference. The
    row filter sits inside so only matching rows are decompressed.
    """
    lines = [
        "FROM (",
        f"    SELECT {', '.join(columns)}, {json_column} #> '{{}}' AS {json_column}",
        f"    FROM {table}",
    ]
    if where:
        lines.append(f"    WHERE {where}")
    lines.extend(["    OFFSET 0", f") AS {table}"])
    return "\n".join(lines)
