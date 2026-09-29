-- Pre-provisioned OIDC subjects for Admin SSO and agent JWT identities.
-- OIDC identifies a principal; database roles and source-role grants remain
-- authoritative for authorization.

ALTER TABLE admin_identities
    ALTER COLUMN password_hash DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS oidc_subject TEXT,
    ADD COLUMN IF NOT EXISTS email TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_identities_oidc_subject
    ON admin_identities (oidc_subject)
    WHERE oidc_subject IS NOT NULL;

ALTER TABLE identities
    ALTER COLUMN api_key_hash DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS oidc_subject TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_oidc_subject
    ON identities (oidc_subject)
    WHERE oidc_subject IS NOT NULL;
