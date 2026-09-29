"""Tests for the worker process entrypoint wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.config import InterLockConfig, WorkerConfig


@pytest.mark.asyncio
async def test_create_worker_runtime_uses_shared_config_factories(monkeypatch):
    """Worker startup should honor shared config, including DB TLS/env settings."""
    from interlock.worker import __main__ as worker_main

    config = InterLockConfig(
        worker=WorkerConfig(
            worker_id="cfg-worker",
            concurrency=4,
            heartbeat_interval=6.5,
            lease_seconds=123,
        )
    )
    pg_pool = AsyncMock(name="pg_pool")
    redis_client = AsyncMock(name="redis_client")
    created_worker = None

    class FakeEmbeddingEngine:
        available = True
        dimension = 384

        async def initialize(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    class FakeFAISSIndex:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def initialize(self) -> None:
            return None

    class FakeIngestionWorker:
        def __init__(self, **kwargs):
            nonlocal created_worker
            self.kwargs = kwargs
            created_worker = self

    monkeypatch.setattr(worker_main, "create_pg_pool", AsyncMock(return_value=pg_pool))
    monkeypatch.setattr(worker_main, "create_redis_client", AsyncMock(return_value=redis_client))
    monkeypatch.setattr(worker_main, "create_default_registry", MagicMock(return_value=object()))
    monkeypatch.setattr(worker_main, "ContentSampler", MagicMock(return_value=object()))
    monkeypatch.setattr(worker_main, "DocumentSummarizer", MagicMock(return_value=object()))
    monkeypatch.setattr(worker_main, "EmbeddingEngine", FakeEmbeddingEngine)
    monkeypatch.setattr(worker_main, "FAISSIndex", FakeFAISSIndex)
    monkeypatch.setattr(worker_main, "EntityManager", MagicMock(return_value=object()))
    monkeypatch.setattr(worker_main, "CategoryManager", MagicMock(return_value=object()))
    monkeypatch.setattr(worker_main, "DocumentIndexer", MagicMock(return_value=object()))
    monkeypatch.setattr(worker_main, "IngestionWorker", FakeIngestionWorker)
    monkeypatch.setattr(worker_main, "_configure_worker_observability", MagicMock())

    runtime = await worker_main._create_worker_runtime(config)

    worker_main.create_pg_pool.assert_awaited_once_with(config.database)
    worker_main.create_redis_client.assert_awaited_once_with(config.redis)
    assert runtime.pg_pool is pg_pool
    assert runtime.redis_client is redis_client
    assert runtime.worker is created_worker
    assert created_worker.kwargs["worker_id"] == "cfg-worker"
    assert created_worker.kwargs["pg_pool"] is pg_pool
    assert created_worker.kwargs["redis_client"] is redis_client
    assert created_worker.kwargs["heartbeat_interval"] == 6.5
    assert created_worker.kwargs["concurrency"] == 4
    assert created_worker.kwargs["lease_seconds"] == 123
