---
title: Observability
description: Health checks, logs, traces and metrics for running InterLock.
sidebar:
  order: 9
---

- **Readiness.** `GET /ready` on the gateway and admin checks every dependency
  and returns each check in its body; use it for readiness probes and alerts.
  `GET /health` is liveness only.
- **Logs.** Each service logs to standard output. Credentials and secret-like
  fields are redacted before logging; upstream error details stay in the log
  rather than being returned to agents.
- **Traces and metrics.** Set `observability.otlp_endpoint` to an OTLP HTTP
  collector to export traces (one span per HTTP request) and metrics. Nothing is
  exported when it is unset. Needs the `otel` extra, which the published image
  includes.
- **Gateway statistics.** `GET /stats` reports cache hit counts, active
  connections and registered sources.
- **The audit log** is the record of what agents did; the usage view summarises
  it. See [Audit and usage](/concepts/audit-and-usage/).
- **Worker health.** The **Workers** page shows each worker's heartbeat, and
  **Ingestion** shows job state.

Service-level objectives and recovery targets are in the
[availability runbook](/operations/runbooks/availability-rpo-rto/).
