"""Licence and source gate for the documentation site's npm dependencies.

Reads `docs-site/package-lock.json` and fails when a package resolves from
anywhere but the public npm registry, or carries a licence the CI dependency
review denies (the same list as `.github/workflows/ci.yml`). Exceptions live in
`tools/docs/npm-license-exceptions.json`, each with a reason, an owner and an
expiry date, and expire loudly.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
LOCKFILE = ROOT / "docs-site" / "package-lock.json"
EXCEPTIONS = ROOT / "tools" / "docs" / "npm-license-exceptions.json"
REGISTRY = "https://registry.npmjs.org/"


def denied_licenses() -> set[str]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            with_ = step.get("with") or {}
            if "deny-licenses" in with_:
                return {item.strip() for item in str(with_["deny-licenses"]).split(",")}
    raise SystemExit("ci.yml has no deny-licenses policy to mirror")


def _license_ids(value: object) -> set[str]:
    return {part for part in re.split(r"[\s()]+|\bOR\b|\bAND\b", str(value or "")) if part}


def main() -> int:
    lock = json.loads(LOCKFILE.read_text())
    denied = denied_licenses()
    exceptions = json.loads(EXCEPTIONS.read_text()) if EXCEPTIONS.exists() else {}
    today = datetime.now(UTC).date()
    problems: list[str] = []
    for path, meta in sorted((lock.get("packages") or {}).items()):
        if not path:
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        resolved = str(meta.get("resolved") or "")
        if resolved and not resolved.startswith(REGISTRY):
            problems.append(f"{name}: resolved outside the npm registry ({resolved})")
        hits = _license_ids(meta.get("license")) & denied
        if not hits:
            continue
        exception = exceptions.get(name)
        if exception and date.fromisoformat(exception["expires"]) >= today:
            continue
        reason = "exception expired" if exception else "denied licence"
        problems.append(f"{name}: {sorted(hits)} ({reason})")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"npm lockfile: {len(lock.get('packages') or {}) - 1} packages checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
