"""DB-free validation of publication provenance; no identifier reverse guessing."""

from uuid import uuid4

import pytest

from apps.common.identifiers import view_name
from apps.workspaces.services.view_sources import (
    ViewSource,
    ViewSourcesError,
    parse_view_sources,
    validate_published_views,
)


def source_map(tenant_id, name="source__visits", table="visits"):
    return {
        "version": 1,
        "views": {name: {"tenant_id": tenant_id, "source_table_name": table}},
    }


def test_multibyte_fitted_view_retains_authoritative_source_identity():
    tenant_id = str(uuid4())
    name = view_name("日" * 22, "raw_visits")
    assert len(name.encode()) == 63
    assert not name.startswith("日" * 22 + "__")
    assert parse_view_sources(source_map(tenant_id, name, "raw_visits"), {tenant_id}) == {
        name: ViewSource(tenant_id, "raw_visits")
    }


def test_only_empty_migration_default_is_legacy():
    assert parse_view_sources({}, set()) is None
    assert parse_view_sources({"version": 1, "views": {}}, set()) == {}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "",
        {"views": {}},
        {"version": 1},
        {"version": True, "views": {}},
        {"version": 2, "views": {}},
        {"version": 1, "views": []},
    ],
)
def test_invalid_explicit_map_never_becomes_legacy(value):
    with pytest.raises(ViewSourcesError):
        parse_view_sources(value, set())


@pytest.mark.parametrize("name", ["", "a" * 64, "日" * 22, "bad\x00name"])
def test_invalid_view_identifiers_are_rejected(name):
    tenant_id = str(uuid4())
    with pytest.raises(ViewSourcesError):
        parse_view_sources(source_map(tenant_id, name), {tenant_id})


@pytest.mark.parametrize("table", ["", None, 3, "a" * 64, "bad\x00name"])
def test_invalid_source_table_identifiers_are_rejected(table):
    tenant_id = str(uuid4())
    with pytest.raises(ViewSourcesError):
        parse_view_sources(source_map(tenant_id, table=table), {tenant_id})


def test_foreign_source_and_mixed_scope_are_rejected():
    local, foreign = str(uuid4()), str(uuid4())
    value = source_map(local)
    value["views"].update(source_map(foreign, "other__visits")["views"])
    with pytest.raises(ViewSourcesError, match="outside this workspace"):
        parse_view_sources(value, {local})


@pytest.mark.parametrize("entry", [None, [], {}, {"tenant_id": 1, "source_table_name": "visits"}])
def test_malformed_source_entries_do_not_assert_ownership(entry):
    with pytest.raises(ViewSourcesError):
        parse_view_sources({"version": 1, "views": {"source__visits": entry}}, set())


@pytest.mark.parametrize("names", [set(), {"missing"}, {"source__visits", "unmapped"}])
def test_catalog_requires_exact_published_view_set(names):
    sources = {"source__visits": ViewSource(str(uuid4()), "visits")}
    with pytest.raises(ViewSourcesError, match="published views"):
        validate_published_views(sources, names)


def test_valid_published_view_set_and_ascii_names_unchanged():
    tenant_id = str(uuid4())
    assert view_name("source", "visits") == "source__visits"
    sources = parse_view_sources(source_map(tenant_id), {tenant_id})
    validate_published_views(sources, {"source__visits"})
