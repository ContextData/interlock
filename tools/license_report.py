"""Licence inventory of exactly what the published image installs.

Installs the hash-pinned `requirements-production.txt` into a scratch virtual
environment with no dependency resolution, reads each distribution's licence
metadata, and writes `licenses.json` and `licenses.md` to the release-evidence
directory. It fails when any package's licence is outside the allowlist below,
so a dependency bump that brings in a new licence family stops the release
rather than slipping into the image.

The allowlist is the position NOTICE describes: permissive licences, plus
LGPL-3.0 for the two named repository connectors and MPL-2.0 (weak,
file-level copyleft) for the named packages.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PERMISSIVE = {
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD",
    "CC0-1.0",
    "CNRI-Python",
    "HPND",
    "ISC",
    "MIT",
    "MIT-CMU",
    "PSF-2.0",
    "Python-2.0",
    "Unlicense",
    "Zlib",
}
# Copyleft accepted only for these packages, and only in these families.
NAMED_EXCEPTIONS = {
    "pygithub": "LGPL-3.0",
    "python-gitlab": "LGPL-3.0",
    "certifi": "MPL-2.0",
    "tqdm": "MPL-2.0",
    "orjson": "MPL-2.0",
}

_CLASSIFIERS = {
    "Apache Software License": "Apache-2.0",
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT",
    "BSD License": "BSD",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU Library or Lesser General Public License (LGPL)": "LGPL-3.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "Historical Permission Notice and Disclaimer (HPND)": "HPND",
    "zlib/libpng License": "Zlib",
}
_FREE_TEXT = [
    (r"\bapache\b.*2", "Apache-2.0"),
    (r"^mit\b|\bmit license\b", "MIT"),
    (r"bsd.?3|new bsd|modified bsd", "BSD-3-Clause"),
    (r"bsd.?2|simplified bsd", "BSD-2-Clause"),
    (r"\bbsd\b", "BSD"),
    (r"mpl.?2|mozilla public license 2", "MPL-2.0"),
    (r"lgpl.?v?3|lesser general public license v3", "LGPL-3.0"),
    (r"\bisc\b", "ISC"),
    (r"\bpsf\b|python software foundation", "PSF-2.0"),
    (r"unlicense", "Unlicense"),
    (r"\bhpnd\b", "HPND"),
]

_DUMP = r"""
import json
from importlib.metadata import distributions
rows = []
for dist in distributions():
    meta = dist.metadata
    rows.append({
        "name": meta["Name"],
        "version": dist.version,
        "expression": meta.get("License-Expression") or "",
        "license": (meta.get("License") or "").strip().splitlines()[0][:200]
        if meta.get("License") else "",
        "classifiers": [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")],
    })
print(json.dumps(rows))
"""


def _families(row: dict[str, object]) -> set[str]:
    """The licence families a distribution declares, most specific source first."""
    expression = str(row.get("expression") or "")
    if expression:
        parts = re.split(r"\s+(?:OR|AND|WITH)\s+|[()]", expression)
        return {
            p.strip().removesuffix("-only").removesuffix("-or-later") for p in parts if p.strip()
        }
    found = set()
    for classifier in row.get("classifiers") or []:
        label = str(classifier).split("::")[-1].strip()
        if label in _CLASSIFIERS:
            found.add(_CLASSIFIERS[label])
    if found:
        return found
    text = str(row.get("license") or "").lower()
    for pattern, family in _FREE_TEXT:
        if re.search(pattern, text):
            return {family}
    return {"UNKNOWN"}


def _is_allowed(name: str, families: set[str]) -> bool:
    """Allowed when every declared family is permissive, or when the only
    non-permissive family is the one this package is a named exception for.

    Deliberately stricter than reading `OR` as a choice: expression parsing
    does not distinguish `OR` from `AND` here, so anything unusual is refused
    for a person to read rather than waved through.
    """
    other = families - PERMISSIVE
    if not other:
        return True
    exception = NAMED_EXCEPTIONS.get(name.lower())
    return exception is not None and other == {exception}


def _install(requirements: Path, venv: Path) -> Path:
    if venv.exists():
        shutil.rmtree(venv)
    subprocess.run(["uv", "venv", "--quiet", "--python", "3.12", str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--no-deps",
            "--python",
            str(python),
            "-r",
            str(requirements),
        ],
        check=True,
    )
    return python


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", default=str(ROOT / "requirements-production.txt"))
    parser.add_argument("--out", default=str(ROOT / "build" / "release-evidence"))
    parser.add_argument("--venv", default=str(ROOT / "build" / "license-venv"))
    args = parser.parse_args()

    python = _install(Path(args.requirements), Path(args.venv))
    raw = subprocess.run([str(python), "-c", _DUMP], check=True, capture_output=True, text=True)
    rows = sorted(json.loads(raw.stdout), key=lambda r: str(r["name"]).lower())

    report = []
    refused = []
    for row in rows:
        families = _families(row)
        allowed = _is_allowed(str(row["name"]), families)
        entry = {
            "name": row["name"],
            "version": row["version"],
            "licenses": sorted(families),
            "allowed": allowed,
        }
        report.append(entry)
        if not allowed:
            refused.append(entry)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "licenses.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Licences of the published image",
        "",
        f"{len(report)} distributions from `requirements-production.txt`.",
        "",
        "| Package | Version | Licence |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {e['name']} | {e['version']} | {' OR '.join(e['licenses'])} |" for e in report]
    (out / "licenses.md").write_text("\n".join(lines) + "\n")

    if refused:
        for entry in refused:
            print(
                f"licence not allowed: {entry['name']} {entry['version']} {entry['licenses']}",
                file=sys.stderr,
            )
        return 1
    print(f"licence report: {len(report)} distributions, all allowed -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
