-- Source catalog: the structure of every registered source, captured when it is
-- configured and refreshed on a schedule.
--
-- Before this, PostgreSQL column structure was written to `schema_catalog` by a
-- scanner that ran only at admin startup, ignored secret references and TLS,
-- and was read by nothing. Other connectors could introspect but threw the
-- result away after one MCP call. These tables replace that with one catalog
-- that roles, policies, agents and the admin all read.
--
-- Four tables, each with one job:
--   source_catalog_scans        the job queue and the scan history, in one
--   source_catalog              one row per node, owned by the scans
--   source_catalog_changes      drift between scans, for review and audit
--   source_catalog_annotations  admin and wizard decisions, which a rescan must
--                               never erase, so they do not live on node rows
--
-- No sample values are ever stored: structure only.

CREATE TABLE IF NOT EXISTS source_catalog_scans (
    id                 BIGSERIAL PRIMARY KEY,
    source_id          TEXT NOT NULL REFERENCES data_sources(source_id) ON DELETE CASCADE,
    trigger            TEXT NOT NULL
                       CHECK (trigger IN ('save', 'manual', 'scheduled', 'startup', 'api')),
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    requested_by       TEXT,
    requested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    not_before         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    worker_id          TEXT,
    lease_expires_at   TIMESTAMPTZ,
    attempt            INT NOT NULL DEFAULT 0,
    collector          TEXT,
    nodes_seen         INT,
    added_count        INT,
    removed_count      INT,
    changed_count      INT,
    truncated          BOOLEAN NOT NULL DEFAULT FALSE,
    truncation         JSONB NOT NULL DEFAULT '{}',
    baseline           BOOLEAN NOT NULL DEFAULT FALSE,
    changes_truncated  BOOLEAN NOT NULL DEFAULT FALSE,
    error_code         TEXT,
    error_message      TEXT,
    metadata           JSONB NOT NULL DEFAULT '{}'
);

-- At most one waiting and one running scan per source. A save made while a scan
-- runs still queues a follow-up, and a source is never scanned twice at once.
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_scan_pending
    ON source_catalog_scans (source_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_scan_running
    ON source_catalog_scans (source_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_catalog_scans_claim
    ON source_catalog_scans (not_before) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_catalog_scans_recent
    ON source_catalog_scans (source_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS source_catalog (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES data_sources(source_id) ON DELETE CASCADE,
    node_type           TEXT NOT NULL CHECK (node_type IN (
                            'source', 'database', 'schema', 'table', 'view',
                            'materialized_view', 'foreign_table', 'column',
                            'bucket', 'prefix', 'channel', 'repository',
                            'object', 'field', 'index', 'collection')),
    -- Identity: the native-case path from the source root. Identifiers and S3
    -- keys can contain dots, so identity is an array, never a dotted string.
    path                TEXT[] NOT NULL,
    parent_path         TEXT[] NOT NULL,
    depth               SMALLINT NOT NULL,
    name                TEXT NOT NULL,
    -- Matching: the string enforcement compares patterns against, e.g.
    -- `sales.customers.email`. Kept apart from identity on purpose.
    resource_key        TEXT NOT NULL,
    ordinal             INT,
    data_type           TEXT,
    heuristic_class     TEXT,
    attributes          JSONB NOT NULL DEFAULT '{}',
    attributes_hash     TEXT NOT NULL,
    first_seen_scan_id  BIGINT,
    last_seen_scan_id   BIGINT,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_at          TIMESTAMPTZ,
    removed_at          TIMESTAMPTZ,
    UNIQUE (source_id, path)
);

CREATE INDEX IF NOT EXISTS idx_source_catalog_children
    ON source_catalog (source_id, parent_path) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_source_catalog_resource
    ON source_catalog (source_id, resource_key);
CREATE INDEX IF NOT EXISTS idx_source_catalog_type
    ON source_catalog (source_id, node_type) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_source_catalog_name
    ON source_catalog (lower(name) text_pattern_ops);

CREATE TABLE IF NOT EXISTS source_catalog_annotations (
    id                     BIGSERIAL PRIMARY KEY,
    source_id              TEXT NOT NULL REFERENCES data_sources(source_id) ON DELETE CASCADE,
    path                   TEXT[] NOT NULL,
    classification         TEXT NOT NULL
                           CHECK (classification IN ('pii', 'sensitive', 'not_pii', 'public')),
    classification_source  TEXT NOT NULL
                           CHECK (classification_source IN ('admin', 'wizard', 'scanner', 'import')),
    note                   TEXT,
    applied_by             TEXT,
    applied_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, path)
);

CREATE TABLE IF NOT EXISTS source_catalog_changes (
    id               BIGSERIAL PRIMARY KEY,
    scan_id          BIGINT NOT NULL REFERENCES source_catalog_scans(id) ON DELETE CASCADE,
    source_id        TEXT NOT NULL REFERENCES data_sources(source_id) ON DELETE CASCADE,
    node_type        TEXT NOT NULL,
    path             TEXT[] NOT NULL,
    change           TEXT NOT NULL CHECK (change IN ('added', 'removed', 'changed')),
    before           JSONB,
    after            JSONB,
    exposure         JSONB,
    acknowledged_at  TIMESTAMPTZ,
    acknowledged_by  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_catalog_changes_source
    ON source_catalog_changes (source_id, created_at DESC);
