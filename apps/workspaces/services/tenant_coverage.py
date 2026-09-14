"""Interpret persisted view coverage without treating malformed data as complete."""


def parse_coverage(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    for key in ("included_tenants", "excluded_tenants"):
        entries = value.get(key)
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("tenant_id"), str)
            or not entry["tenant_id"]
            for entry in entries
        ):
            return None
    return value


def coverage_complete(value) -> bool | None:
    coverage = parse_coverage(value)
    return not coverage["excluded_tenants"] if coverage is not None else None


def coverage_warning(value) -> str:
    # Empty coverage is the legacy migration default, not a partial-build claim.
    if value is None or value == {}:
        return ""
    coverage = parse_coverage(value)
    if coverage is None:
        return "Source coverage is unknown. When answering, verify which sources the data covers."
    excluded = coverage["excluded_tenants"]
    if not excluded:
        return ""
    names = ", ".join(
        f"{entry.get('provider') or 'source'}: {entry.get('external_id') or entry['tenant_id']}"
        for entry in excluded
    )
    return (
        f"Sources excluded from the current query layer: {names}. "
        "When answering from this query layer, disclose the missing data; "
        "do not present partial results as covering the whole workspace."
    )
