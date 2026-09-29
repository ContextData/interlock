-- Onyx Migration 005: production remediation foundation
--
-- Forward-only schema updates for the AgentGate v4.1 remediation plan.
-- This keeps migration history intact while adding the fields needed for
-- source-aware audit, complete cache policy selection, deterministic cache
-- invalidation, richer ingestion progress, and entity-aware discovery.

-- ---------------------------------------------------------------------------
-- Cache strategy expansion
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS data_sources_cache_strategy_check;
    ALTER TABLE data_sources ADD CONSTRAINT data_sources_cache_strategy_check
        CHECK (cache_strategy IN (
            'deterministic_first',
            'semantic_first',
            'semantic_only',
            'deterministic_only',
            'bypass'
        ));
END $$;

ALTER TABLE cache_policies
    ADD COLUMN IF NOT EXISTS strategy_override TEXT,
    ADD COLUMN IF NOT EXISTS max_stale_seconds INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS size_limit_bytes BIGINT,
    ADD COLUMN IF NOT EXISTS semantic_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS invalidation JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    ALTER TABLE cache_policies DROP CONSTRAINT IF EXISTS cache_policies_strategy_override_check;
    ALTER TABLE cache_policies ADD CONSTRAINT cache_policies_strategy_override_check
        CHECK (
            strategy_override IS NULL OR strategy_override IN (
                'deterministic_first',
                'semantic_first',
                'semantic_only',
                'deterministic_only',
                'bypass'
            )
        );
END $$;

-- Dependency index for deterministic L2 invalidation. Runtime cache writes can
-- attach entries to source/table/asset dimensions and delete the matching keys
-- after writes without scanning all Redis keys.
CREATE TABLE IF NOT EXISTS cache_dependencies (
    id              BIGSERIAL PRIMARY KEY,
    cache_key       TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    table_name      TEXT,
    asset_path      TEXT,
    protocol        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_dependencies_unique_scope
    ON cache_dependencies (
        cache_key,
        source_id,
        COALESCE(table_name, ''),
        COALESCE(asset_path, '')
    );

CREATE INDEX IF NOT EXISTS idx_cache_dependencies_source_table
    ON cache_dependencies (source_id, table_name);

CREATE INDEX IF NOT EXISTS idx_cache_dependencies_source_asset
    ON cache_dependencies (source_id, asset_path);

CREATE INDEX IF NOT EXISTS idx_cache_dependencies_expires
    ON cache_dependencies (expires_at)
    WHERE expires_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Richer audit event envelope
-- ---------------------------------------------------------------------------
ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS protocol TEXT,
    ADD COLUMN IF NOT EXISTS route TEXT,
    ADD COLUMN IF NOT EXISTS normalized_operation TEXT,
    ADD COLUMN IF NOT EXISTS intent TEXT,
    ADD COLUMN IF NOT EXISTS upstream_target TEXT,
    ADD COLUMN IF NOT EXISTS policy_decision JSONB,
    ADD COLUMN IF NOT EXISTS approval_id BIGINT,
    ADD COLUMN IF NOT EXISTS approval_status TEXT,
    ADD COLUMN IF NOT EXISTS redaction_stats JSONB,
    ADD COLUMN IF NOT EXISTS cost_metadata JSONB;

CREATE INDEX IF NOT EXISTS idx_audit_log_protocol_created
    ON audit_log (protocol, created_at);

CREATE INDEX IF NOT EXISTS idx_audit_log_approval
    ON audit_log (approval_id, created_at)
    WHERE approval_id IS NOT NULL;

-- Ensure current, previous, and next month partitions exist during migration.
SELECT create_audit_partition((NOW() - INTERVAL '1 month')::DATE);
SELECT create_audit_partition(NOW()::DATE);
SELECT create_audit_partition((NOW() + INTERVAL '1 month')::DATE);

-- Approval execution can now fail after human approval if the upstream write
-- errors. Preserve that terminal state explicitly instead of leaving rows as
-- merely "approved".
DO $$
BEGIN
    ALTER TABLE write_approval_queue DROP CONSTRAINT IF EXISTS write_approval_queue_status_check;
    ALTER TABLE write_approval_queue ADD CONSTRAINT write_approval_queue_status_check
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'failed'));
END $$;

-- ---------------------------------------------------------------------------
-- Ingestion queue state and progress
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ALTER TABLE ingestion_jobs DROP CONSTRAINT IF EXISTS ingestion_jobs_status_check;
    ALTER TABLE ingestion_jobs ADD CONSTRAINT ingestion_jobs_status_check
        CHECK (status IN (
            'queued',
            'extracting',
            'summarizing',
            'indexing',
            'processing',
            'completed',
            'failed',
            'cancelled'
        ));
END $$;

ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stage TEXT,
    ADD COLUMN IF NOT EXISTS progress_current INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS progress_total INTEGER,
    ADD COLUMN IF NOT EXISTS ocr_pages INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ocr_cost FLOAT8 NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS llm_cost FLOAT8 NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_lease
    ON ingestion_jobs (lease_expires_at)
    WHERE lease_expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_worker_status
    ON ingestion_jobs (worker_id, status);

-- ---------------------------------------------------------------------------
-- Entity cross-reference enrichment
-- ---------------------------------------------------------------------------
ALTER TABLE entity_document_xref
    ADD COLUMN IF NOT EXISTS prominence_label TEXT,
    ADD COLUMN IF NOT EXISTS mention_count INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS context_snippet TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_entity_xref_prominence_label
    ON entity_document_xref (entity_text, prominence_label);
