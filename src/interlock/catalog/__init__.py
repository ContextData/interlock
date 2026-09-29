"""The source catalog: what each registered source contains, structurally.

A collector enumerates a source's structure - schemas, tables and columns for
SQL; prefixes, channels or repositories elsewhere - into a `CatalogSnapshot`.
The worker runs collectors as queued scans and `store.apply_snapshot` records
the result, with drift between scans. Roles, policies, agents and the admin
read the catalog; nothing here ever reads row data.
"""
