---
title: Write source roles
description: Create and edit a source role's statements in the console.
sidebar:
  order: 4
---

On a source's page, under **Source Roles**, choose **New Role**, or **Edit** an
existing one. A role needs a key (lowercase, used by policies), a name, and one
or more statements.

For each statement:

- **Effect**: `allow` or `deny`.
- **Action**: grouped by read, write, admin and discovery; only this
  connector's actions are offered.
- **Resource type**: filtered to what the chosen action applies to.
- **Resource pattern**: with a hint for the type, and suggestions from the
  source's catalog (for example `sales.customers.*` or a Slack channel).
- **Conditions**: typed controls for the conditions this action supports, such
  as write risk levels or allowed channels. **Edit as JSON** is there for
  anything else.

The editor warns as you type: a pattern that matches nothing in the catalog, a
column-scoped statement that will make `SELECT *` on that table refuse, a
legacy `public.` prefix on a MySQL source. It refuses an action or condition
this connector does not have, and a condition on a `deny` statement that can
never match.

Use the role **dry-run** to check a request against the role before granting
it. Delete a role with **Delete** once no identity holds it.

How statements are evaluated is in [Source roles](/concepts/source-roles/).
