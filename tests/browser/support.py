"""Reusable assertions and page helpers for Admin browser certification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRIMARY_ADMIN_ROUTES = (
    ("overview", "/dashboard/overview", "Overview"),
    ("data-sources", "/dashboard/data-sources", "Data Sources"),
    ("connectors", "/dashboard/connectors", "Connectors"),
    ("source-detail", "/dashboard/data-sources/e2e_pg", "Configuration"),
    ("source-roles", "/dashboard/data-sources/e2e_pg/roles", "Source Roles"),
    ("identities", "/dashboard/access-control/identities", "Identities"),
    ("policies", "/dashboard/policies", "Policies"),
    ("ingestion", "/dashboard/ingestion", "Ingestion Jobs"),
    ("workers", "/dashboard/workers", "Worker Status"),
    ("discovery", "/dashboard/discovery", "Discovery"),
    ("categories", "/dashboard/categories", "Category Browser"),
    ("entities", "/dashboard/entities", "Entity Explorer"),
    ("catalog", "/dashboard/catalog", "Catalog"),
    ("audit", "/dashboard/audit-costs", "Audit"),
    ("write-safety", "/dashboard/write-safety", "Write Safety"),
    ("proxy", "/dashboard/proxy", "Proxy Monitor"),
    ("policy-analytics", "/dashboard/policy-analytics", "Policy Analytics"),
    ("alerts", "/dashboard/alerts", "Alerts"),
)

# Form and wizard pages. Kept separate from PRIMARY_ADMIN_ROUTES because they
# are entry points into a flow rather than destinations, but they still have to
# be certified: none of them was covered when every text input written without
# a `type` attribute - 135 of them - silently lost its styling.
FORM_ADMIN_ROUTES = (
    ("source-wizard", "/dashboard/source-wizard", "New Data Source"),
    ("data-source-new", "/dashboard/data-sources/new", "New"),
    ("identity-new", "/dashboard/access-control/identities/new", "New Identity"),
    ("policy-new", "/dashboard/policies/new", "New Policy"),
    ("alert-new", "/dashboard/alerts/new", "New Alert"),
    ("source-role-new", "/dashboard/data-sources/e2e_pg/roles/new", "Role"),
)


SEEDED_SECRET_VALUES = (
    "e2e-admin-password",
    "ag-e2e-api-key",
    "ag-e2e-denied-key",
    "e2e-pg-password",
    "source_pass",
    "e2e-s3-secret-key",
)

BROWSER_ADMIN_PASSWORD = "interlock-browser-cert-password"


@dataclass
class BrowserDiagnostics:
    """Capture browser security and runtime failures without hiding context."""

    console_errors: list[str] = field(default_factory=list)
    csp_violations: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)

    def attach(self, page: Any) -> None:
        def on_console(message: Any) -> None:
            text = str(message.text)
            lowered = text.casefold()
            if "content security policy" in lowered or "violates the following" in lowered:
                self.csp_violations.append(text)
            if message.type == "error":
                self.console_errors.append(text)

        page.on("console", on_console)
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def reset(self) -> None:
        self.console_errors.clear()
        self.csp_violations.clear()
        self.page_errors.clear()

    def assert_clean(self, route: str) -> None:
        assert not self.csp_violations, f"CSP violation on {route}: {self.csp_violations}"
        assert not self.page_errors, f"Browser error on {route}: {self.page_errors}"


def login_admin(page: Any, base_url: str, username: str, password: str) -> None:
    page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
    page.locator("#lf-user").fill(username)
    page.locator("#lf-pass").fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(r".*/dashboard/overview(?:\?.*)?$"))
    # Two lockups ship, one shown per theme; either proves the shell rendered.
    assert page.locator(".sidebar-logo").count() >= 1


def wait_for_htmx(page: Any) -> None:
    """Wait until HTMX has no request or settling nodes left in the DOM."""
    page.wait_for_function("""
        () => !document.body.classList.contains('htmx-request') &&
              document.querySelectorAll('.htmx-request, .htmx-settling').length === 0
        """)


def assert_full_admin_shell(page: Any, expected_text: str) -> None:
    assert page.locator(".sidebar-logo").count() >= 1
    assert page.locator("#main-content").count() == 1
    assert expected_text.casefold() in page.locator("body").inner_text().casefold()


def assert_security_headers(response: Any, route: str) -> None:
    assert response is not None, f"No navigation response for {route}"
    headers = {key.casefold(): value for key, value in response.all_headers().items()}
    assert headers.get("content-security-policy"), f"Missing CSP header on {route}"
    assert headers.get("x-content-type-options") == "nosniff", route
    assert headers.get("x-frame-options"), route


def assert_no_page_overflow(page: Any, route: str) -> None:
    dimensions = page.evaluate("""
        () => ({
          documentClient: document.documentElement.clientWidth,
          documentScroll: document.documentElement.scrollWidth,
          bodyClient: document.body.clientWidth,
          bodyScroll: document.body.scrollWidth
        })
        """)
    assert dimensions["documentScroll"] <= dimensions["documentClient"] + 1, (
        route,
        dimensions,
    )
    assert dimensions["bodyScroll"] <= dimensions["bodyClient"] + 1, (route, dimensions)


def assert_no_overflowing_children(page: Any, route: str) -> None:
    """No element may paint outside the panel that owns it.

    `assert_no_clipped_content` only inspects containers that hide their
    overflow. A card with the default visible overflow, holding a table wider
    than its grid track, painted over the neighbouring card instead - which is
    exactly what identity detail did at 1440px: the grants table's Revoke
    column and the API Key copy landed on top of the next panel. Content inside
    a container that scrolls is reachable and therefore fine.
    """
    offenders = page.evaluate("""
        () => {
          const scrolls = (el) => {
            const s = getComputedStyle(el);
            return s.overflowX === 'auto' || s.overflowX === 'scroll';
          };
          const out = [];
          const panels = document.querySelectorAll('.card, .form-card, .table-wrapper, main section');
          panels.forEach((panel) => {
            const ps = getComputedStyle(panel);
            if (ps.display === 'none' || ps.visibility === 'hidden' || scrolls(panel)) return;
            const box = panel.getBoundingClientRect();
            if (box.width === 0) return;
            panel.querySelectorAll('*').forEach((child) => {
              const cs = getComputedStyle(child);
              if (cs.display === 'none' || cs.visibility === 'hidden') return;
              if (cs.position === 'fixed' || cs.position === 'absolute') return;
              for (let node = child.parentElement; node && node !== panel; node = node.parentElement) {
                if (scrolls(node)) return;
              }
              const r = child.getBoundingClientRect();
              if (r.width === 0 && r.height === 0) return;
              const spill = Math.round(Math.max(r.right - box.right, box.left - r.left));
              if (spill > 1) {
                out.push({
                  panel: String(panel.className || panel.tagName).slice(0, 40),
                  child: String(child.className || child.tagName).slice(0, 40),
                  spillPx: spill,
                });
              }
            });
          });
          return out.slice(0, 8);
        }
        """)
    assert not offenders, f"Content painted outside its panel on {route}: {offenders}"


def assert_no_secret_values(page: Any, extra_values: tuple[str, ...] = ()) -> None:
    values = tuple(value for value in SEEDED_SECRET_VALUES + extra_values if value)
    snapshot = page.evaluate("""
        () => ({
          html: document.documentElement.outerHTML,
          text: document.body.innerText,
          values: Array.from(document.querySelectorAll('input, textarea, select'))
            .map((node) => node.value || '')
        })
        """)
    haystacks = (snapshot["html"], snapshot["text"], *snapshot["values"])
    for secret in values:
        assert all(
            secret not in haystack for haystack in haystacks
        ), f"Secret value {secret!r} was present in the rendered DOM"


def capture_evidence(page: Any, artifact_dir: Path | None, name: str) -> None:
    if artifact_dir is None:
        return
    artifact_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact_dir / f"{name}.png"), full_page=True)


# ---------------------------------------------------------------------------
# Presentation certification
#
# These encode findings from the UI rehabilitation rather than trusting a
# one-off measurement: before it, the source-detail page had 45 elements below
# WCAG AA, the declared webfont had never once loaded, and the overflow check
# below could not see content clipped inside a scroll container.
# ---------------------------------------------------------------------------

# Composites every translucent layer down to an opaque colour before
# measuring. Without this, `rgba(...)` backgrounds report the wrong ratio -
# and most quiet status fills in dark mode are translucent.
_CONTRAST_PROBE = """
() => {
  const parse = (s) => {
    const m = (s || '').match(/[\\d.]+/g);
    if (!m) return null;
    const [r, g, b] = m.slice(0, 3).map(Number);
    return [r, g, b, m.length > 3 ? Number(m[3]) : 1];
  };
  const over = (fg, bg) => [0, 1, 2].map(i => fg[i] * fg[3] + bg[i] * (1 - fg[3]));
  const lum = ([r, g, b]) => {
    const a = [r, g, b].map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
  };
  const ratio = (f, b) => {
    const L1 = lum(f), L2 = lum(b);
    const [hi, lo] = L1 > L2 ? [L1, L2] : [L2, L1];
    return (hi + 0.05) / (lo + 0.05);
  };
  const bgOf = (el) => {
    const stack = [];
    let e = el;
    while (e) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c[3] > 0) stack.push(c);
      e = e.parentElement;
    }
    let base = [255, 255, 255];
    for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
    return base;
  };
  const failures = [];
  document.querySelectorAll('body *').forEach(el => {
    if (el.children.length) return;
    const text = (el.textContent || '').trim();
    if (!text) return;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) return;
    if (el.closest('.sr-only')) return;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const fg = parse(st.color);
    if (!fg) return;
    const size = parseFloat(st.fontSize);
    const weight = parseInt(st.fontWeight) || 400;
    const required = (size >= 24 || (size >= 18.66 && weight >= 700)) ? 3 : 4.5;
    const bg = bgOf(el);
    const measured = ratio(over(fg, bg), bg);
    if (measured < required) {
      failures.push({
        text: text.slice(0, 40),
        ratio: Math.round(measured * 100) / 100,
        required,
        px: size,
        selector: String(el.className || el.tagName).slice(0, 40),
      });
    }
  });
  return failures;
}
"""


def assert_text_contrast_aa(page: Any, route: str, theme: str) -> None:
    """Every rendered text node must meet WCAG AA against its real backdrop."""
    failures = page.evaluate(_CONTRAST_PROBE)
    assert (
        not failures
    ), f"{len(failures)} element(s) below WCAG AA on {route} [{theme}]: {failures[:6]}"


def assert_no_clipped_content(page: Any, route: str) -> None:
    """No element may be cut off with no way to scroll to it.

    `assert_no_page_overflow` only compares document/body scrollWidth, so
    content clipped inside an `overflow: hidden` container passed while being
    unreachable - which is how the mobile table lost 8 of its 11 columns.
    """
    clipped = page.evaluate("""
        () => {
          const out = [];
          document.querySelectorAll('body *').forEach(el => {
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return;
            const hiddenX = st.overflowX === 'hidden' || st.overflowX === 'clip';
            if (!hiddenX) return;
            // Deliberate truncation is not clipping: an ellipsis tells the
            // reader there is more, and these carry the full string in a
            // title. Silent cut-off with neither is the bug being guarded.
            if (st.textOverflow === 'ellipsis') return;
            if (el.scrollWidth > el.clientWidth + 1) {
              out.push({
                selector: String(el.className || el.tagName).slice(0, 40),
                scrollWidth: el.scrollWidth,
                clientWidth: el.clientWidth,
              });
            }
          });
          return out;
        }
        """)
    assert not clipped, f"Content clipped with no scroll affordance on {route}: {clipped}"


# Families the console self-hosts. Both are SIL OFL, so they can ship in the
# repository; the previous pairing was Geist / Geist Mono.
BUNDLED_FONT_FAMILIES = ("Instrument Sans", "JetBrains Mono")


def assert_webfonts_loaded(page: Any, route: str) -> None:
    """Every declared typeface must actually load.

    `--font-sans` named a face with no @font-face anywhere for months, so every
    screen silently rendered in a fallback and nothing caught it.
    """
    state = page.evaluate("""
        () => ({
          faces: [...document.fonts].map(f => f.family),
          requests: performance.getEntriesByType('resource')
            .filter(r => /\\.woff2?(\\?|$)/.test(r.name)).length,
        })
        """)
    assert state["requests"] > 0, f"No webfont file was requested on {route}: {state}"
    for family in BUNDLED_FONT_FAMILIES:
        assert any(
            family in loaded for loaded in state["faces"]
        ), f"{family} @font-face not registered on {route}: {state}"


def set_theme(page: Any, base_url: str, theme: str) -> None:
    """Pin the colour theme the way the console does - via its cookie."""
    domain = base_url.split("//", 1)[-1].split(":", 1)[0]
    page.context.add_cookies(
        [{"name": "interlock_theme", "value": theme, "domain": domain, "path": "/"}]
    )


def assert_form_controls_are_styled(page: Any, route: str) -> None:
    """Text inputs must be laid out by the stylesheet, not left at UA default.

    An `<input>` with no `type` attribute is a text field, but a selector list
    that enumerates `input[type=...]` never matches one. 135 inputs in these
    templates are written without a type, so they rendered at the UA default
    width (~147px) beside their label instead of filling the row.
    """
    unstyled = page.evaluate("""
        () => {
          const skip = new Set(['checkbox','radio','hidden','submit','button','reset','file','range','color','image']);
          const out = [];
          document.querySelectorAll('input, select, textarea').forEach(el => {
            if (el.tagName === 'INPUT' && skip.has((el.getAttribute('type') || 'text').toLowerCase())) return;
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return;
            const box = el.getBoundingClientRect();
            if (!box.width) return;
            // The stylesheet gives every text control a border and a min
            // height; a UA-default control has neither.
            const styled = st.borderStyle !== 'none'
                && parseFloat(st.minHeight) >= 28
                && parseFloat(st.paddingLeft) >= 6;
            if (!styled) {
              out.push({
                name: el.getAttribute('name') || el.tagName,
                type: el.getAttribute('type') || '(none)',
                width: Math.round(box.width),
                minHeight: st.minHeight,
                border: st.borderStyle,
              });
            }
          });
          return out;
        }
        """)
    assert not unstyled, f"Unstyled form controls on {route}: {unstyled}"
