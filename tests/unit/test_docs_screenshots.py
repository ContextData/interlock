"""Every screenshot a page uses exists in both themes, and none is orphaned."""

from __future__ import annotations

import re

import yaml

from tests.unit.docs_site import SITE, all_pages

SHOTS = SITE / "src" / "assets" / "screenshots"


def _scenes() -> set[str]:
    manifest = yaml.safe_load((SITE / "screenshots.yaml").read_text())
    return {str(scene["name"]) for scene in manifest["scenes"]}


def _referenced() -> set[str]:
    names: set[str] = set()
    for page in all_pages():
        names |= set(re.findall(r'<Screenshot\s+name="([^"]+)"', page.read_text()))
    return names


def test_every_referenced_screenshot_is_a_scene_captured_in_both_themes() -> None:
    scenes = _scenes()
    for name in sorted(_referenced()):
        assert name in scenes, f"{name} is used by a page but not in screenshots.yaml"
        for theme in ("light", "dark"):
            assert (SHOTS / theme / f"{name}.png").is_file(), f"{theme}/{name}.png is missing"


def test_no_scene_or_image_is_orphaned() -> None:
    scenes, referenced = _scenes(), _referenced()
    assert scenes == referenced, (
        f"scenes nobody uses: {sorted(scenes - referenced)}; "
        f"used but not captured: {sorted(referenced - scenes)}"
    )
    on_disk = {p.stem for theme in ("light", "dark") for p in (SHOTS / theme).glob("*.png")}
    assert on_disk == scenes, f"images without a scene: {sorted(on_disk - scenes)}"
