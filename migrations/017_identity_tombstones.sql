-- Identity tombstones: who a deleted identity was.
--
-- `audit_log` records only `identity_id`, and deleting an identity removes its
-- row, so every audit view that joins `identities` for a name showed a bare
-- `#7` for traffic from an identity that had since been deleted. `audit_log`
-- stays append-only; instead the delete leaves a tombstone here that the views
-- fall back to.
--
-- Backfill: identities deleted through the admin API before this migration
-- left an `identity.delete` entry whose `before` snapshot carries the name.
-- Identities removed by raw SQL left nothing and stay unnamed.

CREATE TABLE IF NOT EXISTS identity_tombstones (
    identity_id  BIGINT PRIMARY KEY,
    name         TEXT NOT NULL,
    team         TEXT,
    deleted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_by   BIGINT REFERENCES admin_identities(id) ON DELETE SET NULL
);

INSERT INTO identity_tombstones (identity_id, name, team, deleted_at, deleted_by)
SELECT DISTINCT ON (deleted.identity_id)
       deleted.identity_id, deleted.name, deleted.team, deleted.created_at, deleted.admin_id
FROM (
    SELECT (a.detail->'before'->>'id')::bigint AS identity_id,
           a.detail->'before'->>'name'         AS name,
           a.detail->'before'->>'team'         AS team,
           a.created_at,
           a.admin_id,
           a.id                                AS audit_id
    FROM admin_audit_log a
    WHERE a.action = 'identity.delete'
      AND a.success
      AND jsonb_typeof(a.detail->'before') = 'object'
      AND a.detail->'before'->>'id' ~ '^[0-9]+$'
      AND COALESCE(a.detail->'before'->>'name', '') <> ''
) AS deleted
WHERE NOT EXISTS (SELECT 1 FROM identities i WHERE i.id = deleted.identity_id)
ORDER BY deleted.identity_id, deleted.audit_id DESC
ON CONFLICT (identity_id) DO NOTHING;
