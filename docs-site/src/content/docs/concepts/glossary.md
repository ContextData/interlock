---
title: Glossary
description: The terms used across InterLock's documentation.
sidebar:
  order: 17
---

| Term | Meaning |
|---|---|
| Agent | A program, often an AI assistant, that reads or writes data through InterLock. |
| Identity | How InterLock knows an agent; every request is made by one. |
| API key | The secret an identity authenticates with. Stored only as a peppered hash. |
| Data source | One system agents reach through InterLock, such as a database, bucket or workspace. |
| Source ID | The name agents use for a source: the database name on the PostgreSQL wire, `source_id` over MCP. |
| Connector | The code that knows how to talk to one kind of source. Must be active before sources can use it. |
| Source role | A named set of allow and deny statements on one source. |
| Statement | One rule in a source role: effect, action, resource type, resource pattern, conditions. |
| Grant | One identity holding one source role on one source. |
| Policy rule | Organisation-wide governance applied after roles allow a request: deny, redact, rate-limit, cap write risk. |
| Catalog | The recorded structure of each source, from scans. |
| Drift | What changed in a source's structure between two scans. |
| Discovery | Finding content across sources by meaning, from indexed summaries and embeddings. |
| Approval queue | Where medium and high risk writes wait for a reviewer. |
| Redaction | Replacing a sensitive value in a response with a marker such as `[REDACTED:EMAIL]`. |
| Audit log | One row per governed request, whatever the outcome. |
| Gateway | The service agents connect to. |
| Admin | The console and its API. |
| Worker | The service that runs scans and ingestion. |
