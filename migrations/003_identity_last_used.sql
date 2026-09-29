-- Onyx Migration 003: identity last-used tracking
--
-- The Phase 3 identity detail page (operator console expansion) needs
-- to surface when each API key was last actually used. The data plane
-- has no interactive login, so we track activity on the request path:
-- AuthManager.authenticate() bumps ``last_used_at`` whenever a key
-- resolves against the cache miss path (cheap, no-op on hot-cached
-- sessions).
--
-- Also adds ``rotated_at`` so the dashboard can call out keys that
-- have never been rotated since creation.

ALTER TABLE identities
    ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rotated_at   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_identities_last_used
    ON identities (last_used_at DESC NULLS LAST);
