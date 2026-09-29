"""Run the quick start as an acceptance test, from an empty stack to an audited query.

Starts a fresh Compose project (its own name, ports and volumes), then does
what the quick start tells a person to do, through the endpoints the console
uses: sign in with the default password and change it, register the sample
database through the wizard, allow reads by policy, create an agent with the
wizard's `read` role. It then checks, at the client, the outcomes the guide
promises:

- the same statement over PostgreSQL and MCP decodes to the same redacted rows,
  cold, warm, and after the other protocol has warmed its cache;
- a write the role does not allow is refused over both protocols and the data
  is unchanged;
- the audit log has the requests, matched to the MCP calls by correlation ID,
  with the right protocol, outcome and redaction record.

Only the standard library is used, so it runs anywhere Docker and Python 3 do.
It needs no model keys and no external accounts, and it proves nothing about
security, performance or release readiness: those are separate suites.

    python3 tools/smoke_eval.py            # build, run, tear down
    python3 tools/smoke_eval.py --keep     # leave the stack running afterwards

Writes build/smoke-eval/result.json and exits non-zero if any check failed.
"""

from __future__ import annotations

import argparse
import csv
import html
import http.cookiejar
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "smoke-eval"
PROJECT = "interlock-smoke"
PORTS = {
    "admin": int(os.environ.get("SMOKE_ADMIN_PORT", "19090")),
    "gateway": int(os.environ.get("SMOKE_GATEWAY_PORT", "13001")),
    "pg": int(os.environ.get("SMOKE_PG_PORT", "15434")),
}
SQL = "SELECT name, email, plan FROM customers ORDER BY id LIMIT 3"
REDACTED_EMAIL = "[REDACTED:EMAIL]"


@dataclass
class Check:
    name: str
    passed: bool
    expected: str
    actual: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def check(self, name: str, passed: bool, expected: str, actual: Any) -> bool:
        self.checks.append(Check(name, bool(passed), expected, str(actual)[:300]))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}", flush=True)
        if not passed:
            print(f"         expected: {expected}\n         actual:   {str(actual)[:300]}")
        return bool(passed)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


# --- the stack ----------------------------------------------------------------


def _override_file() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "ports.override.yml"
    path.write_text(
        "services:\n"
        "  admin:\n    ports: !override\n"
        f'      - "127.0.0.1:{PORTS["admin"]}:9090"\n'
        "  gateway:\n    ports: !override\n"
        f'      - "127.0.0.1:{PORTS["gateway"]}:3000"\n'
        f'      - "127.0.0.1:{PORTS["pg"]}:5432"\n'
    )
    return path


def _compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    command = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(_override_file()),
        "--profile",
        "quickstart",
        *args,
    ]
    return subprocess.run(command, cwd=ROOT, check=check, text=True, capture_output=capture)


def _psql(api_key: str, sql: str) -> subprocess.CompletedProcess:
    """psql inside the stack's own PostgreSQL container, as the quick start does."""
    dsn = "host=gateway port=5432 user=agent dbname=sample_shop sslmode=disable"
    return _compose(
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={api_key}",
        "postgres",
        "psql",
        dsn,
        "-X",
        "-A",
        "-t",
        "-F",
        "|",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
        check=False,
        capture=True,
    )


# --- HTTP ---------------------------------------------------------------------


class Admin:
    def __init__(self) -> None:
        self.base = f"http://127.0.0.1:{PORTS['admin']}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect()
        )
        self.csrf = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, Any] | None = None,
        body: Any = None,
        accept_json: bool = False,
    ) -> tuple[int, dict[str, str], str]:
        headers = {"X-CSRF-Token": self.csrf} if self.csrf else {}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if accept_json:
            headers["Accept"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode()

    def refresh_csrf(self) -> None:
        _, _, text = self.request("GET", "/auth/csrf")
        self.csrf = json.loads(text)["csrf"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _mcp(api_key: str, request_id: int, arguments: dict[str, Any], correlation_id: str) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORTS['gateway']}/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "interlock_query", "arguments": arguments},
            }
        ).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
            "X-Correlation-ID": correlation_id,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _mcp_rows(result: dict) -> list[list[str]]:
    rows = json.loads(result["result"]["content"][0]["text"])
    return [[str(r["name"]), str(r["email"]), str(r["plan"])] for r in rows]


def _psql_rows(proc: subprocess.CompletedProcess) -> list[list[str]]:
    return [line.split("|") for line in proc.stdout.splitlines() if line.strip()]


# --- the journey --------------------------------------------------------------


def run(report: Report) -> None:
    admin = Admin()
    new_password = "Smoke-" + secrets.token_urlsafe(12)

    print("\nSign in and change the default password")
    status, _, text = admin.request(
        "POST", "/auth/login", form={"username": "admin", "password": "admin"}, accept_json=True
    )
    report.check(
        "default sign-in asks for a new password",
        status == 200 and json.loads(text or "{}").get("password_change_required") is True,
        "200 with password_change_required: true",
        f"{status} {text[:120]}",
    )
    admin.refresh_csrf()
    status, _, _ = admin.request(
        "POST",
        "/auth/change-password",
        form={
            "current_password": "admin",
            "new_password": new_password,
            "confirm_password": new_password,
        },
    )
    report.check("password change accepted", status in (200, 204, 303), "200, 204 or 303", status)
    admin.refresh_csrf()
    status, _, text = admin.request("GET", "/api/data-sources", accept_json=True)
    report.check("admin API opens after the change", status == 200, "200", status)

    print("\nRegister the sample database through the wizard")
    status, headers, text = admin.request(
        "POST",
        "/dashboard/source-wizard/save",
        form={
            "name": "Sample shop",
            "source_type": "postgresql",
            "connector_key": "postgresql",
            "host": "sample-postgres",
            "port": "5432",
            "database": "shop",
            "user": "shop_reader",
            "password": "shop-reader-dev-only",
            "sslmode": "disable",
            "allow_private_egress": "on",
            "cache_strategy": "deterministic_first",
            "create_default_roles": "on",
        },
    )
    report.check(
        "wizard saves the source as sample_shop",
        status == 303 and headers.get("location") == "/dashboard/data-sources/sample_shop",
        "303 to /dashboard/data-sources/sample_shop",
        f"{status} {headers.get('location')}",
    )
    status, _, text = admin.request("POST", "/api/data-sources/sample_shop/test")
    report.check(
        "test connection is healthy",
        status == 200 and json.loads(text).get("ok") is True,
        '{"ok": true}',
        text[:160],
    )

    print("\nAllow reads by policy and create the agent")
    status, _, text = admin.request(
        "POST",
        "/api/policies",
        body={
            "name": "allow-sample-shop-reads",
            "priority": 10,
            "conditions": {"source_ids": ["sample_shop"], "operation_types": ["read"]},
            "actions": {"effect": "allow"},
        },
    )
    report.check("policy created", status in (200, 201), "200 or 201", f"{status} {text[:120]}")
    _, _, form_page = admin.request("GET", "/dashboard/access-control/identities/new")
    options = json.loads(
        html.unescape(re.search(r"data-role-options='([^']*)'", form_page).group(1))
    )
    read_role = next(
        (o for o in options.get("sample_shop", []) if o.get("role_key", o.get("key")) == "read"),
        None,
    )
    report.check(
        "the wizard created the read role",
        read_role is not None,
        "a role with key read on sample_shop",
        options.get("sample_shop"),
    )
    status, _, text = admin.request(
        "POST",
        "/dashboard/access-control/identities/create",
        form={
            "name": "smoke-agent",
            "agent_type": "claude_code",
            "generate_key": "on",
            "grant_source_id": "sample_shop",
            "grant_role_id": str((read_role or {}).get("id", "")),
        },
    )
    match = re.search(
        r"cannot recover the raw value\.\s*</p>\s*.*?<code[^>]*>([^<]+)</code>", text, re.S
    )
    api_key = html.unescape(match.group(1)).strip() if match else ""
    report.check("identity created and key shown once", bool(api_key), "an API key", status)
    if not api_key:
        return

    print("\nThe same statement over both protocols")
    cold_pg = _psql(api_key, SQL)
    pg_rows = _psql_rows(cold_pg)
    report.check(
        "psql, cold: three rows, emails redacted",
        cold_pg.returncode == 0
        and len(pg_rows) == 3
        and all(r[1] == REDACTED_EMAIL for r in pg_rows),
        f"3 rows with {REDACTED_EMAIL}",
        cold_pg.stdout + cold_pg.stderr,
    )
    correlation = {}
    for label, request_id in (("MCP after psql", 1), ("MCP, warm", 2)):
        correlation[label] = f"smoke-{secrets.token_hex(6)}"
        result = _mcp(
            api_key, request_id, {"source_id": "sample_shop", "sql": SQL}, correlation[label]
        )
        report.check(
            f"{label}: the same rows",
            _mcp_rows(result) == pg_rows,
            str(pg_rows),
            result,
        )
    warm_pg = _psql(api_key, SQL)
    report.check(
        "psql after MCP: the same rows (no cache crossing protocols)",
        warm_pg.returncode == 0 and _psql_rows(warm_pg) == pg_rows,
        str(pg_rows),
        warm_pg.stdout + warm_pg.stderr,
    )

    print("\nA write the role does not allow")
    correlation["MCP delete"] = f"smoke-{secrets.token_hex(6)}"
    result = _mcp(
        api_key,
        3,
        {"source_id": "sample_shop", "sql": "DELETE FROM orders WHERE id = 1"},
        correlation["MCP delete"],
    )
    structured = result.get("result", {}).get("structuredContent", {})
    report.check(
        "MCP delete refused by the source role",
        result.get("result", {}).get("isError") is True and structured.get("status") == "denied",
        "isError true, status denied",
        result,
    )
    refused = _psql(api_key, "DELETE FROM orders WHERE id = 1")
    report.check(
        "psql delete refused by the source role",
        refused.returncode != 0 and "Source role denied" in refused.stderr,
        "an error naming Source role denied",
        refused.stderr,
    )
    count = _psql(api_key, "SELECT count(*) FROM orders")
    report.check("orders unchanged", count.stdout.strip() == "5", "5", count.stdout.strip())

    print("\nThe audit log")
    deadline = time.monotonic() + 45
    rows: list[dict[str, str]] = []
    while time.monotonic() < deadline:
        _, _, text = admin.request("GET", "/dashboard/audit-costs/export.csv?limit=200")
        rows = list(csv.DictReader(io.StringIO(text)))
        by_id = {r.get("correlation_id"): r for r in rows}
        if all(c in by_id for c in correlation.values()):
            break
        time.sleep(2)
    by_id = {r.get("correlation_id"): r for r in rows}
    for label, cid in correlation.items():
        row = by_id.get(cid)
        want = "denied" if "delete" in label else "success"
        report.check(
            f"audit: {label} recorded by correlation ID",
            row is not None and row["protocol"] == "mcp" and row["status"] == want,
            f"protocol mcp, status {want}",
            row,
        )
    warm_mcp = by_id.get(correlation["MCP, warm"])
    report.check(
        "audit: the cached MCP answer is recorded as redacted",
        warm_mcp is not None
        and warm_mcp["cache_hit"] == "True"
        and warm_mcp["pii_detected"] == "True",
        "cache_hit True, pii_detected True",
        warm_mcp,
    )
    pg = [r for r in rows if r["protocol"] == "postgresql"]
    report.check(
        "audit: the psql requests are recorded",
        sum(r["status"] == "success" for r in pg) >= 3 and any(r["status"] == "denied" for r in pg),
        "at least 3 successful and 1 denied postgresql rows",
        [(r["status"], r["cache_hit"]) for r in pg],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    parser.add_argument("--no-build", action="store_true", help="reuse built images")
    args = parser.parse_args()

    report = Report()
    started = time.monotonic()
    print(f"Starting Compose project {PROJECT} on ports {PORTS}")
    try:
        _compose("down", "-v", "--remove-orphans", check=False, capture=True)
        up = ["up", "-d", "--wait"] + ([] if args.no_build else ["--build"])
        _compose(*up)
        run(report)
    except Exception as exc:  # a crash is a failed evaluation, reported as one
        report.check("the evaluation ran to the end", False, "no exception", repr(exc))
    finally:
        if not args.keep:
            _compose("down", "-v", "--remove-orphans", check=False, capture=True)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "seconds": round(time.monotonic() - started),
                "passed": not report.failed,
                "checks": [asdict(c) for c in report.checks],
            },
            indent=2,
        )
    )
    print(f"\n{len(report.checks) - len(report.failed)}/{len(report.checks)} checks passed")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
