-- Repair JSON values that were stored as JSON string scalars.
--
-- The control-plane pools register a jsonb codec that serialises parameters,
-- and several writers serialised them first, so PostgreSQL stored the text
-- '{"k": 1}' as a JSON string instead of the object {"k": 1}. Readers that
-- expect an object then failed; the console's Data Sources pages returned 500.
--
-- Only a string scalar whose text parses as a JSON object or array is
-- converted. Any other string is left exactly as it was, so a value this
-- repair does not understand cannot fail the upgrade.

DO $$
DECLARE
    target record;
    candidate record;
    parsed jsonb;
BEGIN
    FOR target IN
        SELECT *
        FROM (VALUES
            ('data_sources', 'metadata'),
            ('data_sources', 'connection_config'),
            ('identities', 'metadata'),
            ('source_roles', 'metadata'),
            ('source_role_permissions', 'constraints'),
            ('identity_source_role_grants', 'metadata'),
            ('policy_rules', 'conditions'),
            ('policy_rules', 'actions'),
            ('cache_dependencies', 'metadata'),
            ('discovery_assets', 'summary')
        ) AS affected(table_name, column_name)
    LOOP
        IF to_regclass(target.table_name) IS NULL THEN
            CONTINUE;
        END IF;

        FOR candidate IN EXECUTE format(
            'SELECT ctid AS row_ref, %I #>> %L AS text_value FROM %I WHERE jsonb_typeof(%I) = %L',
            target.column_name, '{}', target.table_name, target.column_name, 'string'
        )
        LOOP
            BEGIN
                parsed := candidate.text_value::jsonb;
            EXCEPTION WHEN others THEN
                parsed := NULL;
            END;

            IF parsed IS NOT NULL AND jsonb_typeof(parsed) IN ('object', 'array') THEN
                EXECUTE format('UPDATE %I SET %I = $1 WHERE ctid = $2', target.table_name, target.column_name)
                    USING parsed, candidate.row_ref;
            END IF;
        END LOOP;
    END LOOP;
END
$$;
