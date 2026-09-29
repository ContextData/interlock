"""Alert rule evaluation.

The evaluator is intentionally pure given a PG pool: it does not touch
Redis, does not call out, does not write back. The dashboard route
that triggers a manual fire is responsible for inserting into
``alert_history`` and dispatching notifications.

Each ``condition_type`` produces a single ``observed_value`` over the
rule's window and is compared against ``threshold`` with ``comparator``.

Supported condition_types:
    - error_rate         - fraction (0..1) of audit_log rows with status='error'
    - denial_rate        - fraction (0..1) with status='denied'
    - p95_latency_ms     - 95th percentile latency_ms
    - request_volume     - count of audit_log rows
    - queue_depth        - count of write_approval_queue rows with status='pending'

Scoping is via optional ``source_id`` and ``identity_id`` columns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    fired: bool
    observed_value: float | None
    message: str


_COMPARATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


async def evaluate_rule(pool: Any, rule: dict[str, Any]) -> EvaluationResult:
    """Compute the observed_value for a rule and compare to threshold.

    Returns an EvaluationResult. Never raises - on query failure the
    rule is reported as not-fired with a diagnostic message.
    """
    cond = rule["condition_type"]
    cmp = rule.get("comparator", ">")
    threshold = float(rule["threshold"])
    window = int(rule.get("window_seconds", 300))

    where = ["created_at >= NOW() - make_interval(secs => $1)"]
    params: list[Any] = [window]
    idx = 2
    source_id = rule.get("source_id")
    identity_id = rule.get("identity_id")
    if source_id:
        where.append(f"source_id = ${idx}")
        params.append(source_id)
        idx += 1
    if identity_id:
        where.append(f"identity_id = ${idx}")
        params.append(int(identity_id))
        idx += 1

    if cond == "queue_depth":
        sql = "SELECT count(*)::float FROM write_approval_queue WHERE status = 'pending'"
        if source_id:
            sql += " AND source_id = $1"
            params = [source_id]
        else:
            params = []
        try:
            observed = await pool.fetchval(sql, *params) or 0.0
        except Exception as exc:
            return EvaluationResult(False, None, f"query failed: {exc}")
    elif cond == "request_volume":
        sql = f"SELECT count(*)::float FROM audit_log WHERE {' AND '.join(where)}"
        try:
            observed = await pool.fetchval(sql, *params) or 0.0
        except Exception as exc:
            return EvaluationResult(False, None, f"query failed: {exc}")
    elif cond == "error_rate":
        observed = await _rate(pool, where, params, "status = 'error'")
    elif cond == "denial_rate":
        observed = await _rate(pool, where, params, "status = 'denied'")
    elif cond == "p95_latency_ms":
        sql = (
            "SELECT COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)"
            f"::float FROM audit_log WHERE {' AND '.join(where)}"
        )
        try:
            observed = await pool.fetchval(sql, *params) or 0.0
        except Exception as exc:
            return EvaluationResult(False, None, f"query failed: {exc}")
    else:
        return EvaluationResult(False, None, f"unknown condition_type: {cond}")

    op = _COMPARATORS.get(cmp)
    if op is None:
        return EvaluationResult(False, observed, f"unknown comparator: {cmp}")

    fired = op(float(observed), threshold)
    msg = f"{cond} {cmp} {threshold} -> observed {observed:.4g} " f"({'FIRED' if fired else 'ok'})"
    return EvaluationResult(fired=fired, observed_value=float(observed), message=msg)


async def _rate(pool: Any, where: list[str], params: list[Any], numerator: str) -> float:
    sql = (
        "SELECT CASE WHEN count(*) = 0 THEN 0 "
        f"ELSE (count(*) FILTER (WHERE {numerator}))::float / count(*) END "
        f"FROM audit_log WHERE {' AND '.join(where)}"
    )
    try:
        v = await pool.fetchval(sql, *params)
        return float(v or 0.0)
    except Exception as exc:
        logger.debug("rate query failed: %s", exc)
        return 0.0
