"""Capture screenshots and video of a running InterLock Admin console.

Intended for documentation and marketing material, so it points at whatever
deployment you give it rather than at a fixture: a compose stack, or a real
cluster. It reuses the browser helpers the certification suite already has -
login, theme pinning, HTMX settling - instead of reimplementing them, so the
pages here are driven the same way the tests drive them.

The reason this is a standalone tool rather than another test: a test's job is
to fail, and it should not also be deciding what a marketing asset looks like.

Nothing is written until it has been checked for secrets. Every capture runs
the same DOM scan the certification suite uses, extended with the values of
whichever environment variables you name with --secret-env, and a page that
fails is skipped with a warning rather than silently saved. A screenshot of an
admin console is exactly the kind of artifact that leaks an API key into a blog
post.

Example:

    export TOUR_ADMIN_PASSWORD=...
    uv run python -m tools.capture.console_tour \\
        --base-url https://admin.example.com \\
        --password-env TOUR_ADMIN_PASSWORD \\
        --source-id sales_pg \\
        --secret-env TOUR_ADMIN_PASSWORD,AGENT_KEY \\
        --out build/console-tour --video

Documentation mode reads named scenes from a manifest instead of walking every
route, and writes `<out>/<theme>/<scene>.png` at the viewport size:

    uv run python -m tools.capture.console_tour \\
        --base-url http://127.0.0.1:9090 --password-env TOUR_ADMIN_PASSWORD \\
        --manifest docs-site/screenshots.yaml \\
        --out docs-site/src/assets/screenshots --viewport-only
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.browser.support import (  # noqa: E402
    FORM_ADMIN_ROUTES,
    PRIMARY_ADMIN_ROUTES,
    assert_no_secret_values,
    login_admin,
    set_theme,
    wait_for_htmx,
)

# The order a reader should meet the product in, rather than the order the
# route table happens to list. Overview first because it is the landing page;
# audit and write-safety last because they are the payoff.
NARRATIVE = (
    "overview",
    "data-sources",
    "connectors",
    "source-detail",
    "source-roles",
    "identities",
    "policies",
    "ingestion",
    "workers",
    "discovery",
    "categories",
    "entities",
    "catalog",
    "proxy",
    "policy-analytics",
    "alerts",
    "audit",
    "write-safety",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", required=True, help="Admin console base URL")
    p.add_argument("--username", default="admin")
    p.add_argument(
        "--password-env",
        default="TOUR_ADMIN_PASSWORD",
        help="Environment variable holding the admin password. The password is "
        "never taken on the command line, where it would reach the shell history "
        "and the process table.",
    )
    p.add_argument("--out", default="build/console-tour", help="Directory to write into")
    p.add_argument(
        "--source-id", default="", help="A real source id, for the detail and roles pages"
    )
    p.add_argument("--themes", default="light,dark")
    p.add_argument("--viewports", default="1440x1000,390x844")
    p.add_argument(
        "--secret-env",
        default="",
        help="Comma-separated environment variable NAMES whose values must never "
        "appear in a capture. Names, not values.",
    )
    p.add_argument("--video", action="store_true", help="Also record flow videos")
    p.add_argument(
        "--manifest",
        default="",
        help="YAML file of named scenes to capture instead of every route (documentation mode)",
    )
    p.add_argument(
        "--viewport-only",
        action="store_true",
        help="Capture the viewport rather than the full scrolling page",
    )
    p.add_argument("--timeout-ms", type=int, default=30000)
    return p.parse_args(argv)


def _routes(source_id: str) -> list[tuple[str, str, str]]:
    """Primary routes in narrative order, with the fixture source substituted."""
    by_name = {name: (name, path, text) for name, path, text in PRIMARY_ADMIN_ROUTES}
    ordered = [by_name[name] for name in NARRATIVE if name in by_name]
    ordered += [row for row in PRIMARY_ADMIN_ROUTES if row[0] not in set(NARRATIVE)]
    if not source_id:
        return ordered
    return [(name, path.replace("e2e_pg", source_id), text) for name, path, text in ordered]


def _secret_values(spec: str) -> tuple[str, ...]:
    values: list[str] = []
    for name in (part.strip() for part in spec.split(",")):
        if not name:
            continue
        value = os.environ.get(name)
        if value:
            values.append(value)
        else:
            print(f"  warning: --secret-env named {name} but it is unset", file=sys.stderr)
    return tuple(values)


def all_secrets(password: str, secret_env_spec: str) -> tuple[str, ...]:
    """Every value that must never appear in a capture.

    The admin password is included whether or not the operator listed it: it
    is the one credential this tool is guaranteed to be holding, because it
    had to sign in with it, and forgetting to name it is the easy mistake.
    """
    values = _secret_values(secret_env_spec)
    if password and password not in values:
        values = values + (password,)
    return values


def _safe_shot(page: Any, out: Path, name: str, secrets: tuple[str, ...]) -> bool:
    """Screenshot only after the page is proven free of the named secrets."""
    try:
        assert_no_secret_values(page, secrets)
    except AssertionError as exc:
        print(f"  SKIPPED {name}: {exc}", file=sys.stderr)
        return False
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f"{name}.png"), full_page=True)
    return True


@dataclass(frozen=True)
class Scene:
    """One documentation screenshot: a page, what must be on it, what to hide."""

    name: str
    path: str
    wait_for: str = ""
    masks: tuple[str, ...] = ()
    viewport: tuple[int, int] = (1440, 900)


# Values that change on every capture and would churn every image in a diff.
DEFAULT_MASKS = ("time", "[data-timestamp]", ".sidebar-version")


def load_manifest(path: Path) -> list[Scene]:
    """Read and validate a scene manifest; a malformed one fails before any capture."""
    data = yaml.safe_load(path.read_text()) or {}
    scenes: list[Scene] = []
    seen: set[str] = set()
    for raw in data.get("scenes") or []:
        name = str(raw["name"])
        if name in seen:
            raise ValueError(f"duplicate scene name: {name}")
        seen.add(name)
        path_value = str(raw["path"])
        if not path_value.startswith("/"):
            raise ValueError(f"scene {name}: path must start with /")
        width, _, height = str(raw.get("viewport", "1440x900")).partition("x")
        scenes.append(
            Scene(
                name=name,
                path=path_value,
                wait_for=str(raw.get("wait_for", "")),
                masks=tuple(str(m) for m in raw.get("masks", ())),
                viewport=(int(width), int(height)),
            )
        )
    if not scenes:
        raise ValueError(f"{path} lists no scenes")
    return scenes


def _stamp(out: Path) -> None:
    """Record when and from which commit the captures were taken."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    out.mkdir(parents=True, exist_ok=True)
    (out / "CAPTURED").write_text(
        f"captured: {datetime.now(UTC).date().isoformat()}\ncommit: {commit}\n"
    )


def run_manifest(
    browser: Any, args: Any, password: str, secrets: tuple[str, ...], out: Path
) -> tuple[int, int]:
    scenes = load_manifest(Path(args.manifest))
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    written = failed = 0
    for theme in themes:
        for scene in scenes:
            ctx = browser.new_context(
                viewport={"width": scene.viewport[0], "height": scene.viewport[1]},
                reduced_motion="reduce",
            )
            ctx.set_default_timeout(args.timeout_ms)
            page = ctx.new_page()
            try:
                set_theme(page, args.base_url, theme)
                login_admin(page, args.base_url, args.username, password)
                page.goto(f"{args.base_url}{scene.path}", wait_until="domcontentloaded")
                wait_for_htmx(page)
                if scene.wait_for:
                    page.wait_for_selector(scene.wait_for)
                assert_no_secret_values(page, secrets)
                target = out / theme
                target.mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(target / f"{scene.name}.png"),
                    full_page=not args.viewport_only,
                    mask=[page.locator(sel) for sel in (*DEFAULT_MASKS, *scene.masks)],
                    mask_color="#8a8f98",
                    animations="disabled",
                )
                written += 1
                print(f"[{theme}] {scene.name}")
            except Exception as exc:  # noqa: BLE001 - every failure fails the run
                failed += 1
                print(f"  FAILED {theme}/{scene.name}: {exc}", file=sys.stderr)
            finally:
                ctx.close()
    _stamp(out)
    return written, failed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    password = os.environ.get(args.password_env, "")
    if not password:
        print(f"error: {args.password_env} is not set", file=sys.stderr)
        return 2

    secrets = all_secrets(password, args.secret_env)

    out = Path(args.out)
    viewports = []
    for spec in args.viewports.split(","):
        w, _, h = spec.strip().partition("x")
        viewports.append((int(w), int(h)))
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]

    from playwright.sync_api import sync_playwright

    written = skipped = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        if args.manifest:
            try:
                written, failed = run_manifest(browser, args, password, secrets, out)
            finally:
                browser.close()
            print(f"\nwrote {written} file(s); {failed} failed")
            return 0 if failed == 0 else 1
        try:
            for width, height in viewports:
                label = "desktop" if width >= 1000 else "mobile"
                for theme in themes:
                    ctx = browser.new_context(
                        viewport={"width": width, "height": height},
                        reduced_motion="reduce",
                    )
                    ctx.set_default_timeout(args.timeout_ms)
                    page = ctx.new_page()
                    set_theme(page, args.base_url, theme)
                    login_admin(page, args.base_url, args.username, password)
                    print(f"[{label}/{theme}] signed in")

                    for name, path, _expected in _routes(args.source_id):
                        page.goto(f"{args.base_url}{path}", wait_until="domcontentloaded")
                        wait_for_htmx(page)
                        if _safe_shot(page, out / label / theme, name, secrets):
                            written += 1
                        else:
                            skipped += 1

                    # Form pages are entry points into a flow; a reader looking
                    # for "how do I add a source" wants to see them.
                    for name, path, _expected in FORM_ADMIN_ROUTES:
                        target = path.replace("e2e_pg", args.source_id) if args.source_id else path
                        page.goto(f"{args.base_url}{target}", wait_until="domcontentloaded")
                        wait_for_htmx(page)
                        if _safe_shot(page, out / label / theme, f"form-{name}", secrets):
                            written += 1
                        else:
                            skipped += 1
                    ctx.close()

            if args.video:
                written += _record_flows(browser, args, password, secrets, out)
        finally:
            browser.close()

    print(f"\nwrote {written} file(s); skipped {skipped} for containing a secret")
    print(f"output: {out.resolve()}")
    return 0 if skipped == 0 else 1


def _record_flows(
    browser: Any, args: Any, password: str, secrets: tuple[str, ...], out: Path
) -> int:
    """Record short videos of the flows a reader most wants to watch."""
    flows = {
        "approval-review": ("/dashboard/write-safety", "/dashboard/audit-costs"),
        "source-registration": ("/dashboard/data-sources", "/dashboard/source-wizard"),
    }
    made = 0
    for flow, paths in flows.items():
        video_dir = out / "video" / flow
        video_dir.mkdir(parents=True, exist_ok=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            reduced_motion="reduce",
            record_video_dir=str(video_dir),
            record_video_size={"width": 1440, "height": 1000},
        )
        ctx.set_default_timeout(args.timeout_ms)
        page = ctx.new_page()
        try:
            login_admin(page, args.base_url, args.username, password)
            for path in paths:
                page.goto(f"{args.base_url}{path}", wait_until="domcontentloaded")
                wait_for_htmx(page)
                try:
                    assert_no_secret_values(page, secrets)
                except AssertionError as exc:
                    print(f"  SKIPPED video {flow}: {exc}", file=sys.stderr)
                    raise
                page.wait_for_timeout(1200)
            made += 1
            print(f"[video] {flow}")
        except AssertionError:
            pass
        finally:
            ctx.close()
    return made


if __name__ == "__main__":
    raise SystemExit(main())
