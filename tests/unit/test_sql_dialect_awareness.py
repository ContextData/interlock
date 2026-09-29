"""A source's SQL is read in the source's own grammar.

Everything was parsed as PostgreSQL. MySQL's standard identifier quoting -
backticks - is not PostgreSQL syntax, so `SELECT label FROM \\`tbl\\`` failed to
parse. The failure was handled conservatively, which is the right default for
SQL nobody can read: the statement was classified as an unknown write at HIGH
risk with no tables extracted. Source roles then refused it, because no role
grants `db.execute_raw`.

The consequence was that a MySQL source could not be queried using MySQL's own
quoting. Backticks are idiomatic, are emitted by ORMs and by mysqldump, and are
*required* for identifiers that are reserved words or contain special
characters - so those were unreachable through the proxy entirely. A plain
SELECT was refused as an unclassifiable write, and its audit row recorded
operation=WRITE.

The conservative default is not what changed, and these tests pin that it did
not: SQL that genuinely cannot be parsed is still treated as dangerous. What
changed is that valid SQL is no longer sent to it.
"""

from __future__ import annotations

import pytest

from interlock.core.normalizer import DEFAULT_DIALECT, dialect_for, normalize_sql
from interlock.core.write_classifier import WriteClassifier

BACKTICKED = "SELECT label FROM `tbl`"


class TestDialectMapping:
    @pytest.mark.parametrize(
        ("source_type", "expected"),
        [
            ("mysql", "mysql"),
            ("mariadb", "mysql"),
            ("postgresql", "postgres"),
            ("postgres", "postgres"),
            ("snowflake", "snowflake"),
            ("mssql", "tsql"),
            ("MySQL", "mysql"),
            ("  mysql  ", "mysql"),
        ],
    )
    def test_known_source_types_map_to_their_grammar(self, source_type: str, expected: str) -> None:
        assert dialect_for(source_type) == expected

    @pytest.mark.parametrize("value", ["", None, "notaconnector", "s3"])
    def test_unknown_source_types_fall_back_to_the_previous_behaviour(self, value) -> None:
        """An unmapped connector must be no worse off than before dialects existed."""
        assert dialect_for(value) == DEFAULT_DIALECT


class TestNormalizer:
    def test_backticks_parse_under_the_mysql_grammar(self) -> None:
        result = normalize_sql(BACKTICKED, "src", dialect="mysql").model_dump()

        assert result["operation"] == "SELECT"
        assert result["tables"] == ["tbl"]

    def test_backticks_still_fail_under_postgres_which_is_correct(self) -> None:
        """Backticks are genuinely not PostgreSQL. The bug was using it for MySQL."""
        result = normalize_sql(BACKTICKED, "src", dialect="postgres").model_dump()

        assert result["operation"] is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT `label` FROM tbl",
            "SELECT a FROM `db`.`tbl`",
            "INSERT INTO `tbl` (`a`) VALUES (1)",
            "UPDATE `tbl` SET `a` = 1 WHERE `b` = 2",
        ],
    )
    def test_every_backtick_position_parses(self, sql: str) -> None:
        """Table, column, qualified name, and across statement types."""
        assert normalize_sql(sql, "src", dialect="mysql").model_dump()["operation"] is not None

    def test_the_default_dialect_is_unchanged_for_existing_callers(self) -> None:
        """PG-wire callers must behave exactly as before."""
        plain = "SELECT a FROM t"
        assert (
            normalize_sql(plain, "src").model_dump()["normalized_sql"]
            == normalize_sql(plain, "src", dialect="postgres").model_dump()["normalized_sql"]
        )


class TestWriteClassifier:
    def test_a_backticked_select_is_not_a_write_under_mysql(self) -> None:
        """The assertion that matters: it used to classify as an UNKNOWN write."""
        assert WriteClassifier(dialect="mysql").classify(BACKTICKED) is None

    def test_a_backticked_write_is_still_classified_as_a_write(self) -> None:
        """Parsing more must not stop anything being recognised as dangerous."""
        classification = WriteClassifier(dialect="mysql").classify(
            "DELETE FROM `tbl` WHERE `id` = 1"
        )

        assert classification is not None
        assert classification.operation_type == "DELETE"

    def test_unparseable_sql_is_still_conservatively_high_risk(self) -> None:
        """The safety property, pinned.

        The fix stops valid SQL reaching the conservative default. It must not
        weaken the default itself - SQL nobody can read stays dangerous.
        """
        classification = WriteClassifier(dialect="mysql").classify("this is not sql at all ((")

        assert classification is not None
        assert classification.operation_type == "UNKNOWN"
        assert classification.risk_level.value == "high"

    def test_a_per_call_dialect_overrides_the_instance_default(self) -> None:
        """One shared classifier serves sources that speak different SQL.

        The pipeline holds a single classifier for every source, so the
        dialect has to travel with the request rather than with the instance.
        """
        shared = WriteClassifier()

        assert shared.classify(BACKTICKED) is not None  # conservative, wrong grammar
        assert shared.classify(BACKTICKED, "mysql") is None  # right grammar

    def test_an_unparseable_statement_names_no_tables(self) -> None:
        """A conservative classification must not invent scope it could not read."""
        classification = WriteClassifier().classify("not sql ((")

        assert classification is not None
        assert classification.tables == []
