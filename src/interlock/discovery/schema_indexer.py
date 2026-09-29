"""Index a registered PostgreSQL source into the discovery catalog.

AUDIT-COVERS: P4-T05, P4-T07 (schema-introspection variant)

This module is the bridge between ``SchemaCataloger`` (which produces
asset dicts from ``information_schema``) and ``DocumentIndexer`` (which
writes to ``discovery_assets``, ``entity_document_xref``,
``category_taxonomy``, and the FAISS index).

The original ``DocumentIndexer.index_document`` expects a free-text
``content`` blob and a ``DocumentSummary``. For a relational schema we
construct a synthetic content blob from the table name plus the column
names and types (so PG full-text search has something to chew on) and a
synthetic ``DocumentSummary`` whose:

- ``title`` is the table name
- ``summary`` is a one-line human-readable description
- ``topics`` are the underscore-split tokens of the table name
- ``entities`` are the table's columns; we also feed
  ``EntityManager.store_entities`` directly so we can label each entity
  with its column classification (free_text / identifier / numeric / etc.)
- ``category_path`` is ``schema.table-prefix`` (the schema acts as
  the top-level category; a heuristic groups tables that share a
  common ``_``-separated prefix).
- ``quality_signals`` come from the column count and row estimate.

The embedding is computed from the same searchable text so the FAISS
index has a real vector for vector search.

All steps are best-effort: if no embedding engine is configured, the
asset is still cataloged (PG full-text + metadata search still work).
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from interlock.cache.embedding import EmbeddingEngine
from interlock.discovery.cataloger import SchemaCataloger, classify_column
from interlock.discovery.category import CategoryManager
from interlock.discovery.entities import EntityManager
from interlock.worker.indexer import DocumentIndexer
from interlock.worker.summarizer import DocumentSummary

logger = logging.getLogger(__name__)


def _format_summary_text(table_name: str, columns: list[dict[str, Any]]) -> str:
    """Build a one-line, human-readable summary for a table."""
    parts = [f"Table '{table_name}' with {len(columns)} columns."]
    head = ", ".join(c["column_name"] for c in columns[:8])
    if head:
        if len(columns) > 8:
            parts.append(f"Columns include: {head}, ...")
        else:
            parts.append(f"Columns: {head}.")
    return " ".join(parts)


def _format_search_content(table_name: str, columns: list[dict[str, Any]]) -> str:
    """Build the searchable blob: table name + column names + types.

    PG ``to_tsvector('english', ...)`` chokes on punctuation but is
    happy with underscored tokens, so we both keep and split the
    underscore tokens to maximize recall.
    """
    bits: list[str] = [table_name, table_name.replace("_", " ")]
    for c in columns:
        name = c.get("column_name", "")
        dtype = c.get("data_type", "")
        bits.append(name)
        bits.append(name.replace("_", " "))
        bits.append(dtype)
    return " ".join(b for b in bits if b)


def _derive_category_path(source_id: str, schema: str, table_name: str) -> str:
    """Pick an ltree-friendly category path for the table.

    The path is ``<source_id>.<schema>.<prefix>`` where ``prefix`` is
    the table name's leading underscore-separated token if it is shared
    by multiple tables, otherwise we just use ``<source_id>.<schema>``.
    DocumentIndexer's ``_assign_category`` further sanitizes the path
    for ltree compatibility.

    We keep the source_id as the top-level segment so the operator's
    Categories tree groups tables by source, which is what they expect
    visually.
    """
    parts = [source_id, schema]
    head = table_name.split("_", 1)[0]
    if head and head != table_name:
        parts.append(head)
    return ".".join(parts)


def _column_entities(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map each column to an entity row with its classification."""
    out: list[dict[str, Any]] = []
    for c in columns:
        name = c.get("column_name") or ""
        dtype = c.get("data_type") or ""
        if not name:
            continue
        cls = classify_column(name, dtype)
        # entity_type drives the badge on the entity tab. We use the
        # column classification so the operator immediately sees which
        # columns are free-text (highest PII risk), identifiers,
        # numeric measures, etc.
        out.append({"text": name, "type": cls, "prominence": 1.0})
    return out


class SchemaIndexer:
    """Turn a registered PG source's schema into discovery catalog rows.

    Composes ``SchemaCataloger`` + ``DocumentIndexer`` so every table
    becomes an asset, every column becomes an entity cross-reference,
    every schema becomes a category branch, and every table gets a
    vector embedding (if embedding is available).
    """

    def __init__(
        self,
        control_pool: asyncpg.Pool,
        embedding_engine: EmbeddingEngine | None = None,
        document_indexer: DocumentIndexer | None = None,
    ) -> None:
        self._cataloger = SchemaCataloger(control_pool)
        self._control_pool = control_pool
        self._engine = embedding_engine
        # If no DocumentIndexer is supplied, build a minimal one so
        # callers do not have to wire every dependency themselves.
        if document_indexer is None:
            entity_manager = EntityManager(control_pool)
            category_manager = CategoryManager(control_pool)
            document_indexer = DocumentIndexer(
                pg_pool=control_pool,
                entity_manager=entity_manager,
                category_manager=category_manager,
                semantic_index=None,  # caller can supply a richer indexer
                embedding_engine=embedding_engine,
            )
        self._indexer = document_indexer

    async def _ensure_ancestor_categories(self, category_path: str) -> None:
        """Idempotently create every ancestor segment of ``category_path``.

        DocumentIndexer's category assignment creates only the leaf
        node; without ancestors the operator cannot drill in from
        ``actualize`` to ``actualize.public.auth``. We pre-create the
        chain with friendly display names so the breadcrumbs work.
        """
        # Use the same sanitization rules DocumentIndexer uses for its
        # leaf path so the chain is consistent.
        parts = category_path.split(".")
        sanitized: list[str] = []
        for part in parts:
            clean = "".join(c if c.isalnum() or c == "_" else "_" for c in part)
            clean = clean.strip("_").lower()
            if clean:
                sanitized.append(clean)
        if not sanitized:
            return

        em = (
            self._indexer._category_manager  # noqa: SLF001
            if hasattr(self._indexer, "_category_manager")
            else None
        )
        if em is None:
            return

        for i in range(1, len(sanitized) + 1):
            sub_path = ".".join(sanitized[:i])
            leaf_label = sanitized[i - 1].replace("_", " ").title()
            try:
                await em.create_category(path=sub_path, name=leaf_label)
            except Exception:
                # create_category is idempotent; log and continue.
                logger.debug(
                    "Ancestor category create failed for %s (likely race)",
                    sub_path,
                )

    async def _index_assets(
        self, source_id: str, assets: list[dict[str, Any]], stats: dict[str, int]
    ) -> None:
        """Index each table asset into discovery, updating `stats` in place."""
        for asset in assets:
            path = asset["asset_path"]  # "schema.table"
            schema, _, table_name = path.partition(".")
            columns: list[dict[str, Any]] = asset["summary"]["columns"]

            content = _format_search_content(table_name, columns)
            summary = DocumentSummary(
                title=table_name,
                doc_type="table",
                summary=_format_summary_text(table_name, columns),
                topics=asset["topics"],
                entities=[c["column_name"] for c in columns],
                category_path=_derive_category_path(source_id, schema or "public", table_name),
                quality_signals={
                    "completeness": 1.0 if columns else 0.0,
                    "structure": 1.0,
                },
            )

            # Pre-create ancestor categories so the category detail
            # page can resolve any intermediate path (e.g. clicking
            # 'actualize' from a breadcrumb).
            await self._ensure_ancestor_categories(summary.category_path)

            embedding: list[float] | None = None
            if self._engine is not None and getattr(self._engine, "available", False):
                try:
                    embedding = await self._engine.embed(content)
                except Exception:
                    logger.warning(
                        "Embedding failed for %s.%s; cataloging without vector",
                        source_id,
                        path,
                    )

            try:
                asset_id = await self._indexer.index_document(
                    source_id=source_id,
                    asset_path=path,
                    asset_type="table",
                    content=content,
                    summary=summary,
                    embedding=embedding,
                )
            except Exception:
                logger.exception(
                    "Failed to index asset source=%s path=%s",
                    source_id,
                    path,
                )
                stats["errors"] += 1
                continue

            # The default DocumentIndexer entity store labels everything
            # as type="unknown". Replace that with a column-classified
            # set so the entity tab shows useful badges.
            try:
                entities = _column_entities(columns)
                if entities:
                    em = (
                        self._indexer._entity_manager  # noqa: SLF001 - intentional
                        if hasattr(self._indexer, "_entity_manager")
                        else None
                    )
                    if em is not None:
                        # Wipe the placeholder rows DocumentIndexer wrote
                        # so the classified entities take precedence.
                        await self._control_pool.execute(
                            "DELETE FROM entity_document_xref WHERE document_id = $1",
                            asset_id,
                        )
                        added = await em.store_entities(asset_id, entities)
                        stats["entities"] += added
            except Exception:
                logger.exception(
                    "Failed to re-store classified entities for asset_id=%s",
                    asset_id,
                )
                stats["errors"] += 1

            stats["tables"] += 1

    async def index_from_catalog(self, source_id: str) -> dict[str, int]:
        """Index a source's tables into discovery from the source catalog.

        The worker calls this after a catalog scan changes a SQL source, so
        discovery reflects the same structure roles and policies see, rather than
        a second introspection that could disagree with it. Tables the catalog
        now records as removed are pruned from discovery, which the old startup
        scanner never did.
        """
        stats = {"tables": 0, "entities": 0, "errors": 0, "pruned": 0}
        rows = await self._control_pool.fetch(
            """
            SELECT t.path AS table_path, t.name AS table_name,
                   t.attributes AS table_attributes,
                   c.name AS column_name, c.data_type, c.ordinal,
                   c.attributes AS column_attributes
            FROM source_catalog t
            LEFT JOIN source_catalog c
                   ON c.source_id = t.source_id AND c.parent_path = t.path
                  AND c.node_type = 'column' AND c.removed_at IS NULL
            WHERE t.source_id = $1
              AND t.node_type IN ('table', 'view', 'materialized_view', 'foreign_table')
              AND t.removed_at IS NULL
            ORDER BY t.path, c.ordinal
            """,
            source_id,
        )
        assets = _assets_from_catalog_rows(source_id, rows)
        await self._index_assets(source_id, assets, stats)

        live_paths = [asset["asset_path"] for asset in assets]
        pruned = await self._control_pool.execute(
            """
            DELETE FROM discovery_assets
            WHERE source_id = $1 AND asset_type = 'table'
              AND asset_path NOT LIKE '%://%'
              AND asset_path <> ALL($2::text[])
            """,
            source_id,
            live_paths,
        )
        stats["pruned"] = _affected(pruned)
        logger.info(
            "Indexed source=%s from catalog tables=%d entities=%d pruned=%d errors=%d",
            source_id,
            stats["tables"],
            stats["entities"],
            stats["pruned"],
            stats["errors"],
        )
        return stats

    async def index_source(self, source_id: str, upstream_pool: asyncpg.Pool) -> dict[str, int]:
        """Introspect ``upstream_pool`` and index every table found.

        Returns a stats dict: ``{tables, entities, errors}``.
        """
        stats = {"tables": 0, "entities": 0, "errors": 0}

        try:
            assets = await self._cataloger.introspect_source(source_id, upstream_pool)
        except Exception:
            logger.exception("Schema introspection failed for source=%s", source_id)
            stats["errors"] += 1
            return stats

        await self._index_assets(source_id, assets, stats)

        logger.info(
            "Indexed source=%s tables=%d entities=%d errors=%d",
            source_id,
            stats["tables"],
            stats["entities"],
            stats["errors"],
        )
        return stats


def _affected(status: str) -> int:
    """Row count from an asyncpg command status such as ``DELETE 3``."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        import json

        value = json.loads(value or "{}")
    return value if isinstance(value, dict) else {}


def _assets_from_catalog_rows(source_id: str, rows: list[Any]) -> list[dict[str, Any]]:
    """Catalog rows, one per table x column, as the asset dicts indexing takes.

    The asset path keeps the `schema.table` form discovery has always used.
    """
    assets: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        path = tuple(row["table_path"])
        asset = assets.get(path)
        if asset is None:
            table_name = str(row["table_name"])
            asset = {
                "source_id": source_id,
                "asset_type": "table",
                "asset_path": ".".join(path),
                "title": table_name,
                "summary": {
                    "columns": [],
                    "row_estimate": _json_object(row["table_attributes"]).get("row_estimate") or 0,
                },
                "topics": [part for part in table_name.split("_") if len(part) > 1],
            }
            assets[path] = asset
        if row["column_name"] is not None:
            asset["summary"]["columns"].append(
                {
                    "column_name": row["column_name"],
                    "data_type": row["data_type"] or "",
                    "is_nullable": (
                        "YES" if _json_object(row["column_attributes"]).get("nullable") else "NO"
                    ),
                    "column_default": None,
                    "ordinal_position": row["ordinal"],
                }
            )
    for asset in assets.values():
        asset["summary"]["column_count"] = len(asset["summary"]["columns"])
    return list(assets.values())
