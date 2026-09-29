"""Plain-English descriptions of policy rules, for the console.

Every condition and action the engine reads (`core/policy.py`) has a phrase
here, so a rule never reads as "Custom conditions". Keys the engine ignores
are named as ignored rather than described, because describing them would
claim an effect the rule does not have.
"""

from __future__ import annotations

import json
from typing import Any

from interlock.core.policy import rule_source_ids

_CONDITION_KEYS = frozenset(
    {
        "source_id",
        "source_ids",
        "roles",
        "identity_roles",
        "global_roles",
        "operation_types",
        "tables",
        "columns",
        "classification_tags",
        "classifications",
    }
)


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[str]:
    if value in (None, "", [], ()):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v)]
    return [str(value)]


def _join(items: list[str], conjunction: str = "or") -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def _plural(items: list[str], one: str, many: str) -> str:
    return one if len(items) == 1 else many


def describe_conditions(conditions: Any) -> str:
    """Which requests a rule matches, e.g. "requests to sales_pg for writes"."""
    cond = as_dict(conditions)
    parts: list[str] = []
    sources = rule_source_ids(cond)
    if len(sources) > 3:
        shown = ", ".join(sources[:3])
        parts.append(f"to sources {shown} and {len(sources) - 3} more")
    elif sources:
        parts.append(f"to {_plural(sources, 'source', 'sources')} {_join(sources)}")
    roles = _items(cond.get("roles"))
    if roles:
        parts.append(
            f"from identities granted the {_join(roles)} source "
            f"{_plural(roles, 'role', 'roles')}"
        )
    identity_roles = _items(cond.get("identity_roles") or cond.get("global_roles"))
    if identity_roles:
        parts.append(
            f"from identities with the {_join(identity_roles)} "
            f"{_plural(identity_roles, 'label', 'labels')}"
        )
    operations = _items(cond.get("operation_types"))
    if operations:
        parts.append(f"for {_join(operations)} operations")
    tables = _items(cond.get("tables"))
    if tables:
        parts.append(f"touching {_plural(tables, 'table', 'tables')} {_join(tables)}")
    columns = _items(cond.get("columns"))
    if columns:
        parts.append(f"reading {_plural(columns, 'column', 'columns')} {_join(columns)}")
    classifications = _items(cond.get("classification_tags") or cond.get("classifications"))
    if classifications:
        parts.append(f"touching data classified {_join(classifications)}")
    text = f"requests {', '.join(parts)}" if parts else "every request"
    ignored = sorted(k for k in cond if k not in _CONDITION_KEYS)
    if ignored:
        text += f" (ignored condition {_plural(ignored, 'key', 'keys')}: {', '.join(ignored)})"
    return text


def describe_actions(actions: Any) -> list[str]:
    """What a matching rule does beyond allowing, e.g. redaction."""
    act = as_dict(actions)
    extras: list[str] = []
    redact = _items(act.get("redact_columns"))
    if redact:
        extras.append(f"redacts {_join(redact, 'and')} in results")
    rate = act.get("rate_limit")
    limit: Any = None
    window: Any = 60
    if isinstance(rate, int):
        limit = rate
    elif isinstance(rate, dict):
        limit = rate.get("limit") or rate.get("requests") or rate.get("requests_per_minute")
        window = rate.get("window_seconds") or rate.get("window") or 60
    if limit:
        extras.append(f"limits requests to {limit} per {window} seconds")
    cap = act.get("write_risk_cap") or act.get("max_write_risk") or act.get("max_risk")
    if cap:
        extras.append(f"refuses writes riskier than {str(cap).lower()}")
    return extras


def effect_of(actions: Any) -> str:
    """The effect as the engine reads it: anything but `allow` denies."""
    return "allow" if as_dict(actions).get("effect", "deny") == "allow" else "deny"


def describe_policy(conditions: Any, actions: Any) -> str:
    """One sentence, e.g. "Denies requests from identities with the quarantined label"."""
    verb = "Allows" if effect_of(actions) == "allow" else "Denies"
    sentence = f"{verb} {describe_conditions(conditions)}"
    extras = describe_actions(actions) if verb == "Allows" else []
    if extras:
        sentence += "; " + "; ".join(extras)
    return sentence + "."


def applies_to_source(conditions: Any, source_id: str) -> bool:
    """Whether the engine would consider this rule for requests to `source_id`."""
    sources = rule_source_ids(as_dict(conditions))
    return not sources or source_id in sources
