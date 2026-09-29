"""Small deterministic mock enterprise APIs for compose-backed E2E tests."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SALESFORCE_RECORDS = {
    "Account": [
        {
            "Id": "001-e2e",
            "Name": "Acme Claims",
            "LastModifiedDate": "2026-05-01T00:00:00Z",
        }
    ],
    "Case": [
        {
            "Id": "500-e2e",
            "Subject": "Claim review escalation",
            "Status": "New",
            "LastModifiedDate": "2026-05-01T00:00:00Z",
        }
    ],
}

NOTION_PAGES = [
    {
        "object": "page",
        "id": "page-e2e",
        "last_edited_time": "2026-05-01T00:00:00Z",
        "properties": {
            "Name": {"title": [{"plain_text": "Claims operations runbook"}]},
        },
    }
]


class Handler(BaseHTTPRequestHandler):
    server_version = "InterLockE2EMock/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json({"ok": True})
            return
        if parsed.path == "/reset":
            self._json({"ok": True})
            return
        if parsed.path == "/services/data":
            self._json([{"version": "60.0", "url": "/services/data/v60.0"}])
            return
        if parsed.path.endswith("/describe"):
            object_name = parsed.path.strip("/").split("/")[-2]
            self._json(
                {
                    "name": object_name,
                    "fields": [
                        {"name": "Id", "type": "id"},
                        {"name": "Name", "type": "string"},
                        {"name": "Subject", "type": "string"},
                        {"name": "LastModifiedDate", "type": "datetime"},
                    ],
                }
            )
            return
        if parsed.path.endswith("/query"):
            query = parse_qs(parsed.query).get("q", [""])[0]
            object_name = "Case" if " FROM Case" in query else "Account"
            self._json(
                {
                    "totalSize": len(SALESFORCE_RECORDS[object_name]),
                    "records": SALESFORCE_RECORDS[object_name],
                }
            )
            return
        if "/sobjects/" in parsed.path:
            parts = parsed.path.strip("/").split("/")
            object_name, object_id = parts[-2], parts[-1]
            records = SALESFORCE_RECORDS.get(object_name, [])
            for record in records:
                if record["Id"] == object_id:
                    self._json(record)
                    return
            self._json({"error": "not found"}, status=404)
            return
        if parsed.path == "/v1/users/me":
            self._json({"object": "user", "id": "notion-bot"})
            return
        if parsed.path == "/v1/pages/page-e2e":
            self._json(NOTION_PAGES[0])
            return
        if parsed.path == "/v1/blocks/page-e2e/children":
            self._json(
                {
                    "results": [
                        {
                            "object": "block",
                            "id": "block-e2e",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [
                                    {
                                        "plain_text": "Review, approve, and archive claim escalations."
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
            return
        self._json({"error": "not found", "path": parsed.path}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/v1/search":
            self._json({"results": NOTION_PAGES})
            return
        self._json({"error": "not found", "path": parsed.path}, status=404)

    def log_message(self, *_args: object) -> None:
        return

    def _json(self, payload: object, *, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(os.environ.get("ENTERPRISE_SOURCES_PORT", "8090"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
