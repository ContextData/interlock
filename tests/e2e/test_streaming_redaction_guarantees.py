"""Redaction on genuinely chunked responses, through the real gateway.

The suite had no coverage of chunked streaming at all. `/text/customer` and
`/csv/customers` do take the streaming branch, but the mock sent each as a
single write with a content-length, so reassembly across chunk boundaries was
never exercised - the tests looked like streaming coverage and were not.

The fixtures used here send real `Transfer-Encoding: chunked` responses with
caller-placed boundaries (`?split=a,b,c` in byte offsets), so a test can put a
cut exactly where it wants one.

What each test is for:

- A value wrapped inside a quoted CSV field must still be redacted. Splitting
  CSV on raw newlines let CREDIT_CARD, PHONE and MRN escape, because those
  patterns' separator classes include `\\s` and so can span a newline.
- A chunk boundary placed mid-value must not matter, in any format.
- A stream cut short by the response limit must say so in its own body, since
  HTTP cannot answer 413 once bytes are on the wire.
- A scanner failure must be detectable by one marker across every format.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.e2e.support.config import E2EConfig

pytestmark = [pytest.mark.e2e]

# The fixture wraps these across a newline inside a quoted field, so the
# constants are the *fragments* either side of the break - the whole value
# never appears contiguously in the body, which is the entire point.
CARD_HEAD = "4111 1111 1111"
CARD_TAIL = "1111 on file"
PHONE_TAIL = "1212 today"
SSN = "123-45-6789"
MARKER = "[REDACTED:SCAN_FAILED]"


def _stream(config: E2EConfig, path: str, timeout: float = 45) -> httpx.Response:
    return httpx.get(
        f"{config.gateway_url}/proxy/{config.source_id_http}/{path}",
        headers={"Authorization": f"Bearer {config.agent_api_key}"},
        timeout=timeout,
    )


def _upstream(config: E2EConfig, path: str) -> str:
    """What the origin serves without governance, for the contrast."""
    return httpx.get(f"{config.http_upstream_url}/{path}", timeout=20).text


def test_the_fixture_is_actually_chunked(e2e_config: E2EConfig) -> None:
    """Guards every other test in this file.

    If the mock reverted to sending a content-length, the streaming path would
    still run but reassembly would never be exercised, and these tests would
    pass while proving nothing - which is exactly the state the suite was in
    before this file existed.
    """
    with httpx.stream("GET", f"{e2e_config.http_upstream_url}/stream/csv", timeout=20) as r:
        assert (
            r.headers.get("transfer-encoding") == "chunked"
        ), f"the streaming fixture is not chunked: {dict(r.headers)}"
        assert "content-length" not in r.headers


class TestCsvQuotedNewlines:
    def test_a_value_wrapped_in_a_quoted_field_is_redacted(self, e2e_config: E2EConfig) -> None:
        """The leak, proven by contrast with the ungoverned origin."""
        raw = _upstream(e2e_config, "stream/csv")
        served = _stream(e2e_config, "stream/csv").text

        assert f"{CARD_HEAD}\n{CARD_TAIL}" in raw and "415-555\n1212" in raw, (
            "the fixture no longer wraps a value across a quoted newline, so this "
            f"test proves nothing: {raw!r}"
        )
        assert "REDACTED" in served, f"nothing was redacted at all: {served!r}"
        assert (
            f"{CARD_HEAD}\n{CARD_TAIL}" not in served
        ), f"the card wrapped across a quoted newline reached the caller: {served!r}"
        assert not (
            "415-555" in served and PHONE_TAIL in served
        ), f"the phone wrapped across a quoted newline reached the caller: {served!r}"

    @pytest.mark.parametrize("split", ["10", "25", "40", "10,30,50"])
    def test_chunk_boundaries_do_not_change_the_outcome(
        self, e2e_config: E2EConfig, split: str
    ) -> None:
        """Redaction must not depend on where the network happened to cut.

        The offsets walk across the wrapped values deliberately.
        """
        served = _stream(e2e_config, f"stream/csv?split={split}").text

        assert "REDACTED" in served, f"split={split} produced no redaction: {served!r}"
        assert (
            f"{CARD_HEAD}\n{CARD_TAIL}" not in served
        ), f"split={split} leaked the card: {served!r}"

    def test_the_csv_shape_survives_redaction(self, e2e_config: E2EConfig) -> None:
        """A redactor that mangles the format has broken the response instead."""
        served = _stream(e2e_config, "stream/csv").text

        assert served.startswith("id,note\n")
        assert served.count("\n") >= 3, f"records were lost or merged: {served!r}"


class TestOtherFormats:
    @pytest.mark.parametrize("split", ["", "12", "20,45"])
    def test_ndjson_records_are_redacted_across_boundaries(
        self, e2e_config: E2EConfig, split: str
    ) -> None:
        path = "stream/ndjson" + (f"?split={split}" if split else "")
        served = _stream(e2e_config, path).text

        assert SSN not in served, f"split={split!r} leaked the SSN: {served!r}"
        assert "ada@example.com" not in served, f"split={split!r} leaked the email"
        assert served.count("\n") == 2, f"ndjson records were lost or merged: {served!r}"

    @pytest.mark.parametrize("split", ["", "15", "30,44"])
    def test_plain_text_is_redacted_across_boundaries(
        self, e2e_config: E2EConfig, split: str
    ) -> None:
        path = "stream/text" + (f"?split={split}" if split else "")
        served = _stream(e2e_config, path).text

        assert SSN not in served, f"split={split!r} leaked the SSN: {served!r}"
        assert "REDACTED" in served


class TestTruncationIsDiagnosable:
    def test_a_response_declared_too_large_is_refused_cleanly(self, e2e_config: E2EConfig) -> None:
        """The contrast case: the limit is known before anything is sent."""
        response = _stream(e2e_config, "stream/oversize-declared")

        assert response.status_code == 413
        assert "response_too_large" in response.text

    def test_a_stream_cut_short_names_the_reason_in_its_own_body(
        self, e2e_config: E2EConfig
    ) -> None:
        """HTTP cannot answer 413 once bytes are on the wire, so it says so in-band.

        The connection is still torn down without a terminating chunk, which
        is deliberate: a clean close would leave a naive client holding a
        well-formed 200 with a short body, and that is the quieter, worse
        failure. The marker is what makes the loud failure diagnosable.
        """
        received = b""
        transport_failed = False
        url = (
            f"{e2e_config.gateway_url}/proxy/{e2e_config.source_id_http}"
            "/stream/huge?bytes=80000000"
        )
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {e2e_config.agent_api_key}"},
                timeout=60,
            ) as response:
                for chunk in response.iter_raw():
                    received += chunk
        except httpx.RemoteProtocolError:
            transport_failed = True

        body = received.decode("utf-8", errors="replace")

        assert transport_failed, (
            "the truncated stream closed cleanly, so a client sees a well-formed "
            "200 and a short body with nothing to signal the truncation"
        )
        assert MARKER in body, (
            f"the truncated stream carried no marker, leaving the client unable to "
            f"tell truncation from an upstream crash: {body[-200:]!r}"
        )
        assert "response_too_large" in body, f"the marker does not name the reason: {body[-200:]!r}"


class TestScannerFailuresAreDetectable:
    def test_invalid_utf8_partway_through_is_marked(self, e2e_config: E2EConfig) -> None:
        """The decoder cannot continue, and the caller is told rather than shorted."""
        response = _stream(e2e_config, "stream/badutf8")

        assert response.status_code == 200
        assert (
            MARKER in response.text
        ), f"an undecodable stream ended without a marker: {response.text!r}"

    @pytest.mark.asyncio
    async def test_a_streamed_request_is_audited(
        self, e2e_config: E2EConfig, control_db: Any
    ) -> None:
        """Streaming must not skip the audit trail the buffered path writes."""
        import asyncio

        before = int(await control_db.fetchval("SELECT COALESCE(MAX(id), 0) FROM audit_log"))
        _stream(e2e_config, "stream/csv")

        rows: list[Any] = []
        for _ in range(24):
            rows = await control_db.fetch(
                "SELECT id, status, request_metadata FROM audit_log"
                " WHERE id > $1 AND source_id = $2",
                before,
                e2e_config.source_id_http,
            )
            if rows:
                break
            await asyncio.sleep(0.25)

        assert rows, "a streamed response produced no audit row"
