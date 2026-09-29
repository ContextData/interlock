---
title: Rotate and revoke keys
description: Replace an agent's key, or cut an agent off immediately.
sidebar:
  order: 2
---

- **Rotate**: on the identity's page, **Rotate API Key** issues a new key,
  shown once. The old key stops working at once and its cached session is
  dropped. Update the agent before or right after.
- **Revoke**: disable the identity (the console, or `PUT /api/identities/{id}`
  with `"enabled": false`). The next request is refused, because every request
  is revalidated against the database.
- **Delete**: removes the identity and its grants. Its audit rows keep its
  name, marked deleted.

Rotating the server-side API-key pepper invalidates every key at once; there is
no dual-pepper transition. Treat it as a planned re-key of every agent.
