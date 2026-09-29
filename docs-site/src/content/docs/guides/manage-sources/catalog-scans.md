---
title: Catalog scans
description: When sources are scanned, how to rescan, and how to read drift.
sidebar:
  order: 4
---

A worker scans a source's structure when it is saved, when you press
**Rescan** in the source's **Catalog** section, and when its last successful
scan is older than `catalog.refresh_interval_seconds` (a day by default). The
API equivalent is `POST /api/data-sources/{id}/catalog/scans`.

The Catalog section shows the last scan, its history, the structure found, the
columns marked as PII, and **drift**: what was added, removed or changed. A new
table or column lists the roles that can already read it through wildcard
patterns, so you can see new exposure as it appears. Acknowledge drift once
reviewed.

The **Catalog** page searches across sources, and **Access analytics** shows
what agents actually read, from the audit log: most-read tables, PII columns
read, catalogued objects never read, and denied attempts.

Scans are bounded (see the `catalog.*` settings in
[Configuration](/reference/configuration/)). A scan that hits a cap says so and
never marks objects as removed.
