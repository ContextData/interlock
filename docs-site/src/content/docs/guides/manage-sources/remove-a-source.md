---
title: Disable or remove a source
description: Stop serving a source immediately, or delete it.
sidebar:
  order: 5
---

- **Disable** from the source's page to stop serving it while keeping its
  configuration, roles and history. The next request on every protocol is
  refused with `Unknown data source`, cached answers included, and open
  PostgreSQL sessions end on their next statement. Enable it again to resume.
- **Delete** removes the source with its roles, grants, catalog, discovery index and ingestion jobs. Its audit rows are kept.

Both are recorded in the admin audit log, and both clear the source's cache on
every gateway.
