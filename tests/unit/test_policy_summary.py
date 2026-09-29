"""Policy rules read as sentences, and the source page lists the ones that apply."""

from __future__ import annotations

import pytest

from interlock.admin.policy_summary import (
    applies_to_source,
    describe_conditions,
    describe_policy,
    effect_of,
)


@pytest.mark.parametrize(
    ("conditions", "actions", "expected"),
    [
        ({}, {"effect": "allow"}, "Allows every request."),
        (
            {"identity_roles": ["quarantined"]},
            {"effect": "deny"},
            "Denies requests from identities with the quarantined label.",
        ),
        (
            {"source_id": "sales_pg", "tables": ["sales.payroll"]},
            {"effect": "deny"},
            "Denies requests to source sales_pg, touching table sales.payroll.",
        ),
        (
            {"source_ids": ["public_api"]},
            {"effect": "allow", "redact_columns": ["email", "phone"]},
            "Allows requests to source public_api; redacts email and phone in results.",
        ),
        (
            {"roles": ["analyst", "writer"], "operation_types": ["write"]},
            {"effect": "allow", "write_risk_cap": "Medium", "rate_limit": 30},
            "Allows requests from identities granted the analyst or writer source roles, "
            "for write operations; limits requests to 30 per 60 seconds; "
            "refuses writes riskier than medium.",
        ),
        (
            {"columns": ["ssn"], "classifications": ["pii"]},
            {},
            "Denies requests reading column ssn, touching data classified pii.",
        ),
        (
            {"source_ids": ["a", "b", "c", "d", "e"]},
            {"effect": "allow"},
            "Allows requests to sources a, b, c and 2 more.",
        ),
        (
            {"source_ids": ["a", "b"], "team": "x"},
            {"effect": "allow"},
            "Allows requests to sources a or b (ignored condition key: team).",
        ),
    ],
)
def test_describe_policy(conditions: dict, actions: dict, expected: str) -> None:
    assert describe_policy(conditions, actions) == expected


def test_a_rule_without_an_effect_denies_as_the_engine_does() -> None:
    assert effect_of({}) == "deny"
    assert effect_of({"effect": "ALLOW"}) == "deny"
    assert effect_of('{"effect": "allow"}') == "allow"


def test_conditions_accept_json_strings() -> None:
    assert describe_conditions('{"operation_types": ["read"]}') == "requests for read operations"


@pytest.mark.parametrize(
    ("conditions", "applies"),
    [
        ({}, True),
        ({"source_id": "sales_pg"}, True),
        ({"source_ids": ["sales_pg", "hr"]}, True),
        ({"source_ids": ["hr"]}, False),
        ({"source_id": "hr"}, False),
        # source_ids wins over source_id, as in the engine.
        ({"source_ids": ["hr"], "source_id": "sales_pg"}, False),
    ],
)
def test_applies_to_source_uses_the_engine_rule(conditions: dict, applies: bool) -> None:
    assert applies_to_source(conditions, "sales_pg") is applies
