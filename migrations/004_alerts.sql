-- Onyx Migration 004: alerting
--
-- Operator-defined threshold rules plus a fire-log. The Phase 3 plan
-- (P3-T12) calls for threshold rules over latency, error rate, queue
-- depth, circuit-breaker state, and cost - we ship the first four
-- here; cost rules can layer on top of the same condition_type slot.
--
-- The evaluator is a pure function (``onyx.admin.alerts.evaluate_rule``)
-- so it can be exercised by tests, the dashboard's "Evaluate now"
-- button, and (later) a cron / scheduler.

CREATE TABLE IF NOT EXISTS alert_rules (
    id                   BIGSERIAL PRIMARY KEY,
    name                 TEXT UNIQUE NOT NULL,
    description          TEXT,
    -- Condition: one of "error_rate" | "p95_latency_ms" | "denial_rate"
    --                   | "queue_depth" | "request_volume"
    condition_type       TEXT NOT NULL,
    -- Comparator: ">" | ">=" | "<" | "<="
    comparator           TEXT NOT NULL DEFAULT '>',
    threshold            DOUBLE PRECISION NOT NULL,
    -- Optional filter scoping a rule to one source or one identity.
    source_id            TEXT,
    identity_id          BIGINT,
    -- Window the condition is evaluated over.
    window_seconds       INTEGER NOT NULL DEFAULT 300,
    -- Channel + target. Channel is "slack" | "email" | "webhook" | "log".
    notification_channel TEXT NOT NULL DEFAULT 'log',
    notification_target  TEXT,
    enabled              BOOLEAN NOT NULL DEFAULT TRUE,
    last_evaluated_at    TIMESTAMPTZ,
    last_fired_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled
    ON alert_rules (enabled, condition_type);

CREATE TABLE IF NOT EXISTS alert_history (
    id              BIGSERIAL PRIMARY KEY,
    rule_id         BIGINT REFERENCES alert_rules(id) ON DELETE CASCADE,
    rule_name       TEXT,
    fired_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observed_value  DOUBLE PRECISION,
    message         TEXT,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_history_rule
    ON alert_history (rule_id, fired_at DESC);

CREATE INDEX IF NOT EXISTS idx_alert_history_unack
    ON alert_history (fired_at DESC)
    WHERE acknowledged_at IS NULL;
