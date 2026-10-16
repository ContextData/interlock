---
title: Manage admin users
description: Add, change, reset, disable and remove the people who sign in to the console.
sidebar:
  order: 5
---

Console users are the people who sign in to the admin console. They are
separate from agent identities, which connect to the gateway with an API key.
Only administrators (`owner` or `admin`) can manage console users.

## Your own password

Any admin can change their own password from **Change password** in the
sidebar: at least 12 characters, not `admin`, not the username and not the
current password. Changing it signs that account out everywhere else.

## Adding a console user

Open **Console Users** in the sidebar and choose **+ New Console User**. Enter a
username (lowercase letters, digits and `. _ @ -`) and pick a user type:

| User type | Roles | Can |
|---|---|---|
| Administrator | `owner` | everything, including managing console users |
| Read-write | `security_admin` | change sources, policies, agents and their keys, and review approvals; not console users |
| Read-only | `viewer` | open every page and read API; change nothing |
| Custom | any of `security_admin`, `source_admin`, `policy_admin`, `approval_reviewer`, `auditor`, `viewer` | what those roles allow, for example only reviewing approvals |

InterLock generates a temporary password and shows it **once**. Give it to the
person privately. When they first sign in they must choose their own password
before anything else opens. InterLock keeps only its hash, so if it is lost,
reset it.

The same works through the API, as an administrator:

```bash
curl -sS -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:9090/api/admin-users \
  -d '{"username": "jane", "user_type": "read_only"}'
```

The response holds `temporary_password`, once. For a custom user, send
`"user_type": "custom"` and `"roles": ["approval_reviewer", "auditor"]`.

## Changing, resetting and removing

From a user's page:

- **Edit** changes their type or roles, email and SSO subject. A role change
  takes effect on their next request.
- **Reset password** gives them a new temporary password, shown once, and signs
  them out.
- **Disable** signs them out at once and stops them signing in. **Enable**
  reverses it.
- **Delete** is offered only for a disabled user. Audit records keep their
  username.

Two rules keep the console reachable. An administrator cannot change their own
type, disable, reset or delete their own account here; ask another
administrator. And the last enabled administrator can never be demoted,
disabled or deleted: add or enable another administrator first.

Every change is recorded in the admin audit log as `admin_user.create`,
`update`, `reset_password`, `disable`, `enable` or `delete`. If the audit log
cannot be written, the request fails rather than going unrecorded.

## Single sign-on

With OIDC enabled, password sign-in is limited to administrators, as a
break-glass path. Everyone else signs in through your identity provider, so a
non-administrator console user needs their SSO subject (the `sub` claim): enter
it when adding them, and no password is created. An administrator added while
SSO is on still gets a temporary password for break-glass sign-in.

At SSO sign-in, a user's roles are their own plus those mapped from their
identity-provider groups by `auth.oidc.admin_group_role_map`, so a group can
give a Read-only user more. Only administrators can set the SSO subject of a
console user, because that subject decides which account an SSO sign-in
becomes.

## If no administrator can sign in

If every administrator is locked out, an operator with access to the control
database can add one. Generate a password hash inside the admin container:

```bash
docker compose exec admin python -c \
  "from interlock.admin.auth import hash_password; print(hash_password('a-temporary-passphrase'))"
```

Then insert the account with a temporary password it must change:

```sql
INSERT INTO admin_identities (username, password_hash, roles, enabled, must_change_password)
VALUES ('recovery', '<hash from above>', ARRAY['owner'], TRUE, TRUE);
```

Roles are described in [Admin access](/concepts/admin-access/).
