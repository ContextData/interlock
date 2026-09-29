-- Enterprise connector foundation metadata.
-- Keeps existing source_type values working while allowing broad connector
-- families for new native sources.

ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS data_sources_source_type_check;
ALTER TABLE data_sources ADD CONSTRAINT data_sources_source_type_check
    CHECK (
        source_type IN (
            'postgresql', 'mysql', 'http', 's3', 'saas',
            'database', 'warehouse', 'object_storage', 'search', 'collaboration'
        )
    );

UPDATE data_sources
SET metadata = COALESCE(metadata, '{}'::jsonb)
    || jsonb_build_object(
        'connector_key',
        CASE source_type
            WHEN 'postgresql' THEN 'postgresql'
            WHEN 'mysql' THEN 'mysql'
            WHEN 'http' THEN 'generic_rest'
            WHEN 's3' THEN 's3'
            ELSE COALESCE(metadata->>'connector_key', source_type)
        END,
        'connector_family',
        CASE source_type
            WHEN 'postgresql' THEN 'database'
            WHEN 'mysql' THEN 'database'
            WHEN 'http' THEN 'http'
            WHEN 's3' THEN 'object_storage'
            ELSE COALESCE(metadata->>'connector_family', source_type)
        END,
        'source_roles_version',
        GREATEST(COALESCE((metadata->>'source_roles_version')::int, 1), 2)
    )
WHERE NOT metadata ? 'connector_key'
   OR NOT metadata ? 'connector_family'
   OR COALESCE((metadata->>'source_roles_version')::int, 1) < 2;
