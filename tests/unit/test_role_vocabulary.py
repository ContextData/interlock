"""The role vocabulary cannot drift from what requests actually carry.

`role_vocabulary.py` describes, per connector, the actions, resource types and
constraint keys a statement may use. These tests hold it to the code: builders
and the core evaluator are driven through a corpus of operations, and every
action and resource type they produce must be in the vocabulary; every shipped
template must validate; every constraint key must be one the evaluator reads,
offered only where a request for that connector carries the compared value.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections import defaultdict
from typing import Any

import pytest

from interlock.connections.connectors import CONNECTOR_DEFINITIONS, get_adapter
from interlock.connections.role_vocabulary import (
    default_template,
    pick_targets,
    validate_statements,
    vocabulary_for,
)
from interlock.core import source_roles
from interlock.core.source_roles import (
    CONSTRAINT_METADATA_KEYS,
    EVALUATED_CONSTRAINT_KEYS,
    _resource_matches,
    _resource_type_for,
    build_permission_request,
)

ACTIVE = sorted(k for k, d in CONNECTOR_DEFINITIONS.items() if d.status != "planned")
PLANNED = sorted(k for k, d in CONNECTOR_DEFINITIONS.items() if d.status == "planned")

# Planned connectors are not offered in the console. MongoDB's templates speak
# the SaaS vocabulary while its builder emits SQL actions (backlog). The test
# fails if this stops being true, so the entry cannot outlive the drift.
KNOWN_PLANNED_DRIFT = {"mongodb"}

_OPS = [
    "read",
    "list",
    "bucket.list",
    "browse",
    "prefix.list",
    "write",
    "upload",
    "delete",
    "describe",
    "search",
    "query",
    "document",
    "get",
    "index",
    "channel",
    "file",
    "issue",
    "pr",
    "code",
    "execute",
    "workflow",
    "download",
    "send",
    "point",
    "upsert",
    "message",
    "metadata",
    "update",
    "create",
    "remove",
    "gmail",
    "calendar",
    "doc",
    "sheet",
    "slide",
    "chat",
    "admin",
    "export",
    "pipeline",
]
_SQL = [
    "SELECT * FROM t",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1 WHERE b = 2",
    "DELETE FROM t WHERE a = 1",
    "CREATE TABLE x (a int)",
    "DROP TABLE t",
    "ALTER TABLE t ADD c int",
    "TRUNCATE t",
    "CALL p()",
]
_REFS = [
    "s3://b/p/x.csv",
    "github://repo/o/r",
    "github://issue/o/r/1",
    "github://pr/o/r/2",
    "github://file/o/r/README.md",
    "github://workflow/o/r/ci.yml",
    "gitlab://issue/g/p/1",
    "gitlab://pipeline/g/p/3",
    "slack://channel/C1/message/1",
    "slack://channel/C1",
    "slack://file/F1",
    "gmail://user/u/message/1",
    "gdrive://file/1",
    "zendesk:ticket/1",
    "notion:page/1",
    "salesforce:Account/1",
    "opensearch://idx/1",
    "qdrant://c/point/1",
]
_WRITE_OPS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "CALL"]
_PROTOCOL = {"database": "postgresql", "warehouse": "snowflake", "http": "http"}


def _adapter_requests(key: str) -> list[Any]:
    adapter = get_adapter(key, {"connector_key": key})
    requests = []
    metas: list[dict[str, Any]] = [{}]
    metas += [{"asset_ref": ref} for ref in _REFS]
    metas += [{"sql": sql} for sql in _SQL]
    metas += [{"collection": "c"}, {"index": "i"}, {"object_type": "issue"}]
    for op in _OPS + _SQL:
        for meta in metas:
            try:
                requests.append(
                    adapter.build_permission_request(
                        source_id="s", identity_id=1, operation=op, metadata=dict(meta)
                    )
                )
            except Exception:
                continue
    return requests


def _core_requests(key: str) -> list[Any]:
    family = CONNECTOR_DEFINITIONS[key].family
    protocol = _PROTOCOL.get(family, family)
    requests = []
    for op in _OPS:
        requests.append(
            build_permission_request(
                protocol=protocol, source_id="s", identity_id=1, operation=op, method="GET"
            )
        )
    if protocol == "http":
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            requests.append(
                build_permission_request(
                    protocol="http", source_id="s", identity_id=1, operation="x", method=method
                )
            )
    if family in {"database", "warehouse"}:
        for op in _WRITE_OPS:
            requests.append(
                build_permission_request(
                    protocol=protocol,
                    source_id="s",
                    identity_id=1,
                    operation="query",
                    tables=["t"],
                    metadata={"write_operation_type": op},
                )
            )
    for tool in ("agentgate_discover", "agentgate_related_documents"):
        requests.append(
            build_permission_request(
                protocol="mcp",
                source_id="s",
                identity_id=1,
                operation=tool,
                metadata={"tool_name": tool},
            )
        )
    return requests


def _observed(key: str) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = defaultdict(set)
    for request in _adapter_requests(key) + _core_requests(key):
        seen[request.action].add(_resource_type_for(request))
    return seen


@pytest.mark.parametrize("key", ACTIVE)
def test_every_emitted_action_and_type_is_in_the_vocabulary(key: str) -> None:
    vocab = vocabulary_for(key)
    for action, types in _observed(key).items():
        assert action in vocab.actions, f"{key}: requests carry {action!r}, not in the vocabulary"
        missing = types - set(vocab.actions[action].resource_types)
        assert not missing, f"{key}: {action} is checked against {missing}, not offered"


@pytest.mark.parametrize("key", ACTIVE + PLANNED)
def test_every_shipped_template_validates(key: str) -> None:
    vocab = vocabulary_for(key)
    errors = [
        (template, issue.code, issue.message)
        for template, statements in CONNECTOR_DEFINITIONS[key].role_templates.items()
        for issue in validate_statements(vocab, statements)
        if issue.severity == "error"
    ]
    if key in KNOWN_PLANNED_DRIFT:
        assert errors, f"{key} no longer drifts; drop it from KNOWN_PLANNED_DRIFT"
    else:
        assert errors == []


def _constraint_keys_read_by_the_evaluator() -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(source_roles._constraints_match)))
    keys = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "constraints"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    return keys | set(CONSTRAINT_METADATA_KEYS)


def test_evaluated_constraint_keys_are_exactly_what_the_evaluator_reads() -> None:
    assert _constraint_keys_read_by_the_evaluator() == set(EVALUATED_CONSTRAINT_KEYS)


# Keys compared with a request attribute rather than a metadata key, or filled
# in by the gateway rather than the connector builder.
_GATEWAY_POPULATED = {"require_where", "methods"}
_ATTRIBUTE = {"risk_levels": "risk_level", "methods": "method"}


@pytest.mark.parametrize("key", ACTIVE)
def test_constraints_are_offered_only_where_requests_carry_the_value(key: str) -> None:
    """A condition on a value no request carries silently never matches."""
    vocab = vocabulary_for(key)
    requests = _adapter_requests(key)
    for action_key, action in vocab.actions.items():
        for constraint in action.constraints:
            assert constraint in EVALUATED_CONSTRAINT_KEYS
            if constraint in _GATEWAY_POPULATED:
                continue
            if constraint == "risk_levels" and vocab.family in {"database", "warehouse"}:
                continue  # the gateway's write classifier sets it on SQL requests
            for_action = [r for r in requests if r.action == action_key]
            if constraint in _ATTRIBUTE:
                carried = any(getattr(r, _ATTRIBUTE[constraint]) for r in for_action)
            else:
                metadata_key = {
                    "file_extensions": "file_extension",
                    "path_prefixes": "path",
                    "object_types": "object_type",
                    **CONSTRAINT_METADATA_KEYS,
                }[constraint]
                carried = any((r.metadata or {}).get(metadata_key) for r in for_action)
            assert carried, f"{key}: {action_key} offers {constraint!r} but never carries it"


@pytest.mark.parametrize("key", ACTIVE)
def test_default_template_is_a_reader(key: str) -> None:
    role_key, statements = default_template(key)
    assert role_key and statements
    vocab = vocabulary_for(key)
    assert all(
        s["effect"] == "allow" and vocab.actions[s["action"]].group in {"read", "discovery"}
        for s in statements
    ), f"{key} would start a new role from {role_key}"


def test_default_templates_for_the_live_connectors() -> None:
    assert default_template("s3")[0] == "reader"
    assert default_template("slack")[0] == "channel_reader"
    assert default_template("github")[0] == "repo_reader"
    assert default_template("snowflake")[0] == "analyst"


@pytest.mark.parametrize(
    ("key", "node_type", "resource_key", "request_meta", "operation"),
    [
        ("s3", "prefix", "s3://b/reports/", {"asset_ref": "s3://b/reports/q1.csv"}, "read"),
        (
            "slack",
            "channel",
            "slack://channel/C1",
            {"asset_ref": "slack://channel/C1/message/9"},
            "message",
        ),
        (
            "github",
            "repository",
            "github://repo/o/r",
            {"asset_ref": "github://issue/o/r/4"},
            "issue",
        ),
        (
            "github",
            "repository",
            "github://repo/o/r",
            {"asset_ref": "github://file/o/r/a.py"},
            "code",
        ),
        ("qdrant", "collection", "qdrant://c", {"collection": "c"}, "query"),
    ],
)
def test_pick_targets_cover_what_the_builder_asks_for(
    key: str, node_type: str, resource_key: str, request_meta: dict[str, Any], operation: str
) -> None:
    targets = pick_targets(key, node_type, resource_key)
    assert targets is not None
    request = get_adapter(key, {"connector_key": key}).build_permission_request(
        source_id="s", identity_id=1, operation=operation, metadata=request_meta
    )
    rtype = _resource_type_for(request)
    assert rtype in targets.patterns, (rtype, targets.patterns)
    pattern = targets.patterns[rtype]
    assert any(_resource_matches(pattern, resource) for resource in request.resources), (
        pattern,
        request.resources,
    )


def test_sql_pick_targets_are_unchanged() -> None:
    assert pick_targets("postgresql", "table", "sales.customers").patterns == {
        "db.table": "sales.customers.*"
    }
    column = pick_targets("postgresql", "column", "sales.customers.ssn")
    assert column is not None and column.default_type == "db.column"
    assert column.patterns["db.column"] == "sales.customers.ssn"


# --- validation rules -------------------------------------------------------------


def _one(**statement: Any) -> dict[str, Any]:
    base = {
        "effect": "allow",
        "action": "storage.object.read",
        "resource_type": "storage.object",
        "resource_pattern": "s3://b/*",
        "constraints": {},
    }
    return {**base, **statement}


def _codes(statements: list[dict[str, Any]], **kw: Any) -> list[tuple[str, str]]:
    return [
        (i.severity, i.code) for i in validate_statements(vocabulary_for("s3"), statements, **kw)
    ]


def test_a_valid_statement_has_no_issues() -> None:
    assert _codes([_one()]) == []


def test_owner_and_blocked_wildcards_are_fine() -> None:
    assert _codes([_one(action="*", resource_type="*", resource_pattern="*")]) == []
    assert _codes([_one(effect="deny", action="*", resource_type="*", resource_pattern="*")]) == []


def test_a_glob_action_must_cover_a_real_action() -> None:
    assert _codes([_one(action="storage.object.*")]) == []
    assert ("error", "unknown_action") in _codes([_one(action="db.table.*")])


def test_an_action_from_another_connector_is_refused() -> None:
    assert _codes([_one(action="db.table.select", resource_type="db.table")]) == [
        ("error", "unknown_action")
    ]


def test_the_resource_type_must_fit_the_action() -> None:
    assert _codes([_one(action="storage.bucket.list", resource_type="storage.object")]) == [
        ("error", "resource_type_not_for_action")
    ]


def test_an_unknown_constraint_is_refused_because_it_would_widen_the_statement() -> None:
    assert _codes([_one(constraints={"columns": ["id"]})]) == [("error", "unknown_constraint")]


def test_constraint_values_are_type_checked() -> None:
    assert _codes([_one(constraints={"file_extensions": ".csv"})]) == [("error", "constraint_type")]
    assert _codes([_one(constraints={"risk_levels": ["urgent"]})]) == [("error", "constraint_type")]


def test_an_inapplicable_condition_warns_on_allow_and_blocks_on_deny() -> None:
    assert _codes([_one(constraints={"channel_ids": ["C1"]})]) == [
        ("warning", "constraint_never_matches")
    ]
    assert _codes([_one(effect="deny", constraints={"channel_ids": ["C1"]})]) == [
        ("error", "deny_never_fires")
    ]


def test_a_pattern_in_the_wrong_shape_warns() -> None:
    assert _codes([_one(resource_pattern="public.x")]) == [("warning", "pattern_shape")]


def test_a_stored_statement_outside_the_vocabulary_is_kept_until_edited() -> None:
    legacy = _one(action="custom.thing", resource_type="custom")
    assert _codes([legacy], stored=[legacy]) == [("warning", "kept_as_saved")]
    edited = {**legacy, "resource_pattern": "s3://other/*"}
    assert ("error", "unknown_action") in _codes([edited], stored=[legacy])


def test_patterns_are_enforced_only_where_the_gateway_enforces_them() -> None:
    assert vocabulary_for("postgresql").patterns_enforced
    assert vocabulary_for("generic_rest").patterns_enforced
    assert not vocabulary_for("s3").patterns_enforced
    assert not vocabulary_for("slack").patterns_enforced


def test_introspection_advertises_only_constraint_keys_the_evaluator_reads() -> None:
    import re
    from pathlib import Path

    source = Path(inspect.getsourcefile(get_adapter)).read_text()  # type: ignore[arg-type]
    for block in re.findall(r'"role_constraint_keys": \[(.*?)\]', source, re.S):
        for key in re.findall(r'"([a-z_]+)"', block):
            assert key in EVALUATED_CONSTRAINT_KEYS, f"introspect() advertises unread key {key!r}"


def test_constraint_specs_list_only_conditions_the_evaluator_knows() -> None:
    from interlock.connections.role_vocabulary import constraint_specs
    from interlock.core.source_roles import (
        CONSTRAINT_METADATA_KEYS,
        EVALUATED_CONSTRAINT_KEYS,
    )

    keys = [spec.key for spec in constraint_specs()]
    assert keys and keys == sorted(set(keys))
    assert set(keys) <= set(EVALUATED_CONSTRAINT_KEYS) | set(CONSTRAINT_METADATA_KEYS)
