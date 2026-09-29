---
title: Grant and revoke access
description: Give an existing agent access to a source, or take it away, without re-keying.
sidebar:
  order: 3
---

On the identity's page, **Source-role grants** lists what it holds. Add a grant
by choosing a source and one of its roles; revoke one with its **Revoke**
action. Both apply to the agent's next request and neither changes its key.

The API equivalents:

```bash
curl -sS -b cookies.txt -X POST "$ADMIN/api/identities/$ID/source-role-grants" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"source_id": "sales_pg", "role_id": 12}'
curl -sS -b cookies.txt -X DELETE "$ADMIN/api/identities/$ID/source-role-grants/$GRANT_ID" \
  -H "X-CSRF-Token: $CSRF"
```

Revoked grants are kept as history. A role cannot be deleted while any identity
holds an active grant of it.
