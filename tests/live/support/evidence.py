"""Structured evidence emitted by live tests, collated into the report.

A test records what it proved; it does not decide whether it passed. The
verdict a test *claims* is reconciled against the outcome pytest *observed*
by a hook in `tests/live/conftest.py`, and the weaker of the two wins. Without
that reconciliation `record()` would be one more self-reported status - the
exact shape of assertion the governance audit was commissioned to eliminate.

Redaction happens here, at capture, rather than at write time. An unredacted
value is therefore never held in memory beyond the call that produced it, so
a crash, a traceback, or a debugger session cannot surface one from the
accumulated evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from tests.live.support.credentials import Redactor

__all__ = ["Control", "Evidence", "Verdict", "record", "reset", "records", "write_json"]


class Control(StrEnum):
    """The governance controls a live system can be certified for."""

    UPSTREAM = "upstream_reachable_and_privileges_correct"
    ROLE_ALLOW = "source_role_grants_access"
    ROLE_DENY = "source_role_denies_access"
    POLICY_DENY = "policy_denies_access"
    REDACTION = "pii_is_redacted_before_the_caller"
    WRITE_SAFETY = "an_approved_write_reaches_the_upstream"
    APPROVAL_GATING = "an_unapproved_write_never_reaches_the_upstream"
    AUDIT = "the_audit_row_matches_what_happened"
    CACHE_ISOLATION = "cached_answers_do_not_cross_identities"


class Verdict(StrEnum):
    """Ordered worst to best; `min()` over the members is meaningful."""

    FAIL = "FAIL"
    UNPROVEN = "UNPROVEN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PASS = "PASS"


_ORDER = {
    Verdict.FAIL: 0,
    Verdict.UNPROVEN: 1,
    Verdict.NOT_APPLICABLE: 2,
    Verdict.PASS: 3,
}


def weaker(left: Verdict, right: Verdict) -> Verdict:
    return left if _ORDER[left] <= _ORDER[right] else right


@dataclass
class Evidence:
    system: str
    control: Control
    verdict: Verdict
    detail: str = ""
    nodeid: str = ""
    ground_truth: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "control": str(self.control),
            "verdict": str(self.verdict),
            "detail": self.detail,
            "nodeid": self.nodeid,
            "ground_truth": self.ground_truth,
        }


_RECORDS: list[Evidence] = []
_REDACTOR: Redactor | None = None


def configure(redactor: Redactor) -> None:
    global _REDACTOR
    _REDACTOR = redactor


def reset() -> None:
    _RECORDS.clear()


def records() -> list[Evidence]:
    return list(_RECORDS)


def record(
    system: str,
    control: Control,
    verdict: Verdict,
    *,
    detail: str = "",
    **ground_truth: Any,
) -> Evidence:
    """Capture one certified control, redacting as it is captured.

    `ground_truth` is for what the independent reader saw - the row that was
    or was not there, the object key that survived. It is the part of the
    report that distinguishes a proof from a claim.
    """
    redactor = _REDACTOR or Redactor([])
    entry = Evidence(
        system=system,
        control=control,
        verdict=verdict,
        detail=redactor.text(detail) if detail else "",
        ground_truth=redactor.obj(ground_truth),
    )
    _RECORDS.append(entry)
    return entry


def write_json(path: Path, *, run_id: str, redactor: Redactor) -> Path:
    """Persist the run's evidence. Redaction is applied a second time here.

    Belt and braces: `record()` already redacted, but the report is the
    artifact that leaves the machine, so it is scrubbed again on the way out.
    """
    payload = {
        "run_id": run_id,
        "records": [entry.as_dict() for entry in _RECORDS],
    }
    text = redactor.text(json.dumps(payload, indent=2, sort_keys=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
