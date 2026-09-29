"""CSRF is carried by a header, and that is a security property, not a detail.

The middleware docstring claimed it also accepted a `csrf_token` form field.
It never did - `request.form()` is not called anywhere in that module - and
the claim was worse than merely inaccurate: it invited someone to "restore"
the missing support and weaken the control while believing they were fixing a
bug.

A cross-origin HTML form can post fields but cannot set a custom header, which
is precisely why header-only defeats form-POST CSRF. Accepting the token from
a form field would remove that property.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from interlock.admin import auth_middleware

_SOURCE = inspect.getsource(auth_middleware)
_TEMPLATES = Path(auth_middleware.__file__).parent / "templates"
_DASHBOARD_JS = Path(auth_middleware.__file__).parent / "static" / "js" / "dashboard.js"


class TestHeaderOnly:
    def test_the_token_is_read_from_the_header(self) -> None:
        assert 'request.headers.get("x-csrf-token"' in _SOURCE

    def test_the_request_body_is_never_read_for_a_token(self) -> None:
        """Reading form data in middleware would also consume the body stream.

        Downstream routes would then receive an empty request unless it were
        buffered and replayed - real complexity, for a change that weakens the
        control.
        """
        # Narrow on purpose. An earlier version of this test rejected the
        # substring "csrf_token" anywhere in the module, which also matched
        # `get_csrf_token` - the legitimate Redis lookup. A test that fails on
        # a correct symbol trains people to weaken it.
        assert "request.form(" not in _SOURCE
        assert ".form()" not in _SOURCE, (
            "the middleware now reads the request body. If form-field CSRF "
            "support was added, it removes the property that makes header-only "
            "effective: a cross-origin form cannot set a custom header."
        )

    def test_the_docstring_does_not_promise_form_field_support(self) -> None:
        """The docstring is the thing that was wrong, so it is what is pinned."""
        docstring = auth_middleware.__doc__ or ""

        assert "or csrf_token form field" not in docstring
        assert "X-CSRF-Token" in docstring

    def test_the_enforcement_is_unconditional_for_unsafe_methods(self) -> None:
        """A comment once described a Bearer-token bypass that does not exist.

        Pinned because a reader who believed it would look for the exemption
        when debugging a 403, and because if anyone ever adds one this should
        be a deliberate change rather than a quiet one.
        """
        assert "if request.method.upper() in UNSAFE_METHODS:" in _SOURCE
        # No Authorization-based short-circuit around the check.
        csrf_block = _SOURCE.split("if request.method.upper() in UNSAFE_METHODS:")[1][:600]
        assert "Authorization" not in csrf_block
        assert "bearer" not in csrf_block.lower()


class TestDocumentedBypassList:
    def test_the_docstring_lists_the_paths_that_actually_bypass(self) -> None:
        """It omitted /ready and /auth/oidc, so the list read as shorter than it is."""
        docstring = auth_middleware.__doc__ or ""

        for prefix in auth_middleware._BYPASS_PREFIXES:
            assert prefix in docstring, (
                f"{prefix} bypasses authentication but is not in the documented "
                "bypass list; an incomplete list of what skips auth is the kind "
                "of thing a reviewer relies on"
            )


class TestTheTokenActuallyReachesRequests:
    def test_no_template_references_a_csrf_form_field(self) -> None:
        """A dangling selector is the fossil the docstring was describing.

        `discovery.html` carried `hx-include="[name='csrf']"` while no template
        rendered such a field. htmx matches nothing silently, so it neither
        worked nor failed - it just implied a mechanism that did not exist.
        """
        offenders = [
            str(path.relative_to(_TEMPLATES))
            for path in _TEMPLATES.rglob("*.html")
            if "name='csrf'" in path.read_text() or 'name="csrf"' in path.read_text()
        ]

        assert (
            not offenders
        ), f"template(s) reference a csrf form field that nothing renders: {offenders}"

    def test_the_header_is_attached_globally_by_the_dashboard_script(self) -> None:
        """Why no form needs to carry the token in the first place."""
        script = _DASHBOARD_JS.read_text()

        assert "htmx:configRequest" in script
        assert "X-CSRF-Token" in script

    @pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
    def test_every_unsafe_verb_is_covered_by_the_script(self, verb: str) -> None:
        """A verb the script misses would 403 on every attempt."""
        script = _DASHBOARD_JS.read_text().lower()

        assert verb in script, f"{verb} requests would carry no CSRF token"
