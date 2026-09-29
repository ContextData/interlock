-- Connector activation: which connectors an operator may register sources on.
--
-- Every connector in the registry used to be offered for new sources,
-- including ones never exercised against a real system. A connector is now
-- offered only when it has an active row here; a connector added in code later
-- has no row and so starts inactive until an operator turns it on. Existing
-- sources are unaffected by activation: it governs registration, not traffic.
--
-- Seeded with the connectors proven against real systems, plus any connector
-- that already has a registered source, so an upgrade never hides a connector
-- an operator is using.

CREATE TABLE IF NOT EXISTS connector_activation (
    connector_key  TEXT PRIMARY KEY,
    active         BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by     BIGINT REFERENCES admin_identities(id) ON DELETE SET NULL
);

INSERT INTO connector_activation (connector_key, active)
VALUES
    ('postgresql', TRUE),
    ('mysql', TRUE),
    ('s3', TRUE),
    ('slack', TRUE),
    ('github', TRUE),
    ('generic_rest', TRUE)
ON CONFLICT (connector_key) DO NOTHING;

INSERT INTO connector_activation (connector_key, active)
SELECT DISTINCT metadata->>'connector_key', TRUE
FROM data_sources
WHERE jsonb_typeof(metadata) = 'object'
  AND COALESCE(metadata->>'connector_key', '') <> ''
ON CONFLICT (connector_key) DO UPDATE SET active = TRUE;
