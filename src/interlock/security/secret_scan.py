"""Fail when known live credentials or local credential artifacts are present.

This is a deliberately low-noise public-beta guard. It blocks credential-shaped
artifacts from tracked and untracked commit candidates without trying to
replace a full external secret scanner.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_PATTERNS = (
    # Cloud provider keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bdop_v1_[a-f0-9]{64}\b"),
    # Slack
    re.compile(r"\bxoxb-\d{10,}-\d{10,}-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bxoxp-\d{10,}-[A-Za-z0-9-]{16,}\b"),
    # Model provider keys
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    # Git forge tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    # Managed-database passwords (DigitalOcean issues AVNS_-prefixed secrets)
    re.compile(r"\bAVNS_[A-Za-z0-9_-]{12,}\b"),
    # Any managed-database hostname, not one specific internal cluster: a
    # hardcoded internal hostname would itself disclose infrastructure names.
    re.compile(r"\b[a-z0-9-]+-do-user-\d+-\d+\.[a-z]\.db\.ondigitalocean\.com\b"),
    # AWS managed endpoints carry a per-account identifier: RDS
    # (<name>.<12 chars>.<region>.rds.amazonaws.com) and ElastiCache
    # (<name>.<6 chars>.[ng.0001.]<region code>.cache.amazonaws.com).
    re.compile(r"\b[a-z0-9-]+\.[a-z0-9]{12}\.[a-z]{2}-[a-z]+-\d\.rds\.amazonaws\.com\b"),
    re.compile(
        r"\b[a-z0-9.-]+\.[a-z0-9]{6}\.(?:ng\.\d{4}\.)?[a-z]{2,4}\d\.cache\.amazonaws\.com\b"
    ),
    # Private keys of any flavor. The trailing body requirement distinguishes
    # real key material from the header-only literals that redaction tests use
    # to assert a key never reaches a log line.
    re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
        r"[\s\\nrt\"']*[A-Za-z0-9+/]{40,}"
    ),
    # Credentials embedded in connection URIs, excluding well-known local
    # development values that are documented throughout the repo, and
    # excluding unsubstituted placeholders. A password segment opening with
    # $, {, < or % is a shell variable, a template expression, or an
    # angle-bracket placeholder -- never key material. Without this the
    # scanner flags the documented, correct way to write a connection string
    # ("postgresql://agent:$API_KEY@host/db") and pressures doc authors into
    # inventing literal-looking examples instead, which is the outcome it
    # exists to prevent.
    re.compile(
        r"\b(?:postgresql|postgres|mysql|mongodb|rediss?|amqp)://"
        # The username may be empty: ElastiCache AUTH URLs are rediss://:secret@host.
        r"[A-Za-z0-9._-]*:(?!onyx_dev@|pass@|password@)(?![$<{%])[^\s:@/]{8,}@"
    ),
    # Placeholder that must never survive into a release
    re.compile(r"\bchange_me_[a-z0-9_]{4,}\b"),
    # Service-account key files named after an organization + key id
    re.compile(r"\b[a-z][a-z0-9-]{3,}\d*-[a-f0-9]{12,}\.json\b"),
)

# Line-scoped opt-out for deliberate synthetic fixtures, e.g. a test that
# asserts a credential is redacted from an error message. Write it as
#   "...literal..."  # secret-scan: allow - synthetic fixture, asserts redaction
ALLOW_PRAGMA = "secret-scan: allow"

SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}


def _candidate_files(root: Path, *, staged_only: bool = False) -> list[Path]:
    try:
        command = (
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
            if staged_only
            else ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
        )
        proc = subprocess.run(
            command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return [
            path
            for path in root.rglob("*")
            if path.is_file() and not any(part in SKIP_PARTS for part in path.parts)
        ]
    return [
        root / line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not any(part in SKIP_PARTS for part in Path(line.strip()).parts)
    ]


def scan(
    root: Path,
    patterns: tuple[re.Pattern[str], ...] = DEFAULT_PATTERNS,
    *,
    staged_only: bool = False,
) -> list[str]:
    findings: list[str] = []
    for path in _candidate_files(root, staged_only=staged_only):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            findings.append(f"{path.relative_to(root)}: cannot read file: {exc}")
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                line_number = text.count("\n", 0, match.start()) + 1
                line = text.splitlines()[line_number - 1] if text else ""
                # Escape hatch for deliberate synthetic fixtures. Keep it
                # line-scoped so allowing one fixture cannot silence a whole
                # file, and always state why in the same comment.
                if ALLOW_PRAGMA in line:
                    continue
                relative = path.relative_to(root)
                findings.append(f"{relative}:{line_number}: contains blocked sensitive pattern")
                break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root to scan")
    parser.add_argument(
        "--staged-only",
        action="store_true",
        help="scan only paths staged in the Git index",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings = scan(root, staged_only=args.staged_only)
    if findings:
        print("Sensitive artifact scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("Sensitive artifact scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
