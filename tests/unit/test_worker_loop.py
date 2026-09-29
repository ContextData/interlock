"""Tests for IngestionWorker core loop."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from interlock.worker.extractors.base import ExtractedContent, ExtractionRegistry
from interlock.worker.indexer import DocumentIndexer
from interlock.worker.loop import _CLAIM_SQL, IngestionWorker, JobLeaseLostError
from interlock.worker.summarizer import DocumentSummarizer, DocumentSummary


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    return pool


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.set = AsyncMock()
    r.get = AsyncMock(return_value=None)
    return r


@pytest.fixture
def mock_extractor():
    ext = AsyncMock()
    ext.can_handle = MagicMock(return_value=True)
    ext.extract = AsyncMock(
        return_value=ExtractedContent(
            text="Extracted document text content.",
            metadata={"pages": 5},
            page_count=5,
            word_count=5,
        )
    )
    return ext


@pytest.fixture
def mock_registry(mock_extractor):
    reg = MagicMock(spec=ExtractionRegistry)
    reg.get_extractor = MagicMock(return_value=mock_extractor)
    return reg


@pytest.fixture
def mock_summarizer():
    s = AsyncMock(spec=DocumentSummarizer)
    s.summarize = AsyncMock(
        return_value=DocumentSummary(
            title="Test Doc",
            doc_type="report",
            summary="A test document.",
            topics=["testing"],
            entities=["Alice"],
            category_path="engineering.testing",
        )
    )
    return s


@pytest.fixture
def mock_indexer():
    idx = AsyncMock(spec=DocumentIndexer)
    idx.index_document = AsyncMock(return_value=42)
    idx._embedding_engine = None
    return idx


@pytest.fixture
def sample_job():
    return {
        "id": 101,
        "source_id": "src-1",
        "file_path": "/data/reports/test.pdf",
        "status": "processing",
        "priority_score": 5.0,
        "worker_id": "worker-test",
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        "retry_count": 0,
        "metadata": {},
        "created_at": None,
    }


def make_worker(
    mock_pool,
    mock_redis=None,
    mock_registry=None,
    mock_summarizer=None,
    mock_indexer=None,
    max_retries=3,
    lease_seconds=300,
    max_lease_reclaims=3,
):
    return IngestionWorker(
        worker_id="test-worker-1",
        pg_pool=mock_pool,
        redis_client=mock_redis,
        extraction_registry=mock_registry,
        summarizer=mock_summarizer,
        indexer=mock_indexer,
        heartbeat_interval=1.0,
        max_retries=max_retries,
        poll_interval=0.01,
        lease_seconds=lease_seconds,
        max_lease_reclaims=max_lease_reclaims,
    )


class TestClaimJob:
    """Tests for _claim_job."""

    @pytest.mark.asyncio
    async def test_claim_uses_skip_locked(self, mock_pool):
        """Test that claim query uses FOR UPDATE SKIP LOCKED."""
        assert "FOR UPDATE SKIP LOCKED" in _CLAIM_SQL
        assert "priority_score DESC" in _CLAIM_SQL
        assert "created_at ASC" in _CLAIM_SQL
        assert "lease_reclaim_count" in _CLAIM_SQL
        assert "quarantined_at" in _CLAIM_SQL
        assert "lease_reclaim_limit_exceeded" in _CLAIM_SQL

    @pytest.mark.asyncio
    async def test_claim_returns_job_dict(self, mock_pool, sample_job):
        """Test that _claim_job returns a dict when a job is available."""
        # asyncpg.Record is dict-like, mock as dict
        mock_pool.fetchrow = AsyncMock(return_value=sample_job)
        worker = make_worker(mock_pool)
        job = await worker._claim_job()

        assert job is not None
        assert job["id"] == 101
        assert job["source_id"] == "src-1"
        mock_pool.fetchrow.assert_awaited_once_with(_CLAIM_SQL, "test-worker-1", "300", 3)

    @pytest.mark.asyncio
    async def test_claim_uses_configured_lease_seconds(self, mock_pool, sample_job):
        """Test that initial claim uses the worker's configured lease duration."""
        mock_pool.fetchrow = AsyncMock(return_value=sample_job)
        worker = make_worker(mock_pool, lease_seconds=42)

        await worker._claim_job()

        mock_pool.fetchrow.assert_awaited_once_with(_CLAIM_SQL, "test-worker-1", "42", 3)

    @pytest.mark.asyncio
    async def test_claim_uses_configured_reclaim_limit(self, mock_pool, sample_job):
        """Test that stale lease reclaim uses the configured quarantine bound."""
        mock_pool.fetchrow = AsyncMock(return_value=sample_job)
        worker = make_worker(mock_pool, max_lease_reclaims=2)

        await worker._claim_job()

        mock_pool.fetchrow.assert_awaited_once_with(_CLAIM_SQL, "test-worker-1", "300", 2)

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_empty(self, mock_pool):
        """Test that _claim_job returns None when no jobs available."""
        mock_pool.fetchrow = AsyncMock(return_value=None)
        worker = make_worker(mock_pool)
        job = await worker._claim_job()
        assert job is None


class TestProcessJob:
    """Tests for _process_job pipeline."""

    @pytest.mark.asyncio
    async def test_full_pipeline(
        self, mock_pool, mock_registry, mock_summarizer, mock_indexer, sample_job
    ):
        """Test process_job calls extract -> sample -> summarize -> index."""
        worker = make_worker(
            mock_pool,
            mock_registry=mock_registry,
            mock_summarizer=mock_summarizer,
            mock_indexer=mock_indexer,
        )

        await worker._process_job(sample_job)

        # Extract was called
        extractor = mock_registry.get_extractor.return_value
        extractor.extract.assert_awaited_once_with("/data/reports/test.pdf")

        # Summarize was called
        mock_summarizer.summarize.assert_awaited_once()
        call_args = mock_summarizer.summarize.call_args
        assert call_args[1]["file_path"] == "/data/reports/test.pdf"

        # Index was called
        mock_indexer.index_document.assert_awaited_once()
        idx_args = mock_indexer.index_document.call_args
        assert idx_args[1]["source_id"] == "src-1"
        assert idx_args[1]["asset_path"] == "/data/reports/test.pdf"

    @pytest.mark.asyncio
    async def test_no_extractor_returns_empty_content(
        self, mock_pool, mock_summarizer, mock_indexer, sample_job
    ):
        """Test fallback when no extractor matches."""
        empty_registry = MagicMock(spec=ExtractionRegistry)
        empty_registry.get_extractor = MagicMock(return_value=None)

        worker = make_worker(
            mock_pool,
            mock_registry=empty_registry,
            mock_summarizer=mock_summarizer,
            mock_indexer=mock_indexer,
        )
        await worker._process_job(sample_job)

        # Summarizer still called, just with empty content
        mock_summarizer.summarize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_summarizer_uses_fallback(
        self, mock_pool, mock_registry, mock_indexer, sample_job
    ):
        """Test that a fallback summary is created when no summarizer."""
        worker = make_worker(
            mock_pool,
            mock_registry=mock_registry,
            mock_indexer=mock_indexer,
        )
        await worker._process_job(sample_job)

        mock_indexer.index_document.assert_awaited_once()
        idx_args = mock_indexer.index_document.call_args
        summary = idx_args[1]["summary"]
        assert isinstance(summary, DocumentSummary)
        assert summary.title == "test.pdf"

    @pytest.mark.asyncio
    async def test_lease_lost_after_embedding_skips_index_side_effects(
        self, mock_pool, mock_registry, mock_summarizer, mock_indexer, sample_job
    ):
        """A stolen/cancelled lease after embedding must not publish index writes."""
        embedding_engine = MagicMock()
        embedding_engine.available = True
        embedding_engine.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        mock_indexer._embedding_engine = embedding_engine
        mock_pool.execute = AsyncMock(side_effect=["UPDATE 1", "UPDATE 1", "UPDATE 1", "UPDATE 0"])
        worker = make_worker(
            mock_pool,
            mock_registry=mock_registry,
            mock_summarizer=mock_summarizer,
            mock_indexer=mock_indexer,
        )

        with pytest.raises(JobLeaseLostError):
            await worker._process_job(sample_job)

        embedding_engine.embed.assert_awaited_once()
        mock_indexer.index_document.assert_not_awaited()


class TestMarkComplete:
    """Tests for _mark_complete."""

    @pytest.mark.asyncio
    async def test_mark_complete_updates_status(self, mock_pool):
        """Test that _mark_complete sets status to completed."""
        mock_pool.execute = AsyncMock()
        worker = make_worker(mock_pool)
        await worker._mark_complete(101)

        mock_pool.execute.assert_awaited_once()
        sql = mock_pool.execute.call_args[0][0]
        assert "completed" in sql
        assert "completed_at" in sql
        assert "status = 'indexing'" in sql
        assert mock_pool.execute.call_args[0][1] == 101

    @pytest.mark.asyncio
    async def test_mark_complete_returns_false_when_job_was_cancelled(self, mock_pool):
        """Completion should not overwrite cancellation or stolen leases."""
        mock_pool.execute = AsyncMock(return_value="UPDATE 0")
        worker = make_worker(mock_pool)

        completed = await worker._mark_complete(101)

        assert completed is False

    @pytest.mark.asyncio
    async def test_mark_complete_requeues_pending_refresh_followup(self, mock_pool):
        """Completing old active work should queue any sync-scheduled fresh version."""
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")
        worker = make_worker(mock_pool)

        completed = await worker._mark_complete(101)

        assert completed is True
        sql = mock_pool.execute.await_args.args[0]
        assert "pending_refresh_metadata" in sql
        assert "THEN 'queued'" in sql
        assert "worker_id = CASE" in sql
        assert "THEN NULL" in sql


class TestLeaseRefresh:
    @pytest.mark.asyncio
    async def test_set_stage_guards_worker_and_active_status(self, mock_pool):
        mock_pool.execute = AsyncMock(return_value="UPDATE 1")
        worker = make_worker(mock_pool, lease_seconds=42)

        await worker._set_stage(101, "summarizing", status="summarizing")

        sql = mock_pool.execute.await_args.args[0]
        assert "worker_id = $5" in sql
        assert "status IN ('extracting', 'summarizing', 'indexing', 'processing')" in sql
        assert mock_pool.execute.await_args.args[4] == "42"

    @pytest.mark.asyncio
    async def test_set_stage_raises_when_lease_lost(self, mock_pool):
        mock_pool.execute = AsyncMock(return_value="UPDATE 0")
        worker = make_worker(mock_pool)

        with pytest.raises(JobLeaseLostError):
            await worker._set_stage(101, "summarizing", status="summarizing")


class TestMarkFailed:
    """Tests for _mark_failed with retry logic."""

    @pytest.mark.asyncio
    async def test_mark_failed_with_retry(self, mock_pool):
        """Test mark_failed requeues when retry_count < max_retries."""
        mock_pool.fetchrow = AsyncMock(return_value={"retry_count": 1})
        mock_pool.execute = AsyncMock()

        worker = make_worker(mock_pool, max_retries=3)
        await worker._mark_failed(101, "Connection timeout")

        # Should requeue (retry_count 1 + 1 = 2 < 3)
        mock_pool.execute.assert_awaited_once()
        sql = mock_pool.execute.call_args[0][0]
        assert "queued" in sql
        assert mock_pool.execute.call_args[0][1] == "Connection timeout"
        assert mock_pool.execute.call_args[0][2] == 2  # new retry_count

    @pytest.mark.asyncio
    async def test_mark_failed_no_retry_at_max(self, mock_pool):
        """Test mark_failed stays failed when retry_count >= max_retries."""
        mock_pool.fetchrow = AsyncMock(return_value={"retry_count": 2})
        mock_pool.execute = AsyncMock()

        worker = make_worker(mock_pool, max_retries=3)
        await worker._mark_failed(101, "Permanent error")

        # Should stay failed (retry_count 2 + 1 = 3, not < 3)
        mock_pool.execute.assert_awaited_once()
        sql = mock_pool.execute.call_args[0][0]
        assert "failed" in sql
        assert "completed_at" in sql

    @pytest.mark.asyncio
    async def test_mark_failed_nonexistent_job(self, mock_pool):
        """Test mark_failed handles missing job gracefully."""
        mock_pool.fetchrow = AsyncMock(return_value=None)
        mock_pool.execute = AsyncMock()

        worker = make_worker(mock_pool)
        await worker._mark_failed(999, "Error")

        mock_pool.execute.assert_not_awaited()


class TestHeartbeat:
    """Tests for heartbeat writing to Redis."""

    @pytest.mark.asyncio
    async def test_heartbeat_writes_to_redis(self, mock_pool, mock_redis):
        """Test heartbeat writes worker status to Redis."""
        worker = make_worker(mock_pool, mock_redis=mock_redis)
        worker._running = True
        worker._current_job_id = 42

        # Run heartbeat briefly then stop
        async def stop_after():
            await asyncio.sleep(0.05)
            worker._running = False

        await asyncio.gather(
            worker._heartbeat(),
            stop_after(),
        )

        # Redis set should have been called at least once
        assert mock_redis.set.await_count >= 1
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        assert key == "worker:heartbeat:test-worker-1"
        value = json.loads(call_args[0][1])
        assert value["worker_id"] == "test-worker-1"
        assert value["status"] == "active"
        assert value["metadata"]["current_job"] == 42
        assert value["metadata"]["current_job_id"] == 42
        assert value["metadata"]["active_job_ids"] == []
        assert value["metadata"]["lease_seconds"] == 300

    @pytest.mark.asyncio
    async def test_heartbeat_metadata_lists_active_jobs(self, mock_pool, mock_redis):
        """Heartbeat metadata mirrors top-level job fields for Admin views."""
        worker = make_worker(mock_pool, mock_redis=mock_redis)
        worker._running = True
        worker._current_job_id = 42
        worker._active_job_ids = {42, 43}

        async def stop_after():
            await asyncio.sleep(0.05)
            worker._running = False

        await asyncio.gather(worker._heartbeat(), stop_after())

        value = json.loads(mock_redis.set.call_args[0][1])
        assert value["current_job_id"] == 42
        assert value["active_job_ids"] == [42, 43]
        assert value["metadata"]["current_job"] == 42
        assert value["metadata"]["active_job_ids"] == [42, 43]
        assert value["metadata"]["active_jobs"] == 2
        assert value["metadata"]["lease_expires_at"] is not None

    @pytest.mark.asyncio
    async def test_heartbeat_no_redis(self, mock_pool):
        """Test heartbeat is a no-op without Redis."""
        worker = make_worker(mock_pool)
        worker._running = True

        async def stop_after():
            await asyncio.sleep(0.05)

        worker._running = False

        # Should not raise
        await asyncio.gather(
            worker._heartbeat(),
            stop_after(),
        )


class TestCancellation:
    """Tests for releasing claimed jobs on worker cancellation."""

    @pytest.mark.asyncio
    async def test_cancelled_claimed_job_is_requeued(self, mock_pool, sample_job):
        mock_pool.execute = AsyncMock()
        worker = make_worker(mock_pool)
        worker._process_job = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await worker._run_claimed_job(sample_job)

        mock_pool.execute.assert_awaited_once()
        sql = mock_pool.execute.call_args[0][0]
        assert "status = 'queued'" in sql
        assert "worker_id = NULL" in sql
        assert mock_pool.execute.call_args[0][1] == 101
        assert mock_pool.execute.call_args[0][2] == "test-worker-1"

    @pytest.mark.asyncio
    async def test_heartbeat_redis_error_handled(self, mock_pool, mock_redis):
        """Test heartbeat handles Redis errors gracefully."""
        mock_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))
        worker = make_worker(mock_pool, mock_redis=mock_redis)
        worker._running = True

        async def stop_after():
            await asyncio.sleep(0.05)
            worker._running = False

        # Should not raise
        await asyncio.gather(
            worker._heartbeat(),
            stop_after(),
        )


class TestStartStop:
    """Tests for worker lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_tasks(self, mock_pool):
        """Test that start creates claim and heartbeat tasks."""
        worker = make_worker(mock_pool)

        # Prevent actual loop execution by immediately stopping
        async def auto_stop():
            await asyncio.sleep(0.02)
            await worker.stop()

        await worker.start()
        assert worker.running is True
        assert worker._claim_task is not None
        assert worker._heartbeat_task is not None

        # Clean up
        await worker.stop()
        assert worker.running is False

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self, mock_pool):
        """Test that stop cancels running tasks."""
        worker = make_worker(mock_pool)
        mock_pool.fetchrow = AsyncMock(return_value=None)  # No jobs to claim

        await worker.start()
        assert worker._claim_task is not None

        await worker.stop()
        assert worker.running is False
        assert worker._claim_task is None
        assert worker._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self, mock_pool):
        """Test that calling start twice does not create duplicate tasks."""
        worker = make_worker(mock_pool)
        mock_pool.fetchrow = AsyncMock(return_value=None)

        await worker.start()
        task1 = worker._claim_task

        await worker.start()  # Second start
        task2 = worker._claim_task

        assert task1 is task2  # Same task, not replaced
        await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, mock_pool):
        """Test that stop is safe when worker is not running."""
        worker = make_worker(mock_pool)
        await worker.stop()  # Should not raise


class TestClaimLoop:
    """Tests for the _claim_loop integration."""

    @pytest.mark.asyncio
    async def test_claim_loop_processes_job(
        self, mock_pool, mock_registry, mock_summarizer, mock_indexer, sample_job
    ):
        """Test that claim loop processes a job and marks complete."""
        call_count = 0

        async def claim_then_none(*args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return sample_job
            return None

        mock_pool.fetchrow = AsyncMock(side_effect=claim_then_none)
        mock_pool.execute = AsyncMock()

        worker = make_worker(
            mock_pool,
            mock_registry=mock_registry,
            mock_summarizer=mock_summarizer,
            mock_indexer=mock_indexer,
        )

        # Run for a short time
        worker._running = True

        async def stop_after():
            await asyncio.sleep(0.1)
            worker._running = False

        await asyncio.gather(
            worker._claim_loop(),
            stop_after(),
        )

        # Job should have been processed
        mock_indexer.index_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claim_loop_handles_process_error(self, mock_pool, sample_job):
        """Test claim loop marks job failed on processing error."""
        call_count = 0

        async def claim_then_none(*args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return sample_job
            # For mark_failed fetchrow (retry_count lookup)
            if call_count == 2:
                return {"retry_count": 0}
            return None

        mock_pool.fetchrow = AsyncMock(side_effect=claim_then_none)
        mock_pool.execute = AsyncMock()

        # Worker with no registry/summarizer/indexer will fail during _extract
        # because no extractor is found, but that just returns empty content.
        # Force an error in the indexer instead.
        bad_indexer = AsyncMock(spec=DocumentIndexer)
        bad_indexer.index_document = AsyncMock(side_effect=RuntimeError("Boom"))
        bad_indexer._embedding_engine = None

        worker = make_worker(mock_pool, mock_indexer=bad_indexer)
        worker._running = True

        async def stop_after():
            await asyncio.sleep(0.1)
            worker._running = False

        await asyncio.gather(
            worker._claim_loop(),
            stop_after(),
        )

        # mark_failed should have been called (execute for requeue)
        assert mock_pool.execute.await_count >= 1
