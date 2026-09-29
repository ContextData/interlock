"""Tests for the write classifier."""

import pytest

from interlock.core.write_classifier import WriteClassifier
from interlock.models import RiskLevel


@pytest.fixture
def classifier() -> WriteClassifier:
    return WriteClassifier()


@pytest.fixture
def classifier_with_critical() -> WriteClassifier:
    return WriteClassifier(critical_tables={"audit_log", "users", "public.secrets"})


# -- Read-only statements -> None --


class TestReadStatements:
    def test_simple_select_returns_none(self, classifier: WriteClassifier):
        result = classifier.classify("SELECT 1")
        assert result is None

    def test_select_from_table_returns_none(self, classifier: WriteClassifier):
        result = classifier.classify("SELECT * FROM foo WHERE id = 1")
        assert result is None

    def test_select_with_join_returns_none(self, classifier: WriteClassifier):
        result = classifier.classify("SELECT a.x, b.y FROM a JOIN b ON a.id = b.a_id")
        assert result is None

    def test_select_with_subquery_returns_none(self, classifier: WriteClassifier):
        result = classifier.classify("SELECT * FROM (SELECT id FROM foo) sub")
        assert result is None

    def test_select_with_read_cte_returns_none(self, classifier: WriteClassifier):
        result = classifier.classify("WITH cte AS (SELECT * FROM foo) SELECT * FROM cte")
        assert result is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT nextval('orders_id_seq')",
            "SELECT setval('orders_id_seq', 42)",
            "SELECT pg_notify('events', 'payload')",
            "SELECT pg_advisory_lock(42)",
        ],
    )
    def test_side_effecting_select_is_high_risk(self, classifier: WriteClassifier, sql: str):
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "SELECT SIDE EFFECT"


# -- Empty / invalid SQL --


class TestEdgeCases:
    def test_empty_string_returns_none(self, classifier: WriteClassifier):
        result = classifier.classify("")
        assert result is None

    def test_whitespace_only_returns_none(self, classifier: WriteClassifier):
        result = classifier.classify("   \n\t  ")
        assert result is None

    def test_none_like_empty(self, classifier: WriteClassifier):
        # classify_multi handles None-ish
        result = classifier.classify("")
        assert result is None

    def test_parse_error_defaults_to_high(self, classifier: WriteClassifier):
        result = classifier.classify("THIS IS NOT VALID SQL AT ALL BLARGH")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "UNKNOWN"
        assert "Parse error" in result.reason


# -- INSERT --


class TestInsert:
    def test_insert_regular_table_is_low(self, classifier: WriteClassifier):
        result = classifier.classify("INSERT INTO orders (id, name) VALUES (1, 'foo')")
        assert result is not None
        assert result.risk_level == RiskLevel.LOW
        assert result.operation_type == "INSERT"
        assert "orders" in result.tables

    def test_insert_values_is_low(self, classifier: WriteClassifier):
        result = classifier.classify("INSERT INTO foo VALUES (1)")
        assert result is not None
        assert result.risk_level == RiskLevel.LOW

    def test_insert_select_is_low(self, classifier: WriteClassifier):
        result = classifier.classify("INSERT INTO foo SELECT * FROM bar")
        assert result is not None
        assert result.risk_level == RiskLevel.LOW
        assert result.operation_type == "INSERT"

    def test_insert_critical_table_is_high(self, classifier_with_critical: WriteClassifier):
        result = classifier_with_critical.classify("INSERT INTO audit_log (msg) VALUES ('x')")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "INSERT"
        assert "critical table" in result.reason.lower()

    def test_insert_schema_qualified_critical_table(
        self, classifier_with_critical: WriteClassifier
    ):
        result = classifier_with_critical.classify("INSERT INTO public.secrets (k) VALUES ('v')")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH


# -- UPDATE --


class TestUpdate:
    def test_update_with_where_is_medium(self, classifier: WriteClassifier):
        result = classifier.classify("UPDATE foo SET x = 1 WHERE id = 5")
        assert result is not None
        assert result.risk_level == RiskLevel.MEDIUM
        assert result.operation_type == "UPDATE"
        assert result.has_where is True
        assert "foo" in result.tables

    def test_update_without_where_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("UPDATE foo SET x = 1")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "UPDATE"
        assert result.has_where is False
        assert "without WHERE" in result.reason

    def test_update_critical_table_with_where_is_high(
        self, classifier_with_critical: WriteClassifier
    ):
        result = classifier_with_critical.classify("UPDATE users SET name = 'a' WHERE id = 1")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert "critical table" in result.reason.lower()


# -- DELETE --


class TestDelete:
    def test_delete_with_where_is_medium(self, classifier: WriteClassifier):
        result = classifier.classify("DELETE FROM foo WHERE id = 5")
        assert result is not None
        assert result.risk_level == RiskLevel.MEDIUM
        assert result.operation_type == "DELETE"
        assert result.has_where is True
        assert "foo" in result.tables

    def test_delete_without_where_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("DELETE FROM foo")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DELETE"
        assert result.has_where is False
        assert "without WHERE" in result.reason

    def test_delete_critical_table_with_where_is_high(
        self, classifier_with_critical: WriteClassifier
    ):
        result = classifier_with_critical.classify("DELETE FROM users WHERE id = 1")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH


# -- DROP --


class TestDrop:
    def test_drop_table_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("DROP TABLE foo")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DROP"
        assert "foo" in result.tables

    def test_drop_table_if_exists_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("DROP TABLE IF EXISTS foo")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH

    def test_drop_index_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("DROP INDEX idx_foo")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DROP"

    def test_drop_schema_qualified(self, classifier: WriteClassifier):
        result = classifier.classify("DROP TABLE public.foo")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert "public.foo" in result.tables


# -- TRUNCATE --


class TestTruncate:
    def test_truncate_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("TRUNCATE foo")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "TRUNCATE"
        assert "foo" in result.tables

    def test_truncate_multiple_tables(self, classifier: WriteClassifier):
        result = classifier.classify("TRUNCATE foo, bar")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert "foo" in result.tables
        assert "bar" in result.tables


# -- ALTER TABLE --


class TestAlterTable:
    def test_alter_table_add_column_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("ALTER TABLE foo ADD COLUMN bar integer")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "ALTER TABLE"
        assert "foo" in result.tables

    def test_alter_table_drop_column_is_high(self, classifier: WriteClassifier):
        result = classifier.classify("ALTER TABLE foo DROP COLUMN bar")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH


# -- EXECUTE / DO (BLOCKED) --


class TestBlocked:
    def test_execute_is_blocked(self, classifier: WriteClassifier):
        result = classifier.classify("EXECUTE stmt_name")
        assert result is not None
        assert result.blocked is True
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "EXECUTE"

    def test_do_block_is_blocked(self, classifier: WriteClassifier):
        result = classifier.classify("DO $$ BEGIN RAISE NOTICE 'hi'; END $$")
        assert result is not None
        assert result.blocked is True
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DO"

    def test_execute_with_params_is_blocked(self, classifier: WriteClassifier):
        result = classifier.classify("EXECUTE stmt_name(1, 'foo')")
        assert result is not None
        assert result.blocked is True


class TestSessionCommands:
    @pytest.mark.parametrize(
        "sql,operation",
        [
            ("SET ROLE admin", "SET"),
            ("SET search_path TO private", "SET"),
            ("RESET ROLE", "RESET"),
            ("SELECT 1; SET ROLE admin", "SET"),
        ],
    )
    def test_set_and_reset_are_high_risk(
        self, classifier: WriteClassifier, sql: str, operation: str
    ):
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == operation


# -- Multi-statement --


class TestMultiStatement:
    def test_select_then_insert_returns_insert(self, classifier: WriteClassifier):
        result = classifier.classify("SELECT 1; INSERT INTO foo VALUES (1)")
        assert result is not None
        assert result.risk_level == RiskLevel.LOW
        assert result.operation_type == "INSERT"

    def test_insert_then_drop_returns_drop(self, classifier: WriteClassifier):
        result = classifier.classify("INSERT INTO foo VALUES (1); DROP TABLE bar")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DROP"

    def test_mixed_read_write(self, classifier: WriteClassifier):
        sql = "SELECT * FROM a; UPDATE b SET x = 1 WHERE id = 2; SELECT * FROM c"
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.MEDIUM
        assert result.operation_type == "UPDATE"

    def test_classify_multi_returns_list(self, classifier: WriteClassifier):
        sql = "INSERT INTO a VALUES (1); DELETE FROM b WHERE id = 1"
        results = classifier.classify_multi(sql)
        assert len(results) == 2
        assert results[0].operation_type == "INSERT"
        assert results[1].operation_type == "DELETE"

    def test_classify_multi_excludes_reads(self, classifier: WriteClassifier):
        sql = "SELECT 1; INSERT INTO foo VALUES (1); SELECT 2"
        results = classifier.classify_multi(sql)
        assert len(results) == 1
        assert results[0].operation_type == "INSERT"

    def test_all_reads_returns_empty_list(self, classifier: WriteClassifier):
        sql = "SELECT 1; SELECT 2; SELECT 3"
        results = classifier.classify_multi(sql)
        assert results == []

    def test_highest_risk_wins_across_statements(self, classifier: WriteClassifier):
        sql = "INSERT INTO a VALUES (1); UPDATE b SET x = 1; DELETE FROM c"
        result = classifier.classify(sql)
        assert result is not None
        # DELETE without WHERE is HIGH, which beats MEDIUM (UPDATE without WHERE is also HIGH)
        assert result.risk_level == RiskLevel.HIGH

    def test_blocked_wins_over_high(self, classifier: WriteClassifier):
        sql = "DROP TABLE foo; DO $$ BEGIN NULL; END $$"
        result = classifier.classify(sql)
        assert result is not None
        assert result.blocked is True

    def test_multi_statement_write_after_read_is_detected(self, classifier: WriteClassifier):
        sql = "SELECT id FROM customers; DELETE FROM customers WHERE id = 1"
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.MEDIUM
        assert result.operation_type == "DELETE"


# -- CTE with writes --


class TestCTEWrites:
    def test_cte_with_insert(self, classifier: WriteClassifier):
        sql = "WITH ins AS (INSERT INTO foo VALUES (1) RETURNING *) SELECT * FROM ins"
        result = classifier.classify(sql)
        assert result is not None
        assert result.operation_type == "INSERT"
        assert result.risk_level == RiskLevel.LOW

    def test_cte_with_delete_no_where_is_high(self, classifier: WriteClassifier):
        sql = "WITH del AS (DELETE FROM foo RETURNING *) SELECT * FROM del"
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DELETE"

    def test_cte_with_delete_where_is_medium(self, classifier: WriteClassifier):
        sql = "WITH del AS (DELETE FROM foo WHERE id = 1 RETURNING *) SELECT * FROM del"
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.MEDIUM

    def test_cte_with_update(self, classifier: WriteClassifier):
        sql = "WITH upd AS (UPDATE foo SET x = 1 WHERE id = 1 RETURNING *) SELECT * FROM upd"
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.MEDIUM
        assert result.operation_type == "UPDATE"

    def test_cte_critical_table(self, classifier_with_critical: WriteClassifier):
        sql = "WITH ins AS (INSERT INTO users (name) VALUES ('x') RETURNING *) SELECT * FROM ins"
        result = classifier_with_critical.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH

    def test_nested_data_modifying_cte_is_detected(self, classifier: WriteClassifier):
        sql = (
            "SELECT * FROM (" "WITH del AS (DELETE FROM foo RETURNING *) " "SELECT * FROM del" ") s"
        )
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DELETE"

    def test_insert_select_data_modifying_cte_reports_highest_risk(
        self, classifier: WriteClassifier
    ):
        sql = (
            "INSERT INTO audit_log "
            "WITH del AS (DELETE FROM foo RETURNING id) "
            "SELECT id FROM del"
        )
        results = classifier.classify_multi(sql)
        assert [result.operation_type for result in results] == ["INSERT", "DELETE"]
        result = classifier.classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DELETE"


# -- Critical tables configuration --


class TestCriticalTables:
    def test_no_critical_tables_by_default(self, classifier: WriteClassifier):
        result = classifier.classify("INSERT INTO users VALUES (1)")
        assert result is not None
        assert result.risk_level == RiskLevel.LOW

    def test_critical_table_elevates_insert(self, classifier_with_critical: WriteClassifier):
        result = classifier_with_critical.classify("INSERT INTO users VALUES (1)")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH

    def test_critical_table_elevates_update_with_where(
        self, classifier_with_critical: WriteClassifier
    ):
        result = classifier_with_critical.classify("UPDATE users SET name = 'x' WHERE id = 1")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH

    def test_non_critical_table_unchanged(self, classifier_with_critical: WriteClassifier):
        result = classifier_with_critical.classify("INSERT INTO orders VALUES (1)")
        assert result is not None
        assert result.risk_level == RiskLevel.LOW

    def test_schema_qualified_critical_match(self, classifier_with_critical: WriteClassifier):
        # "public.secrets" is in critical_tables
        result = classifier_with_critical.classify("INSERT INTO public.secrets VALUES (1)")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH

    def test_unqualified_name_matches_critical(self):
        # If critical_tables has "users", then "public.users" should also match
        c = WriteClassifier(critical_tables={"users"})
        result = c.classify("INSERT INTO public.users VALUES (1)")
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH


# -- Table extraction --


class TestTableExtraction:
    def test_insert_extracts_table(self, classifier: WriteClassifier):
        result = classifier.classify("INSERT INTO my_table VALUES (1)")
        assert result is not None
        assert result.tables == ["my_table"]

    def test_update_extracts_table(self, classifier: WriteClassifier):
        result = classifier.classify("UPDATE my_table SET x = 1 WHERE id = 1")
        assert result is not None
        assert result.tables == ["my_table"]

    def test_delete_extracts_table(self, classifier: WriteClassifier):
        result = classifier.classify("DELETE FROM my_table WHERE id = 1")
        assert result is not None
        assert result.tables == ["my_table"]

    def test_truncate_extracts_tables(self, classifier: WriteClassifier):
        result = classifier.classify("TRUNCATE table_a, table_b")
        assert result is not None
        assert "table_a" in result.tables
        assert "table_b" in result.tables

    def test_drop_extracts_table(self, classifier: WriteClassifier):
        result = classifier.classify("DROP TABLE my_table")
        assert result is not None
        assert "my_table" in result.tables

    def test_alter_extracts_table(self, classifier: WriteClassifier):
        result = classifier.classify("ALTER TABLE my_table ADD COLUMN x int")
        assert result is not None
        assert result.tables == ["my_table"]

    def test_schema_qualified_table(self, classifier: WriteClassifier):
        result = classifier.classify("INSERT INTO myschema.my_table VALUES (1)")
        assert result is not None
        assert result.tables == ["myschema.my_table"]


class TestAdversarialSqlSafety:
    """P0 public-beta SQL safety regressions."""

    @pytest.mark.parametrize(
        "sql,operation",
        [
            ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v", "MERGE"),
            ("CREATE TABLE public.new_table(id int)", "CREATE TABLE"),
            ("CREATE INDEX idx_users_id ON users(id)", "CREATE INDEX"),
            ("CREATE FUNCTION f() RETURNS int LANGUAGE SQL AS 'SELECT 1'", "CREATE FUNCTION"),
            ("CREATE ROLE analyst", "CREATE ROLE"),
            ("GRANT SELECT ON users TO analyst", "GRANT"),
            ("REVOKE SELECT ON users FROM analyst", "REVOKE"),
            ("COPY users FROM STDIN", "COPY"),
            ("SELECT * INTO archived_users FROM users", "SELECT INTO"),
        ],
    )
    def test_unknown_or_high_impact_mutating_statements_are_high_risk(
        self, classifier: WriteClassifier, sql: str, operation: str
    ) -> None:
        result = classifier.classify(sql)

        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == operation

    def test_multi_statement_select_then_drop_is_detected(
        self, classifier: WriteClassifier
    ) -> None:
        result = classifier.classify("SELECT 1; DROP TABLE users")

        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "DROP"


class TestFunctionsGovernanceCannotSee:
    """Statements that parse as reads but change session state, or read data
    through a function no table-level control can see.

    Found while planning the source catalog: `SELECT set_config('search_path',
    ...)` was accepted though a plain `SET` was refused, and
    `table_to_xml('public.orders', ...)` returned a table that a deny policy
    refused to `SELECT * FROM orders`.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT set_config('search_path', 'sales', false)",
            "select SET_CONFIG('statement_timeout', '0', false)",
            "SELECT pg_terminate_backend(123)",
            "SELECT lo_import('/etc/passwd')",
        ],
    )
    def test_session_and_server_side_effects_are_high_risk(self, sql: str) -> None:
        result = WriteClassifier().classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "SELECT SIDE EFFECT"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT table_to_xml('public.orders', true, false, '')",
            "SELECT table_to_xmlschema('public.orders', true, false, '')",
            "SELECT query_to_xml('select * from orders', true, false, '')",
            "SELECT schema_to_xml('sales', true, false, '')",
            "SELECT database_to_xml(true, false, '')",
            "SELECT * FROM dblink('dbname=x', 'select 1') AS t(a int)",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT lo_get(12345)",
        ],
    )
    def test_opaque_data_reads_are_high_risk(self, sql: str) -> None:
        result = WriteClassifier().classify(sql)
        assert result is not None
        assert result.risk_level == RiskLevel.HIGH
        assert result.operation_type == "SELECT OPAQUE READ"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT name FROM customers",
            "SELECT xmlelement(name x, 'y')",
            "SELECT count(*) FROM orders",
            "SELECT table_name FROM information_schema.tables",
        ],
    )
    def test_ordinary_reads_are_not_caught(self, sql: str) -> None:
        """Prefix matching must not sweep up unrelated names."""
        assert WriteClassifier().classify(sql) is None
