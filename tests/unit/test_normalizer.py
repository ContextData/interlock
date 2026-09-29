"""Tests for SQL normalizer and fingerprinter."""

from __future__ import annotations

from interlock.core.normalizer import normalize_sql
from interlock.models import NormalizedQuery

SOURCE_ID = "src_test_001"


class TestBasicNormalization:
    """Basic SELECT normalization."""

    def test_simple_select(self):
        result = normalize_sql("SELECT id, name FROM users", SOURCE_ID)
        assert isinstance(result, NormalizedQuery)
        assert result.normalized_sql is not None
        assert result.parse_error is False
        assert result.operation == "SELECT"

    def test_whitespace_normalization(self):
        """Extra whitespace and mixed case produce the same fingerprint."""
        r1 = normalize_sql("SELECT  id FROM  users", SOURCE_ID)
        r2 = normalize_sql("select id from users", SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint

    def test_case_insensitivity(self):
        """SELECT vs select produce the same normalized output."""
        r1 = normalize_sql("SELECT id FROM users", SOURCE_ID)
        r2 = normalize_sql("select id from users", SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint
        assert r1.normalized_sql == r2.normalized_sql


class TestWhereClauseSorting:
    """AND conditions reordered alphabetically."""

    def test_and_conditions_sorted(self):
        sql1 = "SELECT * FROM t WHERE z = 1 AND a = 2 AND m = 3"
        sql2 = "SELECT * FROM t WHERE a = 2 AND m = 3 AND z = 1"
        r1 = normalize_sql(sql1, SOURCE_ID)
        r2 = normalize_sql(sql2, SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint

    def test_or_conditions_not_sorted(self):
        """OR conditions should NOT be reordered (semantics differ for short-circuit)."""
        sql = "SELECT * FROM t WHERE z = 1 OR a = 2"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False
        # Just ensure it parses without error; OR order preserved

    def test_nested_and_sorted(self):
        """Nested ANDs within OR should each be sorted."""
        sql1 = "SELECT * FROM t WHERE (z = 1 AND a = 2) OR (y = 3 AND b = 4)"
        sql2 = "SELECT * FROM t WHERE (a = 2 AND z = 1) OR (b = 4 AND y = 3)"
        r1 = normalize_sql(sql1, SOURCE_ID)
        r2 = normalize_sql(sql2, SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint


class TestLiteralReplacement:
    """Literal replacement with $N parameters."""

    def test_integer_literal(self):
        result = normalize_sql("SELECT * FROM t WHERE id = 42", SOURCE_ID)
        assert "$1" in result.normalized_sql
        assert 42 in result.parameters

    def test_string_literal(self):
        result = normalize_sql("SELECT * FROM t WHERE name = 'alice'", SOURCE_ID)
        assert "$1" in result.normalized_sql
        assert "alice" in result.parameters

    def test_float_literal(self):
        result = normalize_sql("SELECT * FROM t WHERE score > 3.14", SOURCE_ID)
        assert "$1" in result.normalized_sql
        assert any(abs(p - 3.14) < 0.001 for p in result.parameters if isinstance(p, float))

    def test_boolean_literal(self):
        result = normalize_sql("SELECT * FROM t WHERE active = true", SOURCE_ID)
        assert "$1" in result.normalized_sql
        assert True in result.parameters

    def test_null_literal(self):
        """NULL in a value position (e.g. INSERT) should be parameterized."""
        result = normalize_sql("INSERT INTO t (a) VALUES (NULL)", SOURCE_ID)
        assert None in result.parameters

    def test_multiple_literals_numbered_sequentially(self):
        result = normalize_sql("SELECT * FROM t WHERE a = 1 AND b = 'x' AND c = 3.0", SOURCE_ID)
        assert len(result.parameters) == 3
        # After AND-sorting, parameter numbering may reorder, but all values present
        assert 1 in result.parameters
        assert "x" in result.parameters


class TestTableExtraction:
    """Table name extraction from simple and JOINed queries."""

    def test_single_table(self):
        result = normalize_sql("SELECT * FROM users", SOURCE_ID)
        assert "users" in result.tables

    def test_joined_tables(self):
        result = normalize_sql("SELECT * FROM users u JOIN orders o ON u.id = o.user_id", SOURCE_ID)
        assert "users" in result.tables
        assert "orders" in result.tables

    def test_schema_qualified_table(self):
        result = normalize_sql("SELECT * FROM public.users", SOURCE_ID)
        assert "public.users" in result.tables

    def test_multiple_joins(self):
        sql = "SELECT * FROM a " "JOIN b ON a.id = b.a_id " "JOIN c ON b.id = c.b_id"
        result = normalize_sql(sql, SOURCE_ID)
        assert "a" in result.tables
        assert "b" in result.tables
        assert "c" in result.tables

    def test_insert_table(self):
        result = normalize_sql("INSERT INTO users (name) VALUES ('x')", SOURCE_ID)
        assert "users" in result.tables

    def test_update_table(self):
        result = normalize_sql("UPDATE users SET name = 'x' WHERE id = 1", SOURCE_ID)
        assert "users" in result.tables

    def test_delete_table(self):
        result = normalize_sql("DELETE FROM users WHERE id = 1", SOURCE_ID)
        assert "users" in result.tables


class TestOperationDetection:
    """Operation type detection."""

    def test_select(self):
        result = normalize_sql("SELECT 1", SOURCE_ID)
        assert result.operation == "SELECT"

    def test_insert(self):
        result = normalize_sql("INSERT INTO t (a) VALUES (1)", SOURCE_ID)
        assert result.operation == "INSERT"

    def test_update(self):
        result = normalize_sql("UPDATE t SET a = 1", SOURCE_ID)
        assert result.operation == "UPDATE"

    def test_delete(self):
        result = normalize_sql("DELETE FROM t WHERE id = 1", SOURCE_ID)
        assert result.operation == "DELETE"


class TestMultiStatement:
    """Multi-statement queries split by semicolons."""

    def test_two_statements(self):
        sql = "SELECT 1; SELECT 2"
        results = normalize_sql(sql, SOURCE_ID)
        assert isinstance(results, list)
        assert len(results) == 2
        assert all(isinstance(r, NormalizedQuery) for r in results)

    def test_trailing_semicolon(self):
        """Trailing semicolon should not create an extra empty statement."""
        sql = "SELECT 1;"
        result = normalize_sql(sql, SOURCE_ID)
        # Single statement with trailing semicolon - returns single result
        assert isinstance(result, NormalizedQuery)

    def test_three_statements(self):
        sql = "SELECT 1; SELECT 2; SELECT 3"
        results = normalize_sql(sql, SOURCE_ID)
        assert isinstance(results, list)
        assert len(results) == 3


class TestSubqueries:
    """Subquery handling."""

    def test_subquery_in_where(self):
        sql = "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False
        assert "users" in result.tables
        assert "orders" in result.tables

    def test_subquery_in_from(self):
        sql = "SELECT * FROM (SELECT id FROM users) sub"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False


class TestCTEs:
    """WITH (CTE) clause handling."""

    def test_cte_table_extraction(self):
        sql = "WITH active AS (SELECT * FROM users WHERE active = true) SELECT * FROM active"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False
        assert "users" in result.tables

    def test_multiple_ctes(self):
        sql = (
            "WITH a AS (SELECT * FROM users), "
            "b AS (SELECT * FROM orders) "
            "SELECT * FROM a JOIN b ON a.id = b.user_id"
        )
        result = normalize_sql(sql, SOURCE_ID)
        assert "users" in result.tables
        assert "orders" in result.tables


class TestCommentsStripped:
    """Comments should be stripped during normalization."""

    def test_line_comment(self):
        sql = "-- this is a comment\nSELECT id FROM users"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False
        assert result.operation == "SELECT"

    def test_block_comment(self):
        sql = "/* block comment */ SELECT id FROM users"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False

    def test_comments_only(self):
        """Comments-only input should trigger parse error fallback."""
        sql = "-- just a comment"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is True


class TestParseErrorFallback:
    """ParseError fallback to raw hash."""

    def test_invalid_sql(self):
        result = normalize_sql("NOT VALID SQL !!!", SOURCE_ID)
        assert result.parse_error is True
        assert result.normalized_sql is None
        assert result.fingerprint  # should still have a hash

    def test_parse_error_deterministic(self):
        """Same invalid SQL produces same fingerprint."""
        r1 = normalize_sql("BROKEN SQL", SOURCE_ID)
        r2 = normalize_sql("BROKEN SQL", SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint


class TestEmptySQL:
    """Empty and whitespace-only SQL."""

    def test_empty_string(self):
        result = normalize_sql("", SOURCE_ID)
        assert result.parse_error is True
        assert result.original_sql == ""

    def test_whitespace_only(self):
        result = normalize_sql("   \n\t  ", SOURCE_ID)
        assert result.parse_error is True

    def test_none_like_empty(self):
        """Passing None-ish empty string."""
        result = normalize_sql("", SOURCE_ID)
        assert isinstance(result, NormalizedQuery)


class TestParameterizedQueries:
    """ORM-generated queries with $1, $2 placeholders."""

    def test_existing_params_renumbered(self):
        sql = "SELECT * FROM users WHERE id = $1 AND name = $2"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False
        # Params should capture the original placeholder references
        assert len(result.parameters) == 2

    def test_mixed_params_and_literals(self):
        sql = "SELECT * FROM users WHERE id = $1 AND age > 30"
        result = normalize_sql(sql, SOURCE_ID)
        assert result.parse_error is False
        assert len(result.parameters) == 2


class TestFingerprintDeterminism:
    """Fingerprint determinism and composition."""

    def test_same_query_same_fingerprint(self):
        sql = "SELECT * FROM users WHERE id = 1"
        r1 = normalize_sql(sql, SOURCE_ID)
        r2 = normalize_sql(sql, SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint

    def test_different_literals_same_fingerprint(self):
        """Queries differing only in literal values should match."""
        r1 = normalize_sql("SELECT * FROM users WHERE id = 1", SOURCE_ID)
        r2 = normalize_sql("SELECT * FROM users WHERE id = 999", SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint

    def test_source_id_affects_fingerprint(self):
        sql = "SELECT 1"
        r1 = normalize_sql(sql, "source_a")
        r2 = normalize_sql(sql, "source_b")
        assert r1.fingerprint != r2.fingerprint

    def test_role_context_affects_fingerprint(self):
        sql = "SELECT 1"
        r1 = normalize_sql(sql, SOURCE_ID, role_context="admin")
        r2 = normalize_sql(sql, SOURCE_ID, role_context="reader")
        assert r1.fingerprint != r2.fingerprint

    def test_no_role_context_vs_empty(self):
        sql = "SELECT 1"
        r1 = normalize_sql(sql, SOURCE_ID, role_context=None)
        r2 = normalize_sql(sql, SOURCE_ID, role_context="")
        assert r1.fingerprint == r2.fingerprint

    def test_reordered_where_same_fingerprint(self):
        """WHERE a = 1 AND b = 2 should match WHERE b = 2 AND a = 1."""
        r1 = normalize_sql("SELECT * FROM t WHERE a = 1 AND b = 2", SOURCE_ID)
        r2 = normalize_sql("SELECT * FROM t WHERE b = 2 AND a = 1", SOURCE_ID)
        assert r1.fingerprint == r2.fingerprint


class TestOriginalSQLPreserved:
    """The original_sql field should always contain the input."""

    def test_original_preserved(self):
        sql = "  SELECT  id  FROM  users  "
        result = normalize_sql(sql, SOURCE_ID)
        assert result.original_sql == sql
