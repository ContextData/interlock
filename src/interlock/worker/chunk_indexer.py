"""Chunk-level indexer - embeds and stores individual chunks in the semantic index.

Status: implemented and unit-tested, but NOT wired into the live
request path. See the "Code Present But Not On The Request Path" table in
docs-site/src/content/docs/reference/feature-status.md before treating this as current behavior.
"""

from __future__ import annotations

import logging
from typing import Any

from interlock.cache.embedding import EmbeddingEngine
from interlock.cache.faiss_index import FAISSIndex
from interlock.models import Chunk

logger = logging.getLogger(__name__)


class ChunkIndexer:
    """Indexes individual chunks into the semantic index.

    Each chunk gets its own embedding and metadata entry, enabling
    sub-document level semantic retrieval.
    """

    def __init__(
        self,
        semantic_index: FAISSIndex | None = None,
        embedding_engine: EmbeddingEngine | None = None,
    ) -> None:
        self._semantic_index = semantic_index
        self._embedding_engine = embedding_engine

    async def index_chunks(
        self,
        document_id: int,
        source_id: str,
        chunks: list[Chunk],
    ) -> int:
        """Embed and index each chunk. Returns count indexed.

        Key format: "{source_id}:{document_id}:chunk:{index}"
        Metadata: document_id, chunk_index, source_id, first_words
        """
        if not chunks:
            return 0

        if self._semantic_index is None or self._embedding_engine is None:
            logger.warning("ChunkIndexer missing semantic_index or embedding_engine - skipping")
            return 0

        if not self._embedding_engine.available:
            logger.warning("Embedding engine not available - skipping chunk indexing")
            return 0

        # Batch embed all chunk texts
        texts = [c.text for c in chunks]
        embeddings = await self._embedding_engine.embed_batch(texts)

        count = 0
        for chunk, embedding in zip(chunks, embeddings):
            if not embedding:
                continue

            key = f"{source_id}:{document_id}:chunk:{chunk.index}"
            first_words = " ".join(chunk.text.split()[:10])

            metadata: dict[str, Any] = {
                "document_id": document_id,
                "chunk_index": chunk.index,
                "source_id": source_id,
                "first_words": first_words,
                "start_word": chunk.start_word,
                "end_word": chunk.end_word,
            }
            # Merge any chunk-level metadata
            metadata.update(chunk.metadata)

            try:
                await self._semantic_index.upsert(
                    key=key,
                    embedding=embedding,
                    metadata=metadata,
                )
                count += 1
            except Exception:
                logger.exception(
                    "Failed to index chunk %d of document %d", chunk.index, document_id
                )

        logger.info(
            "Indexed %d/%d chunks for document %d (source=%s)",
            count,
            len(chunks),
            document_id,
            source_id,
        )
        return count
