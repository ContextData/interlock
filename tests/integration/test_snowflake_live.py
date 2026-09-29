"""Optional live Snowflake connector checks.

These tests are skipped unless explicit Snowflake credentials are provided in
the environment. They are intended for connector certification, not default CI.
"""

from __future__ import annotations

import os

import pytest

from interlock.connections.connectors import get_adapter


def _snowflake_config() -> dict[str, str | bool]:
    return {
        "account": os.environ.get("SNOWFLAKE_ACCOUNT", ""),
        "user": os.environ.get("SNOWFLAKE_USER", ""),
        "password": os.environ.get("SNOWFLAKE_PASSWORD", ""),
        "password_ref": os.environ.get("SNOWFLAKE_PASSWORD_REF", ""),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", ""),
        "database": os.environ.get("SNOWFLAKE_DATABASE", ""),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", ""),
        "role": os.environ.get("SNOWFLAKE_ROLE", ""),
        "authenticator": os.environ.get("SNOWFLAKE_AUTHENTICATOR", ""),
        "collect_query_history": False,
    }


def _has_live_snowflake_config() -> bool:
    cfg = _snowflake_config()
    return bool(cfg["account"] and cfg["user"] and (cfg["password"] or cfg["password_ref"]))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(
        not _has_live_snowflake_config(),
        reason="Set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, and SNOWFLAKE_PASSWORD or SNOWFLAKE_PASSWORD_REF to run live Snowflake tests.",
    ),
]


@pytest.mark.asyncio
async def test_live_snowflake_probe_and_select_one() -> None:
    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    cfg = _snowflake_config()

    probe = await adapter.probe(cfg)
    assert probe.healthy is True, probe.error

    result = await adapter.execute_read(
        {
            "source_id": "live_snowflake",
            "identity_id": 1,
            "query": "SELECT 1 AS INTERLOCK_SMOKE",
            "connection_config": cfg,
        }
    )
    assert result["rows"] == [{"INTERLOCK_SMOKE": 1}]
    assert result["cost_metadata"]["query_id"]


@pytest.mark.asyncio
async def test_live_snowflake_introspect_when_database_schema_configured() -> None:
    cfg = _snowflake_config()
    if not cfg.get("database") or not cfg.get("schema"):
        pytest.skip("Set SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA for live introspection")

    adapter = get_adapter("snowflake", {"connector_key": "snowflake"})
    result = await adapter.introspect(cfg)

    assert result["connector"] == "snowflake"
    assert result["dialect"] == "snowflake"
    assert "tables" in result
