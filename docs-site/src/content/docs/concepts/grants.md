---
title: Grants
description: How identities receive source roles, and how grants are changed and revoked.
sidebar:
  order: 9
---

A **grant** gives one identity one source role on one source. An identity can
hold several roles on a source; their allows add up and any deny wins.

- **Add** a grant from the identity's page (or `POST
  /api/identities/{id}/source-role-grants`). It applies to the next request;
  the agent keeps its key.
- **Revoke** it the same way. The next request is refused.
- A grant can carry an **expiry**, after which it no longer counts.

Revoked grants stay in the record, so an identity's page shows what it held and
when. A role with an active grant cannot be deleted.

Legacy role labels on an identity are not grants and confer no access. They
exist only for policy conditions.
