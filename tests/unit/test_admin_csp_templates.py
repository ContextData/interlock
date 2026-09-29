from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path("src/interlock/admin/templates")
INLINE_HANDLER_MARKERS = (
    "onclick=",
    "onchange=",
    "onsubmit=",
    "oninput=",
    "onload=",
    "onerror=",
)


def test_admin_templates_do_not_use_inline_event_handlers() -> None:
    offenders: list[str] = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for marker in INLINE_HANDLER_MARKERS:
            if marker in text:
                offenders.append(f"{path}:{marker}")

    assert offenders == []


def test_admin_templates_do_not_use_inline_styles() -> None:
    offenders: list[str] = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if "style=" in text:
            offenders.append(str(path))

    assert offenders == []


def test_admin_templates_only_load_static_script_tags_from_base() -> None:
    offenders: list[str] = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if "<script" not in text:
            continue
        if path.name == "base.html":
            continue
        offenders.append(str(path))

    assert offenders == []


def test_base_disables_htmx_inline_style_and_script_eval() -> None:
    text = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")

    assert '"includeIndicatorStyles": false' in text
    assert '"allowScriptTags": false' in text
    assert '"allowEval": false' in text
