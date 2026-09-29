-- Onyx: Initial Database Schema
-- Migration: 001_initial_schema.sql
-- Run via: psql -f migrations/001_initial_schema.sql
-- Or mount to: docker-entrypoint-initdb.d/

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. data_sources - registered upstream data sources
-- ---------------------------------------------------------------------------
CREATE TABLE data_sources (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('postgresql', 'http', 's3', 'saas')),
    connection_config JSONB NOT NULL DEFAULT '{}',
    cache_strategy  TEXT NOT NULL DEFAULT 'deterministic_first'
                    CHECK (cache_strategy IN ('deterministic_first', 'semantic_first', 'semantic_only')),
    enabled         BOOLEAN DEFAULT TRUE,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 2. schema_catalog - column-level metadata for PG sources
-- ---------------------------------------------------------------------------
CREATE TABLE schema_catalog (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT REFERENCES data_sources(source_id) ON DELETE CASCADE,
    table_schema    TEXT DEFAULT 'public',
    table_name      TEXT NOT NULL,
    column_name     TEXT NOT NULL,
    data_type       TEXT NOT NULL,
    classification  TEXT DEFAULT 'unknown'
                    CHECK (classification IN ('free_text', 'identifier', 'numeric', 'temporal', 'boolean', 'unknown')),
    pii_scan_tier   TEXT DEFAULT 'auto'
                    CHECK (pii_scan_tier IN ('auto', 'fast_only', 'deep', 'skip')),
    metadata        JSONB DEFAULT '{}',
    UNIQUE (source_id, table_schema, table_name, column_name)
);

-- ---------------------------------------------------------------------------
-- 3. classification_tags - PII classification tags
-- ---------------------------------------------------------------------------
CREATE TABLE classification_tags (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    description     TEXT,
    regex_pattern   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 4. cache_policies - per-source cache configuration with semantic thresholds
-- ---------------------------------------------------------------------------
CREATE TABLE cache_policies (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT REFERENCES data_sources(source_id) ON DELETE CASCADE UNIQUE,
    l1_max_size     INTEGER DEFAULT 10000,
    l1_ttl_seconds  INTEGER DEFAULT 60,
    l2_ttl_seconds  INTEGER DEFAULT 300,
    semantic_auto_serve_threshold FLOAT8 DEFAULT 0.98,
    semantic_verify_threshold     FLOAT8 DEFAULT 0.92,
    enabled         BOOLEAN DEFAULT TRUE,
    metadata        JSONB DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- 5. identities - agent/user identities
-- ---------------------------------------------------------------------------
CREATE TABLE identities (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    api_key_hash    TEXT UNIQUE NOT NULL,
    agent_type      TEXT DEFAULT 'custom',
    team            TEXT,
    roles           TEXT[] DEFAULT '{}',
    mapped_pg_role  TEXT,
    enabled         BOOLEAN DEFAULT TRUE,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 6. policy_rules - RBAC access control rules
-- ---------------------------------------------------------------------------
CREATE TABLE policy_rules (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    priority        INTEGER DEFAULT 0,
    conditions      JSONB NOT NULL,
    actions         JSONB NOT NULL,
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 7. audit_log - append-only audit trail (partitioned by day)
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    id              BIGSERIAL,
    identity_id     BIGINT,
    source_id       TEXT,
    operation       TEXT NOT NULL,
    sql_fingerprint TEXT,
    cache_hit       BOOLEAN DEFAULT FALSE,
    cache_tier      TEXT,
    latency_ms      FLOAT8,
    pii_detected    BOOLEAN DEFAULT FALSE,
    pii_types       TEXT[],
    risk_level      TEXT,
    status          TEXT DEFAULT 'success',
    error_message   TEXT,
    request_metadata JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
) PARTITION BY RANGE (created_at);

CREATE INDEX idx_audit_log_created_at
    ON audit_log (created_at);
CREATE INDEX idx_audit_log_identity_created
    ON audit_log (identity_id, created_at);
CREATE INDEX idx_audit_log_source_created
    ON audit_log (source_id, created_at);

-- Helper function: create a monthly audit partition
CREATE OR REPLACE FUNCTION create_audit_partition(partition_date DATE)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    partition_name TEXT;
    start_date     DATE;
    end_date       DATE;
BEGIN
    start_date     := date_trunc('month', partition_date)::DATE;
    end_date       := (start_date + INTERVAL '1 month')::DATE;
    partition_name := 'audit_log_' || to_char(start_date, 'YYYY_MM');

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_log
         FOR VALUES FROM (%L) TO (%L)',
        partition_name, start_date, end_date
    );
END;
$$;

-- Create partition for the current month
SELECT create_audit_partition(NOW()::DATE);

-- ---------------------------------------------------------------------------
-- 8. write_approval_queue - pending write approvals
-- ---------------------------------------------------------------------------
CREATE TABLE write_approval_queue (
    id              BIGSERIAL PRIMARY KEY,
    identity_id     BIGINT NOT NULL,
    source_id       TEXT NOT NULL,
    sql_text        TEXT NOT NULL,
    risk_level      TEXT NOT NULL CHECK (risk_level IN ('low', 'medium', 'high')),
    status          TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed')),
    approved_by     TEXT,
    executed_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ NOT NULL,
    request_metadata JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_write_approval_queue_status_created
    ON write_approval_queue (status, created_at);

-- ---------------------------------------------------------------------------
-- 9. discovery_assets - indexed documents/tables for discovery
-- ---------------------------------------------------------------------------
CREATE TABLE discovery_assets (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT REFERENCES data_sources(source_id) ON DELETE CASCADE,
    asset_type      TEXT NOT NULL,
    asset_path      TEXT NOT NULL,
    title           TEXT,
    summary         JSONB,
    category_path   ltree,
    topics          TEXT[],
    embedding       FLOAT8[],
    search_vector   tsvector,
    quality_score   FLOAT8,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_id, asset_type, asset_path)
);

CREATE INDEX idx_discovery_assets_search_vector
    ON discovery_assets USING GIN (search_vector);
CREATE INDEX idx_discovery_assets_topics
    ON discovery_assets USING GIN (topics);
CREATE INDEX idx_discovery_assets_category_path
    ON discovery_assets USING GIST (category_path);

-- ---------------------------------------------------------------------------
-- 10. entity_document_xref - entity cross-references
-- ---------------------------------------------------------------------------
CREATE TABLE entity_document_xref (
    id              BIGSERIAL PRIMARY KEY,
    entity_text     TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    document_id     BIGINT REFERENCES discovery_assets(id) ON DELETE CASCADE,
    prominence      FLOAT8 DEFAULT 1.0,
    metadata        JSONB DEFAULT '{}',
    UNIQUE (entity_text, entity_type, document_id)
);

CREATE INDEX idx_entity_xref_entity
    ON entity_document_xref (entity_text, entity_type);
CREATE INDEX idx_entity_xref_document
    ON entity_document_xref (document_id);

-- ---------------------------------------------------------------------------
-- 11. category_taxonomy - hierarchical categories
-- ---------------------------------------------------------------------------
CREATE TABLE category_taxonomy (
    id              BIGSERIAL PRIMARY KEY,
    path            ltree UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    embedding       FLOAT8[],
    document_count  INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_category_taxonomy_path
    ON category_taxonomy USING GIST (path);

-- ---------------------------------------------------------------------------
-- 12. ingestion_jobs - document processing queue
-- ---------------------------------------------------------------------------
CREATE TABLE ingestion_jobs (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT REFERENCES data_sources(source_id) ON DELETE CASCADE,
    file_path       TEXT NOT NULL,
    status          TEXT DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
    priority_score  FLOAT8 DEFAULT 0.0,
    worker_id       TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,
    retry_count     INTEGER DEFAULT 0,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_id, file_path)
);

CREATE INDEX idx_ingestion_jobs_claim
    ON ingestion_jobs (status, priority_score DESC);

-- ---------------------------------------------------------------------------
-- NOTIFY channels (for application-level pub/sub)
-- ---------------------------------------------------------------------------
-- The following LISTEN/NOTIFY channels are used by Onyx services:
--
--   onyx_config_changed   - fired when data_sources or cache_policies change
--   onyx_policy_changed   - fired when policy_rules are created/updated/deleted
--   onyx_write_approval   - fired when a write_approval_queue entry is approved/rejected
--
-- Usage from application code:
--   NOTIFY onyx_config_changed, '{"source_id": "warehouse"}';
--   NOTIFY onyx_policy_changed, '{"rule_id": 42, "action": "updated"}';
--   NOTIFY onyx_write_approval, '{"approval_id": 7, "status": "approved"}';

