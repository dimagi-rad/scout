"""
Integration tests for the MCP server tools.

Tests the full tool handler → service → response envelope chain.
Database access is mocked at the Django ORM / psycopg boundary.
"""

import pytest

from mcp_server.envelope import (
    NOT_FOUND,
    VALIDATION_ERROR,
    Timer,
    error_response,
    success_response,
)

# --- Envelope tests ---


class TestEnvelopeFormat:
    """Verify the response envelope structure."""

    def test_success_response_structure(self):
        envelope = success_response(
            {"tables": ["users"]},
            project_id="abc",
            schema="public",
            timing_ms=42,
        )
        assert envelope["success"] is True
        assert envelope["data"] == {"tables": ["users"]}
        assert envelope["project_id"] == "abc"
        assert envelope["schema"] == "public"
        assert envelope["timing_ms"] == 42
        assert "warnings" not in envelope

    def test_success_response_with_warnings(self):
        envelope = success_response(
            {"rows": []},
            project_id="abc",
            schema="public",
            warnings=["Results truncated to 500 rows"],
        )
        assert envelope["warnings"] == ["Results truncated to 500 rows"]

    def test_success_response_omits_none_timing(self):
        envelope = success_response(
            {"rows": []},
            project_id="abc",
            schema="public",
        )
        assert "timing_ms" not in envelope

    def test_error_response_structure(self):
        envelope = error_response(VALIDATION_ERROR, "Bad SQL")
        assert envelope["success"] is False
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        assert envelope["error"]["message"] == "Bad SQL"
        assert "detail" not in envelope["error"]

    def test_error_response_with_detail(self):
        envelope = error_response(
            NOT_FOUND,
            "Table 'foo' not found",
            detail="Did you mean: foobar, foo_bar",
        )
        assert envelope["error"]["detail"] == "Did you mean: foobar, foo_bar"

    def test_timer_returns_positive_ms(self):
        timer = Timer()
        assert timer.elapsed_ms >= 0


# --- Server tool handler tests ---
# NOTE: Tool handler tests for list_tables, describe_table, and get_metadata
# have been moved to test_mcp_tenant_tools.py which tests the current tenant-based
# code paths (load_tenant_context + execute_internal_query).


# --- Auth token extraction tests ---


class TestAuthTokenExtraction:
    """Test MCP auth token extraction from _meta field."""

    def test_extract_tokens_from_meta(self):
        from mcp_server.auth import extract_oauth_tokens

        meta = {"oauth_tokens": {"commcare": "tok_abc", "commcare_connect": "tok_xyz"}}
        assert extract_oauth_tokens(meta) == {"commcare": "tok_abc", "commcare_connect": "tok_xyz"}

    def test_extract_tokens_missing_meta(self):
        from mcp_server.auth import extract_oauth_tokens

        assert extract_oauth_tokens({}) == {}

    def test_extract_tokens_none_meta(self):
        from mcp_server.auth import extract_oauth_tokens

        assert extract_oauth_tokens(None) == {}


class TestAuditLogScrubbing:
    """Test that oauth_tokens are scrubbed from audit log extra_fields."""

    def test_scrub_removes_oauth_tokens(self):
        from mcp_server.envelope import scrub_extra_fields

        extra = {"measures": ["visits.count"], "oauth_tokens": {"commcare": "secret"}}
        scrubbed = scrub_extra_fields(extra)
        assert "oauth_tokens" not in scrubbed
        assert scrubbed["measures"] == ["visits.count"]

    def test_scrub_noop_when_no_tokens(self):
        from mcp_server.envelope import scrub_extra_fields

        extra = {"measures": ["visits.count"]}
        assert scrub_extra_fields(extra) == {"measures": ["visits.count"]}


class TestAuthTokenExpiredCode:
    """Test AUTH_TOKEN_EXPIRED error code exists."""

    def test_code_defined(self):
        from mcp_server.envelope import AUTH_TOKEN_EXPIRED

        assert AUTH_TOKEN_EXPIRED == "AUTH_TOKEN_EXPIRED"


class TestCommCareCaseLoaderAuth:
    def test_uses_bearer_header_for_oauth(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader

        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            json={"cases": [], "next": None},
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "oauth", "value": "mytoken"},
        )
        loader.load()
        assert requests_mock.last_request.headers["Authorization"] == "Bearer mytoken"

    def test_uses_apikey_header_for_api_key(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader

        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            json={"cases": [], "next": None},
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "api_key", "value": "user@example.com:abc123"},
        )
        loader.load()
        assert (
            requests_mock.last_request.headers["Authorization"] == "ApiKey user@example.com:abc123"
        )

    def test_raises_auth_error_on_401(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareAuthError, CommCareCaseLoader

        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            status_code=401,
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "api_key", "value": "user:key"},
        )
        with pytest.raises(CommCareAuthError):
            loader.load()
