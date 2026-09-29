#!/usr/bin/env python3
"""Run the incremental V1 strict type gate.

Every Python file in the configured runtime areas is checked. Existing debt is
recorded as exact file/error-code/message counts; line numbers are deliberately
excluded so harmless edits do not churn the baseline. New diagnostics, increased
counts, and stale diagnostics all fail the check.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "typecheck" / "v1_targets.toml"
ERROR_PATTERN = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): error: (?P<message>.+?)  \[(?P<code>[^]]+)\]$"
)


@dataclass(frozen=True, order=True)
class Diagnostic:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class Area:
    name: str
    paths: tuple[str, ...]
    strict_files: frozenset[str]
    max_baseline_errors: int


@dataclass(frozen=True)
class Manifest:
    config: Path
    baseline: Path
    areas: tuple[Area, ...]

    @property
    def strict_files(self) -> frozenset[str]:
        return frozenset(path for area in self.areas for path in area.strict_files)

    @property
    def target_files(self) -> tuple[str, ...]:
        files: set[str] = set()
        for area in self.areas:
            for raw_path in area.paths:
                path = ROOT / raw_path
                if path.is_dir():
                    files.update(item.relative_to(ROOT).as_posix() for item in path.rglob("*.py"))
                elif path.suffix == ".py":
                    files.add(path.relative_to(ROOT).as_posix())
        return tuple(sorted(files))


def _load_manifest(path: Path) -> Manifest:
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if payload.get("schema_version") != 1:
        raise ValueError(f"Unsupported manifest schema in {path}")

    areas = tuple(
        Area(
            name=str(area["name"]),
            paths=tuple(str(item) for item in area["paths"]),
            strict_files=frozenset(str(item) for item in area["strict_files"]),
            max_baseline_errors=int(area["max_baseline_errors"]),
        )
        for area in payload["areas"]
    )
    manifest = Manifest(
        config=ROOT / str(payload["config"]),
        baseline=ROOT / str(payload["baseline"]),
        areas=areas,
    )
    targets = set(manifest.target_files)
    missing = manifest.strict_files - targets
    if missing:
        raise ValueError(f"Strict files are outside the configured targets: {sorted(missing)}")
    return manifest


def _normalize_path(raw_path: str) -> str:
    path = Path(raw_path)
    if path.is_absolute():
        return path.relative_to(ROOT).as_posix()
    return path.as_posix()


def _parse_diagnostics(output: str) -> tuple[Counter[Diagnostic], list[str]]:
    diagnostics: Counter[Diagnostic] = Counter()
    malformed_errors: list[str] = []
    for line in output.splitlines():
        match = ERROR_PATTERN.match(line)
        if match:
            diagnostics[
                Diagnostic(
                    path=_normalize_path(match.group("path")),
                    code=match.group("code"),
                    message=match.group("message"),
                )
            ] += 1
        elif ": error:" in line:
            malformed_errors.append(line)
    return diagnostics, malformed_errors


def _load_baseline(path: Path) -> Counter[Diagnostic]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"Unsupported baseline schema in {path}")
    return Counter(
        {
            Diagnostic(
                path=str(item["path"]),
                code=str(item["code"]),
                message=str(item["message"]),
            ): int(item["count"])
            for item in payload["errors"]
        }
    )


def _write_baseline(path: Path, diagnostics: Counter[Diagnostic]) -> None:
    entries: list[dict[str, Any]] = []
    for diagnostic in sorted(diagnostics):
        entries.append(
            {
                "path": diagnostic.path,
                "code": diagnostic.code,
                "message": diagnostic.message,
                "count": diagnostics[diagnostic],
            }
        )
    payload = {"schema_version": 1, "errors": entries}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _format_counts(title: str, counts: Counter[Diagnostic]) -> Iterable[str]:
    if not counts:
        return ()
    lines = [title]
    for diagnostic in sorted(counts):
        lines.append(
            f"  {diagnostic.path} [{diagnostic.code}] x{counts[diagnostic]}: "
            f"{diagnostic.message}"
        )
    return lines


def _run_mypy(manifest: Manifest) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(manifest.config),
        *manifest.target_files,
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _area_for_path(manifest: Manifest, path: str) -> str:
    for area in manifest.areas:
        if path in area.strict_files:
            return area.name
        for configured in area.paths:
            if path == configured or path.startswith(configured.rstrip("/") + "/"):
                return area.name
    return "untracked"


def _baseline_counts_by_area(manifest: Manifest, diagnostics: Counter[Diagnostic]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for diagnostic, count in diagnostics.items():
        counts[_area_for_path(manifest, diagnostic.path)] += count
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Replace the baseline after reviewing current diagnostics.",
    )
    parser.add_argument("--list-targets", action="store_true")
    args = parser.parse_args()

    manifest = _load_manifest(args.manifest.resolve())
    if args.list_targets:
        for path in manifest.target_files:
            mode = "strict" if path in manifest.strict_files else "baseline"
            print(f"{_area_for_path(manifest, path):18} {mode:8} {path}")
        return 0

    result = _run_mypy(manifest)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    diagnostics, malformed_errors = _parse_diagnostics(result.stdout)
    if result.returncode not in {0, 1}:
        print(result.stdout, file=sys.stderr, end="")
        print(f"mypy failed with exit code {result.returncode}", file=sys.stderr)
        return 2
    if malformed_errors:
        print("Unparseable mypy errors; refusing to weaken the baseline:", file=sys.stderr)
        print("\n".join(malformed_errors), file=sys.stderr)
        return 2

    targets = set(manifest.target_files)
    outside = Counter(
        {item: count for item, count in diagnostics.items() if item.path not in targets}
    )
    strict = Counter(
        {item: count for item, count in diagnostics.items() if item.path in manifest.strict_files}
    )
    baseline_diagnostics = diagnostics - strict - outside
    if outside or strict:
        for line in _format_counts("Diagnostics outside the V1 target set:", outside):
            print(line, file=sys.stderr)
        for line in _format_counts("Regressions in strict V1 files:", strict):
            print(line, file=sys.stderr)
        return 1

    area_counts = _baseline_counts_by_area(manifest, baseline_diagnostics)
    over_budget = {
        area.name: (area_counts[area.name], area.max_baseline_errors)
        for area in manifest.areas
        if area_counts[area.name] > area.max_baseline_errors
    }
    if over_budget:
        for name, (actual, maximum) in sorted(over_budget.items()):
            print(
                f"{name} baseline debt is {actual}, above its ratchet ceiling of {maximum}.",
                file=sys.stderr,
            )
        return 1

    if args.update_baseline:
        _write_baseline(manifest.baseline, baseline_diagnostics)
        print(
            f"Updated {manifest.baseline.relative_to(ROOT)} with "
            f"{sum(baseline_diagnostics.values())} diagnostics."
        )
        return 0

    expected = _load_baseline(manifest.baseline)
    new = baseline_diagnostics - expected
    resolved = expected - baseline_diagnostics
    if new or resolved:
        for line in _format_counts("New V1 type errors:", new):
            print(line, file=sys.stderr)
        for line in _format_counts(
            "Resolved errors that must be removed from the baseline:", resolved
        ):
            print(line, file=sys.stderr)
        return 1

    summary = ", ".join(f"{name}={area_counts[name]}" for name in sorted(area_counts))
    print(
        f"V1 type assurance passed: {len(manifest.target_files)} files checked, "
        f"{len(manifest.strict_files)} strict-clean, "
        f"{sum(baseline_diagnostics.values())} pinned diagnostics"
        + (f" ({summary})" if summary else "")
        + "."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
