"""Intent Capture Module.

Extracts natural-language intent strings from SQL queries, MCP tool calls,
and HTTP requests. These intent strings serve as input to semantic cache and
discovery enrichment.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

_OP_MAP: dict[str, str] = {
    "=": "equals",
    "eq": "equals",
    "<>": "not equals",
    "!=": "not equals",
    ">": "greater than",
    "gt": "greater than",
    "<": "less than",
    "lt": "less than",
    ">=": "at least",
    "gte": "at least",
    "<=": "at most",
    "lte": "at most",
    "like": "like",
    "ilike": "ilike",
}


def capture_sql_intent(sql: str) -> str:
    """Extract intent from a SQL query."""

    if not sql or not sql.strip():
        return ""
    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except ParseError:
        return _clean_raw(sql)
    if statement is None:
        return _clean_raw(sql)

    parts: list[str] = []
    if isinstance(statement, exp.Select):
        _extract_select(statement, parts)
    elif isinstance(statement, exp.Insert):
        _extract_insert(statement, parts)
    elif isinstance(statement, exp.Update):
        _extract_update(statement, parts)
    elif isinstance(statement, exp.Delete):
        _extract_delete(statement, parts)
    else:
        return _clean_raw(sql)
    return " ".join(part for part in parts if part).lower()


def capture_mcp_intent(tool_name: str, arguments: dict) -> str:
    """Extract intent from an MCP tool call."""

    parts: list[str] = [tool_name]
    for key, value in sorted(arguments.items()):
        parts.append(str(key))
        parts.append(str(value))
    return " ".join(parts).lower().strip()


def capture_http_intent(
    method: str,
    path: str,
    params: dict | None = None,
) -> str:
    """Extract intent from an HTTP request."""

    parts: list[str] = [method.upper()]
    segments = [seg for seg in path.strip("/").split("/") if seg]
    parts.extend(segments)
    if params:
        for key in sorted(params.keys()):
            parts.append(str(key))
            parts.append(str(params[key]))
    return " ".join(parts)


def _extract_select(stmt: exp.Select, parts: list[str]) -> None:
    parts.append("select")
    for target in stmt.expressions:
        _extract_expression_terms(target, parts)
    parts.append("from")
    for table in stmt.find_all(exp.Table):
        parts.append(_table_name(table))
    for join in stmt.args.get("joins") or []:
        if isinstance(join, exp.Join):
            parts.append("join")
            if isinstance(join.this, exp.Table):
                parts.append(_table_name(join.this))
            on_expr = join.args.get("on")
            if on_expr is not None:
                parts.append("on")
                _extract_condition(on_expr, parts)
    where = stmt.args.get("where")
    if where is not None:
        parts.append("where")
        _extract_condition(where.this if isinstance(where, exp.Where) else where, parts)
    if stmt.args.get("group"):
        parts.append("group by")
        _extract_expression_terms(stmt.args["group"], parts)
    if stmt.args.get("order"):
        parts.append("order by")
        _extract_expression_terms(stmt.args["order"], parts)


def _extract_insert(stmt: exp.Insert, parts: list[str]) -> None:
    parts.append("insert into")
    target = stmt.this
    if isinstance(target, exp.Table):
        parts.append(_table_name(target))
    elif isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
        parts.append(_table_name(target.this))
        for column in target.expressions:
            parts.append(column.name)


def _extract_update(stmt: exp.Update, parts: list[str]) -> None:
    parts.append("update")
    if isinstance(stmt.this, exp.Table):
        parts.append(_table_name(stmt.this))
    if stmt.expressions:
        parts.append("set")
        for assignment in stmt.expressions:
            _extract_expression_terms(
                assignment.left if hasattr(assignment, "left") else assignment, parts
            )
    where = stmt.args.get("where")
    if where is not None:
        parts.append("where")
        _extract_condition(where.this if isinstance(where, exp.Where) else where, parts)


def _extract_delete(stmt: exp.Delete, parts: list[str]) -> None:
    parts.append("delete from")
    if isinstance(stmt.this, exp.Table):
        parts.append(_table_name(stmt.this))
    where = stmt.args.get("where")
    if where is not None:
        parts.append("where")
        _extract_condition(where.this if isinstance(where, exp.Where) else where, parts)


def _extract_condition(node: exp.Expression, parts: list[str]) -> None:
    if isinstance(node, exp.And):
        _extract_condition(node.left, parts)
        parts.append("and")
        _extract_condition(node.right, parts)
        return
    if isinstance(node, exp.Or):
        _extract_condition(node.left, parts)
        parts.append("or")
        _extract_condition(node.right, parts)
        return
    if isinstance(node, exp.Predicate):
        _extract_expression_terms(node.left if hasattr(node, "left") else node, parts)
        op = node.token_type.value if getattr(node, "token_type", None) else node.key
        parts.append(_OP_MAP.get(str(op).lower(), str(op).lower()))
        _extract_expression_terms(node.right if hasattr(node, "right") else node.expression, parts)
        return
    _extract_expression_terms(node, parts)


def _extract_expression_terms(node: Any, parts: list[str]) -> None:
    if node is None:
        return
    if isinstance(node, exp.Star):
        return
    if isinstance(node, exp.Column):
        parts.append(node.name)
        return
    if isinstance(node, exp.Table):
        parts.append(_table_name(node))
        return
    if isinstance(node, exp.Func):
        parts.append(type(node).__name__.lower())
    if isinstance(node, exp.Literal):
        parts.append(str(node.to_py()).lower())
        return
    if isinstance(node, exp.Boolean):
        parts.append("true" if node.this else "false")
        return
    if isinstance(node, exp.Expression):
        for child in node.args.values():
            if isinstance(child, list):
                for item in child:
                    _extract_expression_terms(item, parts)
            else:
                _extract_expression_terms(child, parts)


def _table_name(table: exp.Table) -> str:
    name = table.name or ""
    db = table.db
    if db:
        name = f"{db}.{name}"
    return name


def _clean_raw(sql: str) -> str:
    """Fallback: return lowercased SQL with collapsed whitespace."""

    return re.sub(r"\s+", " ", sql.strip().lower())
