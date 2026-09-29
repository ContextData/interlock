"""Tiny deterministic HTTP upstream for compose-backed E2E tests.

The service intentionally uses only the Python standard library so the E2E
compose overlay can run it from ``python:3.12-slim`` without installing app
dependencies.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

CALLS: list[dict[str, Any]] = []
MUTATIONS: list[dict[str, Any]] = []
# Approval notifications land here. Kept in their own list, and routed before
# the generic mutation handler, so they do not appear as governed HTTP writes
# and skew the tests that count those.
SLACK_CALLS: list[dict[str, Any]] = []
SLACK_FAILING = {"on": False}


class Handler(BaseHTTPRequestHandler):
    server_version = "InterLockE2EHTTP/1.0"
    # Chunked transfer encoding needs HTTP/1.1. The default is HTTP/1.0, under
    # which every response carries a content-length and the gateway's
    # streaming reassembly is never exercised - which is why the existing
    # text and csv fixtures, though they take the streaming branch, prove
    # nothing about how it handles real chunk boundaries.
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._record()
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json({"status": "ok"})
            return
        if parsed.path == "/json/customer":
            self._json(
                {
                    "id": 1,
                    "name": "Ada Lovelace",
                    "email": "ada@example.com",
                    "ssn": "123-45-6789",
                }
            )
            return
        if parsed.path == "/text/customer":
            self._text("Ada Lovelace SSN 123-45-6789\n")
            return
        if parsed.path == "/csv/customers":
            self._text(
                "id,name,email,ssn\n1,Ada Lovelace,ada@example.com,123-45-6789\n",
                content_type="text/csv",
            )
            return
        if parsed.path.startswith("/stream/"):
            self._stream(parsed)
            return
        if parsed.path == "/calls":
            self._json({"calls": CALLS, "mutations": MUTATIONS})
            return
        if parsed.path == "/slack/calls":
            self._json({"calls": SLACK_CALLS})
            return
        if parsed.path == "/slack/reset":
            SLACK_CALLS.clear()
            SLACK_FAILING["on"] = False
            self._json({"status": "reset"})
            return
        if parsed.path == "/slack/fail":
            SLACK_FAILING["on"] = parse_qs(parsed.query).get("on", ["1"])[0] == "1"
            self._json({"failing": SLACK_FAILING["on"]})
            return
        if parsed.path == "/reset":
            CALLS.clear()
            MUTATIONS.clear()
            self._json({"status": "reset"})
            return
        if parsed.path == "/failure":
            self._json({"error": "upstream failure"}, status=503)
            return
        self._json({"error": "not found", "path": parsed.path}, status=404)

    def do_POST(self) -> None:
        if urlparse(self.path).path.startswith("/slack/"):
            self._slack()
            return
        self._mutation("POST")

    def do_PUT(self) -> None:
        self._mutation("PUT")

    def do_PATCH(self) -> None:
        self._mutation("PATCH")

    def do_DELETE(self) -> None:
        self._mutation("DELETE")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _slack(self) -> None:
        """Stand in for a Slack webhook or chat.postMessage endpoint."""
        parsed = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("content-length", "0") or 0))
        SLACK_CALLS.append(
            {
                "path": parsed.path,
                "authorization": self.headers.get("authorization"),
                "body": body.decode("utf-8", errors="replace"),
            }
        )
        if SLACK_FAILING["on"]:
            self._json({"ok": False, "error": "simulated_outage"}, status=500)
            return
        if parsed.path.endswith("/chat.postMessage"):
            self._json({"ok": True, "ts": "1700000000.000100"})
            return
        self._text("ok")

    def _mutation(self, method: str) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", "0") or 0))
        self._record(body=body.decode("utf-8", errors="replace"))
        MUTATIONS.append(
            {
                "method": method,
                "path": urlparse(self.path).path,
                "body": body.decode("utf-8", errors="replace"),
            }
        )
        self._json({"status": "mutated", "method": method, "count": len(MUTATIONS)})

    def _record(self, body: str | None = None) -> None:
        parsed = urlparse(self.path)
        CALLS.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "authorization": self.headers.get("authorization"),
                "body": body,
            }
        )

    # -- streaming fixtures --------------------------------------------------
    #
    # Real chunked responses with caller-controlled boundaries, so a test can
    # place a split exactly where it wants one. Everything else in this mock
    # sends a single write with a content-length.

    def _stream(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        kind = parsed.path.rsplit("/", 1)[-1]

        if kind == "csv":
            # A quoted CSV field whose value wraps across an embedded newline.
            # Read as CSV this is one record; split on raw newlines it is two
            # fragments, and a value spanning the break matches no pattern in
            # either half. CREDIT_CARD, PHONE and MRN can all span a newline
            # because their separator classes include \s.
            body = (
                "id,note\n"
                '1,"card 4111 1111 1111\n1111 on file"\n'
                '2,"call 415-555\n1212 today"\n'
            )
            self._chunked(body, "text/csv", self._splits(query, body))
            return

        if kind == "ndjson":
            body = '{"id":1,"ssn":"123-45-6789"}\n' '{"id":2,"email":"ada@example.com"}\n'
            self._chunked(body, "application/x-ndjson", self._splits(query, body))
            return

        if kind == "text":
            body = "Ada Lovelace SSN 123-45-6789 and card 4111 1111 1111 1111\n"
            self._chunked(body, "text/plain", self._splits(query, body))
            return

        if kind == "huge":
            # Far larger than any configured response limit, to trip the
            # mid-stream bound after bytes are already on the wire.
            size = int((query.get("bytes") or ["20000000"])[0])
            self._chunked_bytes((b"x" * 65536 for _ in range(max(1, size // 65536))), "text/plain")
            return

        if kind == "badutf8":
            # A truncated multi-byte sequence, split so the decoder meets it
            # mid-stream rather than at the start.
            self._chunked_bytes(
                iter([b"first line is fine\n", b"second \xe2\x28\xa1 broken\n"]),
                "text/plain",
            )
            return

        if kind == "oversize-declared":
            # Declares a content-length above the limit, so the *pre-stream*
            # check rejects it cleanly. The contrast case for a mid-stream
            # trip, which cannot answer cleanly because bytes have been sent.
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", "999999999")
            self.end_headers()
            return

        self._json({"error": "unknown stream fixture", "kind": kind}, status=404)

    @staticmethod
    def _splits(query: dict[str, list[str]], body: str) -> list[int]:
        """Byte offsets at which to cut the body, from ?split=a,b,c."""
        raw = (query.get("split") or [""])[0]
        if not raw:
            return []
        encoded = len(body.encode("utf-8"))
        return [
            offset
            for offset in (int(piece) for piece in raw.split(",") if piece.strip())
            if 0 < offset < encoded
        ]

    def _chunked(self, payload: str, content_type: str, splits: list[int]) -> None:
        data = payload.encode("utf-8")
        pieces: list[bytes] = []
        previous = 0
        for offset in sorted(splits):
            pieces.append(data[previous:offset])
            previous = offset
        pieces.append(data[previous:])
        self._chunked_bytes(iter([piece for piece in pieces if piece]), content_type)

    def _chunked_bytes(self, pieces: Any, content_type: str) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for piece in pieces:
            self.wfile.write(f"{len(piece):x}\r\n".encode("ascii"))
            self.wfile.write(piece)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(
        self,
        payload: str,
        *,
        status: int = 200,
        content_type: str = "text/plain",
    ) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(os.environ.get("HTTP_UPSTREAM_PORT", "8088"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"InterLock E2E HTTP upstream listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
