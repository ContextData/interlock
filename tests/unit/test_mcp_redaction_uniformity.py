"""Every content-returning MCP tool must redact, not just the query one.

Redaction used to live inline in `_execute_query`. `_discover`,
`_related_documents` and `_describe_source` had none, so `interlock_query`
redacted a value while `interlock_discover` returned the identical value
verbatim. That was a real disclosure - the discovery catalogue is populated by
the ingestion pipeline from upstream documents and messages, which is exactly
where free-text PII lives - and it contradicted
`docs-site/src/content/docs/reference/contracts/mcp-v1.md`, which scopes redaction to tool execution generally
rather than to one tool.

These tests are structural on purpose. Asserting the behaviour of each handler
end to end would need a live stack; asserting that no handler serialises a
payload it has not passed through the shared redactor catches a *new* handler
added without the step, which is the way this gap would recur.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from interlock.gateway.mcp_adapter import MCPAdapter

_SOURCE = Path(inspect.getfile(MCPAdapter)).read_text()

# Handlers that return upstream or catalogue content to a caller. A handler
# here that does not redact is a disclosure.
CONTENT_HANDLERS = (
    "_execute_query",
    "_discover",
    "_describe_source",
    "_related_documents",
)

# Handlers that return only control-plane facts the caller is already
# authorized to see, so they carry no upstream content to redact.
CONTROL_HANDLERS = ("_list_sources",)


def _handler(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is no longer a handler in mcp_adapter; update this test")


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


@pytest.mark.parametrize("handler", CONTENT_HANDLERS)
def test_every_content_handler_redacts_before_responding(handler: str) -> None:
    """The regression that mattered: three of four handlers did not."""
    calls = _calls(_handler(handler))

    redacts = "_redact_rows" in calls or "process_row" in calls
    assert redacts, (
        f"{handler} returns content to a caller without passing it through "
        "_redact_rows. interlock_query redacting a value while another tool "
        "returns it verbatim is a disclosure, and mcp-v1.md scopes redaction to "
        "tool execution generally."
    )


@pytest.mark.parametrize("handler", CONTENT_HANDLERS)
def test_every_content_handler_reports_detections_to_the_audit_trail(handler: str) -> None:
    """A redaction nobody can see afterwards is half a control.

    `_related_documents` used to set a `redaction_required` flag on its audit
    payload while performing no redaction - recording that redaction was
    needed and not doing it. The inverse is just as bad: redacting without
    recording leaves no evidence PII was ever present.
    """
    body = ast.get_source_segment(_SOURCE, _handler(handler)) or ""
    assert "pii_detected" in body, (
        f"{handler} does not pass pii_detected to its audit call, so a redaction "
        "it performed leaves no trace in the audit trail"
    )


@pytest.mark.parametrize("handler", CONTROL_HANDLERS)
def test_control_plane_handlers_are_deliberately_exempt(handler: str) -> None:
    """Pins the exemption, so it is a decision rather than an omission.

    If a control-plane handler ever starts returning upstream content, this
    test should be updated and the handler moved into CONTENT_HANDLERS - the
    failure mode is that nobody notices the category changed.
    """
    body = ast.get_source_segment(_SOURCE, _handler(handler)) or ""
    assert "connection_config" not in body, (
        f"{handler} now touches connection_config, so it may be returning upstream "
        "content. Move it to CONTENT_HANDLERS and give it redaction."
    )


def test_the_shared_redactor_fails_closed_on_a_scanner_error() -> None:
    """A scanner exception must drop the row, never pass it through.

    Failing open here would be worse than having no scanner, because the
    caller and the audit trail would both record a redacted response.
    """
    source = ast.get_source_segment(_SOURCE, _handler("_redact_rows")) or ""
    assert "except Exception" in source
    assert (
        "row redacted after scanner failure" in source
    ), "_redact_rows does not replace a row whose scan failed; it must fail closed"


def test_the_shared_redactor_is_used_rather_than_reimplemented() -> None:
    """One definition, so a fix or a bug applies everywhere at once.

    Four separate inline scanner loops is how the handlers drifted apart in
    the first place.
    """
    assert _SOURCE.count("async def _redact_rows") == 1
    # The query handler's own historical inline loop must be gone.
    query_body = ast.get_source_segment(_SOURCE, _handler("_execute_query")) or ""
    assert query_body.count("process_row") <= 1, (
        "_execute_query still contains an inline scanner loop; it should call "
        "_redact_rows like the other handlers"
    )
