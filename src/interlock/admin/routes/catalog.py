"""Admin console views over the source catalog.

The Catalog section on a source's detail page - last scan, Rescan, the tree,
PII annotations and drift - and a cross-source search page. Everything here
reads what the workers' scans recorded; nothing connects to a source.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from interlock.admin.audit import audit_admin_action, mutation_audit_detail
from interlock.catalog import read as catalog_read
from interlock.catalog.analytics import access_analytics
from interlock.catalog.collectors import is_enforced
from interlock.catalog.compat import naming_report
from interlock.catalog.queue import enqueue_catalog_scan
from interlock.catalog.validation import (
    LintWarning,
    lint_policy,
    lint_statements,
    load_catalog_view,
)
from interlock.connections.connectors import get_connector
from interlock.connections.role_vocabulary import pick_targets, validate_statements, vocabulary_for

logger = logging.getLogger(__name__)

router = APIRouter(tags=["catalog"])

CLASSIFICATIONS = ("pii", "sensitive", "not_pii", "public")
NODE_TYPE_FILTERS = ("table", "column", "schema", "bucket", "prefix", "channel", "repository")


def _render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    # The dashboard's renderer, so theme and account context stay identical.
    from interlock.admin.routes.dashboard import _render as render

    return render(request, template, context)


def _admin_username(request: Request) -> str | None:
    admin = getattr(request.state, "admin", None)
    username = getattr(admin, "username", None)
    return str(username) if username else None


def _parse_path(raw: str) -> list[str] | None:
    """A node path from the console: a JSON array of strings, or None."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        return None
    return value


async def _connector_key(pool: Any, source_id: str) -> str | None:
    row = await pool.fetchrow(
        "SELECT source_type, metadata FROM data_sources WHERE source_id = $1", source_id
    )
    return None if row is None else catalog_read.connector_key_of(row)


async def _section(request: Request, source_id: str, notice: str = "") -> HTMLResponse:
    pool = request.app.state.pg_pool
    status = await catalog_read.source_status(pool, source_id)
    if status is None:
        return HTMLResponse("<p class='muted'>Unknown data source.</p>", status_code=404)
    connector_key = await _connector_key(pool, source_id) or ""
    return _render(
        request,
        "partials/catalog_section.html",
        {
            "source_id": source_id,
            "status": status,
            "enforced": is_enforced(connector_key),
            "nodes": await catalog_read.children(pool, source_id, []),
            "changes": await catalog_read.changes(pool, source_id),
            "history": await catalog_read.scan_history(pool, source_id),
            "classifications": CLASSIFICATIONS,
            "notice": notice,
        },
    )


@router.get("/dashboard/data-sources/{source_id}/catalog", response_class=HTMLResponse)
async def catalog_section(source_id: str, request: Request) -> HTMLResponse:
    return await _section(request, source_id)


@router.post("/dashboard/data-sources/{source_id}/catalog/rescan", response_class=HTMLResponse)
async def catalog_rescan(source_id: str, request: Request) -> HTMLResponse:
    scan_id = await enqueue_catalog_scan(
        request.app.state.pg_pool,
        source_id,
        trigger="manual",
        requested_by=_admin_username(request),
    )
    await audit_admin_action(
        request,
        action="data_source.catalog_scan",
        resource="data_source",
        resource_id=source_id,
        success=scan_id is not None,
        detail=mutation_audit_detail(status_code=202, extra={"scan_id": scan_id}),
        error=None if scan_id is not None else "not collectable",
    )
    notice = (
        f"Scan {scan_id} queued. The worker picks it up within seconds."
        if scan_id is not None
        else "Nothing queued: the source is disabled, or its connector has no collector."
    )
    return await _section(request, source_id, notice=notice)


@router.post(
    "/dashboard/data-sources/{source_id}/catalog/changes/acknowledge",
    response_class=HTMLResponse,
)
async def catalog_acknowledge(
    source_id: str, request: Request, change_id: str = Form("")
) -> HTMLResponse:
    """Acknowledge one drift entry, or every open one when no id is given."""
    pool = request.app.state.pg_pool
    admin = _admin_username(request)
    if change_id:
        try:
            target = int(change_id)
        except ValueError:
            return HTMLResponse("<p class='muted'>Invalid change id.</p>", status_code=400)
        result = await pool.execute(
            "UPDATE source_catalog_changes SET acknowledged_at = NOW(), acknowledged_by = $3 "
            "WHERE source_id = $1 AND id = $2 AND acknowledged_at IS NULL",
            source_id,
            target,
            admin,
        )
    else:
        result = await pool.execute(
            "UPDATE source_catalog_changes SET acknowledged_at = NOW(), acknowledged_by = $2 "
            "WHERE source_id = $1 AND acknowledged_at IS NULL",
            source_id,
            admin,
        )
    count = int(str(result).rsplit(" ", 1)[-1]) if result else 0
    await audit_admin_action(
        request,
        action="data_source.catalog_changes_acknowledge",
        resource="data_source",
        resource_id=source_id,
        success=True,
        detail=mutation_audit_detail(
            status_code=200, extra={"change_id": change_id or "all", "acknowledged": count}
        ),
    )
    return await _section(request, source_id, notice=f"{count} change(s) acknowledged.")


@router.post(
    "/dashboard/data-sources/{source_id}/catalog/annotations",
    response_class=HTMLResponse,
)
async def catalog_annotate(
    source_id: str,
    request: Request,
    path: str = Form(...),
    classification: str = Form(""),
) -> HTMLResponse:
    """Set, or clear with an empty value, the admin's classification of a node.

    Annotations live apart from node rows, so a rescan never erases them, and
    an admin's decision always replaces the wizard's or the scanner's.
    """
    pool = request.app.state.pg_pool
    node_path = _parse_path(path)
    if not node_path or (classification and classification not in CLASSIFICATIONS):
        return HTMLResponse("<span class='muted'>Invalid annotation.</span>", status_code=400)
    exists = await pool.fetchval(
        "SELECT 1 FROM source_catalog WHERE source_id = $1 AND path = $2::text[]",
        source_id,
        node_path,
    )
    if not exists:
        return HTMLResponse("<span class='muted'>Unknown catalog node.</span>", status_code=404)
    before = await pool.fetchval(
        "SELECT classification FROM source_catalog_annotations "
        "WHERE source_id = $1 AND path = $2::text[]",
        source_id,
        node_path,
    )
    if classification:
        await pool.execute(
            """
            INSERT INTO source_catalog_annotations
                (source_id, path, classification, classification_source, applied_by)
            VALUES ($1, $2::text[], $3, 'admin', $4)
            ON CONFLICT (source_id, path) DO UPDATE SET
                classification = EXCLUDED.classification,
                classification_source = 'admin',
                applied_by = EXCLUDED.applied_by,
                applied_at = NOW()
            """,
            source_id,
            node_path,
            classification,
            _admin_username(request),
        )
    else:
        await pool.execute(
            "DELETE FROM source_catalog_annotations WHERE source_id = $1 AND path = $2::text[]",
            source_id,
            node_path,
        )
    await audit_admin_action(
        request,
        action="data_source.catalog_annotate",
        resource="data_source",
        resource_id=source_id,
        success=True,
        detail=mutation_audit_detail(
            before={"classification": before},
            after={"classification": classification or None},
            status_code=200,
            extra={"path": node_path},
        ),
    )
    return _render(
        request,
        "partials/catalog_classification.html",
        {
            "source_id": source_id,
            "path_param": json.dumps(node_path, separators=(",", ":")),
            "classification": classification or None,
            "classification_source": "admin" if classification else None,
            "classifications": CLASSIFICATIONS,
        },
    )


@router.get("/dashboard/catalog/children", response_class=HTMLResponse)
async def catalog_children(
    request: Request, source_id: str, path: str = "", pick: bool = False
) -> HTMLResponse:
    node_path = _parse_path(path)
    if node_path is None:
        return HTMLResponse("<p class='muted'>Invalid path.</p>", status_code=400)
    pool = request.app.state.pg_pool
    nodes = await catalog_read.children(pool, source_id, node_path)
    picks: dict[str, Any] = {}
    if pick:
        row = await pool.fetchrow(
            "SELECT source_type, metadata FROM data_sources WHERE source_id = $1", source_id
        )
        if row is not None:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata or "{}")
            key = get_connector(str(row["source_type"]), metadata or None).key
            picks = {
                node.resource_key: pick_targets(key, node.node_type, node.resource_key)
                for node in nodes
            }
    return _render(
        request,
        "partials/catalog_children.html",
        {
            "source_id": source_id,
            "nodes": nodes,
            "classifications": CLASSIFICATIONS,
            "pick": pick,
            "picks": picks,
        },
    )


@router.get("/dashboard/catalog/picker", response_class=HTMLResponse)
async def catalog_picker(request: Request, cond_source_id: str = "") -> HTMLResponse:
    """The picker for a policy's source, once one is chosen and catalogued."""
    source_id = cond_source_id.strip()
    if not source_id:
        return HTMLResponse("")
    view = await load_catalog_view(request.app.state.pg_pool, source_id)
    if view is None or not view.scanned:
        return HTMLResponse("")
    return _render(request, "partials/catalog_picker.html", {"picker_source_id": source_id})


def _statements_from_form(form: Any) -> list[dict[str, Any]]:
    """Statement rows as typed so far; incomplete rows are kept for linting."""
    getlist = getattr(form, "getlist", None)
    if getlist is None:
        return []
    columns = [
        list(getlist(name))
        for name in (
            "permission_effect",
            "permission_action",
            "permission_resource_type",
            "permission_resource_pattern",
            "permission_constraints",
        )
    ]
    rows: list[dict[str, Any]] = []
    for index in range(max((len(c) for c in columns), default=0)):
        effect, action, resource_type, pattern, constraint_text = (
            str(c[index]).strip() if index < len(c) else "" for c in columns
        )
        try:
            constraints = json.loads(constraint_text) if constraint_text else {}
        except ValueError:
            constraints = {}
        rows.append(
            {
                "effect": effect or "allow",
                "action": action,
                "resource_type": resource_type,
                "resource_pattern": pattern,
                "constraints": constraints,
            }
        )
    return rows


@router.post("/dashboard/data-sources/{source_id}/roles/lint", response_class=HTMLResponse)
async def lint_role(source_id: str, request: Request) -> HTMLResponse:
    pool = request.app.state.pg_pool
    row = await pool.fetchrow(
        "SELECT source_type, metadata FROM data_sources WHERE source_id = $1", source_id
    )
    if row is None:
        return HTMLResponse("")
    form = await request.form()
    statements = _statements_from_form(form)
    # The connector's vocabulary first: these are the problems that block a
    # save, shown while the author is still typing.
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata or "{}")
    vocab = vocabulary_for(get_connector(str(row["source_type"]), metadata or None).key)
    warnings: list[LintWarning] = []
    for index, statement in enumerate(statements):
        if not statement["action"]:
            continue
        for issue in validate_statements(vocab, [statement]):
            prefix = "Will not save: " if issue.severity == "error" else ""
            warnings.append(
                LintWarning(
                    code=issue.code,
                    message=prefix + issue.message,
                    index=index,
                    field=issue.field,
                )
            )
    view = await load_catalog_view(pool, source_id)
    if view is not None:
        warnings.extend(lint_statements(view, statements))
    return _render(request, "partials/catalog_lint.html", {"warnings": warnings, "checked": True})


def _split(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


async def _policy_warnings(
    pool: Any, *, source_id: str, conditions: dict[str, Any], actions: dict[str, Any]
) -> list[LintWarning]:
    view = await load_catalog_view(pool, source_id) if source_id else None
    return lint_policy(
        view,
        tables=_split(conditions.get("tables")),
        columns=_split(conditions.get("columns")),
        redact_columns=_split(actions.get("redact_columns")),
        effect=str(actions.get("effect") or "deny"),
    )


@router.post("/dashboard/policies/lint", response_class=HTMLResponse)
async def lint_policy_form(request: Request) -> HTMLResponse:
    form = await request.form()
    warnings = await _policy_warnings(
        request.app.state.pg_pool,
        source_id=str(form.get("cond_source_id") or "").strip(),
        conditions={
            "tables": str(form.get("cond_tables") or ""),
            "columns": str(form.get("cond_columns") or ""),
        },
        actions={
            "effect": str(form.get("action_effect") or "allow"),
            "redact_columns": str(form.get("action_redact_columns") or ""),
        },
    )
    return _render(request, "partials/catalog_lint.html", {"warnings": warnings, "checked": True})


@router.post("/api/policies/validate", response_model=None)
async def validate_policy(request: Request) -> dict[str, Any] | JSONResponse:
    """Catalog warnings for a policy rule, without saving it.

    Body: `{"conditions": {...}, "actions": {...}}` in the shape
    `/api/policies` stores. Warnings are advisory; nothing here refuses a rule.
    """
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=422)
    raw_conditions = body.get("conditions")
    raw_actions = body.get("actions")
    conditions: dict[str, Any] = raw_conditions if isinstance(raw_conditions, dict) else {}
    actions: dict[str, Any] = raw_actions if isinstance(raw_actions, dict) else {}
    source_ids = _split(conditions.get("source_ids")) or _split(conditions.get("source_id"))
    warnings: list[LintWarning] = []
    for source_id in source_ids or [""]:
        warnings.extend(
            await _policy_warnings(
                request.app.state.pg_pool,
                source_id=source_id,
                conditions=conditions,
                actions=actions,
            )
        )
    return {"warnings": [w.as_dict() for w in warnings]}


@router.get("/dashboard/catalog", response_class=HTMLResponse)
async def catalog_page(
    request: Request,
    q: str = "",
    source: str = "",
    node_type: str = "",
    classification: str = "",
) -> HTMLResponse:
    from interlock.admin.routes.dashboard import _is_inner_htmx, _is_sidebar_htmx

    pool = request.app.state.pg_pool
    q = q.strip()[:200]
    if node_type not in NODE_TYPE_FILTERS:
        node_type = ""
    if classification not in (*CLASSIFICATIONS, "pii_any"):
        classification = ""
    results = await catalog_read.search(
        pool, query=q, source_id=source, node_type=node_type, classification=classification
    )
    source_rows = await pool.fetch(
        "SELECT DISTINCT source_id FROM source_catalog ORDER BY source_id"
    )
    ctx: dict[str, Any] = {
        "active_page": "catalog",
        "q": q,
        "source": source,
        "node_type": node_type,
        "classification": classification,
        "results": results,
        "result_limit": catalog_read.SEARCH_LIMIT,
        "sources": [str(r["source_id"]) for r in source_rows],
        "node_types": NODE_TYPE_FILTERS,
        "overview": await catalog_read.overview(pool),
    }
    if _is_inner_htmx(request):
        return _render(request, "partials/catalog_results.html", ctx)
    if _is_sidebar_htmx(request):
        ctx["content_only"] = True
    return _render(request, "pages/catalog.html", ctx)


@router.get("/api/catalog/naming-report")
async def catalog_naming_report(request: Request) -> dict[str, Any]:
    """Role statements and deny policies whose meaning catalog naming changes."""
    findings = await naming_report(request.app.state.pg_pool)
    return {"findings": [finding.as_dict() for finding in findings]}


@router.get("/dashboard/catalog/analytics", response_class=HTMLResponse)
async def catalog_analytics(request: Request, source: str = "", days: int = 30) -> HTMLResponse:
    """Most-read tables, PII columns read, refusals and unused tables for one source."""
    from interlock.admin.routes.dashboard import _is_sidebar_htmx

    pool = request.app.state.pg_pool
    days = days if days in (7, 30, 90) else 30
    sources = [
        str(row["source_id"])
        for row in await pool.fetch(
            "SELECT DISTINCT source_id FROM source_catalog "
            "WHERE node_type = 'column' ORDER BY source_id"
        )
    ]
    if source not in sources:
        source = sources[0] if sources else ""
    ctx: dict[str, Any] = {
        "active_page": "catalog",
        "sources": sources,
        "source": source,
        "days": days,
        "analytics": await access_analytics(pool, source, days=days) if source else None,
    }
    if _is_sidebar_htmx(request):
        ctx["content_only"] = True
    return _render(request, "pages/catalog_analytics.html", ctx)
