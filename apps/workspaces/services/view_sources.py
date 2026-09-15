"""Validate source identities captured by workspace view publication.

Names are presentation identifiers, not a reversible encoding of a tenant. In
particular, fitting a multibyte name to PostgreSQL's limit can remove its prefix.
"""

from dataclasses import dataclass

from apps.common.identifiers import PG_MAX_IDENTIFIER_BYTES

VIEW_SOURCES_VERSION = 1


class ViewSourcesError(ValueError):
    """Explicit publication provenance cannot safely identify the source views."""


@dataclass(frozen=True)
class ViewSource:
    tenant_id: str
    source_table_name: str


def _identifier(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and len(value.encode("utf-8")) <= PG_MAX_IDENTIFIER_BYTES
    )


def parse_view_sources(value, tenant_ids: set[str]) -> dict[str, ViewSource] | None:
    """Return a scoped map, or None only for the pre-migration empty default.

    An explicit version with a missing/invalid entry is never legacy data. Consumers
    must also check the mapped keys against the views they are actually publishing.
    """
    if value == {}:
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != VIEW_SOURCES_VERSION
        or not isinstance(value.get("views"), dict)
    ):
        raise ViewSourcesError("The workspace view source map is invalid. Rebuild the query layer.")
    sources = {}
    for name, entry in value["views"].items():
        if (
            not _identifier(name)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("tenant_id"), str)
            or entry["tenant_id"] not in tenant_ids
            or not _identifier(entry.get("source_table_name"))
        ):
            raise ViewSourcesError(
                "A workspace view source is invalid or outside this workspace. Rebuild the query layer."
            )
        sources[name] = ViewSource(entry["tenant_id"], entry["source_table_name"])
    return sources


def validate_published_views(sources: dict[str, ViewSource], names: set[str]) -> None:
    """Do not publish catalog ownership for an unlisted, missing, or stale view."""
    if set(sources) != names:
        raise ViewSourcesError(
            "The workspace view source map does not match its published views. Rebuild the query layer."
        )
