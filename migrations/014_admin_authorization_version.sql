-- Invalidate Admin sessions immediately when authorization-relevant state changes.

ALTER TABLE admin_identities
    ADD COLUMN IF NOT EXISTS authorization_version BIGINT NOT NULL DEFAULT 1;

CREATE OR REPLACE FUNCTION interlock_bump_admin_authorization_version()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.authorization_version := OLD.authorization_version + 1;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_admin_authorization_version ON admin_identities;
CREATE TRIGGER trg_admin_authorization_version
BEFORE UPDATE OF username, roles, enabled, oidc_subject ON admin_identities
FOR EACH ROW
WHEN (
    OLD.username IS DISTINCT FROM NEW.username
    OR OLD.roles IS DISTINCT FROM NEW.roles
    OR OLD.enabled IS DISTINCT FROM NEW.enabled
    OR OLD.oidc_subject IS DISTINCT FROM NEW.oidc_subject
)
EXECUTE FUNCTION interlock_bump_admin_authorization_version();
