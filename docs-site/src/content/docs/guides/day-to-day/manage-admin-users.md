---
title: Manage admin users
description: Add admin accounts, change passwords and use single sign-on.
sidebar:
  order: 5
---

## Your own password

Any admin can change their own password from **Change password** in the
sidebar: at least 12 characters, not `admin`, not the username and not the
current password. Changing it signs that account out everywhere else.

## Adding another admin

The console has no page for adding admin accounts yet. Until it does, add one in
the control database, with a temporary password it must change at first
sign-in. Generate the hash inside the admin container:

```bash
docker compose exec admin python -c \
  "from interlock.admin.auth import hash_password; print(hash_password('a-temporary-passphrase'))"
```

Then insert the account, choosing its roles:

```sql
INSERT INTO admin_identities (username, password_hash, roles, enabled, must_change_password)
VALUES ('alex', '<hash from above>', ARRAY['source_admin'], TRUE, TRUE);
```

Give the temporary password to the person privately. Disable an account with
`UPDATE admin_identities SET enabled = FALSE WHERE username = 'alex'`; its
sessions end on their next request.

## Single sign-on

With OIDC enabled, admins sign in through your identity provider. Their roles
are the account's own roles plus those mapped from their groups by
`auth.oidc.admin_group_role_map`. An existing admin account is linked to an
identity-provider subject with `PUT /api/admin-auth/oidc/admins/{id}`. Password
sign-in can remain for `owner` and `admin` accounts as a break-glass path.

Roles are described in [Admin access](/concepts/admin-access/).
