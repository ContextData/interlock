-- Align Source Roles and Policy Rules after source-scoped grants became
-- authoritative. This is intentionally conservative: it backfills missing
-- grant rows and marks migrated artifacts for review, but it does not delete
-- production access.

CREATE INDEX IF NOT EXISTS idx_identity_source_role_grants_active
    ON identity_source_role_grants(identity_id, source_id, role_id)
    WHERE enabled = TRUE;

-- Backfill any legacy metadata grants not captured by the original source-role
-- migration. Existing grants are left intact and re-enabled so Admin can review
-- the effective access in one place.
INSERT INTO identity_source_role_grants
    (identity_id, source_id, role_id, enabled, metadata)
SELECT i.id,
       legacy_grant->>'source_id',
       sr.id,
       TRUE,
       jsonb_build_object(
           'migrated', true,
           'migration', '008_role_policy_alignment',
           'legacy_role', legacy_grant->>'role',
           'review_required', true
       )
FROM identities i
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(i.metadata->'source_roles') = 'array'
        THEN i.metadata->'source_roles'
        ELSE '[]'::jsonb
    END
) legacy_grant
JOIN source_roles sr
  ON sr.source_id = legacy_grant->>'source_id'
 AND sr.role_key = lower(legacy_grant->>'role')
ON CONFLICT (identity_id, source_id, role_id) DO UPDATE
SET enabled = TRUE,
    metadata = identity_source_role_grants.metadata
               || EXCLUDED.metadata
               || '{"reconciled_from_legacy_metadata": true}'::jsonb,
    updated_at = NOW();

UPDATE source_roles
SET review_required = TRUE,
    metadata = metadata || '{"review_required_reason": "role-policy-alignment"}'::jsonb,
    updated_at = NOW()
WHERE metadata->>'migrated' = 'true';

-- Mark policy role conditions as source-role keys. A separate identity_roles
-- condition is now used for global identity labels.
UPDATE policy_rules
SET conditions = jsonb_set(
        COALESCE(conditions, '{}'::jsonb),
        '{role_semantics}',
        '"source_role_keys"'::jsonb,
        true
    )
WHERE conditions ? 'roles'
  AND NOT conditions ? 'role_semantics';

-- Give existing read-like roles the explicit discovery actions used by MCP.
INSERT INTO source_role_permissions
    (role_id, effect, action, resource_type, resource_pattern, constraints)
SELECT r.id, 'allow', action_name, 'discovery.asset', '*', '{}'::jsonb
FROM source_roles r
CROSS JOIN (VALUES ('discovery.search'), ('discovery.asset.read')) AS a(action_name)
WHERE r.role_key IN ('read', 'reader', 'analyst')
  AND NOT EXISTS (
      SELECT 1
      FROM source_role_permissions p
      WHERE p.role_id = r.id
        AND p.effect = 'allow'
        AND p.action = action_name
        AND p.resource_type = 'discovery.asset'
        AND p.resource_pattern = '*'
  );

CREATE OR REPLACE FUNCTION agentgate_seed_source_role_permissions(
    p_role_id BIGINT,
    p_role_key TEXT,
    p_source_type TEXT
) RETURNS VOID AS $$
BEGIN
    DELETE FROM source_role_permissions WHERE role_id = p_role_id;

    IF p_role_key = 'blocked' THEN
        INSERT INTO source_role_permissions
            (role_id, effect, action, resource_type, resource_pattern, constraints)
        VALUES (p_role_id, 'deny', '*', '*', '*', '{}');
        RETURN;
    END IF;

    IF p_role_key IN ('owner', 'admin') THEN
        INSERT INTO source_role_permissions
            (role_id, effect, action, resource_type, resource_pattern, constraints)
        VALUES (p_role_id, 'allow', '*', '*', '*', '{}');
        RETURN;
    END IF;

    IF p_source_type IN ('postgresql', 'mysql') THEN
        INSERT INTO source_role_permissions
            (role_id, effect, action, resource_type, resource_pattern, constraints)
        VALUES
            (p_role_id, 'allow', 'db.schema.list', 'db.schema', '*', '{}'),
            (p_role_id, 'allow', 'db.table.describe', 'db.table', '*.*', '{}'),
            (p_role_id, 'allow', 'db.table.select', 'db.table', '*.*', '{}'),
            (p_role_id, 'allow', 'discovery.search', 'discovery.asset', '*', '{}'),
            (p_role_id, 'allow', 'discovery.asset.read', 'discovery.asset', '*', '{}');

        IF p_role_key IN ('writer', 'write') THEN
            INSERT INTO source_role_permissions
                (role_id, effect, action, resource_type, resource_pattern, constraints)
            VALUES
                (p_role_id, 'allow', 'db.table.insert', 'db.table', '*.*', '{}'),
                (p_role_id, 'allow', 'db.table.update', 'db.table', '*.*', '{}');
        END IF;
        RETURN;
    END IF;

    IF p_source_type = 'http' THEN
        INSERT INTO source_role_permissions
            (role_id, effect, action, resource_type, resource_pattern, constraints)
        VALUES
            (p_role_id, 'allow', 'http.get', 'http.path', '/*', '{}'),
            (p_role_id, 'allow', 'http.head', 'http.path', '/*', '{}'),
            (p_role_id, 'allow', 'discovery.search', 'discovery.asset', '*', '{}'),
            (p_role_id, 'allow', 'discovery.asset.read', 'discovery.asset', '*', '{}');

        IF p_role_key IN ('writer', 'write', 'operator') THEN
            INSERT INTO source_role_permissions
                (role_id, effect, action, resource_type, resource_pattern, constraints)
            VALUES
                (p_role_id, 'allow', 'http.post', 'http.path', '/*', '{}'),
                (p_role_id, 'allow', 'http.put', 'http.path', '/*', '{}'),
                (p_role_id, 'allow', 'http.patch', 'http.path', '/*', '{}');
        END IF;
        RETURN;
    END IF;

    INSERT INTO source_role_permissions
        (role_id, effect, action, resource_type, resource_pattern, constraints)
    VALUES (p_role_id, 'allow', '*', '*', '*', '{}');
END;
$$ LANGUAGE plpgsql;
