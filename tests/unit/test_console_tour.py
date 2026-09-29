"""The capture tool must not be able to write a secret into an image.

Its whole reason to exist as a separate tool is that a screenshot of an admin
console is a plausible way to leak an API key into documentation. So the
interesting behaviour is not that it captures pages - it is that it refuses to
capture one that fails the secret scan, and that a page it cannot scan is
treated as unsafe rather than assumed clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.capture.console_tour import (
    _routes,
    _safe_shot,
    _secret_values,
    all_secrets,
    main,
    parse_args,
)


class _Page:
    """Minimal stand-in: records whether a screenshot was taken."""

    def __init__(self, dom: str) -> None:
        self.dom = dom
        self.shots: list[str] = []

    def evaluate(self, _script: str) -> dict[str, object]:
        return {"html": self.dom, "text": self.dom, "values": []}

    def screenshot(self, path: str, full_page: bool = False) -> None:
        del full_page
        self.shots.append(path)


class TestSecretGate:
    def test_a_clean_page_is_captured(self, tmp_path: Path) -> None:
        page = _Page("<html><body>Overview</body></html>")

        assert _safe_shot(page, tmp_path, "overview", ("super-secret-key",)) is True
        assert len(page.shots) == 1

    def test_a_page_containing_a_secret_is_never_written(self, tmp_path: Path) -> None:
        """The failure mode this tool exists to prevent."""
        page = _Page("<html><body>key: super-secret-key</body></html>")

        assert _safe_shot(page, tmp_path, "identities", ("super-secret-key",)) is False
        assert page.shots == []
        assert not list(tmp_path.glob("*.png"))

    def test_the_admin_password_is_always_treated_as_a_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even when the operator forgets to list it in --secret-env."""
        monkeypatch.setenv("OTHER_KEY", "some-agent-key")

        secrets = all_secrets("hunter2-admin-password", "OTHER_KEY")

        assert "hunter2-admin-password" in secrets
        assert "some-agent-key" in secrets

    def test_the_password_is_not_duplicated_when_it_was_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PW", "hunter2-admin-password")

        assert all_secrets("hunter2-admin-password", "PW") == ("hunter2-admin-password",)

    def test_missing_password_refuses_to_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TOUR_ABSENT", raising=False)

        assert (
            main(
                [
                    "--base-url",
                    "https://admin.example.com",
                    "--password-env",
                    "TOUR_ABSENT",
                    "--out",
                    str(tmp_path),
                ]
            )
            == 2
        )

    def test_secret_env_reads_names_not_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Values are never passed on the command line, where they would be
        visible in shell history and the process table."""
        monkeypatch.setenv("A_KEY", "value-a")
        monkeypatch.delenv("B_KEY", raising=False)

        assert _secret_values("A_KEY,B_KEY, ") == ("value-a",)


class TestRouteSelection:
    def test_the_fixture_source_is_replaced_with_a_real_one(self) -> None:
        paths = {name: path for name, path, _ in _routes("sales_pg")}

        assert paths["source-detail"] == "/dashboard/data-sources/sales_pg"
        assert "e2e_pg" not in " ".join(paths.values())

    def test_every_primary_route_is_still_covered(self) -> None:
        """Narrative order must not silently drop a page."""
        from tests.browser.support import PRIMARY_ADMIN_ROUTES

        assert {name for name, _, _ in _routes("")} == {name for name, _, _ in PRIMARY_ADMIN_ROUTES}

    def test_defaults_cover_both_themes_and_both_viewports(self) -> None:
        args = parse_args(["--base-url", "https://admin.example.com"])

        assert args.themes == "light,dark"
        assert args.viewports == "1440x1000,390x844"


def test_a_manifest_names_each_scene_once_and_rejects_bad_paths(tmp_path: Path) -> None:
    from tools.capture.console_tour import load_manifest

    good = tmp_path / "scenes.yaml"
    good.write_text(
        "scenes:\n"
        "  - name: overview\n    path: /dashboard/overview\n"
        "  - name: sources\n    path: /dashboard/data-sources\n"
        "    viewport: 390x844\n    masks: ['.muted']\n"
    )
    scenes = load_manifest(good)
    assert [s.name for s in scenes] == ["overview", "sources"]
    assert scenes[1].viewport == (390, 844) and scenes[1].masks == (".muted",)

    for body, message in (
        ("scenes:\n  - {name: a, path: /x}\n  - {name: a, path: /y}\n", "duplicate"),
        ("scenes:\n  - {name: a, path: dashboard}\n", "must start with /"),
        ("scenes: []\n", "no scenes"),
    ):
        bad = tmp_path / "bad.yaml"
        bad.write_text(body)
        with pytest.raises(ValueError, match=message):
            load_manifest(bad)
