"""Source-role editor routes: a role is only ever read or written under its own source.

Statement rows are keyed by role id alone, so every route that takes a role id
must first prove the role belongs to the source in the URL.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig

_ROLES = {
    # role id -> (source_id, role_key)
    7: ("other", "reader"),
    8: ("shop", "analyst"),
}


class FakeConn:
    def __init__(self, parent: FakePool) -> None:
        self._parent = parent

    async def execute(self, query: str, *args: Any) -> str:
        self._parent.writes.append((query, args))
        return "OK"

    def transaction(self) -> FakeConn:
        self._parent.transactions += 1
        return self

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakePool:
    def __init__(self) -> None:
        self.writes: list[tuple[str, tuple[Any, ...]]] = []
        self.transactions = 0
        # role id -> identities holding an active grant
        self.holders: dict[int, list[dict[str, Any]]] = {}
        # role id -> every grant row, revoked ones included
        self.grants: dict[int, list[dict[str, Any]]] = {}
        self.policies_naming: list[dict[str, Any]] = []
        self.source: dict[str, Any] = {
            "source_type": "postgresql",
            "metadata": {"connector_key": "postgresql"},
        }
        self.stored: list[dict[str, Any]] = [
            {
                "effect": "allow",
                "action": "db.table.select",
                "resource_type": "db.table",
                "resource_pattern": "public.*",
                "constraints": {},
            }
        ]

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "JOIN identities i ON i.id = g.identity_id" in query:
            return self.holders.get(int(args[0]), [])
        if "FROM identity_source_role_grants" in query:
            return self.grants.get(int(args[0]), [])
        if "FROM policy_rules" in query:
            return [p for p in self.policies_naming if set(p["roles"]) & set(args[0])]
        if "FROM source_role_permissions" in query:
            return self.stored
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM data_sources" in query:
            return {"source_id": args[0], "name": args[0], **self.source}
        if "FROM source_roles" in query:
            role_id, source_id = args[0], args[1]
            owner = _ROLES.get(int(role_id))
            if owner is None or owner[0] != source_id:
                return None
            return {
                "id": role_id,
                "source_id": source_id,
                "role_key": owner[1],
                "name": owner[1],
                "description": None,
                "enabled": True,
                "review_required": False,
                "metadata": {},
            }
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.writes.append((query, args))
        return "OK"

    def acquire(self) -> FakeConn:
        return FakeConn(self)


def _make_app(pool: FakePool) -> Any:
    redis = MagicMock()
    redis.aclose = AsyncMock()
    with (
        patch("interlock.admin.app.create_pg_pool", new_callable=AsyncMock) as cp,
        patch("interlock.admin.app.close_pg_pool", new_callable=AsyncMock),
        patch("interlock.admin.app.create_redis_client", new_callable=AsyncMock) as cr,
        patch("interlock.admin.app.close_redis_client", new_callable=AsyncMock),
    ):
        cp.return_value = pool
        cr.return_value = redis
        app = create_app(config=InterLockConfig())
    app.state.pg_pool = pool
    app.state.redis = redis
    app.state.auth_disabled = True
    return app


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
async def client(pool: FakePool) -> Any:
    transport = ASGITransport(app=_make_app(pool))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_ROLE_FORM = {
    "role_key": "reader",
    "name": "Reader",
    "enabled": "on",
    "permission_effect": "allow",
    "permission_action": "db.table.select",
    "permission_resource_type": "db.table",
    "permission_resource_pattern": "*.*",
    "permission_constraints": "{}",
}


def _statement_writes(pool: FakePool) -> list[str]:
    return [q for q, _ in pool.writes if "source_role" in q]


@pytest.mark.anyio
async def test_update_refuses_a_role_from_another_source(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post("/dashboard/data-sources/shop/roles/7", data=_ROLE_FORM)

    assert resp.status_code == 404
    assert "source role #7 on shop" in resp.text
    assert _statement_writes(pool) == []


@pytest.mark.anyio
async def test_update_refuses_a_missing_role(client: AsyncClient, pool: FakePool) -> None:
    resp = await client.post("/dashboard/data-sources/shop/roles/99", data=_ROLE_FORM)

    assert resp.status_code == 404
    assert _statement_writes(pool) == []


@pytest.mark.anyio
async def test_edit_form_refuses_a_role_from_another_source(client: AsyncClient) -> None:
    resp = await client.get("/dashboard/data-sources/shop/roles/7/edit")

    assert resp.status_code == 404
    assert 'name="role_key"' not in resp.text


@pytest.mark.anyio
async def test_edit_form_renders_its_own_role(client: AsyncClient) -> None:
    resp = await client.get("/dashboard/data-sources/shop/roles/8/edit")

    assert resp.status_code == 200
    assert 'value="analyst"' in resp.text


@pytest.mark.anyio
async def test_update_rewrites_statements_in_one_transaction_scoped_to_the_source(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post("/dashboard/data-sources/shop/roles/8", data=_ROLE_FORM)

    assert resp.status_code in (200, 303)
    assert pool.transactions == 1
    deletes = [(q, a) for q, a in pool.writes if "DELETE FROM source_role_permissions" in q]
    assert len(deletes) == 1
    query, args = deletes[0]
    assert "r.source_id = $2" in query
    assert args == (8, "shop")
    inserts = [q for q, _ in pool.writes if "INSERT INTO source_role_permissions" in q]
    assert len(inserts) == 1


def _audit_rows(pool: FakePool) -> list[tuple[Any, ...]]:
    return [a for q, a in pool.writes if "INSERT INTO admin_audit_log" in q]


@pytest.mark.anyio
async def test_delete_is_refused_while_an_identity_holds_the_role(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.holders[8] = [{"id": 3, "name": "analyst-claude"}]

    resp = await client.post(
        "/dashboard/data-sources/shop/roles/8/delete", headers={"HX-Request": "true"}
    )

    assert resp.status_code == 200
    assert "still granted to analyst-claude (#3)" in resp.text
    assert not [q for q, _ in pool.writes if q.lstrip().startswith("DELETE")]
    audits = _audit_rows(pool)
    assert len(audits) == 1 and audits[0][8] is False


@pytest.mark.anyio
async def test_delete_refusal_is_a_conflict_outside_htmx(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.holders[8] = [{"id": 3, "name": "analyst-claude"}]

    resp = await client.post("/dashboard/data-sources/shop/roles/8/delete")

    assert resp.status_code == 409


@pytest.mark.anyio
async def test_delete_removes_an_ungranted_role_and_keeps_its_history(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.grants[8] = [
        {
            "id": 40,
            "identity_id": 5,
            "enabled": False,
            "expires_at": None,
            "granted_by": None,
            "created_at": None,
            "updated_at": None,
        }
    ]
    pool.policies_naming = [{"name": "deny-analysts", "roles": ["analyst"]}]

    resp = await client.post(
        "/dashboard/data-sources/shop/roles/8/delete", headers={"HX-Request": "true"}
    )

    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/dashboard/data-sources/shop"
    deletes = [(q, a) for q, a in pool.writes if "DELETE FROM source_roles" in q]
    assert deletes == [("DELETE FROM source_roles WHERE id = $1 AND source_id = $2", (8, "shop"))]
    assert pool.transactions == 1
    audits = _audit_rows(pool)
    assert len(audits) == 1
    detail = audits[0][5]
    assert audits[0][2] == "source_role.delete" and audits[0][8] is True
    assert detail["before"]["role_key"] == "analyst"
    assert detail["before"]["permissions"][0]["action"] == "db.table.select"
    assert detail["before"]["grants"][0]["identity_id"] == 5
    assert detail["referencing_policies"] == ["deny-analysts"]


@pytest.mark.anyio
async def test_delete_refuses_a_role_from_another_source(
    client: AsyncClient, pool: FakePool
) -> None:
    resp = await client.post("/dashboard/data-sources/shop/roles/7/delete")

    assert resp.status_code == 404
    assert not [q for q, _ in pool.writes if "DELETE" in q]


@pytest.mark.anyio
async def test_a_new_slack_role_starts_from_the_slack_reader_template(
    client: AsyncClient, pool: FakePool
) -> None:
    """Resolved from source_type alone, every SaaS source got HTTP's http.get."""
    pool.source = {"source_type": "saas", "metadata": {"connector_key": "slack"}}

    body = (await client.get("/dashboard/data-sources/team_slack/roles/new")).text

    assert "collab.channel.read" in body
    assert "http.get" not in body


@pytest.mark.anyio
async def test_a_new_s3_role_starts_from_the_s3_reader_template(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.source = {"source_type": "s3", "metadata": {"connector_key": "s3"}}

    body = (await client.get("/dashboard/data-sources/docs_s3/roles/new")).text

    assert "storage.object.read" in body


@pytest.mark.anyio
async def test_a_legacy_statement_saves_unchanged_but_not_edited(
    client: AsyncClient, pool: FakePool
) -> None:
    legacy = {
        "effect": "allow",
        "action": "custom.thing",
        "resource_type": "custom",
        "resource_pattern": "*",
        "constraints": {},
    }
    pool.stored = [legacy]
    form = {
        "role_key": "analyst",
        "name": "analyst",
        "permission_effect": "allow",
        "permission_action": "custom.thing",
        "permission_resource_type": "custom",
        "permission_resource_pattern": "*",
        "permission_constraints": "{}",
    }

    kept = await client.post("/dashboard/data-sources/shop/roles/8", data=form)
    assert kept.status_code in (200, 303) and "not an action" not in kept.text
    assert [q for q, _ in pool.writes if "INSERT INTO source_role_permissions" in q]

    pool.writes.clear()
    edited = await client.post(
        "/dashboard/data-sources/shop/roles/8",
        data={**form, "permission_resource_pattern": "sales.*"},
    )
    assert "is not an action on this connector" in edited.text
    assert not [q for q, _ in pool.writes if "source_role_permissions" in q]


@pytest.mark.anyio
async def test_the_s3_editor_offers_only_s3_actions_as_grouped_dropdowns(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.source = {"source_type": "s3", "metadata": {"connector_key": "s3"}}

    body = (await client.get("/dashboard/data-sources/docs_s3/roles/new")).text

    assert '<select name="permission_action"' in body
    assert '<select name="permission_resource_type"' in body
    assert '<optgroup label="Read">' in body and '<optgroup label="Admin and schema">' in body
    assert 'value="storage.prefix.list"' in body
    assert 'value="db.table.select"' not in body and 'value="http.get"' not in body
    # Patterns come with a hint and a list of suggestions per resource type.
    assert 'list="role-patterns-storage-object"' in body
    assert '<datalist id="role-patterns-storage-object">' in body


@pytest.mark.anyio
async def test_changing_the_action_narrows_the_resource_type_and_keeps_the_pattern(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.source = {"source_type": "s3", "metadata": {"connector_key": "s3"}}

    resp = await client.get(
        "/dashboard/data-sources/docs_s3/roles/statement-row",
        params={
            "permission_action": "storage.bucket.list",
            "permission_resource_type": "storage.object",
            "permission_resource_pattern": "s3://claims/*",
            "permission_constraints": '{"file_extensions": [".csv"]}',
            "row_uid": "r7",
        },
    )

    body = resp.text
    assert 'id="perm-row-r7"' in body
    assert '<option value="storage.bucket" selected>' in body
    assert '<option value="storage.object"' not in body
    assert 'value="s3://claims/*"' in body
    assert "file_extensions" in body


@pytest.mark.anyio
async def test_a_new_row_starts_on_a_read_action(client: AsyncClient, pool: FakePool) -> None:
    pool.source = {"source_type": "saas", "metadata": {"connector_key": "slack"}}

    body = (await client.get("/dashboard/data-sources/team_slack/roles/statement-row")).text

    assert (
        '<option value="collab.message.read" title="Read messages in channels." selected>' in body
    )
    assert '<option value="collab.message" selected>' in body


@pytest.mark.anyio
async def test_a_saved_statement_outside_the_vocabulary_is_shown_and_posted_unchanged(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.stored = [
        {
            "effect": "allow",
            "action": "custom.thing",
            "resource_type": "custom",
            "resource_pattern": "*",
            "constraints": {},
        }
    ]

    body = (await client.get("/dashboard/data-sources/shop/roles/8/edit")).text

    assert '<optgroup label="Kept as saved">' in body
    assert '<option value="custom.thing" selected>' in body
    assert '<option value="custom" selected>custom (kept as saved)</option>' in body


@pytest.mark.anyio
async def test_conditions_are_typed_controls_that_write_the_json_field(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.source = {"source_type": "saas", "metadata": {"connector_key": "slack"}}

    body = (
        await client.get(
            "/dashboard/data-sources/team_slack/roles/statement-row",
            params={
                "permission_action": "collab.message.read",
                "permission_resource_type": "collab.message",
                "permission_constraints": '{"channel_ids": ["C1", "C2"], "tags": ["x"]}',
            },
        )
    ).text

    assert 'data-constraint-key="channel_ids"' in body
    assert 'list="constraint-suggest-channel_ids"' in body
    assert 'value="C1, C2"' in body
    # The JSON field is the only named conditions field, and keeps every key.
    assert 'name="permission_constraints"' in body
    assert "&#34;tags&#34;" in body or '"tags"' in body
    assert "not offered for this action: <code>tags</code>" in body


@pytest.mark.anyio
async def test_sql_writes_offer_risk_levels_and_require_where(
    client: AsyncClient, pool: FakePool
) -> None:
    body = (
        await client.get(
            "/dashboard/data-sources/shop/roles/statement-row",
            params={
                "permission_action": "db.table.update",
                "permission_resource_type": "db.table",
                "permission_constraints": '{"require_where": true, "risk_levels": ["low"]}',
            },
        )
    ).text

    assert 'data-constraint-key="risk_levels" data-constraint-type="enum_list"' in body
    assert 'value="low" checked' in body and 'value="high" >' in body
    assert 'data-constraint-key="require_where" data-constraint-type="bool"' in body
    assert 'value="true" checked' in body


@pytest.mark.anyio
async def test_the_form_offers_catalogued_values_for_conditions(
    client: AsyncClient, pool: FakePool
) -> None:
    pool.source = {"source_type": "saas", "metadata": {"connector_key": "slack"}}
    original = pool.fetch

    async def fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "node_type IN ('channel', 'repository', 'index', 'collection')" in query:
            return [
                {"node_type": "channel", "resource_key": "slack://channel/C9", "name": "general"}
            ]
        return await original(query, *args)

    pool.fetch = fetch  # type: ignore[method-assign]
    body = (await client.get("/dashboard/data-sources/team_slack/roles/new")).text
    assert '<datalist id="constraint-suggest-channel_ids">' in body
    assert '<option value="C9" label="#general">' in body
