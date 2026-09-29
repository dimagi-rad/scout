"""The multi-tenant raw-SQL hint must describe the view names Scout actually mints."""

import re

import pytest

from apps.agents.graph.base import _MULTI_TENANT_NAMESPACE_HINT, _MULTI_TENANT_VIEW_NAME_EXAMPLE
from apps.common.identifiers import PG_MAX_IDENTIFIER_BYTES, sanitize_identifier, view_name
from apps.workspaces.services.schema_manager import SchemaManager


class _Tenant:
    def __init__(self, canonical_name: str, external_id: str):
        self.canonical_name = canonical_name
        self.external_id = external_id


def _example_pattern(prefix: str) -> re.Pattern:
    """The hint's example with the prefix filled in, so a ``__`` inside a table name
    (dbt repeat-group models) cannot be mistaken for the separator."""
    pattern = re.escape(_MULTI_TENANT_VIEW_NAME_EXAMPLE)
    pattern = pattern.replace(re.escape("<tenant_prefix>"), re.escape(prefix))
    pattern = pattern.replace(re.escape("<table_name>"), "(?P<table>.+)")
    return re.compile(f"^{pattern}$")


def test_hint_shows_the_example_name():
    assert f"`{_MULTI_TENANT_VIEW_NAME_EXAMPLE}`" in _MULTI_TENANT_NAMESPACE_HINT


@pytest.mark.parametrize("table", ["raw_cases", "stg_visits__repeat_household"])
def test_example_matches_a_minted_view_name(table):
    prefix = SchemaManager()._view_prefix(_Tenant("Dimagi Demo", "dimagi-demo"))

    match = _example_pattern(prefix).match(view_name(prefix, table))

    assert match is not None
    assert match["table"] == table


def test_hint_forbids_composing_names_that_the_helper_hashes():
    tenant = _Tenant("Kangaroo Mother Care- Preterm Infants Parents Network (PIPN)", "pipn")
    prefix = SchemaManager()._view_prefix(tenant)
    table = "stg_visits__repeat_" + "g" * 40

    minted = view_name(prefix, table)

    assert prefix != sanitize_identifier(tenant.canonical_name)
    assert minted != f"{prefix}__{table}"
    assert len(minted.encode()) <= PG_MAX_IDENTIFIER_BYTES
    assert "`list_tables`" in _MULTI_TENANT_NAMESPACE_HINT


def test_hint_combines_tenants_by_union():
    assert "UNION ALL" in _MULTI_TENANT_NAMESPACE_HINT
