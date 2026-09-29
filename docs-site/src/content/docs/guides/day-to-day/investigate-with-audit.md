---
title: Investigate with the audit log
description: Find out who did what, on which source, and what InterLock decided.
sidebar:
  order: 3
---

**Audit & Costs** lists governed requests, newest first. Filter by identity,
source, protocol or outcome. Open an event to see its full record: the
statement or path, the tables touched, the source-role decision and matching
role, the policy decision and matching rule, whether it was cached, the PII
types found and how much was redacted, the approval state for a write, and the
latency.

**Export CSV** downloads the filtered rows, including identity names.

For questions the console does not answer, query `audit_log` in the control
database directly: for example, redaction counts are in its `redaction_stats`
column. Admin actions are in `admin_audit_log`.
