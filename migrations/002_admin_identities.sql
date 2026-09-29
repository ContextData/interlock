-- Onyx Migration 002: Admin Identities
--
-- AUDIT-COVERS: P0-F (admin authentication required)
--
-- Adds the table that backs the admin login flow. Roles are stored as
-- TEXT[] for simple RBAC checks in admin auth middleware. Seed data is
-- intentionally omitted; first-run bootstrap is handled in code via the
-- ONYX_ADMIN__BOOTSTRAP_PASSWORD env var.

CREATE TABLE IF NOT EXISTS admin_identities (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    roles           TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at   TIMESTAMPTZ,
    last_login_ip   TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_identities_username
    ON admin_identities(username);

CREATE INDEX IF NOT EXISTS idx_admin_identities_enabled
    ON admin_identities(enabled);

-- Audit table for admin actions (separate from data-plane audit_log).
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    admin_id        BIGINT REFERENCES admin_identities(id) ON DELETE SET NULL,
    username        TEXT,
    action          TEXT NOT NULL,
    resource        TEXT,
    resource_id     TEXT,
    detail          JSONB,
    request_ip      TEXT,
    user_agent      TEXT,
    success         BOOLEAN NOT NULL DEFAULT TRUE,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_log_admin
    ON admin_audit_log(admin_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_admin_audit_log_action
    ON admin_audit_log(action, created_at DESC);
