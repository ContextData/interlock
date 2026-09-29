"""Tests for intent capture module."""

from __future__ import annotations

from interlock.core.intent import (
    capture_http_intent,
    capture_mcp_intent,
    capture_sql_intent,
)

# ---------------------------------------------------------------------------
# SQL Intent: simple SELECT
# ---------------------------------------------------------------------------


class TestSqlSimpleSelect:
    def test_extracts_table_and_columns(self):
        result = capture_sql_intent("SELECT name, email FROM users")
        assert "select" in result
        assert "name" in result
        assert "email" in result
        assert "users" in result
        assert "from" in result

    def test_select_star(self):
        result = capture_sql_intent("SELECT * FROM orders")
        assert "select" in result
        assert "orders" in result
        assert "from" in result


# ---------------------------------------------------------------------------
# SQL Intent: SELECT with WHERE
# ---------------------------------------------------------------------------


class TestSqlSelectWhere:
    def test_includes_condition_info(self):
        result = capture_sql_intent("SELECT name FROM users WHERE active = true")
        assert "where" in result
        assert "active" in result
        assert "equals" in result
        assert "true" in result

    def test_where_with_and(self):
        result = capture_sql_intent("SELECT id FROM users WHERE active = true AND age > 25")
        assert "and" in result
        assert "active" in result
        assert "age" in result
        assert "greater than" in result


# ---------------------------------------------------------------------------
# SQL Intent: JOIN query
# ---------------------------------------------------------------------------


class TestSqlJoin:
    def test_includes_both_tables(self):
        result = capture_sql_intent(
            "SELECT u.name, o.total FROM users u " "JOIN orders o ON u.id = o.user_id"
        )
        assert "users" in result
        assert "orders" in result
        assert "join" in result

    def test_join_condition(self):
        result = capture_sql_intent("SELECT * FROM users u JOIN orders o ON u.id = o.user_id")
        assert "on" in result
        assert "id" in result
        assert "user_id" in result


# ---------------------------------------------------------------------------
# SQL Intent: aggregation (COUNT, SUM)
# ---------------------------------------------------------------------------


class TestSqlAggregation:
    def test_count(self):
        result = capture_sql_intent("SELECT COUNT(id) FROM users")
        assert "count" in result
        assert "id" in result

    def test_sum(self):
        result = capture_sql_intent("SELECT SUM(amount) FROM orders")
        assert "sum" in result
        assert "amount" in result

    def test_avg(self):
        result = capture_sql_intent("SELECT AVG(price) FROM products")
        assert "avg" in result
        assert "price" in result


# ---------------------------------------------------------------------------
# SQL Intent: GROUP BY / ORDER BY
# ---------------------------------------------------------------------------


class TestSqlGroupOrderBy:
    def test_group_by(self):
        result = capture_sql_intent(
            "SELECT department, COUNT(id) FROM employees GROUP BY department"
        )
        assert "group by" in result
        assert "department" in result

    def test_order_by(self):
        result = capture_sql_intent("SELECT name FROM users ORDER BY created_at")
        assert "order by" in result
        assert "created_at" in result

    def test_group_and_order(self):
        result = capture_sql_intent(
            "SELECT department, COUNT(id) FROM employees " "GROUP BY department ORDER BY department"
        )
        assert "group by" in result
        assert "order by" in result


# ---------------------------------------------------------------------------
# SQL Intent: parse error fallback
# ---------------------------------------------------------------------------


class TestSqlParseError:
    def test_returns_cleaned_raw_sql(self):
        bad_sql = "NOT   VALID   SQL   AT   ALL"
        result = capture_sql_intent(bad_sql)
        assert result == "not valid sql at all"

    def test_empty_string(self):
        assert capture_sql_intent("") == ""

    def test_whitespace_only(self):
        assert capture_sql_intent("   ") == ""


# ---------------------------------------------------------------------------
# SQL Intent: INSERT / UPDATE / DELETE
# ---------------------------------------------------------------------------


class TestSqlDml:
    def test_insert(self):
        result = capture_sql_intent("INSERT INTO users (name, email) VALUES ('alice', 'a@b.com')")
        assert "insert into" in result
        assert "users" in result
        assert "name" in result
        assert "email" in result

    def test_update(self):
        result = capture_sql_intent("UPDATE users SET active = false WHERE id = 5")
        assert "update" in result
        assert "users" in result
        assert "active" in result
        assert "where" in result

    def test_delete(self):
        result = capture_sql_intent("DELETE FROM users WHERE id = 5")
        assert "delete from" in result
        assert "users" in result
        assert "where" in result


# ---------------------------------------------------------------------------
# MCP Intent
# ---------------------------------------------------------------------------


class TestMcpIntent:
    def test_combines_tool_name_and_args(self):
        result = capture_mcp_intent(
            "query_users",
            {"role": "admin", "limit": 10},
        )
        assert "query_users" in result
        assert "role" in result
        assert "admin" in result
        assert "limit" in result
        assert "10" in result

    def test_empty_args_returns_tool_name(self):
        result = capture_mcp_intent("list_databases", {})
        assert result == "list_databases"

    def test_args_sorted_by_key(self):
        result = capture_mcp_intent(
            "search",
            {"z_param": "last", "a_param": "first"},
        )
        # a_param should appear before z_param
        a_pos = result.index("a_param")
        z_pos = result.index("z_param")
        assert a_pos < z_pos


# ---------------------------------------------------------------------------
# HTTP Intent
# ---------------------------------------------------------------------------


class TestHttpIntent:
    def test_get_with_params(self):
        result = capture_http_intent(
            "GET",
            "/api/users",
            {"active": "true", "sort": "name"},
        )
        assert "GET" in result
        assert "api" in result
        assert "users" in result
        assert "active" in result
        assert "true" in result
        assert "sort" in result
        assert "name" in result

    def test_post_no_params(self):
        result = capture_http_intent("POST", "/api/users")
        assert "POST" in result
        assert "api" in result
        assert "users" in result

    def test_params_sorted_by_key(self):
        result = capture_http_intent(
            "GET",
            "/items",
            {"z_key": "1", "a_key": "2"},
        )
        a_pos = result.index("a_key")
        z_pos = result.index("z_key")
        assert a_pos < z_pos

    def test_strips_leading_trailing_slashes(self):
        result = capture_http_intent("GET", "/users/")
        assert result == "GET users"
