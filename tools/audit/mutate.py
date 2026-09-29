#!/usr/bin/env python3
"""Prove that a test actually detects the defect it is supposed to guard.

A test that passes whether or not the control exists is not coverage. This
tool disables one governance control at a time, runs the tests that claim to
cover it, and reports whether anything noticed.

    A mutation that SURVIVES is a finding, not a pass.

This is the technique that caught every regression introduced while fixing the
Write Safety defect, and it is the only check in the audit that does not depend
on the author's judgement about what is worth testing. The suite that missed
that defect was large and green; what it lacked was any evidence that its
assertions were load-bearing.

Usage::

    python tools/audit/mutate.py --list
    python tools/audit/mutate.py --run source-role-always-allow
    python tools/audit/mutate.py --all --scope unit
    python tools/audit/mutate.py --all --json report.json

Mutations tagged ``scope="e2e"`` need the compose stack up (``make e2e-up``).
Rather than rebuilding the image per mutation, the patched file is copied into
the running container and the process restarted, which takes seconds instead of
minutes. The original is always restored, including on failure or interrupt.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    """One disabled control, and the tests that should object."""

    name: str
    control: str
    path: str
    old: str
    new: str
    tests: tuple[str, ...]
    scope: str = "unit"
    containers: tuple[str, ...] = ()
    note: str = ""
    # A survival that is already known, tracked, and accepted. Reported on
    # every run but not fatal, so a documented gap does not block the release
    # gate - and flagged loudly if it stops surviving, because that means the
    # gap was closed and this annotation is now lying.
    expected_survivor: bool = False


@dataclass
class Result:
    mutation: str
    control: str
    caught: bool
    scope: str
    duration_s: float
    detail: str = ""
    failing_tests: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The registry
#
# Each entry names a guarantee the platform makes, the smallest edit that
# breaks it, and the tests that claim to cover it. Keep `new` a drop-in
# replacement: a mutation that fails to import proves nothing.
# ---------------------------------------------------------------------------

MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="approval-execution-noop",
        control="Write safety: an approved write is executed",
        path="src/interlock/core/approval_queue.py",
        old="                    await self._execute_sql(row, metadata)",
        new="                    pass  # MUTATION: approved write never executed",
        tests=(
            "tests/e2e/test_write_approval_execution.py",
            "tests/e2e/test_seeded_mysql_connector.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-admin-1", "interlock-e2e-gateway-1"),
        note=(
            "This reproduces the shipped defect exactly. Before the audit it "
            "survived the entire suite."
        ),
    ),
    Mutation(
        name="approval-failure-unrecorded",
        control="Write safety: a failed write is recorded as failed",
        path="src/interlock/core/approval_queue.py",
        old="jsonb_build_object('execution_error', $2::text, 'failed_at', NOW())",
        new="jsonb_build_object('execution_error', $2, 'failed_at', NOW())",
        tests=("tests/e2e/test_write_approval_execution.py",),
        scope="e2e",
        containers=("interlock-e2e-admin-1", "interlock-e2e-gateway-1"),
        note="Restores the un-castable parameter that made failures read as successes.",
    ),
    Mutation(
        name="source-role-always-allow",
        control="Authorization: source roles gate access to a source",
        path="src/interlock/core/source_roles.py",
        old="    async def evaluate(self, request: PermissionRequest) -> SourceRoleDecision:",
        new=(
            "    async def evaluate(self, request: PermissionRequest) -> SourceRoleDecision:\n"
            "        return SourceRoleDecision(allowed=True, reason='MUTATION')  # noqa"
        ),
        tests=(
            "tests/e2e/test_source_roles_enforcement.py",
            "tests/e2e/test_seeded_mysql_connector.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="redaction-passthrough",
        control="Data protection: PII is redacted from responses",
        path="src/interlock/pipeline/redactor.py",
        # Targets the method actually on the response path. An earlier version
        # of this mutation patched `redact_row`, which nothing calls, and
        # "survived" without disabling anything - a false coverage hole. Any
        # survival here must be checked the same way before being reported.
        old="        if not text or not matches:\n            return text",
        new="        return text  # MUTATION: redaction disabled\n        if not text or not matches:\n            return text",
        # Scanner-driven redaction is asserted in unit and integration tests;
        # the visible [REDACTED:POLICY] in an e2e response comes from policy
        # field redaction, which is a different mechanism. An e2e-only
        # selection made this look like a coverage hole when it is not.
        tests=(
            "tests/unit/test_processor.py",
            "tests/unit/test_p0_d_pg_redaction.py",
            "tests/integration/test_governance.py",
            "tests/e2e/test_seeded_http_governance.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    # --- Phase 1: authorization ------------------------------------------
    Mutation(
        name="source-role-explicit-deny-ignored",
        control="Authorization: an explicit deny beats any allow",
        path="src/interlock/core/source_roles.py",
        old='                if stmt.effect == "deny"\n',
        new="                if False  # MUTATION: explicit deny no longer wins\n",
        tests=(
            "tests/e2e/test_connector_governance_uniformity.py",
            "tests/e2e/test_authorization_guarantees.py",
            "tests/e2e/test_source_roles_enforcement.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="source-role-update-crosses-sources",
        control="Authorization: a role is only rewritten under its own source",
        path="src/interlock/admin/routes/dashboard.py",
        old="    before_role = await _source_role_row(pool, source_id, role_id)\n    if before_role is None:\n",
        new="    before_role = await _source_role_row(pool, source_id, role_id)\n    if False:  # MUTATION: any role id is accepted under any source\n",
        tests=("tests/unit/test_source_role_editor.py",),
        scope="unit",
    ),
    Mutation(
        name="source-role-delete-ignores-active-grants",
        control="Authorization: deleting a role never silently revokes a live grant",
        path="src/interlock/admin/routes/dashboard.py",
        old="    if holders:\n        names = ",
        new="    if False:  # MUTATION: a held role is deleted anyway\n        names = ",
        tests=("tests/unit/test_source_role_editor.py",),
        scope="unit",
    ),
    Mutation(
        name="identity-delete-skips-tombstone",
        control="Audit: a deleted identity is still named in the audit views",
        path="src/interlock/admin/routes/identities.py",
        old="                INSERT INTO identity_tombstones (identity_id, name, team, deleted_by)",
        new="                SELECT 1 -- MUTATION: the name is lost with the identity row",
        tests=("tests/unit/test_admin_api.py",),
        scope="unit",
    ),
    Mutation(
        name="connector-activation-not-enforced",
        control="Onboarding: a source is only registered on an active connector",
        path="src/interlock/connections/activation.py",
        old="    if connector_key not in await active_connector_keys(pool):",
        new="    if False:  # MUTATION: every known connector is accepted",
        tests=("tests/unit/test_connector_activation.py",),
        scope="unit",
    ),
    Mutation(
        name="source-id-not-validated",
        control="Onboarding: a supplied source id is a valid database name and path segment",
        path="src/interlock/admin/source_ids.py",
        old="    if not SOURCE_ID_RE.fullmatch(value):",
        new="    if False:  # MUTATION: any string becomes a source id",
        tests=("tests/unit/test_source_ids.py", "tests/unit/test_admin_api.py"),
        scope="unit",
    ),
    Mutation(
        name="policy-panel-lists-other-sources",
        control="Console: a source page lists only the policies the engine applies to it",
        path="src/interlock/admin/policy_summary.py",
        old="    return not sources or source_id in sources",
        new="    return True  # MUTATION: every rule is shown on every source",
        tests=("tests/unit/test_policy_summary.py", "tests/unit/test_detail_pages.py"),
        scope="unit",
    ),
    Mutation(
        name="usage-cache-rate-counts-refusals",
        control="Console: the cache rate is over served requests, not refusals",
        path="src/interlock/audit/cost.py",
        old='        served = summary["upstream"] + summary["cached"]',
        new='        served = summary["total"]  # MUTATION: denied and failed requests dilute the rate',
        tests=("tests/unit/test_cost.py",),
        scope="unit",
    ),
    Mutation(
        name="role-vocabulary-accepts-unknown-action",
        control="Roles: a statement names only actions the connector's requests carry",
        path="src/interlock/connections/role_vocabulary.py",
        old="    matched = vocab.actions_matching(action)\n    if not matched:\n",
        new="    matched = vocab.actions_matching(action)\n    if False:  # MUTATION: any action is accepted\n",
        tests=("tests/unit/test_role_vocabulary.py",),
        scope="unit",
    ),
    Mutation(
        name="role-vocabulary-accepts-unknown-constraint",
        control="Roles: a condition the evaluator ignores cannot be saved",
        path="src/interlock/connections/role_vocabulary.py",
        old="        if key not in EVALUATED_CONSTRAINT_KEYS:",
        new="        if False:  # MUTATION: unread condition keys pass",
        tests=("tests/unit/test_role_vocabulary.py",),
        scope="unit",
    ),
    Mutation(
        name="role-vocabulary-grandfathers-everything",
        control="Roles: only an unchanged stored statement is kept as saved",
        path="src/interlock/connections/role_vocabulary.py",
        old="    kept = {_normalized(s) for s in stored}",
        new="    kept = {_normalized(s) for s in [*stored, *statements]}  # MUTATION: all kept",
        tests=("tests/unit/test_role_vocabulary.py",),
        scope="unit",
    ),
    Mutation(
        name="constraint-keys-drift",
        control="Roles: the channel condition is enforced by the evaluator",
        path="src/interlock/core/source_roles.py",
        old='    "channel_ids": "channel_id",\n',
        new="",
        tests=("tests/unit/test_role_vocabulary.py", "tests/unit/test_source_roles.py"),
        scope="unit",
    ),
    Mutation(
        name="qdrant-resource-type-dropped",
        control="Roles: Qdrant requests are checked against the collection their template grants",
        path="src/interlock/connections/connectors.py",
        old="            resource_type=resource_type,\n            risk_level=(\n",
        new="            risk_level=(\n",
        tests=("tests/unit/test_role_vocabulary.py", "tests/unit/test_connector_registry.py"),
        scope="unit",
    ),
    Mutation(
        name="role-editor-skips-vocabulary",
        control="Roles: the console refuses a statement outside the source's vocabulary",
        path="src/interlock/admin/routes/dashboard.py",
        old="        error = _vocabulary_error(await _role_vocabulary(pool, source_id), permissions, [])",
        new="        error = None  # MUTATION: statements are saved unchecked",
        tests=("tests/unit/test_source_onboarding_routes.py",),
        scope="unit",
    ),
    Mutation(
        name="role-editor-drops-legacy-statement",
        control="Roles: a saved statement outside the vocabulary is shown and kept, not lost",
        path="src/interlock/admin/templates/partials/source_role_statement_row.html",
        old="      {% if perm.action and not action_known %}",
        new="      {% if false %}",
        tests=("tests/unit/test_source_role_editor.py",),
        scope="unit",
    ),
    Mutation(
        name="role-row-drops-conditions",
        control="Roles: re-rendering a statement row never loses its conditions",
        path="src/interlock/admin/routes/dashboard.py",
        old='        "constraints": constraints,\n    }\n    uid = ',
        new='        "constraints": {},  # MUTATION: conditions lost on re-render\n    }\n    uid = ',
        tests=("tests/unit/test_source_role_editor.py",),
        scope="unit",
    ),
    Mutation(
        name="mcp-cache-before-registry-check",
        control="Cache: a disabled source serves no cached MCP answer",
        path="src/interlock/gateway/mcp_adapter.py",
        old="        if live_registry is not None and live_registry.get(source_id) is None:",
        new="        if False:  # MUTATION: the cache answers for a withdrawn source",
        tests=("tests/unit/test_disabled_source_serves_nothing.py",),
        scope="unit",
    ),
    Mutation(
        name="pg-session-ignores-disabled-source",
        control="Cache: an open PG-wire session ends when its source is disabled",
        path="src/interlock/gateway/pg_proxy.py",
        old="            self._registry is not None\n            and bool(source_id)",
        new="            False  # MUTATION: sessions outlive their source\n            and bool(source_id)",
        tests=("tests/unit/test_disabled_source_serves_nothing.py",),
        scope="unit",
    ),
    Mutation(
        name="registry-reload-forgets-removed-sources",
        control="Cache: a registry reload clears pools and cache for dropped sources",
        path="src/interlock/metadata/registry.py",
        old="        if removed:\n            await self._notify_removed(removed)",
        new="        if False:  # MUTATION: removed sources keep their cache\n            pass",
        tests=("tests/unit/test_disabled_source_serves_nothing.py",),
        scope="unit",
    ),
    Mutation(
        name="forced-change-gate-skipped",
        control="Admin auth: an admin on the default password reaches nothing else",
        path="src/interlock/admin/auth_middleware.py",
        old="        if session.must_change_password and not _reachable_before_password_change(path):",
        new="        if False:  # MUTATION: the default password opens the whole console",
        tests=("tests/unit/test_admin_forced_password_change.py",),
        scope="unit",
    ),
    Mutation(
        name="default-password-accepted-on-change",
        control="Admin auth: the default password cannot be chosen again",
        path="src/interlock/admin/routes/password.py",
        old="    if len(new) < MIN_PASSWORD_LENGTH:",
        new="    if False:  # MUTATION: any new password is accepted",
        tests=("tests/unit/test_admin_forced_password_change.py",),
        scope="unit",
    ),
    # --- Source catalog -------------------------------------------------
    Mutation(
        name="catalog-scan-error-unscrubbed",
        control="Catalog: a failed scan never records a connection string",
        path="src/interlock/catalog/errors.py",
        old='    cleaned = _DSN.sub("<redacted-url>", message)',
        new="    cleaned = message  # MUTATION: credentials reach the scan history",
        tests=("tests/unit/test_catalog.py",),
        scope="unit",
    ),
    Mutation(
        name="catalog-truncated-scan-claims-completeness",
        control="Catalog: a capped scan never marks unreached nodes removed",
        path="src/interlock/catalog/model.py",
        old="        return any(path[: len(subtree)] == subtree for subtree in self.complete_subtrees)",
        new="        return True  # MUTATION: every scan counts as complete",
        tests=("tests/unit/test_catalog.py", "tests/e2e/test_source_catalog.py"),
        scope="unit",
    ),
    Mutation(
        name="catalog-save-path-unhooked",
        control="Catalog: every source save path queues a scan",
        path="src/interlock/admin/routes/dashboard.py",
        old='    await after_source_saved(request, state["source_id"])',
        new="    pass  # MUTATION: the wizard no longer queues a scan",
        tests=("tests/unit/test_catalog.py", "tests/e2e/test_source_catalog.py"),
        scope="unit",
    ),
    Mutation(
        name="catalog-scheduler-ignores-interval",
        control="Catalog: the scheduler refreshes a source only once its interval has passed",
        path="src/interlock/catalog/scheduler.py",
        old="        elif last_success is not None and now - last_success >= timedelta(seconds=interval):",
        new="        elif last_success is not None:  # MUTATION: every tick rescans every source",
        tests=("tests/unit/test_catalog.py",),
        scope="unit",
    ),
    Mutation(
        name="catalog-request-stuck-behind-scheduled-scan",
        control="Catalog: a scan request brings a waiting scan forward",
        path="src/interlock/catalog/queue.py",
        old="    not_before = LEAST(source_catalog_scans.not_before, EXCLUDED.not_before),",
        new="    not_before = source_catalog_scans.not_before,  -- MUTATION: requests wait",
        tests=("tests/e2e/test_source_catalog.py",),
        scope="e2e",
    ),
    Mutation(
        name="catalog-rescan-erases-annotations",
        control="Catalog: an admin's classification survives every rescan",
        path="src/interlock/catalog/store.py",
        old="    async with conn.transaction():",
        new=(
            "    await conn.execute(  # MUTATION: a rescan wipes classifications\n"
            '        "DELETE FROM source_catalog_annotations WHERE source_id = $1", source_id\n'
            "    )\n"
            "    async with conn.transaction():"
        ),
        tests=("tests/e2e/test_source_catalog.py",),
        scope="e2e",
        containers=("interlock-e2e-worker-1-1", "interlock-e2e-worker-2-1"),
    ),
    Mutation(
        name="catalog-wizard-pii-flags-dropped",
        control="Catalog: the wizard's PII flags are recorded as annotations",
        path="src/interlock/admin/routes/dashboard.py",
        old='    for path in _wizard_pii_paths(state["pii_columns"]):',
        new="    for path in []:  # MUTATION: wizard PII flags are discarded",
        tests=("tests/unit/test_source_wizard.py", "tests/e2e/test_source_catalog.py"),
        scope="unit",
    ),
    Mutation(
        name="catalog-discovery-never-prunes",
        control="Discovery: tables removed from the catalog leave discovery",
        path="src/interlock/discovery/schema_indexer.py",
        old="              AND asset_path <> ALL($2::text[])",
        new="              AND FALSE  -- MUTATION: removed tables stay discoverable",
        tests=("tests/e2e/test_source_catalog.py",),
        scope="e2e",
        containers=("interlock-e2e-worker-1-1", "interlock-e2e-worker-2-1"),
    ),
    Mutation(
        name="sql-naming-mysql-reads-as-public",
        control="SQL naming: a bare MySQL table resolves to the configured database",
        path="src/interlock/core/sql_governance.py",
        old="        default = database or LEGACY_SCHEMA\n",
        new="        default = LEGACY_SCHEMA  # MUTATION: every bare name is public again\n",
        tests=(
            "tests/unit/test_sql_governance.py",
            "tests/e2e/test_sql_resource_bypass_characterization.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="sql-naming-legacy-alias-ignored",
        control="SQL naming: roles written as public.<table> keep matching MySQL and Snowflake",
        path="src/interlock/core/source_roles.py",
        old='            aliases = [resource, *variants.get("aliases", [])]\n',
        new="            aliases = [resource]  # MUTATION: legacy spellings stop matching\n",
        tests=("tests/unit/test_sql_governance.py",),
        scope="unit",
    ),
    Mutation(
        name="sql-naming-shadows-ignored",
        control="SQL naming: a deny on a schema earlier on the search path still fires",
        path="src/interlock/core/source_roles.py",
        old='            deny_forms = [*aliases, *variants.get("shadows", [])]\n',
        new="            deny_forms = aliases  # MUTATION: a stale catalog routes around denies\n",
        tests=("tests/unit/test_sql_governance.py",),
        scope="unit",
    ),
    Mutation(
        name="policy-deny-matches-only-as-written",
        control="Policy: a deny on a table fires whichever spelling the query uses",
        path="src/interlock/gateway/pipeline.py",
        old="                deny_table_spellings=view.deny_table_spellings,\n",
        new="                deny_table_spellings=None,  # MUTATION: only exact spellings match\n",
        tests=("tests/e2e/test_sql_resource_bypass_characterization.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="dry-run-off-the-gateway-derivation",
        control="Dry-run: the console explains what the gateway would evaluate",
        path="src/interlock/admin/routes/dashboard.py",
        old="    if sql and connector.key in _SQL_DRY_RUN_CONNECTORS:\n        view = await _dry_run_pipeline(request).governance_view(\n            sql_gateway_request(\n                sql,\n                protocol=protocol,",
        new="    if False:  # MUTATION: the dry-run derives on its own again\n        view = await _dry_run_pipeline(request).governance_view(\n            sql_gateway_request(\n                sql,\n                protocol=protocol,",
        tests=("tests/e2e/test_sql_governance_parity.py",),
        scope="e2e",
        containers=("interlock-e2e-admin-1",),
    ),
    Mutation(
        name="pg-wire-ignores-catalog-naming",
        control="SQL naming: the PG wire resolves table names as MCP does",
        path="src/interlock/gateway/pg_proxy.py",
        old="            sql_naming=self._sql_naming,\n",
        new="            sql_naming=None,  # MUTATION: the PG wire keeps legacy naming\n",
        tests=("tests/e2e/test_sql_governance_parity.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="column-access-never-derived",
        control="Columns: a request carries the columns a statement reads",
        path="src/interlock/gateway/pipeline.py",
        old="        if request.sql and naming is not None and not request.columns:\n",
        new="        if False:  # MUTATION: every request is table-level again\n",
        tests=(
            "tests/unit/test_sql_columns.py",
            "tests/e2e/test_sql_resource_bypass_characterization.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="column-gap-does-not-fail-closed",
        control="Columns: undeterminable columns under a column deny are refused",
        path="src/interlock/core/source_roles.py",
        old='            if any(stmt.effect == "deny" for stmt in scoped):\n',
        new="            if False:  # MUTATION: a whole-row read slips past a column deny\n",
        tests=(
            "tests/unit/test_sql_columns.py",
            "tests/e2e/test_sql_resource_bypass_characterization.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="column-reads-only-from-projections",
        control="Columns: WHERE, JOIN and ORDER BY columns count as read",
        path="src/interlock/core/sql_columns.py",
        old="    for column in scope.columns:\n",
        new=(
            "    for column in [  # MUTATION: only the SELECT list counts\n"
            "        c for s in scope.expression.selects for c in s.find_all(exp.Column)\n"
            "    ]:\n"
        ),
        tests=("tests/unit/test_sql_columns.py",),
        scope="unit",
    ),
    Mutation(
        name="column-star-not-expanded",
        control="Columns: SELECT * is expanded from the catalog",
        path="src/interlock/core/sql_columns.py",
        old='        schema.setdefault(s, {})[t] = {c: "unknown" for c in cols}\n',
        new="        pass  # MUTATION: the catalog never reaches qualify\n",
        tests=("tests/unit/test_sql_columns.py",),
        scope="unit",
    ),
    Mutation(
        name="redact-columns-ignores-lineage",
        control="Redaction: redact_columns follows a renamed column on MCP",
        path="src/interlock/gateway/mcp_adapter.py",
        old='                getattr(gateway_decision, "column_lineage", None) or {},\n',
        new="                {},  # MUTATION: only output names are matched\n",
        tests=("tests/e2e/test_sql_resource_bypass_characterization.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="role-lint-never-finds-unmatched-patterns",
        control="Role editor: a pattern that matches nothing in the catalog is flagged",
        path="src/interlock/catalog/validation.py",
        old="            if not any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):\n",
        new="            if False:  # MUTATION: every pattern looks fine\n",
        tests=("tests/unit/test_catalog_validation.py", "tests/e2e/test_source_catalog.py"),
        scope="unit",
    ),
    Mutation(
        name="role-lint-hides-column-cost",
        control="Role editor: a column-level allow says what it costs",
        path="src/interlock/catalog/validation.py",
        old='        if statement.get("effect", "allow") == "allow" and _is_column_scoped(statement):\n',
        new="        if False:  # MUTATION: column allows look like table allows\n",
        tests=("tests/unit/test_catalog_validation.py",),
        scope="unit",
    ),
    Mutation(
        name="describe-access-names-unreachable-tables",
        control="describe_access: never names a table the caller cannot read",
        path="src/interlock/gateway/mcp_adapter.py",
        old='            if not readable and f"{table}.*" not in whole:\n',
        new="            if False:  # MUTATION: every catalogued table is listed\n",
        tests=("tests/e2e/test_describe_resources.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="describe-access-ignores-denies",
        control="describe_access: denied columns are not listed as readable",
        path="src/interlock/core/source_roles.py",
        old='            if any(s.effect == "deny" for s in matching):\n                continue\n',
        new="            pass  # MUTATION: denies are not subtracted\n",
        tests=("tests/e2e/test_describe_resources.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="describe-access-claims-enforcement",
        control="describe_access: non-SQL sources say their resources are not enforced",
        path="src/interlock/gateway/mcp_adapter.py",
        old="        if connector_key not in _CATALOG_ENFORCED_CONNECTORS:\n",
        new="        if False:  # MUTATION: every source looks enforced\n",
        tests=("tests/e2e/test_describe_resources.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="service-collector-truncation-claims-completeness",
        control="Catalog: a capped non-SQL scan never reports unreached nodes as removed",
        path="src/interlock/catalog/collectors/services.py",
        old="    snapshot.complete_subtrees = [()] if not truncation else list(complete or [])\n",
        new="    snapshot.complete_subtrees = [()]  # MUTATION: capped scans claim completeness\n",
        tests=("tests/unit/test_catalog_services.py",),
        scope="unit",
    ),
    Mutation(
        name="drift-exposure-ignores-denies",
        control="Drift: a role that denies a new object is not reported as exposing it",
        path="src/interlock/core/source_roles.py",
        old='    return any(s.effect == "allow" for s in matching) and not any(\n',
        new='    return any(s.effect == "allow" for s in matching) or not any(  # MUTATION\n',
        tests=("tests/unit/test_catalog_insight.py",),
        scope="unit",
    ),
    Mutation(
        name="describe-access-ignores-legacy-aliases",
        control="describe_access: a legacy public. role is described as reaching its table",
        path="src/interlock/core/source_roles.py",
        old="            forms = [resource, *(aliases or {}).get(resource, [])]\n",
        new="            forms = [resource]  # MUTATION: legacy spellings are not described\n",
        tests=("tests/e2e/test_describe_resources.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="pg-wire-multi-statement-tables-dropped",
        control="Authorization: every statement in a PG-wire message is governed",
        path="src/interlock/gateway/pg_proxy.py",
        old="            tables = _tables_across_statements(statements)",
        new="            tables = []  # MUTATION: only the first statement is governed",
        tests=(
            "tests/unit/test_pg_proxy.py",
            "tests/e2e/test_sql_resource_bypass_characterization.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="set-config-treated-as-read",
        control="Write safety: set_config is not a harmless read",
        path="src/interlock/core/write_classifier.py",
        old='    "set_config",\n',
        new="",
        tests=("tests/unit/test_write_classifier.py",),
        scope="unit",
    ),
    Mutation(
        name="opaque-data-read-unclassified",
        control="Write safety: functions that read data no parser sees are high risk",
        path="src/interlock/core/write_classifier.py",
        old="            opaque = _opaque_data_access_function(rendered)",
        new="            opaque = None  # MUTATION: opaque reads run unseen",
        tests=("tests/unit/test_write_classifier.py",),
        scope="unit",
    ),
    Mutation(
        name="source-role-no-grant-allowed",
        control="Authorization: an identity with no grant on a source is denied",
        path="src/interlock/core/source_roles.py",
        old="        if not statements:",
        new="        if False:  # MUTATION: missing grants no longer deny",
        tests=(
            "tests/e2e/test_authorization_guarantees.py",
            "tests/e2e/test_source_roles_enforcement.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="source-role-partial-coverage-allowed",
        control="Authorization: every requested resource must be allowed",
        path="src/interlock/core/source_roles.py",
        old="        if missing:\n            return SourceRoleDecision(",
        new="        if False:  # MUTATION: unallowed resources tolerated\n            return SourceRoleDecision(",
        tests=(
            "tests/e2e/test_source_roles_enforcement.py",
            "tests/e2e/test_seeded_pg_governance.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="grant-expiry-ignored",
        control="Authorization: an expired grant confers nothing",
        path="src/interlock/core/source_roles.py",
        old="  AND (g.expires_at IS NULL OR g.expires_at > NOW())",
        new="  AND (g.expires_at IS NULL OR TRUE)  -- MUTATION: expiry ignored",
        tests=(
            "tests/e2e/test_authorization_guarantees.py",
            "tests/e2e/test_source_roles_enforcement.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="grant-disabled-ignored",
        control="Authorization: a disabled grant confers nothing",
        path="src/interlock/core/source_roles.py",
        old="  AND g.enabled = TRUE",
        new="  AND (g.enabled = TRUE OR TRUE)  -- MUTATION: disabled grants honoured",
        tests=("tests/e2e/test_source_roles_enforcement.py",),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    # --- Phase 2: write safety -------------------------------------------
    Mutation(
        name="write-approval-bypassed",
        control="Write safety: risky writes require approval before executing",
        path="src/interlock/gateway/pipeline.py",
        old="        if risk_level not in (RiskLevel.HIGH, RiskLevel.MEDIUM):\n            return None",
        new="        if True:  # MUTATION: no write ever requires approval\n            return None",
        tests=(
            "tests/e2e/test_write_safety_guarantees.py",
            "tests/e2e/test_seeded_mysql_connector.py",
            "tests/e2e/test_seeded_http_governance.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="approval-expiry-guard-removed",
        control="Write safety: an expired approval cannot be approved",
        path="src/interlock/core/approval_queue.py",
        old="  AND (expires_at IS NULL OR expires_at > NOW())",
        new="  AND (expires_at IS NULL OR TRUE)  -- MUTATION: expired approvals approvable",
        tests=(
            "tests/e2e/test_write_safety_guarantees.py",
            "tests/e2e/test_seeded_http_governance.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-admin-1", "interlock-e2e-gateway-1"),
    ),
    Mutation(
        name="approval-status-guard-removed",
        control="Write safety: only a pending approval can be approved",
        path="src/interlock/core/approval_queue.py",
        old="  AND status = 'pending'",
        new="  AND status IS NOT NULL  -- MUTATION: any status approvable",
        tests=(
            "tests/e2e/test_write_safety_guarantees.py",
            "tests/e2e/test_write_approval_execution.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-admin-1", "interlock-e2e-gateway-1"),
    ),
    # --- Phase 3: data protection ----------------------------------------
    Mutation(
        name="pii-detection-blinded",
        control="Data protection: PII is detected in returned values",
        path="src/interlock/pipeline/pii_fast.py",
        old="    def scan(self, text: str) -> list[PIIMatch]:",
        new=(
            "    def scan(self, text: str) -> list[PIIMatch]:\n"
            "        return []  # MUTATION: detector finds nothing"
        ),
        tests=(
            "tests/e2e/test_seeded_http_governance.py",
            "tests/e2e/test_seeded_pg_governance.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="pii-fast-scan-disabled",
        control="Data protection: the fast scanner runs on every response",
        path="src/interlock/pipeline/processor.py",
        old="        if self._config.fast_enabled:",
        new="        if False:  # MUTATION: fast scan never runs",
        tests=(
            "tests/unit/test_processor.py",
            "tests/integration/test_governance.py",
            "tests/e2e/test_seeded_http_governance.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="policy-field-redaction-disabled",
        control="Data protection: policy-redacted fields are masked in the response",
        path="src/interlock/gateway/http_proxy.py",
        old='                    output[key] = "[REDACTED:POLICY]"',
        new="                    pass  # MUTATION: policy redaction not applied",
        tests=(
            "tests/e2e/test_data_protection_guarantees.py",
            "tests/e2e/test_seeded_http_governance.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    # --- Phase 4: audit integrity ----------------------------------------
    Mutation(
        name="audit-write-dropped",
        control="Audit integrity: every governed request produces a row",
        path="src/interlock/gateway/pipeline.py",
        old='        """Emit a canonical audit event, preserving strict durability failures."""\n        if self._audit is None:\n            return',
        new='        """Emit a canonical audit event, preserving strict durability failures."""\n        return  # MUTATION: audit never written\n        if self._audit is None:\n            return',
        tests=(
            "tests/e2e/test_regression_audit.py",
            "tests/e2e/test_source_roles_enforcement.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="audit-records-denials-as-success",
        control="Audit integrity: a denied request is recorded as denied",
        # Targets the single point every audit record passes through. An
        # earlier version mutated the egress-guard denial in http_proxy.py,
        # which the seeded sources never trigger - mutating unreachable code
        # proves nothing, and its survival was not a coverage hole.
        path="src/interlock/gateway/pipeline.py",
        old="                    status=status,",
        # tests listed below include the Phase 4 file
        new='                    status="success",  # MUTATION: every row logged as success',
        tests=(
            "tests/e2e/test_audit_integrity_guarantees.py",
            "tests/e2e/test_regression_audit.py",
            "tests/e2e/test_source_roles_enforcement.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    # --- Phase 5: cache correctness ---------------------------------------
    Mutation(
        name="cache-key-ignores-policy-scope",
        control="Cache: a policy change does not keep serving the old answer",
        path="src/interlock/core/normalizer.py",
        old='            policy_scope_hash or "",',
        new='            "",  # MUTATION: policy no longer separates cache entries',
        tests=(
            "tests/e2e/test_cache_correctness_guarantees.py",
            "tests/unit/test_p0_c_cache_key_identity_scope.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    Mutation(
        name="cache-key-ignores-write-barrier",
        control="Cache: a write invalidates entries via the source generation",
        path="src/interlock/core/normalizer.py",
        old="            str(0 if source_generation is None else int(source_generation)),",
        new='            "0",  # MUTATION: write barrier no longer separates entries',
        tests=(
            "tests/e2e/test_cache_correctness_guarantees.py",
            "tests/unit/test_cache_write_barrier.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
    # --- Phase 6: rate limiting -------------------------------------------
    Mutation(
        name="rate-limit-not-enforced",
        control="Rate limiting: an identity over its limit is refused",
        path="src/interlock/gateway/pipeline.py",
        old="            if not rate_result.allowed:",
        new="            if False:  # MUTATION: rate limit result ignored",
        # Selection derived by grepping for tests that reference the limiter,
        # rather than guessed - five earlier survivals in this audit were
        # selection errors, not coverage holes.
        tests=(
            "tests/e2e/test_rate_limit_guarantees.py",
            "tests/unit/test_gateway_pipeline.py",
        ),
        scope="e2e",
        containers=("interlock-e2e-gateway-1",),
    ),
)

BY_NAME = {m.name: m for m in MUTATIONS}


# ---------------------------------------------------------------------------
# Applying and restoring
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )


def _sync_to_containers(mutation: Mutation) -> None:
    """Copy the working-tree file into each container and restart it."""
    for container in mutation.containers:
        _run(["docker", "cp", str(ROOT / mutation.path), f"{container}:/app/{mutation.path}"])
    for container in mutation.containers:
        _run(["docker", "restart", container], timeout=180)
    if mutation.containers:
        _await_ready()


_READY_URLS = ("http://127.0.0.1:3001/ready", "http://127.0.0.1:9090/ready")


def _await_ready(timeout_s: int = 180) -> None:
    """Block until *every* service answers, so tests do not race a restart.

    An earlier version returned on the first healthy endpoint, which let the
    suite start while the other container was still coming up. Those tests then
    failed on connection errors and the runner counted the non-zero exit as
    "mutation caught" - another way to manufacture a catch nobody earned.
    """
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        healthy = 0
        for url in _READY_URLS:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status == 200:
                        healthy += 1
            except (urllib.error.URLError, OSError):
                pass
        if healthy == len(_READY_URLS):
            # Both answered; give in-process listeners a moment to resubscribe.
            time.sleep(1)
            return
        time.sleep(2)


def apply_mutation(mutation: Mutation) -> str:
    """Patch the file on disk and return the original text for restoration."""
    target = ROOT / mutation.path
    original = target.read_text()
    occurrences = original.count(mutation.old)
    if occurrences != 1:
        raise RuntimeError(
            f"{mutation.name}: anchor matched {occurrences} times in {mutation.path}; "
            "the mutation is stale and would test nothing"
        )
    target.write_text(original.replace(mutation.old, mutation.new))
    return original


def restore(mutation: Mutation, original: str) -> None:
    (ROOT / mutation.path).write_text(original)
    _sync_to_containers(mutation)


# ---------------------------------------------------------------------------
# Running one mutation
# ---------------------------------------------------------------------------


def run_mutation(mutation: Mutation, *, verbose: bool = False) -> Result:
    started = time.monotonic()
    original: str | None = None
    try:
        original = apply_mutation(mutation)
        _sync_to_containers(mutation)

        env_prefix = ["env", "INTERLOCK_E2E=1"] if mutation.scope == "e2e" else []
        proc = _run(
            [
                *env_prefix,
                "uv",
                "run",
                "--frozen",
                "pytest",
                *mutation.tests,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ]
        )
        output = proc.stdout + proc.stderr

        # A non-zero exit is not by itself evidence the control is covered.
        # Three ways it lies:
        #   - the mutation broke the module rather than the control
        #   - pytest could not find the selection (exit 4), so nothing ran
        #   - the selection matched no tests at all
        # Counting any of these as "caught" would manufacture exactly the false
        # confidence this tool exists to remove.
        broken = "ERROR collecting" in output or "ImportError" in output
        missing = proc.returncode == 4 or "file or directory not found" in output
        ran_nothing = "no tests ran" in output
        caught = proc.returncode != 0 and not (broken or missing or ran_nothing)

        failing = [
            line.split("::")[-1].split()[0]
            for line in output.splitlines()
            if line.startswith("FAILED")
        ]
        if broken:
            detail = "INVALID: mutation broke the module rather than the control"
        elif missing:
            detail = "INVALID: test selection not found - nothing ran"
        elif ran_nothing:
            detail = "INVALID: selection matched no tests"
        elif caught:
            detail = "detected"
        else:
            detail = "SURVIVED - no test objected"
        if verbose:
            print(output)
        return Result(
            mutation=mutation.name,
            control=mutation.control,
            caught=caught,
            scope=mutation.scope,
            duration_s=round(time.monotonic() - started, 1),
            detail=detail,
            failing_tests=failing[:8],
        )
    finally:
        if original is not None:
            restore(mutation, original)
            # Restoration is the one thing that must not fail silently.
            if (ROOT / mutation.path).read_text() != original:
                raise RuntimeError(f"FAILED TO RESTORE {mutation.path} - fix before continuing")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show the registry")
    parser.add_argument("--run", metavar="NAME", help="run one mutation")
    parser.add_argument("--all", action="store_true", help="run every mutation")
    parser.add_argument("--scope", choices=("unit", "e2e"), help="limit by scope")
    parser.add_argument("--json", metavar="PATH", help="write the report as JSON")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # This runner deliberately removes governance controls from src/ - it makes
    # the source-role evaluator return allow, makes redaction a no-op, stops
    # audit rows being written - and then reruns the suite to check something
    # objects. That is safe against the compose stack, whose upstreams are
    # disposable containers. Against the live certification stack it would mean
    # running a knowingly ungoverned proxy in front of a customer's managed
    # databases, S3 bucket, Slack workspace and Google tenant.
    #
    # Refuse rather than warn. Pinned by tests/unit/test_live_isolation.py.
    if os.environ.get("INTERLOCK_LIVE") == "1":
        print(
            "refusing to run: INTERLOCK_LIVE=1 is set.\n"
            "This runner disables governance controls on purpose, so it must never\n"
            "execute while the stack is configured against real upstream systems.\n"
            "Unset INTERLOCK_LIVE and run `make live-teardown` first.",
            file=sys.stderr,
        )
        return 2

    if args.list:
        for m in MUTATIONS:
            print(f"{m.name:<32} [{m.scope}]  {m.control}")
            if m.note:
                print(f"{'':<32}  note: {m.note}")
        return 0

    if args.run:
        if args.run not in BY_NAME:
            print(f"unknown mutation: {args.run}", file=sys.stderr)
            return 2
        selected = [BY_NAME[args.run]]
    elif args.all:
        selected = [m for m in MUTATIONS if not args.scope or m.scope == args.scope]
    else:
        parser.print_help()
        return 2

    results: list[Result] = []
    for mutation in selected:
        print(f"-- {mutation.name} ({mutation.scope}) ...", flush=True)
        try:
            result = run_mutation(mutation, verbose=args.verbose)
        except RuntimeError as exc:
            result = Result(
                mutation=mutation.name,
                control=mutation.control,
                caught=False,
                scope=mutation.scope,
                duration_s=0.0,
                detail=f"could not run: {exc}",
            )
        results.append(result)
        mark = "caught" if result.caught else "SURVIVED"
        print(f"   {mark}  ({result.duration_s}s)  {result.detail}")

    expected = {m.name for m in MUTATIONS if m.expected_survivor}
    survived = [r for r in results if not r.caught]
    unexpected = [r for r in survived if r.mutation not in expected]
    known = [r for r in survived if r.mutation in expected]
    stale = [r for r in results if r.caught and r.mutation in expected]

    print("\n" + "=" * 68)
    print(f"{len(results) - len(survived)}/{len(results)} mutations detected")

    if known:
        print("\nKNOWN GAPS - tracked and accepted, reported so they stay visible:")
        for r in known:
            print(f"  - {r.control}\n      mutation: {r.mutation}")

    if stale:
        print("\nSTALE ANNOTATIONS - these are marked as expected survivors but were caught.")
        print("The gap was closed; remove expected_survivor so it is enforced from now on:")
        for r in stale:
            print(f"  - {r.mutation}")

    if unexpected:
        print("\nCOVERAGE HOLES - these controls can be removed without any test objecting:")
        for r in unexpected:
            print(f"  - {r.control}\n      mutation: {r.mutation}  ({r.detail})")

    if args.json:
        Path(args.json).write_text(
            json.dumps([r.__dict__ for r in results], indent=2, sort_keys=True)
        )
        print(f"\nreport written to {args.json}")

    return 1 if (unexpected or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
