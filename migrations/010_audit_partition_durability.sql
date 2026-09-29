-- Onyx Migration 010: audit partition durability
--
-- Adds a default audit_log partition and a reusable partition maintenance
-- function so audit inserts do not fail at month rollover if scheduled
-- partition creation is delayed.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'audit_log_default'
          AND n.nspname = current_schema()
    ) THEN
        EXECUTE 'CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS audit_partition_maintenance (
    id              BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    months_back     INTEGER NOT NULL DEFAULT 1,
    months_forward  INTEGER NOT NULL DEFAULT 3,
    last_run_at     TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO audit_partition_maintenance (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;

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
        target_month := (date_trunc('month', NOW()) + (offset_month || ' months')::INTERVAL)::DATE;
        PERFORM create_audit_partition(target_month);
    END LOOP;

    UPDATE audit_partition_maintenance
    SET last_success_at = NOW(),
        last_error = NULL,
        updated_at = NOW()
    WHERE id = TRUE;
EXCEPTION WHEN OTHERS THEN
    UPDATE audit_partition_maintenance
    SET last_error = SQLERRM,
        updated_at = NOW()
    WHERE id = TRUE;
    RAISE;
END;
$$ LANGUAGE plpgsql;

SELECT maintain_audit_partitions(1, 3);

CREATE INDEX IF NOT EXISTS idx_audit_log_default_created_at
    ON audit_log_default (created_at);

CREATE INDEX IF NOT EXISTS idx_audit_log_default_source_created
    ON audit_log_default (source_id, created_at);
