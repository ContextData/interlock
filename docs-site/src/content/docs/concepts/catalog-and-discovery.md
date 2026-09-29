---
title: Catalog and discovery
description: How InterLock records each source's structure, and how agents find content across sources.
sidebar:
  order: 5
---

## The catalog

InterLock records the structure of each source: schemas, tables, views and
columns for SQL sources; buckets and prefixes for object storage; channels for
Slack; repositories for GitHub; indices, collections and fields for search
sources. A worker scans a source when it is saved, when an admin presses
**Rescan**, and on a schedule (daily by default). Scans are bounded by caps and
say when they hit one; a capped scan never records objects as removed.

The catalog is used for:

- **Enforcement.** For SQL sources, column-level rules resolve each column a
  query touches, and `SELECT *`, against the catalog. See
  [Column-level governance](/concepts/column-level-governance/).
- **Writing roles and policies.** The editors suggest tables, columns and
  channels from the catalog, and warn when a pattern matches nothing.
- **Telling agents what they may use.** `interlock_describe_access` lists the
  objects an agent's roles cover, minus what policy would refuse.
- **Drift.** Each scan records what was added, removed or changed since the
  last, and which roles a new object would be readable by.

The catalog only records what the scanning login can see, and stores no data
values. For object storage, Slack and GitHub the structure is recorded but not
yet used to enforce access on agent paths; those sources are gated by action.

## Discovery

Discovery lets an agent find content by meaning rather than by name: a
question such as "the incident report from March" finds the right object in a
bucket. Workers ingest content from sources that support it, extract text,
write summaries and embeddings, and index them. `interlock_discover` searches
by vector similarity, full text and metadata together, and
`interlock_related_documents` finds neighbours of a result.

Discovery results are governed like any other response: only sources the agent
is granted appear, and results pass through PII redaction.
