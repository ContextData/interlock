-- A built-in default admin must change its password before doing anything else.
--
-- `must_change_password` is set only for the account the admin bootstraps with
-- the default password. `password_changed_at` records the last change.
-- A password change now also bumps `authorization_version`, so every other
-- session of that admin is revoked the moment the password changes.

ALTER TABLE admin_identities
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;

DROP TRIGGER IF EXISTS trg_admin_authorization_version ON admin_identities;
CREATE TRIGGER trg_admin_authorization_version
BEFORE UPDATE OF username, roles, enabled, oidc_subject, password_hash ON admin_identities
FOR EACH ROW
WHEN (
    OLD.username IS DISTINCT FROM NEW.username
    OR OLD.roles IS DISTINCT FROM NEW.roles
    OR OLD.enabled IS DISTINCT FROM NEW.enabled
    OR OLD.oidc_subject IS DISTINCT FROM NEW.oidc_subject
    OR OLD.password_hash IS DISTINCT FROM NEW.password_hash
)
EXECUTE FUNCTION interlock_bump_admin_authorization_version();
