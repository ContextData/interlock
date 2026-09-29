"""Unit tests for ingestion admin API endpoints."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from interlock.admin.app import create_app
from interlock.config import InterLockConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> InterLockConfig:
    return InterLockConfig()


class FakeRecord:
    """Minimal asyncpg.Record stand-in."""

    def __init__(self, data: dict):
        self._data = dict(data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __iter__(self):
        return iter(self._data.keys())

    def __getitem__(self, key):
        return self._data[key]


def _job_record(
    id: int = 1,
    source_id: str = "src1",
    file_path: str = "/data/test.txt",
    status: str = "queued",
    priority_score: float = 0.0,
    worker_id: str | None = None,
    error_message: str | None = None,
    retry_count: int = 0,
) -> FakeRecord:
    return FakeRecord(
        {
            "id": id,
            "source_id": source_id,
            "file_path": file_path,
            "status": status,
            "priority_score": priority_score,
            "worker_id": worker_id,
            "started_at": None,
            "completed_at": None,
            "error_message": error_message,
            "retry_count": retry_count,
            "metadata": {},
            "created_at": datetime.now(UTC),
        }
    )


def _setup_app(
    pool_fetch=None,
    pool_fetchrow=None,
    pool_execute=None,
    redis_scan=None,
    redis_get=None,
):
    """Create app with mocked PG pool and Redis."""
    config = _make_config()
    app = create_app(config)

    pool = AsyncMock()
    if pool_fetch is not None:
        pool.fetch = pool_fetch
    if pool_fetchrow is not None:
        pool.fetchrow = pool_fetchrow
    if pool_execute is not None:
        pool.execute = pool_execute

    redis = AsyncMock()
    if redis_scan is not None:
        redis.scan = redis_scan
    if redis_get is not None:
        redis.get = redis_get

    app.state.pg_pool = pool
    app.state.redis = redis
    app.state.auth_disabled = True
    return app, pool, redis


# ---------------------------------------------------------------------------
# Tests: GET /api/ingestion/jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    @pytest.mark.anyio
    async def test_list_jobs_returns_list(self):
        records = [_job_record(1), _job_record(2, status="processing")]
        app, pool, _ = _setup_app(pool_fetch=AsyncMock(return_value=records))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/ingestion/jobs")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == 1

    @pytest.mark.anyio
    async def test_list_jobs_with_status_filter(self):
        records = [_job_record("j1", status="failed")]
        app, pool, _ = _setup_app(pool_fetch=AsyncMock(return_value=records))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/ingestion/jobs?status=failed")

        assert resp.status_code == 200
        pool.fetch.assert_called_once()
        call_args = pool.fetch.call_args
        assert "status" in call_args[0][0].lower() or "failed" in str(call_args)


# ---------------------------------------------------------------------------
# Tests: POST /api/ingestion/jobs
# ---------------------------------------------------------------------------


class TestCreateJob:
    @pytest.mark.anyio
    async def test_create_job(self):
        record = _job_record(10, source_id="mysrc")
        app, pool, _ = _setup_app(pool_fetchrow=AsyncMock(return_value=record))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/ingestion/jobs",
                json={"source_id": "mysrc", "file_path": "/data/test.txt", "priority_score": 5.0},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["source_id"] == "mysrc"


# ---------------------------------------------------------------------------
# Tests: POST /api/ingestion/sources/{source_id}/sync
# ---------------------------------------------------------------------------


class TestSyncSourceAssets:
    @pytest.mark.anyio
    async def test_changed_completed_asset_requeues(self):
        source = FakeRecord(
            {
                "source_id": "src1",
                "source_type": "s3",
                "connection_config": {},
                "metadata": {"connector_key": "s3"},
            }
        )
        upsert_result = FakeRecord({"inserted": False, "status": "queued", "requeued": True})
        app, pool, _ = _setup_app(pool_fetchrow=AsyncMock(side_effect=[source, upsert_result]))

        adapter = AsyncMock()
        adapter.list_assets = AsyncMock(
            return_value=[
                {
                    "asset_path": "s3://bucket/doc.md",
                    "asset_type": "file",
                    "title": "Doc",
                    "etag": "new-etag",
                    "last_modified": "2026-07-17T12:00:00Z",
                }
            ]
        )

        with patch("interlock.admin.routes.ingestion.get_adapter", return_value=adapter):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/ingestion/sources/src1/sync")

        assert resp.status_code == 200
        body = resp.json()
        assert body["assets_seen"] == 1
        assert body["jobs_created"] == 0
        assert body["jobs_requeued"] == 1

        sql = pool.fetchrow.await_args_list[1].args[0]
        assert "ingestion_jobs.status = 'completed'" in sql
        assert "etag" in sql
        assert "last_modified" in sql
        assert "lease_expires_at" in sql

    @pytest.mark.anyio
    async def test_changed_active_asset_schedules_followup_without_relabeling_current_work(self):
        source = FakeRecord(
            {
                "source_id": "src1",
                "source_type": "s3",
                "connection_config": {},
                "metadata": {"connector_key": "s3"},
            }
        )
        upsert_result = FakeRecord({"inserted": False, "status": "indexing", "requeued": True})
        app, pool, _ = _setup_app(pool_fetchrow=AsyncMock(side_effect=[source, upsert_result]))

        adapter = AsyncMock()
        adapter.list_assets = AsyncMock(
            return_value=[
                {
                    "asset_path": "s3://bucket/doc.md",
                    "asset_type": "file",
                    "etag": "new-etag",
                    "last_modified": "2026-07-17T12:00:00Z",
                }
            ]
        )

        with patch("interlock.admin.routes.ingestion.get_adapter", return_value=adapter):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/ingestion/sources/src1/sync")

        assert resp.status_code == 200
        assert resp.json()["jobs_requeued"] == 1

        sql = pool.fetchrow.await_args_list[1].args[0]
        assert "pending_refresh_metadata" in sql
        assert (
            "ingestion_jobs.status IN ('extracting', 'summarizing', 'indexing', 'processing')"
            in sql
        )
        assert "ELSE ingestion_jobs.metadata" in sql
        assert "metadata = EXCLUDED.metadata" not in sql

    @pytest.mark.anyio
    async def test_unchanged_existing_job_is_not_counted_as_requeued(self):
        source = FakeRecord(
            {
                "source_id": "src1",
                "source_type": "s3",
                "connection_config": {},
                "metadata": {"connector_key": "s3"},
            }
        )
        upsert_result = FakeRecord({"inserted": False, "status": "queued", "requeued": False})
        app, pool, _ = _setup_app(pool_fetchrow=AsyncMock(side_effect=[source, upsert_result]))

        adapter = AsyncMock()
        adapter.list_assets = AsyncMock(return_value=[{"asset_path": "s3://bucket/doc.md"}])

        with patch("interlock.admin.routes.ingestion.get_adapter", return_value=adapter):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/ingestion/sources/src1/sync")

        assert resp.status_code == 200
        assert resp.json()["jobs_requeued"] == 0


# ---------------------------------------------------------------------------
# Tests: POST /api/ingestion/jobs/{id}/retry
# ---------------------------------------------------------------------------


class TestRetryJob:
    @pytest.mark.anyio
    async def test_retry_failed_job(self):
        record = _job_record(1, status="queued")
        app, pool, _ = _setup_app(pool_fetchrow=AsyncMock(return_value=record))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/jobs/1/retry")

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_retry_failed_job_writes_admin_audit(self):
        record = _job_record(1, status="queued", retry_count=2)
        app, pool, _ = _setup_app(pool_fetchrow=AsyncMock(return_value=record))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/jobs/1/retry")

        assert resp.status_code == 200
        audit_args = pool.execute.await_args.args
        assert audit_args[3] == "ingestion.job.retry"
        assert audit_args[4] == "ingestion_job"
        detail = audit_args[6]
        assert detail["after"]["status"] == "queued"
        assert detail["after"]["retry_count"] == 2
        assert detail["changed_fields"] == ["error_message", "status", "worker_id"]

    @pytest.mark.anyio
    async def test_retry_non_failed_returns_409(self):
        """Retry returns 409 when job is not in failed state."""
        existing = FakeRecord({"id": 1, "status": "processing"})

        fetchrow = AsyncMock(side_effect=[None, existing])
        app, pool, _ = _setup_app(pool_fetchrow=fetchrow)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/jobs/1/retry")

        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests: POST /api/ingestion/jobs/{id}/cancel
# ---------------------------------------------------------------------------


class TestCancelJob:
    @pytest.mark.anyio
    async def test_cancel_pending_job(self):
        record = _job_record(1, status="cancelled")
        app, pool, _ = _setup_app(pool_fetchrow=AsyncMock(return_value=record))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/jobs/1/cancel")

        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_cancel_running_returns_409(self):
        existing = FakeRecord({"id": 1, "status": "processing"})
        fetchrow = AsyncMock(side_effect=[None, existing])
        app, pool, _ = _setup_app(pool_fetchrow=fetchrow)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/jobs/1/cancel")

        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests: GET /api/ingestion/workers
# ---------------------------------------------------------------------------


class TestListWorkers:
    @pytest.mark.anyio
    async def test_list_workers(self):
        now = time.time()
        heartbeat = json.dumps({"timestamp": now, "metadata": {"cpu": 0.5}})

        redis_scan = AsyncMock(return_value=(b"0", [b"worker:heartbeat:w1"]))
        redis_get = AsyncMock(return_value=heartbeat)

        app, _, redis = _setup_app(redis_scan=redis_scan, redis_get=redis_get)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/ingestion/workers")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["worker_id"] == "w1"
        assert data[0]["stale"] is False


# ---------------------------------------------------------------------------
# Tests: POST /api/ingestion/workers/{id}/health
# ---------------------------------------------------------------------------


class TestWorkerHealthProbe:
    @pytest.mark.anyio
    async def test_healthy_worker(self):
        now = time.time()
        heartbeat = json.dumps({"timestamp": now})

        app, pool, redis = _setup_app(
            redis_get=AsyncMock(return_value=heartbeat),
            pool_execute=AsyncMock(return_value="UPDATE 0"),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/workers/w1/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["alive"] is True
        assert data["stale_jobs_failed"] == 0

    @pytest.mark.anyio
    async def test_stale_worker_fails_jobs(self):
        stale_ts = time.time() - 200  # well past 90s threshold
        heartbeat = json.dumps({"timestamp": stale_ts})

        app, pool, redis = _setup_app(
            redis_get=AsyncMock(return_value=heartbeat),
            pool_execute=AsyncMock(return_value="UPDATE 3"),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/workers/w1/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["alive"] is False
        assert data["stale_jobs_failed"] == 3
        audit_args = pool.execute.await_args_list[-1].args
        assert audit_args[3] == "ingestion.worker.health_probe"
        assert audit_args[4] == "worker"
        assert audit_args[6]["after"]["stale_jobs_failed"] == 3

    @pytest.mark.anyio
    async def test_unknown_worker_returns_404(self):
        app, pool, redis = _setup_app(
            redis_get=AsyncMock(return_value=None),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/ingestion/workers/missing/health")

        assert resp.status_code == 404
