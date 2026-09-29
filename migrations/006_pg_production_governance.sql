-- AgentGate v4.1 PostgreSQL proxy governance foundation.
--
-- Adds dedicated PostgreSQL-style credentials to identities while keeping
-- AgentGate API keys as first-class secrets for PG password auth.

ALTER TABLE identities
    ADD COLUMN IF NOT EXISTS pg_username TEXT,
    ADD COLUMN IF NOT EXISTS pg_password_hash TEXT,
    ADD COLUMN IF NOT EXISTS pg_password_rotated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS auth_metadata JSONB DEFAULT '{}';

CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_pg_username_unique
    ON identities (LOWER(pg_username))
    WHERE pg_username IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_identities_pg_username_lookup
    ON identities (LOWER(pg_username))
    WHERE enabled = TRUE AND pg_username IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_identities_name_lookup
    ON identities (LOWER(name))
    WHERE enabled = TRUE;
