---
title: Activate a connector
description: Make a connector available for new sources, or withdraw it.
sidebar:
  order: 2
---

Sources can only be registered on an active connector. Open **Connectors**: the
table lists active connectors, and **Available to activate** lists the rest. A
source admin or security admin can activate or deactivate any connector that is
not `planned`.

- Activating makes it appear in the new-source dropdowns and accepted by the
  API.
- Deactivating stops new registrations only. Existing sources keep working, and
  their pages say the connector is inactive.
- Each change is recorded in the admin audit log as `connector.activate` or
  `connector.deactivate`.

Check the [connector support matrix](/reference/connector-support-matrix/)
before activating: several connectors have working adapters whose governance
has not been exercised by any test stack.
