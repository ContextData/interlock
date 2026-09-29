-- AgentGate source-scoped IAM-style roles and permission statements.
-- Roles are first-class objects per source. Identity grants reference role ids.

ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS data_sources_source_type_check;
ALTER TABLE data_sources ADD CONSTRAINT data_sources_source_type_check
    CHECK (source_type IN ('postgresql', 'mysql', 'http', 's3', 'saas'));

CREATE TABLE IF NOT EXISTS source_roles (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES data_sources(source_id) ON DELETE CASCADE,
    role_key        TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, role_key)
);

CREATE TABLE IF NOT EXISTS source_role_permissions (
    id               BIGSERIAL PRIMARY KEY,
    role_id          BIGINT NOT NULL REFERENCES source_roles(id) ON DELETE CASCADE,
    effect           TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    action           TEXT NOT NULL,
    resource_type    TEXT NOT NULL,
    resource_pattern TEXT NOT NULL,
    constraints      JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS identity_source_role_grants (
    id          BIGSERIAL PRIMARY KEY,
    identity_id BIGINT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    source_id   TEXT NOT NULL REFERENCES data_sources(source_id) ON DELETE CASCADE,
    role_id     BIGINT NOT NULL REFERENCES source_roles(id) ON DELETE CASCADE,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at  TIMESTAMPTZ,
    granted_by  BIGINT REFERENCES admin_identities(id) ON DELETE SET NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (identity_id, source_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_source_roles_source
    ON source_roles(source_id, enabled);
CREATE INDEX IF NOT EXISTS idx_source_role_permissions_role
    ON source_role_permissions(role_id);
CREATE INDEX IF NOT EXISTS idx_identity_source_role_grants_identity_source
    ON identity_source_role_grants(identity_id, source_id, enabled);

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
            (p_role_id, 'allow', 'db.table.select', 'db.table', '*.*', '{}');

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
            (p_role_id, 'allow', 'http.head', 'http.path', '/*', '{}');

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

DO $$
DECLARE
    src RECORD;
    role_name TEXT;
    roles JSONB;
    role_id BIGINT;
BEGIN
    FOR src IN SELECT source_id, source_type, metadata FROM data_sources LOOP
        roles := COALESCE(src.metadata->'role_types', '[]'::jsonb);
        IF jsonb_typeof(roles) IS DISTINCT FROM 'array' OR jsonb_array_length(roles) = 0 THEN
            IF src.source_type IN ('postgresql', 'mysql') THEN
                roles := '["read","analyst","write","owner"]'::jsonb;
            ELSIF src.source_type = 'http' THEN
                roles := '["read","writer","owner"]'::jsonb;
            ELSE
                roles := '["read","write","owner"]'::jsonb;
            END IF;
        END IF;

        FOR role_name IN SELECT lower(trim(value)) FROM jsonb_array_elements_text(roles) LOOP
            INSERT INTO source_roles
                (source_id, role_key, name, description, review_required, metadata)
            VALUES
                (src.source_id, role_name, initcap(replace(role_name, '_', ' ')),
                 'Migrated default source role', TRUE, '{"migrated": true}'::jsonb)
            ON CONFLICT (source_id, role_key) DO UPDATE
            SET name = EXCLUDED.name,
                review_required = source_roles.review_required OR EXCLUDED.review_required
            RETURNING id INTO role_id;

            PERFORM agentgate_seed_source_role_permissions(
                role_id, role_name, src.source_type
            );
        END LOOP;
    END LOOP;
END $$;

INSERT INTO identity_source_role_grants
    (identity_id, source_id, role_id, metadata)
SELECT i.id,
       legacy_grant->>'source_id',
       sr.id,
       jsonb_build_object('migrated', true, 'legacy_role', legacy_grant->>'role')
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
ON CONFLICT (identity_id, source_id, role_id) DO NOTHING;
