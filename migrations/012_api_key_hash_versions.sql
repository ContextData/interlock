-- Version API-key hashes so legacy SHA-256 records can be upgraded safely.

ALTER TABLE identities
    ADD COLUMN IF NOT EXISTS api_key_hash_version TEXT NOT NULL DEFAULT 'sha256-v1';

ALTER TABLE identities
    DROP CONSTRAINT IF EXISTS identities_api_key_hash_version_check;

ALTER TABLE identities
    ADD CONSTRAINT identities_api_key_hash_version_check
    CHECK (api_key_hash_version IN ('sha256-v1', 'hmac-sha256-v2'));

CREATE INDEX IF NOT EXISTS idx_identities_api_key_hash_version
    ON identities (api_key_hash_version);
