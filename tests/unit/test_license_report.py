"""The licence gate refuses anything outside the position NOTICE states."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "license_report", Path(__file__).resolve().parents[2] / "tools" / "license_report.py"
)
assert _SPEC is not None and _SPEC.loader is not None
license_report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(license_report)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"expression": "MIT"}, {"MIT"}),
        ({"expression": "Apache-2.0 OR MIT"}, {"Apache-2.0", "MIT"}),
        ({"expression": "LGPL-3.0-only"}, {"LGPL-3.0"}),
        ({"classifiers": ["License :: OSI Approved :: BSD License"]}, {"BSD"}),
        ({"license": "Apache 2.0"}, {"Apache-2.0"}),
        ({"license": "Mozilla Public License 2.0"}, {"MPL-2.0"}),
        ({}, {"UNKNOWN"}),
    ],
)
def test_families_are_read_from_expression_classifier_then_text(
    row: dict[str, object], expected: set[str]
) -> None:
    assert license_report._families(row) == expected


@pytest.mark.parametrize(
    ("name", "families", "allowed"),
    [
        ("requests", {"Apache-2.0"}, True),
        ("numpy", {"BSD-3-Clause", "CC0-1.0", "Zlib"}, True),
        ("PyGithub", {"LGPL-3.0"}, True),
        ("certifi", {"MPL-2.0"}, True),
        ("orjson", {"Apache-2.0", "MIT", "MPL-2.0"}, True),
        # Copyleft outside the named exceptions is refused, even beside a
        # permissive option, because OR and AND are not told apart.
        ("some-lib", {"LGPL-3.0"}, False),
        ("some-lib", {"MIT", "GPL-3.0"}, False),
        ("PyGithub", {"GPL-3.0"}, False),
        ("mystery", {"UNKNOWN"}, False),
    ],
)
def test_allow_rule(name: str, families: set[str], allowed: bool) -> None:
    assert license_report._is_allowed(name, families) is allowed
