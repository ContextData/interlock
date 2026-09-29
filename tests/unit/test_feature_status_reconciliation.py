"""The feature-honesty contract is enforced, not merely asserted.

`docs-site/src/content/docs/project/release-process.md` says a capability is not advertised unless
`docs-site/src/content/docs/reference/feature-status.md` and `src/interlock/feature_status.py` "both list it as
`Beta` or `Certified` with matching evidence", and the doc names the module as
its source of truth. Nothing checked that.

The two existing suites each assert a hand-picked set of substrings against
their own side - `test_feature_status.py` against the registry,
`test_mvp_status_docs.py` against the markdown - so the pair could drift
without either failing, and it had:

  - `mcp_tools` evidence and limitation described a different tool surface in
    the code than in the doc;
  - `rrf_ranking` limitations disagreed;
  - `deep_pii_scanner` still told operators to install `onyx[pii]`, a package
    name that stopped existing at the rename.

None of that is exotic. It is what a contract with no mechanism behind it does
over time, and it is the exact failure the governance audit was commissioned to
address: a claim that is asserted rather than verified. So this compares the
two representations field by field and fails on any difference.

Added in phase 8 of the internal governance audit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from interlock.feature_status import FEATURE_STATUSES, FeatureStatus

DOC = (
    Path(__file__).resolve().parents[2]
    / "docs-site"
    / "src"
    / "content"
    / "docs"
    / "reference"
    / "feature-status.md"
)


def _normalize(cell: str) -> str:
    """Compare meaning, not markdown.

    The doc marks up identifiers with backticks (`` `sqlglot` ``) that the
    registry stores as plain text. Normalising those away keeps the test
    focused on the claim, so an author can format the doc without the build
    telling them the contract broke.
    """
    return re.sub(r"\s+", " ", cell.replace("`", "")).strip()


def _rows(table_heading: str) -> dict[str, list[str]]:
    """Return {feature label: cells} for the table under a given heading."""
    body = DOC.read_text()
    section = body.split(table_heading, 1)[1]
    # Stop at the next heading so the two tables cannot bleed into each other.
    section = section.split("\n## ", 1)[0]

    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.fullmatch(r"\|[\s:|-]+\|", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or cells[0] in {"Feature", "Module"}:
            continue
        rows[_normalize(cells[0])] = cells
    return rows


def _enabled_rows() -> dict[str, list[str]]:
    return _rows("## Current Public-Beta Capabilities")


def _disabled_rows() -> dict[str, list[str]]:
    return _rows("## Disabled Or Planned Capabilities")


def _doc_rows() -> dict[str, list[str]]:
    return {**_enabled_rows(), **_disabled_rows()}


def _by_label() -> dict[str, FeatureStatus]:
    return {_normalize(feature.label): feature for feature in FEATURE_STATUSES}


class TestEveryCapabilityAppearsInBoth:
    def test_the_parser_finds_both_tables(self) -> None:
        """A silent parse failure would make every assertion below vacuous."""
        assert len(_enabled_rows()) >= 8
        assert len(_disabled_rows()) >= 3

    def test_no_registry_entry_is_missing_from_the_doc(self) -> None:
        missing = sorted(set(_by_label()) - set(_doc_rows()))

        assert not missing, (
            f"declared in feature_status.py but not published: {missing}. "
            "A capability the code advertises and the doc omits is exactly "
            "what the honesty contract exists to prevent."
        )

    def test_no_published_row_is_missing_from_the_registry(self) -> None:
        unknown = sorted(set(_doc_rows()) - set(_by_label()))

        assert not unknown, (
            f"published but not declared in feature_status.py: {unknown}. "
            "The doc names the module as its source of truth, so a row with "
            "no entry behind it is an unbacked claim."
        )

    def test_public_beta_flag_matches_which_table_the_row_is_in(self) -> None:
        enabled, disabled = _enabled_rows(), _disabled_rows()

        for label, feature in _by_label().items():
            if feature.public_beta:
                assert label in enabled, f"{label} is public_beta but listed as disabled/planned"
            else:
                assert label in disabled, f"{label} is not public_beta but listed as enabled"


@pytest.mark.parametrize("feature", FEATURE_STATUSES, ids=lambda f: f.key)
class TestTheTwoRepresentationsAgree:
    def test_the_state_matches(self, feature: FeatureStatus) -> None:
        row = _doc_rows()[_normalize(feature.label)]

        assert _normalize(row[1]).lower() == feature.state

    def test_the_evidence_matches(self, feature: FeatureStatus) -> None:
        """Only the enabled table carries an evidence column of its own.

        The disabled/planned table has a single "why" column, checked against
        the limitation below, because a capability that is off does not have
        evidence for being on.
        """
        if not feature.public_beta:
            pytest.skip("disabled/planned rows publish a reason, not evidence")
        row = _doc_rows()[_normalize(feature.label)]

        assert _normalize(row[2]) == _normalize(feature.evidence)

    def test_the_limitation_matches(self, feature: FeatureStatus) -> None:
        """The two tables carry the limitation differently, on purpose.

        The enabled table has a dedicated limitation column, so it must match
        exactly. The disabled/planned table has a single "why it is not
        enabled" column that reads as one sentence combining the registry's
        evidence and limitation - splitting it into two columns there would
        publish an "evidence" cell for a capability that is switched off.
        So that column is required to carry both halves rather than to equal
        either one, which is the weaker assertion the structure permits.
        """
        row = _doc_rows()[_normalize(feature.label)]

        if feature.public_beta:
            assert _normalize(row[3]) == _normalize(feature.limitation)
            return

        why = _normalize(row[2]).lower()
        for half in (feature.evidence, feature.limitation):
            expected = _normalize(half).lower().rstrip(".")
            assert expected in why, (
                f"the published reason for {feature.label} drops part of the "
                f"registry entry; missing: {expected!r}"
            )


def test_no_capability_references_a_package_name_that_no_longer_exists() -> None:
    """The rename left `onyx[pii]` in an operator-facing install instruction.

    Caught here rather than by a repo-wide grep because this text is read by
    someone trying to turn a capability on: a wrong extra name sends them to a
    package that cannot be installed, and the surrounding sentence looks
    authoritative.
    """
    stale = [
        feature.key
        for feature in FEATURE_STATUSES
        if re.search(r"\bonyx\b", f"{feature.evidence} {feature.limitation}", re.IGNORECASE)
    ]

    assert not stale, f"stale pre-rename package or namespace name in: {stale}"


def test_no_install_hint_names_the_pre_rename_package() -> None:
    """Log lines telling an operator what to install must name a real package.

    The capability text above was fixed while three runtime warnings kept
    saying `install onyx[pii]` and `install onyx[otel]`.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "interlock"
    stale = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(r"\bonyx\[", path.read_text(encoding="utf-8"))
    ]
    assert not stale, f"install hints name the pre-rename package in: {stale}"
