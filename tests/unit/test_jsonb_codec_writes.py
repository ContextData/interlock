"""JSONB parameters reach the control-plane pool as Python objects.

The pools every service uses register a jsonb codec whose encoder is
`json.dumps` (`src/interlock/db/pool.py`). A caller that serialises the value
first hands the codec a `str`, which it encodes again, so PostgreSQL stores a
JSON *string* scalar instead of an object.

Found on a live test deployment: every console-created source, identity, role, role
permission and grant, every cache dependency and every indexed discovery asset
stored its JSON that way, and the Data Sources pages returned 500 because the
connector lookup received a `str`. Unit tests never saw it because their fake
pools have no codec.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tests.unit.test_source_onboarding_routes import FakePool, _make_app

SRC = Path(__file__).resolve().parents[2] / "src" / "interlock"
_JSONB_PARAM = re.compile(r"\$(\d+)::jsonb?\b")


def _is_json_dumps(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "dumps":
        return False
    owner = node.func.value
    if isinstance(owner, ast.Name) and owner.id == "json":
        return True
    return isinstance(owner, ast.Call) and getattr(owner.func, "id", None) == "__import__"


def _sql_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def _sql_text(node: ast.AST, consts: dict[str, str]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, "")
    return ""


def jsonb_double_encodings(source: str, label: str) -> list[str]:
    """Calls whose SQL binds a jsonb parameter and which pass json.dumps(...)."""
    tree = ast.parse(source)
    consts = _sql_constants(tree)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tuples = {
            target.id: node.value
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            if not any(_JSONB_PARAM.search(_sql_text(arg, consts)) for arg in call.args):
                continue
            args: list[ast.AST] = list(call.args)
            for arg in call.args:
                if isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                    packed = tuples.get(arg.value.id)
                    if packed is not None:
                        args.extend(packed.elts)
            found.extend(f"{label}:{arg.lineno}" for arg in args if _is_json_dumps(arg))
    return found


def test_the_guard_detects_both_shapes_of_the_defect() -> None:
    """A guard that finds nothing on the known-bad shapes proves nothing."""
    snippet = """
import json
_SQL = "INSERT INTO t (a, b) VALUES ($1, $2::jsonb)"
async def direct(pool, x):
    await pool.execute("UPDATE t SET b = $1::jsonb WHERE a = $2", json.dumps(x), 1)
async def packed(conn, x):
    values = (1, json.dumps(x))
    await conn.execute(_SQL, *values)
async def fine(pool, x):
    await pool.execute("SELECT pg_notify('c', $1)", json.dumps(x))
"""
    assert len(jsonb_double_encodings(snippet, "snippet")) == 2


def test_no_jsonb_parameter_is_serialised_before_the_codec_sees_it() -> None:
    violations = [
        hit
        for path in sorted(SRC.rglob("*.py"))
        for hit in jsonb_double_encodings(path.read_text(), str(path.relative_to(SRC)))
    ]
    assert violations == []


class CodecMirroringPool(FakePool):
    """Applies the control-plane jsonb codec to bound parameters, as asyncpg does."""

    def __init__(self) -> None:
        super().__init__()
        self.stored: list[tuple[str, Any]] = []

    def _encode(self, query: Any, args: tuple[Any, ...]) -> None:
        if not isinstance(query, str):
            return
        table = re.search(r"(?:INSERT INTO|UPDATE)\s+([a-z_]+)", query)
        for match in _JSONB_PARAM.finditer(query):
            index = int(match.group(1)) - 1
            if index < len(args):
                # The codec's encoder, then what PostgreSQL hands back.
                self.stored.append(
                    (table.group(1) if table else "?", json.loads(json.dumps(args[index])))
                )

    async def execute(self, *args, **kwargs):
        self._encode(args[0] if args else None, args[1:])
        return await super().execute(*args, **kwargs)

    async def fetchval(self, query, *args):
        self._encode(query, args)
        return await super().fetchval(query, *args)

    async def fetchrow(self, *args, **kwargs):
        self._encode(args[0] if args else None, args[1:])
        return await super().fetchrow(*args, **kwargs)


@pytest.fixture
async def codec_client_and_pool():
    pool = CodecMirroringPool()
    app = _make_app(pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool


@pytest.mark.anyio
async def test_console_source_create_stores_json_objects(codec_client_and_pool) -> None:
    client, pool = codec_client_and_pool
    resp = await client.post(
        "/dashboard/data-sources/create",
        data={
            "source_id": "shop",
            "name": "Shop",
            "source_type": "postgresql",
            "connector_key": "postgresql",
            "cache_strategy": "deterministic_first",
            "host": "postgres",
            "port": "5432",
            "database": "shop",
            "sslmode": "disable",
            "create_default_roles": "on",
        },
    )
    assert resp.status_code == 200
    tables = {table for table, _ in pool.stored}
    assert {"data_sources", "source_roles", "source_role_permissions"} <= tables
    strings = [(table, value) for table, value in pool.stored if isinstance(value, str)]
    assert strings == []


@pytest.mark.anyio
async def test_console_identity_create_stores_json_objects(codec_client_and_pool) -> None:
    client, pool = codec_client_and_pool
    resp = await client.post(
        "/dashboard/access-control/identities/create",
        data={"name": "agent", "agent_type": "custom", "team": "t", "generate_key": "on"},
    )
    assert resp.status_code == 200
    assert "identities" in {table for table, _ in pool.stored}
    assert [value for _, value in pool.stored if isinstance(value, str)] == []
