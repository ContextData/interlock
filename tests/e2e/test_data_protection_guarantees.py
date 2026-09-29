"""Data protection judged across every surface, not just the response.

Phase 3 of the governance audit.

Existing tests check that a governed response is redacted, and they are right
to. What none of them check is where the *unredacted* value went. A response
can be clean while the raw value is sitting in the audit row that describes the
request, in a cache entry that will be served to the next caller, or in a log
line shipped to an aggregator. Redaction that only holds on the response path
is not data protection; it just moves the disclosure somewhere less visible.

So these tests take the real secret from the origin - bypassing the gateway
entirely to learn what it actually is - and then assert its absence everywhere
the platform stores or emits anything about the request.

A note on what is being tested. The visible ``[REDACTED:POLICY]`` marker in an
e2e response comes from policy field redaction. Scanner-driven redaction
(``[REDACTED:SSN]`` and friends) is a separate mechanism covered in
tests/unit/test_processor.py and tests/integration/test_governance.py. Both
end up in the same place for the purposes of this file: the raw value must not
survive anywhere.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import httpx
import pytest

from tests.e2e.support.clients import http_proxy_request, mcp_call, wait_for

pytestmark = [pytest.mark.e2e]


def _raw_upstream_record(e2e_config: Any) -> dict[str, Any]:
    """Read the origin directly, so the secrets are known rather than assumed."""
    with httpx.Client(base_url=e2e_config.http_upstream_url, timeout=10) as client:
        response = client.get("/json/customer")
        response.raise_for_status()
        return dict(response.json())


def _gateway_logs(tail: int = 400) -> str:
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), "interlock-e2e-gateway-1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout + result.stderr


@pytest.fixture
def secrets(e2e_config: Any) -> dict[str, str]:
    record = _raw_upstream_record(e2e_config)
    values = {key: str(value) for key, value in record.items() if key in ("ssn", "email") and value}
    assert values, f"no PII fields found in the origin record: {record}"
    return values


@pytest.mark.asyncio
async def test_the_governed_response_does_not_contain_the_raw_value(
    e2e_config: Any, secrets: dict[str, str]
) -> None:
    """The baseline. If this fails nothing below is meaningful."""
    response = http_proxy_request(
        e2e_config,
        "GET",
        "json/customer",
        headers={"Authorization": f"Bearer {e2e_config.agent_api_key}"},
    )
    assert response.status_code == 200, response.text[:200]

    for field, value in secrets.items():
        assert value not in response.text, f"the raw {field} was returned to the caller: {value!r}"
    assert (
        "REDACTED" in response.text
    ), f"nothing was redacted at all, so the absence above proves nothing: {response.text[:200]}"


@pytest.mark.asyncio
async def test_the_raw_value_is_not_stored_in_the_audit_trail(
    e2e_config: Any, control_db: Any, secrets: dict[str, str]
) -> None:
    """The audit row describes the request; it must not quote the secret.

    This is the surface most likely to leak in practice: audit rows carry
    request metadata, error messages and redaction statistics, all of which are
    assembled near the unredacted payload.
    """
    http_proxy_request(
        e2e_config,
        "GET",
        "json/customer",
        headers={"Authorization": f"Bearer {e2e_config.agent_api_key}"},
    )

    rows = await control_db.fetch(
        "SELECT id, request_metadata::text AS meta, error_message, redaction_stats::text AS stats"
        " FROM audit_log WHERE source_id = $1 ORDER BY created_at DESC LIMIT 20",
        e2e_config.source_id_http,
    )
    assert rows, "no audit rows were written for the request"

    for row in rows:
        haystack = " ".join(str(row[column] or "") for column in ("meta", "error_message", "stats"))
        for field, value in secrets.items():
            assert value not in haystack, f"audit row {row['id']} stores the raw {field}: {value!r}"


@pytest.mark.asyncio
async def test_the_raw_value_is_not_written_to_the_gateway_log(
    e2e_config: Any, secrets: dict[str, str]
) -> None:
    """Logs are shipped off-box, so a leak here escapes the trust boundary."""
    http_proxy_request(
        e2e_config,
        "GET",
        "json/customer",
        headers={"Authorization": f"Bearer {e2e_config.agent_api_key}"},
    )

    logs = _gateway_logs()
    if not logs.strip():
        pytest.skip("gateway logs unavailable in this environment")

    for field, value in secrets.items():
        assert value not in logs, f"the raw {field} was written to the gateway log: {value!r}"


@pytest.mark.asyncio
async def test_a_repeated_request_is_not_served_the_raw_value_from_cache(
    e2e_config: Any, secrets: dict[str, str]
) -> None:
    """A cache that stores the pre-redaction payload leaks on the second call.

    The first response being clean says nothing about what was cached. Only the
    repeat request can distinguish "redacted before caching" from "redacted on
    the way out, once".
    """
    headers = {"Authorization": f"Bearer {e2e_config.agent_api_key}"}
    first = http_proxy_request(e2e_config, "GET", "json/customer", headers=headers)
    assert first.status_code == 200

    for _ in range(3):
        repeat = http_proxy_request(e2e_config, "GET", "json/customer", headers=headers)
        assert repeat.status_code == 200
        for field, value in secrets.items():
            assert value not in repeat.text, (
                f"a repeated request returned the raw {field} - the cache holds "
                f"the unredacted payload: {value!r}"
            )


@pytest.mark.asyncio
async def test_a_redacted_mcp_response_records_what_it_masked(
    e2e_config: Any, control_db: Any
) -> None:
    """Redaction must leave evidence, not just a boolean.

    `audit_log.redaction_stats` is where the operator guide sends an auditor to
    confirm that PII was masked. It was written only by the HTTP
    `redact_columns` path, so on MCP - the path agents actually use - it was
    always null while redaction was in fact happening. Measured on a live
    deployment: 1 of 55 rows carried it. `pii_types` was empty for the same
    reason, the adapter having filtered scanner detections with
    `isinstance(d, dict)` against a model that is not a dict.

    An auditor following the documented procedure would have concluded that no
    redaction occurred.
    """
    response = mcp_call(
        e2e_config,
        "interlock_query",
        {
            "source_id": e2e_config.source_id_mysql,
            "sql": "SELECT id, name, ssn FROM customers LIMIT 3",
        },
    )
    assert response.status_code == 200, response.text
    assert "[REDACTED:" in response.text, "the response was not redacted; nothing to evidence"

    # Audit writes are batched, so the row for this request lands a moment
    # later. Reading immediately raced the flush and picked up an older row.
    async def probe() -> Any:
        return await control_db.fetchrow(
            "SELECT pii_detected, pii_types, redaction_stats"
            " FROM audit_log WHERE source_id = $1 AND protocol = 'mcp'"
            " AND status = 'success' AND pii_detected"
            " ORDER BY created_at DESC LIMIT 1",
            e2e_config.source_id_mysql,
        )

    row = await wait_for(probe, timeout_seconds=25)
    assert row, "no audit row recorded PII for a response that was redacted"

    assert row["pii_types"], "pii_detected was set but pii_types names nothing"
    assert "SSN" in " ".join(row["pii_types"])

    stats = row["redaction_stats"]
    if isinstance(stats, str):
        stats = json.loads(stats)
    assert stats, "redaction happened but redaction_stats is empty"
    assert stats["rows_redacted"] >= 1
    assert sum(stats["pii_redactions"].values()) >= 1
