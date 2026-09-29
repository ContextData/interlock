"""The role editor speaks each source's own vocabulary.

A statement naming an action the source's requests never carry, or a
condition the evaluator ignores, is refused before it is saved.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

pytestmark = [pytest.mark.e2e]


def _headers(admin_session: Any) -> dict[str, str]:
    return {"X-CSRF-Token": admin_session.csrf_token}


@pytest.mark.asyncio
async def test_an_s3_role_cannot_be_saved_with_a_sql_action(
    admin_session: Any, control_db: Any, e2e_config: Any
) -> None:
    source_id = e2e_config.source_id_s3
    statement = {
        "permission_effect": "allow",
        "permission_action": "db.table.select",
        "permission_resource_type": "db.table",
        "permission_resource_pattern": "*.*",
        "permission_constraints": "{}",
    }
    lint = admin_session.client.post(
        f"/dashboard/data-sources/{source_id}/roles/lint",
        data=statement,
        headers=_headers(admin_session),
    )
    assert "Will not save" in lint.text

    role_key = f"e2e_vocab_{secrets.token_hex(3)}"
    saved = admin_session.client.post(
        f"/dashboard/data-sources/{source_id}/roles",
        data={"role_key": role_key, "name": role_key, **statement},
        headers=_headers(admin_session),
    )
    assert "is not an action on this connector" in saved.text
    assert (
        await control_db.fetchval(
            "SELECT 1 FROM source_roles WHERE source_id = $1 AND role_key = $2",
            source_id,
            role_key,
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_new_s3_role_starts_from_the_s3_reader(admin_session: Any, e2e_config: Any) -> None:
    form = admin_session.client.get(f"/dashboard/data-sources/{e2e_config.source_id_s3}/roles/new")
    assert "storage.object.read" in form.text
    assert "http.get" not in form.text
