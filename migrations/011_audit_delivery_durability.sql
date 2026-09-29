-- InterLock Migration 011: durable, idempotent audit delivery
--
-- A partitioned table cannot enforce uniqueness on event_id alone because the
-- partition key is created_at.  audit_event_dedup provides the global claim;
-- writers insert the claim and audit row in one statement/transaction.

ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS event_id UUID DEFAULT gen_random_uuid();

UPDATE audit_log
SET event_id = gen_random_uuid()
WHERE event_id IS NULL;

ALTER TABLE audit_log
    ALTER COLUMN event_id SET DEFAULT gen_random_uuid(),
    ALTER COLUMN event_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_log_event_id_created
    ON audit_log (event_id, created_at);

CREATE TABLE IF NOT EXISTS audit_event_dedup (
    event_id         UUID PRIMARY KEY,
    event_created_at TIMESTAMPTZ NOT NULL,
    persisted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO audit_event_dedup (event_id, event_created_at, persisted_at)
SELECT event_id, created_at, created_at
FROM audit_log
ON CONFLICT (event_id) DO NOTHING;

CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_event_dedup_event_id
    ON audit_event_dedup (event_id);

CREATE INDEX IF NOT EXISTS idx_audit_event_dedup_persisted_at
    ON audit_event_dedup (persisted_at);

CREATE TABLE IF NOT EXISTS audit_dead_letter (
    event_id       UUID PRIMARY KEY,
    payload        JSONB NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 1,
    error_type     TEXT NOT NULL,
    last_error     TEXT NOT NULL,
    first_failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_failed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_dead_letter_unresolved
    ON audit_dead_letter (last_failed_at)
    WHERE resolved_at IS NULL;

ALTER TABLE audit_partition_maintenance
    ADD COLUMN IF NOT EXISTS lock_skips BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0;

CREATE OR REPLACE FUNCTION maintain_audit_partitions(
    months_back INTEGER DEFAULT 1,
    months_forward INTEGER DEFAULT 3
)
RETURNS VOID AS $$
DECLARE
    offset_month INTEGER;
    target_month DATE;
BEGIN
    UPDATE audit_partition_maintenance
    SET last_run_at = NOW(),
        updated_at = NOW()
    WHERE id = TRUE;

    FOR offset_month IN -months_back..months_forward LOOP
        target_month := (
            date_trunc('month', NOW()) + (offset_month || ' months')::INTERVAL
        )::DATE;
        PERFORM create_audit_partition(target_month);
    END LOOP;

    UPDATE audit_partition_maintenance
    SET last_success_at = NOW(),
        last_error = NULL,
        consecutive_failures = 0,
        updated_at = NOW()
    WHERE id = TRUE;
EXCEPTION WHEN OTHERS THEN
    UPDATE audit_partition_maintenance
    SET last_error = SQLERRM,
        consecutive_failures = consecutive_failures + 1,
        updated_at = NOW()
    WHERE id = TRUE;
    RAISE;
END;
$$ LANGUAGE plpgsql;
