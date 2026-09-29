"""Live tests must never be reachable from a gate.

Everything under `tests/live/` talks to real production systems: a customer's
managed databases, an S3 bucket, a Slack workspace, a Google tenant. Two
switches guard them - the `live` marker and `INTERLOCK_LIVE=1` - but switches
are only as good as the commands nobody accidentally wires them into.

So this file reads the Makefile and asserts the isolation structurally. It
runs in the default unit suite, needs no credentials, and fails the build the
moment a gate could reach a live test.

The one that matters most is `audit-mutations`. That runner deliberately
patches governance controls out of `src/` - making the source-role evaluator
return allow, making redaction a no-op - and reruns the suite to check
something objects. Doing that with a gateway pointed at production means
running a knowingly ungoverned proxy against real data.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _ROOT / "Makefile"

# Targets that must never reach a live test, directly or through a dependency.
GATED_TARGETS = (
    "test-unit",
    "test-integration",
    "test-e2e",
    "load",
    "check",
    "check-quick",
    "final-boss-local",
    "audit-mutations",
)

# Targets that are allowed to run live tests. Anything else invoking pytest
# against tests/live is a mistake.
LIVE_TARGETS = ("test-live-upstream", "test-live-governed", "live-certify")


def _makefile() -> str:
    return _MAKEFILE.read_text()


def _targets(text: str) -> dict[str, str]:
    """Map target name -> its recipe body, including its declared prerequisites."""
    targets: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^([a-zA-Z0-9_.-]+):(.*)$", line)
        if match and not line.startswith("\t"):
            if current:
                targets[current] = "\n".join(body)
            current = match.group(1)
            body = [match.group(2)]
        elif current and (line.startswith("\t") or not line.strip()):
            body.append(line)
        elif current and not line.startswith("\t") and line.strip():
            targets[current] = "\n".join(body)
            current = None
            body = []
    if current:
        targets[current] = "\n".join(body)
    return targets


def _reachable(targets: dict[str, str], name: str, seen: set[str] | None = None) -> set[str]:
    """Every target reachable from `name`, following prerequisites and $(MAKE)."""
    seen = seen if seen is not None else set()
    if name in seen or name not in targets:
        return seen
    seen.add(name)
    body = targets[name]
    first_line = body.splitlines()[0] if body else ""
    prerequisites = [p for p in first_line.split() if p in targets]
    submakes = re.findall(r"\$\(MAKE\)\s+([a-zA-Z0-9_.-]+)", body)
    for child in [*prerequisites, *submakes]:
        _reachable(targets, child, seen)
    return seen


def test_the_makefile_is_readable() -> None:
    assert _MAKEFILE.is_file(), "Makefile not found; the other assertions would vacuously pass"


@pytest.mark.parametrize("target", GATED_TARGETS)
def test_no_gate_can_reach_a_live_test(target: str) -> None:
    """Structural, not textual: follows prerequisites and $(MAKE) recursively.

    A grep for 'live' in one recipe would miss a gate that gained a live
    target two dependency hops away.
    """
    targets = _targets(_makefile())
    assert target in targets, f"{target} is no longer a Makefile target; update this test"

    reachable = _reachable(targets, target)
    offenders = sorted(reachable & set(LIVE_TARGETS))
    assert not offenders, (
        f"`make {target}` can reach live target(s) {offenders}, which run against "
        "real production systems. Gates must never depend on them."
    )

    # Also catch a gate that invokes pytest against tests/live directly.
    for name in reachable:
        body = targets.get(name, "")
        for line in body.splitlines():
            if "pytest" in line and "tests/live" in line:
                pytest.fail(
                    f"`make {target}` reaches `{name}`, which runs pytest against "
                    f"tests/live directly: {line.strip()}"
                )


@pytest.mark.parametrize("target", GATED_TARGETS)
def test_no_gate_sets_the_live_environment_switch(target: str) -> None:
    targets = _targets(_makefile())
    reachable = _reachable(targets, target)
    for name in reachable:
        assert "INTERLOCK_LIVE=1" not in targets.get(
            name, ""
        ), f"`make {target}` reaches `{name}`, which sets INTERLOCK_LIVE=1"


def test_every_default_pytest_invocation_excludes_the_live_marker() -> None:
    """A gate that ran `pytest tests/` bare would collect live tests.

    They would still skip, because the conftest requires INTERLOCK_LIVE - but
    relying on one switch when two were designed is how the second one rots.
    """
    targets = _targets(_makefile())
    gated: set[str] = set()
    for target in GATED_TARGETS:
        gated |= _reachable(targets, target)

    unguarded: list[str] = []
    for name in sorted(gated):
        for line in targets.get(name, "").splitlines():
            if "pytest" not in line:
                continue
            selects_a_directory = re.search(r"tests/\w+", line)
            excludes_live = "not live" in line
            if selects_a_directory and not excludes_live:
                # Directory-scoped runs outside tests/live cannot collect live
                # tests, so they need no marker exclusion.
                if "tests/live" in line:
                    unguarded.append(f"{name}: {line.strip()}")
            elif not selects_a_directory and not excludes_live:
                unguarded.append(f"{name}: {line.strip()}")

    assert not unguarded, (
        "pytest invocation(s) reachable from a gate neither scope to a non-live "
        f"directory nor exclude the live marker: {unguarded}"
    )


def test_the_mutation_runner_refuses_to_start_against_live_systems() -> None:
    """The mutation runner disables governance on purpose.

    It patches the source-role evaluator to allow and redaction to a no-op,
    then reruns the suite. Pointed at production, that is a knowingly
    ungoverned proxy in front of real customer data.

    The refusal is *executed*, not grepped for. An earlier version of this test
    asserted only that the string "INTERLOCK_LIVE" appeared in the file, which
    would have passed on a comment mentioning it - a test that cannot fail, and
    the exact pattern the audit this runner belongs to was commissioned to
    eliminate. It duly passed while the guard was crashing on a missing import.
    """
    import os
    import subprocess

    runner = _ROOT / "tools" / "audit" / "mutate.py"
    assert runner.is_file(), "tools/audit/mutate.py not found; update this test"

    refused = subprocess.run(
        [sys.executable, str(runner), "--list"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "INTERLOCK_LIVE": "1"},
        check=False,
    )
    assert refused.returncode != 0, (
        "the mutation runner started with INTERLOCK_LIVE=1. It disables governance "
        "controls on purpose and must never run against production systems."
    )
    assert "refusing to run" in (refused.stderr + refused.stdout).lower()

    # And it must still work normally, or the guard would be a denial of service
    # on the release gate rather than a safety measure.
    allowed = subprocess.run(
        [sys.executable, str(runner), "--list"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "INTERLOCK_LIVE"},
        check=False,
    )
    assert (
        allowed.returncode == 0
    ), f"the mutation runner fails without INTERLOCK_LIVE: {allowed.stderr[-400:]}"


def test_live_targets_exist_and_are_declared_phony() -> None:
    """A live target shadowed by a same-named file would silently not run."""
    text = _makefile()
    targets = _targets(text)
    phony = set(re.findall(r"^\.PHONY:(.*)$", text, re.MULTILINE)[0].split())

    for name in LIVE_TARGETS:
        assert name in targets, f"{name} is missing from the Makefile"
        assert name in phony, f"{name} is not declared .PHONY"
