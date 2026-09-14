import pytest

from apps.workspaces.services.tenant_coverage import (
    coverage_complete,
    coverage_warning,
    parse_coverage,
)


@pytest.mark.parametrize(
    "value",
    [
        [],
        "bad",
        {"included_tenants": [], "excluded_tenants": [None]},
        {"included_tenants": [], "excluded_tenants": [{}]},
    ],
)
def test_malformed_coverage_remains_unknown(value):
    assert parse_coverage(value) is None
    assert coverage_complete(value) is None
    assert "unknown" in coverage_warning(value).lower()


def test_valid_coverage_reports_missing_source_without_external_name():
    value = {
        "included_tenants": [{"tenant_id": "ready"}],
        "excluded_tenants": [{"tenant_id": "missing"}],
    }
    assert coverage_complete(value) is False
    assert "missing" in coverage_warning(value)
    assert "When answering" in coverage_warning(value)
    assert (
        coverage_warning({"included_tenants": [{"tenant_id": "ready"}], "excluded_tenants": []})
        == ""
    )


@pytest.mark.parametrize("value", [None, {}])
def test_absent_legacy_coverage_is_silent(value):
    assert coverage_warning(value) == ""
    assert coverage_complete(value) is None
