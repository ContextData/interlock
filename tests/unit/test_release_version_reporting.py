"""The console reports the release it is actually running.

The sidebar carried the literal `v1.0.0-rc.1` from the first release candidate
through rc.6, and `create_app` carried a second copy of the same literal, so a
deployment cheerfully told operators it was five releases older than it was.
These tests pin the single source and the two places that read it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

import interlock
from interlock import release_version
from interlock.admin.app import _install_template_filters, create_app

ROOT = Path(__file__).resolve().parents[2]
BASE_TEMPLATE = ROOT / "src" / "interlock" / "admin" / "templates" / "base.html"
MCP_ADAPTER = ROOT / "src" / "interlock" / "gateway" / "mcp_adapter.py"
# Anything that reads like a release: 1.0.0rc1, 1.0.0-rc.1, v1.2.3.
RELEASE_LITERAL = re.compile(r"\bv?\d+\.\d+\.\d+(?:[-.]?(?:a|b|rc)\.?\d+)?\b")


def test_the_package_version_comes_from_the_distribution_metadata() -> None:
    """Not a literal in `__init__`, which is what drifted from the tag."""
    source = (ROOT / "src" / "interlock" / "__init__.py").read_text()
    assert "_package_version(" in source
    assert '__version__ = "1.0.0' not in source
    assert interlock.__version__ != "0.0.0.dev0", "the package must be installed for this suite"


@pytest.mark.parametrize(
    ("packaged", "expected"),
    (
        ("1.0.0rc6", "1.0.0-rc.6"),
        ("1.0.0rc10", "1.0.0-rc.10"),
        ("2.3.4a1", "2.3.4-a.1"),
        ("2.3.4b2", "2.3.4-b.2"),
        ("1.0.0", "1.0.0"),
        ("1.2.3.dev4", "1.2.3.dev4"),
    ),
)
def test_release_version_restores_the_semver_form(
    monkeypatch: pytest.MonkeyPatch, packaged: str, expected: str
) -> None:
    """Packaging normalises `1.0.0-rc.6` to `1.0.0rc6`; the tag and chart do not.

    `deploy/scripts/release/validate-release-tag.sh` turns `v1.0.0-rc.6` into
    `1.0.0-rc.6`, and the release preflight compares that to this function, so
    the two forms have to agree.
    """
    monkeypatch.setattr(interlock, "__version__", packaged)
    assert release_version() == expected


def test_the_sidebar_renders_the_running_version() -> None:
    templates = Jinja2Templates(directory=str(BASE_TEMPLATE.parent))
    _install_template_filters(templates)

    assert templates.env.globals["interlock_version"] == release_version()

    source = BASE_TEMPLATE.read_text()
    assert '<span class="sidebar-version">v{{ interlock_version }}</span>' in source

    # Render the shell rather than trusting the two halves separately: an
    # unbound Jinja variable renders as an empty string, so a missing global
    # would ship a sidebar reading "v" and every assertion above would pass.
    rendered = templates.env.get_template("base.html").render()
    assert f'<span class="sidebar-version">v{release_version()}</span>' in rendered


def test_the_admin_app_reports_the_packaged_version() -> None:
    app = create_app()
    assert app.version == interlock.__version__


def test_the_mcp_handshake_reports_the_packaged_version() -> None:
    """Agents read this one, so it drifted where nobody was looking.

    `serverInfo.version` is returned on every `initialize` and in the `_meta`
    of every tool result, and it carried the same `1.0.0-rc.1` literal as the
    sidebar. A deployment that tells its agents the wrong version is worse than
    one that tells its operators, because no human is reading it.
    """
    from interlock.gateway.mcp_adapter import _MCP_SERVER_INFO

    assert _MCP_SERVER_INFO["version"] == release_version()


def test_no_second_release_literal_can_drift_from_the_package_version() -> None:
    """One source or none: this is the check the sidebar needed and lacked.

    Scanning only the shell template and the app factory keeps it honest without
    tripping over the contract documents, which cite `1.0.0-rc.1` deliberately -
    that is the release whose public surface they froze.
    """
    for path in (BASE_TEMPLATE, ROOT / "src" / "interlock" / "admin" / "app.py", MCP_ADAPTER):
        offenders = [
            match.group(0)
            for match in RELEASE_LITERAL.finditer(path.read_text())
            # Python's own floor and the OTel/HTTP versions are not releases of
            # this project; only three-part versions at or above 1.0.0 are.
            if match.group(0) not in {"1.1", "2.0"}
        ]
        assert not offenders, f"{path.name} hardcodes a version: {offenders}"
