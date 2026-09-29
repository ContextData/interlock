"""Write Classifier - classifies SQL write operations by risk level.

Parses SQL statements using the permissively licensed ``sqlglot`` parser and
assigns a risk level based on operation type and target resources.

Risk levels:
- HIGH/BLOCKED: dynamic execution, procedural blocks, DDL/DCL, COPY FROM,
  MERGE, SELECT INTO, destructive writes, writes to critical tables, and
  unrecognized mutating statements.
- MEDIUM: DELETE/UPDATE with a WHERE clause.
- LOW: INSERT into non-critical tables.
"""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from interlock.core.normalizer import DEFAULT_DIALECT
from interlock.models import RiskLevel, WriteClassification

logger = logging.getLogger(__name__)

_BLOCKED_PREFIXES = ("EXECUTE", "DO")
_HIGH_RISK_PREFIXES = (
    "ALTER",
    "ANALYZE",
    "CALL",
    "COMMENT",
    "COPY",
    "CREATE",
    "DROP",
    "GRANT",
    "IMPORT",
    "MERGE",
    "REFRESH",
    "REINDEX",
    "REVOKE",
    "RESET",
    "SET",
    "TRUNCATE",
    "VACUUM",
)

_SIDE_EFFECTING_SELECT_FUNCTIONS = (
    "nextval",
    "setval",
    "pg_notify",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "pg_try_advisory_lock",
    "pg_try_advisory_xact_lock",
    # set_config is SET by another name: on a persistent PostgreSQL-wire session
    # it changes how every later bare table name resolves. A plain SET was already
    # refused; this closes the spelling that parsed as a read.
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "lo_import",
    "lo_export",
)

# Functions that read table or server data without the table appearing in the
# statement, so no parser-based table control can see what they touch:
# `table_to_xml('public.orders', ...)` returned a table that a deny policy
# refused to `SELECT * FROM orders`. Matched as name prefixes because each is a
# family (`table_to_xml`, `table_to_xmlschema`, `table_to_xml_and_xmlschema`;
# `dblink`, `dblink_exec`, ...). They are classified high risk, which the
# PostgreSQL wire refuses and MCP routes to approval, rather than run unseen.
_OPAQUE_DATA_ACCESS_FUNCTION_PREFIXES = (
    "table_to_xml",
    "query_to_xml",
    "cursor_to_xml",
    "schema_to_xml",
    "database_to_xml",
    "dblink",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_get",
)


class WriteClassifier:
    """Classifies SQL write operations by risk level."""

    def __init__(
        self,
        critical_tables: set[str] | None = None,
        dialect: str = DEFAULT_DIALECT,
    ) -> None:
        self._critical_tables: set[str] = critical_tables or set()
        # The grammar used to read statements. Everything was parsed as
        # PostgreSQL, so a MySQL source could not use backtick-quoted
        # identifiers - its own standard quoting - without every statement
        # falling to the conservative UNKNOWN/HIGH default below and being
        # refused by source roles as an unclassifiable raw write.
        #
        # The conservative default itself is right and stays: SQL this
        # classifier genuinely cannot read must be treated as dangerous. The
        # bug was reaching it for SQL that is perfectly valid in the source's
        # own dialect.
        self._dialect = dialect or DEFAULT_DIALECT

    def classify(self, sql: str, dialect: str | None = None) -> WriteClassification | None:
        """Classify a SQL statement's highest write risk.

        `dialect` overrides the instance default for one call, so a single
        shared classifier can serve sources that speak different SQL.
        """

        results = self.classify_multi(sql, dialect)
        if not results:
            return None
        return max(results, key=_risk_sort_key)

    def classify_multi(self, sql: str, dialect: str | None = None) -> list[WriteClassification]:
        """Classify every write statement in a possibly multi-statement string."""

        if not sql or not sql.strip():
            return []

        try:
            statements = sqlglot.parse(sql, read=dialect or self._dialect)
        except ParseError:
            logger.warning("Parse error for SQL, defaulting to HIGH risk: %s", sql[:100])
            return [
                WriteClassification(
                    risk_level=RiskLevel.HIGH,
                    operation_type="UNKNOWN",
                    reason="Parse error - conservative default",
                )
            ]

        results: list[WriteClassification] = []
        for statement in statements:
            if statement is None:
                continue
            classifications = self._classify_expression_tree(statement)
            if classifications:
                results.extend(classifications)
                continue
            fallback = self._classify_by_prefix(statement.sql(dialect="postgres"))
            if fallback is not None:
                results.append(fallback)
        return results

    def _classify_expression_tree(self, statement: exp.Expression) -> list[WriteClassification]:
        results: list[WriteClassification] = []
        seen: set[int] = set()
        for node in statement.walk():
            expr = node[0] if isinstance(node, tuple) else node
            if id(expr) in seen:
                continue
            seen.add(id(expr))
            classification = self._classify_expression(expr)
            if classification is not None:
                results.append(classification)
        return results

    def _classify_expression(self, node: exp.Expression) -> WriteClassification | None:
        if isinstance(node, exp.Command):
            command = _first_keyword(node.sql(dialect="postgres"))
            if command in _BLOCKED_PREFIXES:
                return WriteClassification(
                    risk_level=RiskLevel.HIGH,
                    operation_type=command,
                    reason=f"{command} statements are unclassifiable",
                    blocked=True,
                )
            if command == "CREATE":
                return _high_risk(_create_operation_from_sql(node.sql(dialect="postgres")), [])
            if command in _HIGH_RISK_PREFIXES:
                return _high_risk(command, self._extract_tables(node))
            return None

        if isinstance(node, exp.Drop):
            return _high_risk("DROP", self._extract_tables(node))

        if isinstance(node, exp.TruncateTable):
            return _high_risk("TRUNCATE", self._extract_tables(node))

        if isinstance(node, exp.Alter):
            kind = str(node.args.get("kind") or "").upper()
            operation = f"ALTER {kind}".strip()
            return _high_risk(operation, self._extract_tables(node))

        if isinstance(node, exp.Create):
            kind = str(node.args.get("kind") or "").upper()
            operation = f"CREATE {kind}".strip()
            return _high_risk(operation, self._extract_tables(node))

        if isinstance(node, exp.Grant):
            return _high_risk("GRANT", self._extract_tables(node))

        if isinstance(node, exp.Revoke):
            return _high_risk("REVOKE", self._extract_tables(node))

        if isinstance(node, exp.Copy):
            direction = str(node.args.get("kind") or "").upper()
            operation = "COPY"
            reason = "COPY statements are high risk"
            if direction == "FROM":
                reason = "COPY FROM can mutate source data"
            return WriteClassification(
                risk_level=RiskLevel.HIGH,
                operation_type=operation,
                tables=self._extract_tables(node),
                reason=reason,
            )

        if isinstance(node, exp.Merge):
            return _high_risk("MERGE", self._extract_tables(node))

        if isinstance(node, exp.Select) and node.args.get("into") is not None:
            return WriteClassification(
                risk_level=RiskLevel.HIGH,
                operation_type="SELECT INTO",
                tables=self._extract_tables(node),
                reason="SELECT INTO creates a table",
            )

        if isinstance(node, exp.Select):
            rendered = node.sql(dialect="postgres")
            function = _side_effecting_select_function(rendered)
            if function:
                return WriteClassification(
                    risk_level=RiskLevel.HIGH,
                    operation_type="SELECT SIDE EFFECT",
                    tables=self._extract_tables(node),
                    reason=f"SELECT calls side-effecting function {function}",
                )
            opaque = _opaque_data_access_function(rendered)
            if opaque:
                return WriteClassification(
                    risk_level=RiskLevel.HIGH,
                    operation_type="SELECT OPAQUE READ",
                    tables=self._extract_tables(node),
                    reason=(
                        f"SELECT calls {opaque}, which reads data table-level "
                        "governance cannot see"
                    ),
                )

        if isinstance(node, exp.Delete):
            tables = self._extract_tables(node)
            has_where = node.args.get("where") is not None
            return self._classify_update_or_delete("DELETE", tables, has_where)

        if isinstance(node, exp.Update):
            tables = self._extract_tables(node)
            has_where = node.args.get("where") is not None
            return self._classify_update_or_delete("UPDATE", tables, has_where)

        if isinstance(node, exp.Insert):
            tables = self._extract_tables(node)
            if self._has_critical_table(tables):
                return WriteClassification(
                    risk_level=RiskLevel.HIGH,
                    operation_type="INSERT",
                    tables=tables,
                    reason="Write to critical table",
                )
            return WriteClassification(
                risk_level=RiskLevel.LOW,
                operation_type="INSERT",
                tables=tables,
                reason="INSERT operation",
            )

        return None

    def _classify_update_or_delete(
        self,
        operation: str,
        tables: list[str],
        has_where: bool,
    ) -> WriteClassification:
        is_critical = self._has_critical_table(tables)
        if not has_where or is_critical:
            reason = (
                f"{operation} without WHERE clause" if not has_where else "Write to critical table"
            )
            return WriteClassification(
                risk_level=RiskLevel.HIGH,
                operation_type=operation,
                tables=tables,
                reason=reason,
                has_where=has_where,
            )
        return WriteClassification(
            risk_level=RiskLevel.MEDIUM,
            operation_type=operation,
            tables=tables,
            reason=f"{operation} with WHERE clause",
            has_where=has_where,
        )

    def _classify_by_prefix(self, sql: str) -> WriteClassification | None:
        command = _first_keyword(sql)
        if command == "CREATE":
            return _high_risk(_create_operation_from_sql(sql), [])
        if command in _BLOCKED_PREFIXES:
            return WriteClassification(
                risk_level=RiskLevel.HIGH,
                operation_type=command,
                reason=f"{command} statements are unclassifiable",
                blocked=True,
            )
        if command in _HIGH_RISK_PREFIXES:
            return _high_risk(command, [])
        return None

    def _extract_tables(self, node: exp.Expression) -> list[str]:
        tables: list[str] = []
        for table in node.find_all(exp.Table):
            name = _table_name(table)
            if name and name not in tables:
                tables.append(name)
        return tables

    def _has_critical_table(self, tables: list[str]) -> bool:
        for table in tables:
            if table in self._critical_tables:
                return True
            if "." in table:
                _, name = table.rsplit(".", 1)
                if name in self._critical_tables:
                    return True
        return False


def _high_risk(operation: str, tables: list[str]) -> WriteClassification:
    return WriteClassification(
        risk_level=RiskLevel.HIGH,
        operation_type=operation,
        tables=tables,
        reason=f"{operation} operations are always high risk",
    )


def _table_name(table: exp.Table) -> str:
    name = table.name or ""
    db = table.db
    if db:
        name = f"{db}.{name}"
    return name


def _first_keyword(sql: str) -> str:
    stripped = re.sub(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*", "", sql, flags=re.DOTALL)
    match = re.match(r"([A-Za-z_]+)", stripped)
    return match.group(1).upper() if match else "UNKNOWN"


def _create_operation_from_sql(sql: str) -> str:
    stripped = re.sub(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*", "", sql, flags=re.DOTALL)
    match = re.match(r"(?i)CREATE\s+(?:OR\s+REPLACE\s+)?([A-Za-z_]+)", stripped)
    if not match:
        return "CREATE"
    return f"CREATE {match.group(1).upper()}"


def _side_effecting_select_function(sql: str) -> str | None:
    lowered = sql.lower()
    for name in _SIDE_EFFECTING_SELECT_FUNCTIONS:
        if re.search(rf"\b{re.escape(name)}\s*\(", lowered):
            return name
    return None


def _opaque_data_access_function(sql: str) -> str | None:
    lowered = sql.lower()
    for prefix in _OPAQUE_DATA_ACCESS_FUNCTION_PREFIXES:
        match = re.search(rf"\b({re.escape(prefix)}\w*)\s*\(", lowered)
        if match:
            return match.group(1)
    return None


def _risk_sort_key(c: WriteClassification) -> tuple[int, int]:
    """Sort key for comparing classifications - higher is riskier."""

    blocked_score = 1 if c.blocked else 0
    risk_order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
    return (blocked_score, risk_order.get(c.risk_level, 0))
