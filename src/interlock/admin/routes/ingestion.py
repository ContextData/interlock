"""Admin API routes for ingestion job and worker management."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from interlock.admin.audit import (
    audit_admin_action,
    mutation_audit_detail,
    summarize_admin_resource,
)
from interlock.connections.connectors import get_adapter

router = APIRouter(prefix="/api/ingestion", tags=["ingestion"])

STALE_HEARTBEAT_SECONDS = 90


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    source_id: str
    file_path: str
    priority_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobPriorityUpdate(BaseModel):
    priority_score: float


class JobResponse(BaseModel):
    id: int | str
    source_id: str
    file_path: str
    status: str
    priority_score: float = 0.0
    worker_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    lease_expires_at: datetime | None = None
    stage: str | None = None
    progress_current: int = 0
    progress_total: int | None = None
    ocr_pages: int = 0
    ocr_cost: float = 0.0
    llm_cost: float = 0.0
    last_error_at: datetime | None = None
    error_message: str | None = None
    retry_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class WorkerStatus(BaseModel):
    worker_id: str
    last_heartbeat: float
    stale: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerHealthResponse(BaseModel):
    worker_id: str
    alive: bool
    last_heartbeat: float | None = None
    stale_jobs_failed: int = 0


class SourceSyncResponse(BaseModel):
    source_id: str
    connector_key: str
    assets_seen: int
    jobs_created: int
    jobs_requeued: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    # With the custom JSON codec on the pool, JSONB columns come back as dicts.
    # Guard against str for environments without the codec (e.g. test mocks).
    val = d.get("metadata")
    if isinstance(val, str):
        d["metadata"] = json.loads(val)
    elif val is None:
        d["metadata"] = {}
    return d


def _job_summary(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return summarize_admin_resource(
        _row_to_dict(row),
        fields=(
            "id",
            "source_id",
            "file_path",
            "status",
            "stage",
            "priority_score",
            "worker_id",
            "retry_count",
            "metadata",
        ),
    )


def _normalise_worker_metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    active_job_ids = metadata.get("active_job_ids") or data.get("active_job_ids") or []
    current_job = (
        metadata.get("current_job")
        if metadata.get("current_job") is not None
        else data.get("current_job_id")
    )
    normalised = dict(metadata)
    normalised.setdefault("current_job", current_job)
    normalised.setdefault("current_job_id", data.get("current_job_id"))
    normalised.setdefault("active_job_ids", active_job_ids)
    normalised.setdefault("active_jobs", len(active_job_ids))
    if data.get("status") is not None:
        normalised.setdefault("status", data.get("status"))
    return normalised


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(
    request: Request,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List ingestion jobs with optional status filter."""
    pool = request.app.state.pg_pool
    if status:
        rows = await pool.fetch(
            "SELECT * FROM ingestion_jobs WHERE status = $1 ORDER BY created_at DESC",
            status,
        )
    else:
        rows = await pool.fetch("SELECT * FROM ingestion_jobs ORDER BY created_at DESC")
    return [_row_to_dict(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, request: Request) -> dict[str, Any]:
    """Get details for a single ingestion job."""
    pool = request.app.state.pg_pool
    row = await pool.fetchrow("SELECT * FROM ingestion_jobs WHERE id = $1", job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _row_to_dict(row)


@router.post("/jobs", response_model=JobResponse, status_code=201)
async def create_job(body: JobCreate, request: Request) -> dict[str, Any]:
    """Create a new ingestion job."""
    pool = request.app.state.pg_pool
    row = await pool.fetchrow(
        """
        INSERT INTO ingestion_jobs (source_id, file_path, status, priority_score, metadata)
        VALUES ($1, $2, 'queued', $3, $4)
        RETURNING *
        """,
        body.source_id,
        body.file_path,
        body.priority_score,
        body.metadata,
    )
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="ingestion.job.create",
        resource="ingestion_job",
        resource_id=str(response["id"]),
        success=True,
        detail=mutation_audit_detail(
            after=_job_summary(response),
            changed_fields=("source_id", "file_path", "priority_score", "metadata", "status"),
            status_code=201,
        ),
    )
    return response


@router.post("/sources/{source_id}/sync", response_model=SourceSyncResponse)
async def sync_source_assets(source_id: str, request: Request) -> dict[str, Any]:
    """List connector assets and enqueue ingestion jobs.

    This endpoint is intentionally connector-generic: adapters own how assets
    are listed, while the Admin API owns idempotently turning those assets into
    queued worker jobs.
    """
    pool = request.app.state.pg_pool
    row = await pool.fetchrow(
        """
        SELECT source_id, source_type, connection_config, metadata
        FROM data_sources
        WHERE source_id = $1 AND enabled = TRUE
        """,
        source_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")

    source_metadata = row["metadata"] or {}
    if isinstance(source_metadata, str):
        source_metadata = json.loads(source_metadata)
    connection_config = row["connection_config"] or {}
    if isinstance(connection_config, str):
        connection_config = json.loads(connection_config)

    connector_key = str(source_metadata.get("connector_key") or row["source_type"])
    adapter = get_adapter(str(row["source_type"]), {"connector_key": connector_key})
    try:
        assets = await adapter.list_assets(connection_config)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Connector sync failed: {exc}") from exc

    created = 0
    requeued = 0
    for asset in assets:
        asset_path = str(asset.get("asset_path") or "")
        if not asset_path:
            continue
        metadata = {
            "connector_key": connector_key,
            "asset_type": asset.get("asset_type", "file"),
            "title": asset.get("title"),
            "etag": asset.get("etag"),
            "last_modified": str(asset.get("last_modified") or asset.get("updated_at") or ""),
            "seed": source_metadata.get("seed"),
            **(asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}),
        }
        result = await pool.fetchrow(
            """
            WITH existing AS (
                SELECT status, metadata
                FROM ingestion_jobs
                WHERE source_id = $1 AND file_path = $2
                FOR UPDATE
            ),
            incoming AS (
                SELECT $3::jsonb AS metadata
            ),
            write AS (
            INSERT INTO ingestion_jobs
                (source_id, file_path, status, stage, priority_score,
                 progress_current, progress_total, metadata)
            VALUES ($1, $2, 'queued', 'queued', 5.0, 0, 1, $3::jsonb)
            ON CONFLICT (source_id, file_path) DO UPDATE
            SET status = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN 'queued'
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN 'queued'
                    ELSE ingestion_jobs.status
                END,
                stage = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN 'queued'
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN 'queued'
                    ELSE ingestion_jobs.stage
                END,
                error_message = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN NULL
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN NULL
                    ELSE ingestion_jobs.error_message
                END,
                retry_count = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN 0
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN 0
                    ELSE ingestion_jobs.retry_count
                END,
                worker_id = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN NULL
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN NULL
                    ELSE ingestion_jobs.worker_id
                END,
                started_at = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN NULL
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN NULL
                    ELSE ingestion_jobs.started_at
                END,
                completed_at = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN NULL
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN NULL
                    ELSE ingestion_jobs.completed_at
                END,
                lease_expires_at = CASE
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN NULL
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN NULL
                    ELSE ingestion_jobs.lease_expires_at
                END,
                metadata = CASE
                    WHEN ingestion_jobs.status IN ('extracting', 'summarizing', 'indexing', 'processing')
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         )
                    THEN COALESCE(ingestion_jobs.metadata, '{}'::jsonb)
                         || jsonb_build_object(
                             'pending_refresh_metadata', EXCLUDED.metadata,
                             'pending_refresh_requested_at', NOW(),
                             'pending_refresh_reason', 'asset_changed_while_active'
                         )
                    WHEN ingestion_jobs.status IN ('failed', 'cancelled') THEN EXCLUDED.metadata
                    WHEN ingestion_jobs.status = 'completed'
                         AND (
                             COALESCE(ingestion_jobs.metadata->>'etag', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'etag', '')
                             OR COALESCE(ingestion_jobs.metadata->>'last_modified', '') IS DISTINCT FROM
                                 COALESCE(EXCLUDED.metadata->>'last_modified', '')
                         ) THEN EXCLUDED.metadata
                    ELSE ingestion_jobs.metadata
                END,
                updated_at = NOW()
            RETURNING (xmax = 0) AS inserted,
                      status,
                      EXISTS (
                          SELECT 1
                          FROM existing e
                          WHERE e.status IN ('failed', 'cancelled')
                             OR (
                                 e.status = 'completed'
                                 AND (
                                     COALESCE(e.metadata->>'etag', '') IS DISTINCT FROM
                                         COALESCE((SELECT metadata FROM incoming)->>'etag', '')
                                     OR COALESCE(e.metadata->>'last_modified', '') IS DISTINCT FROM
                                         COALESCE((SELECT metadata FROM incoming)->>'last_modified', '')
                                 )
                             )
                             OR (
                                 e.status IN ('extracting', 'summarizing', 'indexing', 'processing')
                                 AND (
                                     COALESCE(e.metadata->>'etag', '') IS DISTINCT FROM
                                         COALESCE((SELECT metadata FROM incoming)->>'etag', '')
                                     OR COALESCE(e.metadata->>'last_modified', '') IS DISTINCT FROM
                                         COALESCE((SELECT metadata FROM incoming)->>'last_modified', '')
                                 )
                             )
                      ) AS requeued
            )
            SELECT inserted, status, requeued FROM write
            """,
            source_id,
            asset_path,
            metadata,
        )
        if result and result["inserted"]:
            created += 1
        elif result and result["requeued"]:
            requeued += 1

    response = {
        "source_id": source_id,
        "connector_key": connector_key,
        "assets_seen": len(assets),
        "jobs_created": created,
        "jobs_requeued": requeued,
    }
    await audit_admin_action(
        request,
        action="ingestion.source.sync",
        resource="data_source",
        resource_id=source_id,
        success=True,
        detail=mutation_audit_detail(
            after=response,
            changed_fields=("ingestion_jobs",),
            status_code=200,
        ),
    )
    return response


@router.post("/jobs/{job_id}/retry", response_model=JobResponse)
async def retry_job(job_id: int, request: Request) -> dict[str, Any]:
    """Retry a failed ingestion job by resetting it to the queued state.

    P2-F: schema uses ``status='queued'`` and ``error_message`` (not
    ``status='pending'`` / ``error``). The previous implementation set
    fields that did not exist, so retries silently failed.
    """
    pool = request.app.state.pg_pool
    row = await pool.fetchrow(
        """
        UPDATE ingestion_jobs
        SET status = 'queued',
            error_message = NULL,
            worker_id = NULL,
            updated_at = NOW()
        WHERE id = $1 AND status = 'failed'
        RETURNING *
        """,
        job_id,
    )
    if row is None:
        existing = await pool.fetchrow(
            "SELECT id, status FROM ingestion_jobs WHERE id = $1", job_id
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(
            status_code=409,
            detail=f"Job status is '{existing['status']}', only failed jobs can be retried",
        )
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="ingestion.job.retry",
        resource="ingestion_job",
        resource_id=str(job_id),
        success=True,
        detail=mutation_audit_detail(
            after=_job_summary(response),
            changed_fields=("status", "error_message", "worker_id"),
            status_code=200,
        ),
    )
    return response


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(job_id: int, request: Request) -> dict[str, Any]:
    """Cancel a queued ingestion job."""
    pool = request.app.state.pg_pool
    row = await pool.fetchrow(
        """
        UPDATE ingestion_jobs
        SET status = 'cancelled', updated_at = NOW()
        WHERE id = $1 AND status = 'queued'
        RETURNING *
        """,
        job_id,
    )
    if row is None:
        existing = await pool.fetchrow(
            "SELECT id, status FROM ingestion_jobs WHERE id = $1", job_id
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(
            status_code=409,
            detail=f"Job status is '{existing['status']}', only queued jobs can be cancelled",
        )
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="ingestion.job.cancel",
        resource="ingestion_job",
        resource_id=str(job_id),
        success=True,
        detail=mutation_audit_detail(
            after=_job_summary(response),
            changed_fields=("status",),
            status_code=200,
        ),
    )
    return response


@router.put("/jobs/{job_id}/priority", response_model=JobResponse)
async def update_priority(job_id: int, body: JobPriorityUpdate, request: Request) -> dict[str, Any]:
    """Update priority score for an ingestion job."""
    pool = request.app.state.pg_pool
    row = await pool.fetchrow(
        """
        UPDATE ingestion_jobs
        SET priority_score = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        int(job_id),
        body.priority_score,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    response = _row_to_dict(row)
    await audit_admin_action(
        request,
        action="ingestion.job.priority.update",
        resource="ingestion_job",
        resource_id=str(job_id),
        success=True,
        detail=mutation_audit_detail(
            after=_job_summary(response),
            changed_fields=("priority_score",),
            status_code=200,
        ),
    )
    return response


# ---------------------------------------------------------------------------
# Worker endpoints
# ---------------------------------------------------------------------------


@router.get("/workers", response_model=list[WorkerStatus])
async def list_workers(request: Request) -> list[dict[str, Any]]:
    """List worker status from Redis heartbeat keys."""
    redis = request.app.state.redis
    now = time.time()
    workers: list[dict[str, Any]] = []

    # Scan for worker heartbeat keys
    cursor = b"0"
    while True:
        cursor, keys = await redis.scan(cursor, match="worker:heartbeat:*", count=100)
        for key in keys:
            worker_id = (
                key.decode().split(":", 2)[-1] if isinstance(key, bytes) else key.split(":", 2)[-1]
            )
            raw = await redis.get(key)
            if raw is None:
                continue
            data = json.loads(raw)
            last_hb = data.get("timestamp", 0)
            heartbeat_worker_id = str(data.get("worker_id") or worker_id)
            workers.append(
                {
                    "worker_id": heartbeat_worker_id,
                    "last_heartbeat": last_hb,
                    "stale": (now - last_hb) > STALE_HEARTBEAT_SECONDS,
                    "metadata": _normalise_worker_metadata(data),
                }
            )
        if cursor == b"0" or cursor == 0:
            break

    return workers


@router.post("/workers/{worker_id}/health", response_model=WorkerHealthResponse)
async def probe_worker_health(worker_id: str, request: Request) -> dict[str, Any]:
    """Probe worker health. If heartbeat is stale, fail its claimed jobs."""
    redis = request.app.state.redis
    pool = request.app.state.pg_pool
    now = time.time()

    key = f"worker:heartbeat:{worker_id}"
    raw = await redis.get(key)

    if raw is None:
        raise HTTPException(status_code=404, detail="Worker not found")

    data = json.loads(raw)
    last_hb = data.get("timestamp", 0)
    alive = (now - last_hb) <= STALE_HEARTBEAT_SECONDS

    stale_jobs_failed = 0
    if not alive:
        # Mark all jobs claimed by this worker as failed
        result = await pool.execute(
            """
            UPDATE ingestion_jobs
            SET status = 'failed',
                error_message = 'worker heartbeat stale',
                lease_expires_at = NULL,
                last_error_at = NOW(),
                updated_at = NOW()
            WHERE worker_id = $1
              AND status IN ('extracting', 'summarizing', 'indexing', 'processing')
            """,
            worker_id,
        )
        # Extract count from "UPDATE N"
        if result and result.startswith("UPDATE"):
            stale_jobs_failed = int(result.split()[-1])

    response = {
        "worker_id": worker_id,
        "alive": alive,
        "last_heartbeat": last_hb,
        "stale_jobs_failed": stale_jobs_failed,
    }
    await audit_admin_action(
        request,
        action="ingestion.worker.health_probe",
        resource="worker",
        resource_id=worker_id,
        success=True,
        detail=mutation_audit_detail(
            after=response,
            changed_fields=("stale_jobs_failed",) if stale_jobs_failed else (),
            status_code=200,
        ),
    )
    return response
