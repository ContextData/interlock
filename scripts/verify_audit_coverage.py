"""Verify audit remediation coverage markers.

The implementation audit uses stable issue IDs such as P0-A and P1-E. This
script keeps the lightweight coverage contract honest by checking that each
required issue ID appears in an ``AUDIT-COVERS:`` marker somewhere in source or
tests, and that the E2E regression umbrella still declares the complete set.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("src", "tests")
REQUIRED_IDS = {
    *(f"P0-{suffix}" for suffix in "ABCDEF"),
    *(f"P1-{suffix}" for suffix in "ABCDEFG"),
    *(f"P2-{suffix}" for suffix in "ABCDEF"),
}
MARKER_RE = re.compile(r"AUDIT-COVERS:\s*([^\n\r]*)")
ID_RE = re.compile(r"\b(P[0-9]-[A-Z]|SR-[0-9]+|P[0-9]-T[0-9]{2})\b")


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SCAN_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".html", ".js", ".css"}:
                files.append(path)
    return sorted(files)


def _markers_by_id() -> dict[str, set[Path]]:
    found: dict[str, set[Path]] = {}
    for path in _iter_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in MARKER_RE.findall(text):
            for audit_id in ID_RE.findall(marker):
                found.setdefault(audit_id, set()).add(path)
    return found


def main() -> int:
    found = _markers_by_id()
    missing = sorted(REQUIRED_IDS - set(found))
    umbrella = ROOT / "tests" / "e2e" / "test_regression_audit.py"
    umbrella_text = (
        umbrella.read_text(encoding="utf-8", errors="ignore") if umbrella.exists() else ""
    )
    umbrella_missing = sorted(
        audit_id
        for audit_id in REQUIRED_IDS
        if audit_id not in set(ID_RE.findall(" ".join(MARKER_RE.findall(umbrella_text))))
    )

    if missing or umbrella_missing:
        print("Audit coverage verification failed.")
        if missing:
            print("Missing AUDIT-COVERS markers:")
            for audit_id in missing:
                print(f"  - {audit_id}")
        if umbrella_missing:
            print("Missing E2E umbrella markers:")
            for audit_id in umbrella_missing:
                print(f"  - {audit_id}")
        return 1

    print(f"Audit coverage verified: {len(REQUIRED_IDS)} required IDs covered.")
    for audit_id in sorted(REQUIRED_IDS):
        paths = ", ".join(str(path.relative_to(ROOT)) for path in sorted(found[audit_id]))
        print(f"  {audit_id}: {paths}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
