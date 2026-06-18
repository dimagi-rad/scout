"""Unit tests for the single identifier-minting helper (arch #235).

Pure logic — no DB. Every Postgres identifier Scout mints (schema, role, refresh
schema, dbt model/alias) routes through this module so the 63-byte limit and the
(provider, external_id) collision class are guarded in exactly one place.
"""

import re

from apps.common.identifiers import (
    PG_MAX_IDENTIFIER_BYTES,
    dbt_column_alias,
    dbt_model_name,
    dbt_role_name,
    fit_identifier,
    readonly_role_name,
    refresh_schema_name,
    sanitize_identifier,
    tenant_schema_name,
)


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _is_valid_pg_identifier(s: str) -> bool:
    # Lowercase, starts with a letter, only [a-z0-9_], <= 63 bytes.
    return bool(re.match(r"^[a-z][a-z0-9_]*$", s)) and _nbytes(s) <= PG_MAX_IDENTIFIER_BYTES


class TestSanitizeIdentifier:
    def test_lowercases_and_maps_dashes(self):
        # Matches the historical _sanitize_schema_name contract so _view_prefix
        # (and thus existing view names) stay byte-identical. Collision-safety
        # comes from the digest in tenant_schema_name, not from the sanitizer.
        assert sanitize_identifier("My-Domain") == "my_domain"

    def test_strips_dots_and_spaces(self):
        assert sanitize_identifier("a.b c") == "abc"

    def test_strips_other_punctuation(self):
        assert sanitize_identifier("foo (bar)!") == "foobar"

    def test_prefixes_leading_digit_with_t(self):
        assert sanitize_identifier("123") == "t_123"

    def test_empty_becomes_unknown(self):
        assert sanitize_identifier("!!!") == "unknown"


class TestFitIdentifier:
    def test_short_name_returned_verbatim(self):
        assert fit_identifier("t_123", suffix="_ro") == "t_123_ro"

    def test_always_hash_appends_digest_even_when_short(self):
        out = fit_identifier("t_123", unique_key="a:123", always_hash=True)
        assert out.startswith("t_123_")
        assert out != "t_123"
        assert _is_valid_pg_identifier(out)

    def test_distinct_unique_keys_produce_distinct_names(self):
        a = fit_identifier("t_123", unique_key="commcare_connect:123", always_hash=True)
        b = fit_identifier("t_123", unique_key="ocs:123", always_hash=True)
        assert a != b

    def test_deterministic(self):
        a = fit_identifier("t_123", unique_key="ocs:123", always_hash=True)
        b = fit_identifier("t_123", unique_key="ocs:123", always_hash=True)
        assert a == b

    def test_overflow_is_truncated_with_hash_and_keeps_suffix(self):
        long = "x" * 200
        out = fit_identifier(long, suffix="_ro", unique_key=long)
        assert _nbytes(out) <= PG_MAX_IDENTIFIER_BYTES
        assert out.endswith("_ro")
        assert _is_valid_pg_identifier(out)

    def test_two_long_bases_sharing_head_get_distinct_names(self):
        a = fit_identifier("y" * 100 + "_a", unique_key="y" * 100 + "_a")
        b = fit_identifier("y" * 100 + "_b", unique_key="y" * 100 + "_b")
        assert a != b
        assert _nbytes(a) <= PG_MAX_IDENTIFIER_BYTES
        assert _nbytes(b) <= PG_MAX_IDENTIFIER_BYTES


class TestTenantSchemaName:
    def test_is_valid_identifier(self):
        assert _is_valid_pg_identifier(tenant_schema_name("commcare", "my-domain"))

    def test_cross_provider_same_external_id_distinct(self):
        connect = tenant_schema_name("commcare_connect", "123")
        ocs = tenant_schema_name("ocs", "123")
        assert connect != ocs

    def test_punctuation_collision_distinct(self):
        # 'a-b' / 'a_b' / 'a.b' all sanitize to the same base; the digest must
        # keep them distinct.
        names = {
            tenant_schema_name("commcare", "a-b"),
            tenant_schema_name("commcare", "a_b"),
            tenant_schema_name("commcare", "a.b"),
        }
        assert len(names) == 3

    def test_long_external_ids_sharing_prefix_distinct_and_bounded(self):
        a = tenant_schema_name("commcare", "z" * 200 + "a")
        b = tenant_schema_name("commcare", "z" * 200 + "b")
        assert a != b
        assert _nbytes(a) <= PG_MAX_IDENTIFIER_BYTES
        assert _nbytes(b) <= PG_MAX_IDENTIFIER_BYTES

    def test_leaves_room_for_derived_suffixes(self):
        # The _ro / _dbt / _r{8hex} suffixes must still fit within 63 bytes.
        schema = tenant_schema_name("commcare", "z" * 200)
        assert _nbytes(readonly_role_name(schema)) <= PG_MAX_IDENTIFIER_BYTES
        assert _nbytes(dbt_role_name(schema)) <= PG_MAX_IDENTIFIER_BYTES
        assert _nbytes(refresh_schema_name("commcare", "z" * 200, token="a1b2c3d4")) <= (
            PG_MAX_IDENTIFIER_BYTES
        )

    def test_deterministic(self):
        assert tenant_schema_name("ocs", "42") == tenant_schema_name("ocs", "42")


class TestDerivedRoleNames:
    def test_readonly_role_short_is_verbatim(self):
        # Preserves existing prod role names (byte-identical), so no migration.
        assert readonly_role_name("t_123") == "t_123_ro"

    def test_dbt_role_short_is_verbatim(self):
        assert dbt_role_name("t_123") == "t_123_dbt"

    def test_readonly_role_overflow_is_bounded_and_injective(self):
        s1 = "a" * 62
        s2 = "a" * 61 + "b"
        r1 = readonly_role_name(s1)
        r2 = readonly_role_name(s2)
        assert _nbytes(r1) <= PG_MAX_IDENTIFIER_BYTES
        assert r1.endswith("_ro")
        # Distinct schemas must not share a truncated _ro role.
        assert r1 != r2

    def test_refresh_schema_is_unique_per_token(self):
        a = refresh_schema_name("commcare", "dom", token="aaaaaaaa")
        b = refresh_schema_name("commcare", "dom", token="bbbbbbbb")
        assert a != b
        assert _is_valid_pg_identifier(a)


class TestDbtNames:
    def test_short_model_name_verbatim(self):
        assert dbt_model_name("stg_case_patient") == "stg_case_patient"

    def test_long_model_names_sharing_head_distinct_and_bounded(self):
        a = dbt_model_name("stg_form_" + "q" * 100 + "_a")
        b = dbt_model_name("stg_form_" + "q" * 100 + "_b")
        assert a != b
        assert _nbytes(a) <= PG_MAX_IDENTIFIER_BYTES
        assert _nbytes(b) <= PG_MAX_IDENTIFIER_BYTES

    def test_column_alias_disambiguates_then_fits(self):
        seen: dict[str, int] = {}
        a = dbt_column_alias("colname", seen)
        b = dbt_column_alias("colname", seen)
        assert a != b  # second occurrence disambiguated

    def test_long_aliases_sharing_head_distinct_and_bounded(self):
        seen: dict[str, int] = {}
        a = dbt_column_alias("p" * 100 + "_a", seen)
        b = dbt_column_alias("p" * 100 + "_b", seen)
        assert a != b
        assert _nbytes(a) <= PG_MAX_IDENTIFIER_BYTES
        assert _nbytes(b) <= PG_MAX_IDENTIFIER_BYTES
