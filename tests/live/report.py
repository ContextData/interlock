"""Project the certification evidence into a redacted markdown report.

This module runs nothing. It reads the JSON that a live run emitted and the
matrix pinned in `test_certification_matrix.py`, and writes a document. That
separation is deliberate: the report has no independent existence, so it
cannot claim a control that no test exercised, and every row it prints cites
the pytest nodeid that produced it.

Redaction is applied again here even though `evidence.record` already
redacted at capture. This is the artifact that leaves the machine, and the
writer refuses to emit at all if `assert_no_secrets` finds residue - a report
that might contain a credential is worth less than no report.

    uv run python -m tests.live.report
    uv run python -m tests.live.report --json build/certification/<run>.json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from tests.live.support.credentials import Redactor, assert_no_secrets, load_credentials

REPORT_DIR = Path("build/certification")

_VERDICT_MARK = {
    "PASS": "pass",
    "FAIL": "**FAIL**",
    "NOT_APPLICABLE": "n/a",
    "UNPROVEN": "unproven",
}


def _latest_json() -> Path | None:
    candidates = sorted(glob.glob(str(REPORT_DIR / "*-live-certification.json")))
    return Path(candidates[-1]) if candidates else None


def _load_matrix() -> tuple[dict, tuple[str, ...], dict]:
    from tests.live.test_certification_matrix import ENVIRONMENT_LIMITS, MATRIX, SYSTEMS

    return MATRIX, SYSTEMS, ENVIRONMENT_LIMITS


def _observed(records: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group evidence by (system, control).

    Systems are recorded under both their bare name (Tier 0, e.g. "mysql") and
    their source id (Tier 1, e.g. "live_cert_mysql"), so both are folded onto
    the bare name the matrix uses.
    """
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        system = str(record["system"]).removeprefix("live_cert_")
        system = "postgresql" if system == "postgres" else system
        grouped[(system, str(record["control"]))].append(record)
    return grouped


def _render(payload: dict[str, Any], redactor: Redactor) -> str:
    matrix, systems, limits = _load_matrix()
    records = payload.get("records", [])
    observed = _observed(records)

    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[str(record["verdict"])] += 1

    lines: list[str] = []
    lines.append("# InterLock Live Certification")
    lines.append("")
    lines.append(f"Run `{payload.get('run_id', 'unknown')}`.")
    lines.append("")
    lines.append(
        "Certification of governance against **real** upstream systems. Every "
        "assertion behind this report reads ground truth from the upstream with a "
        "different credential than the gateway uses, so no conclusion rests on the "
        "gateway's own report of what it did."
    )
    lines.append("")
    lines.append(
        f"{counts.get('PASS', 0)} passing, {counts.get('FAIL', 0)} failing, "
        f"{counts.get('NOT_APPLICABLE', 0)} not applicable, "
        f"{counts.get('UNPROVEN', 0)} unproven, across {len(records)} recorded checks."
    )
    lines.append("")

    # -- findings first, because a report that buries them is decoration ----
    failures = [r for r in records if r["verdict"] == "FAIL"]
    if failures:
        lines.append("## Failing controls")
        lines.append("")
        lines.append(
            "Stated before the successes. A certification whose failures are found "
            "at the bottom of a table has already misled its reader."
        )
        lines.append("")
        for record in failures:
            lines.append(f"### {record['system']} - {record['control']}")
            lines.append("")
            lines.append(redactor.text(record.get("detail", "")))
            lines.append("")
            lines.append(f"Evidence: `{record.get('nodeid', 'unknown')}`")
            lines.append("")
            ground = record.get("ground_truth") or {}
            if ground:
                lines.append("```json")
                lines.append(redactor.text(json.dumps(ground, indent=2, sort_keys=True)))
                lines.append("```")
                lines.append("")

    # -- the matrix ---------------------------------------------------------
    lines.append("## Coverage matrix")
    lines.append("")
    lines.append(
        "Declared coverage on the left, what this run actually observed on the "
        "right. A declared claim with no observation means the test did not run - "
        "usually absent credentials."
    )
    lines.append("")
    lines.append("| System | Control | Declared | Observed | Evidence |")
    lines.append("|---|---|---|---|---|")

    for system in systems:
        for (matrix_system, control), (verdict, reference) in sorted(
            matrix.items(), key=lambda item: str(item[0][1])
        ):
            if matrix_system != system:
                continue
            seen = observed.get((system, str(control)), [])
            if seen:
                worst = min(seen, key=lambda r: list(_VERDICT_MARK).index(str(r["verdict"])))
                observed_mark = _VERDICT_MARK.get(str(worst["verdict"]), str(worst["verdict"]))
                nodeid = str(worst.get("nodeid", "")).rsplit("::", 1)[-1]
            else:
                observed_mark = "not run"
                nodeid = ""
            declared = verdict.lower() if verdict != "FAILING" else "**failing**"
            lines.append(
                f"| {system} | {str(control).replace('_', ' ')} | {declared} "
                f"| {observed_mark} | `{nodeid}` |"
            )

    lines.append("")

    # -- stated gaps --------------------------------------------------------
    lines.append("## What is not covered, and why")
    lines.append("")
    lines.append(
        "Every gap carries a reason. A control omitted without one is "
        "indistinguishable from a control nobody thought about."
    )
    lines.append("")
    for (system, control), (verdict, reason) in sorted(
        matrix.items(), key=lambda item: (item[0][0], str(item[0][1]))
    ):
        if verdict in ("NOT_APPLICABLE", "UNPROVEN"):
            lines.append(
                f"- **{system} / {str(control).replace('_', ' ')}** "
                f"({verdict.lower().replace('_', ' ')}): {reason}"
            )
    lines.append("")

    if limits:
        lines.append("### Environment limits")
        lines.append("")
        for system, note in sorted(limits.items()):
            lines.append(f"- **{system}**: {note}")
        lines.append("")

    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append("make live-up        # compose stack with live credentials injected")
    lines.append("make live-certify   # seed, run, report, teardown")
    lines.append("```")
    lines.append("")
    lines.append(
        "Live tests never run in `make check`, `make test-e2e` or "
        "`make final-boss-local`, and `make audit-mutations` refuses to start while "
        "`INTERLOCK_LIVE` is set - it patches governance controls out of the source, "
        "which must never be pointed at production systems."
    )
    lines.append("")

    return "\n".join(lines)


def build(json_path: Path | None = None, output: Path | None = None) -> Path:
    source = json_path or _latest_json()
    if source is None or not source.is_file():
        raise SystemExit(
            f"no certification evidence found in {REPORT_DIR}/. Run `make live-certify` first."
        )

    payload = json.loads(source.read_text())
    credentials = load_credentials()
    redactor = Redactor(credentials.secrets)

    text = redactor.text(_render(payload, redactor))
    # The gate: refuse to write rather than emit a report that might carry a
    # secret or a sensitive hostname.
    assert_no_secrets(text, credentials.secrets)

    destination = output or (REPORT_DIR / f"{payload.get('run_id', 'run')}-live-certification.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="evidence file to project")
    parser.add_argument("--output", type=Path, default=None, help="markdown destination")
    args = parser.parse_args(argv)

    destination = build(args.json, args.output)
    payload = json.loads((args.json or _latest_json()).read_text())
    failures = sum(1 for r in payload.get("records", []) if r["verdict"] == "FAIL")
    print(f"wrote {destination}")
    if failures:
        print(f"{failures} failing control(s) - see the report's first section")
    # Exit code reflects the certification, not the writing of it.
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
