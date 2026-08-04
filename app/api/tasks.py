"""Sync task management API endpoints."""

import os
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app import database as db
from app.services.sync_engine import SyncEngine
from app.services.scheduler import scheduler
from app.config import LOCAL_SYNC_ROOT

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _transfers_with_speed(progress) -> list:
    """Build active_transfers list, computing per-file speed on the server.

    The frontend can compute speed from transferred-byte delta between polls,
    but that's unreliable when many transfers upload at similar rates (the
    stale `transferred` value for in-flight files can stay constant while
    server-side the bytes are flowing through the socket). Computing speed
    server-side gives accurate per-file rates.

    Returns a list of dicts, each augmented with a `speed` field in bytes/s.
    """
    now = time.time()
    samples = SyncEngine._transfer_samples.setdefault(progress.task_id, {})
    out = []
    for t in progress.active_transfers.values():
        prev = samples.get(t.transfer_id)
        speed = 0.0
        if prev is not None:
            prev_bytes, prev_time = prev
            dt = now - prev_time
            if dt > 0:
                delta = t.transferred - prev_bytes
                if delta > 0:
                    speed = delta / dt
        samples[t.transfer_id] = (t.transferred, now)
        out.append({
            "transfer_id": t.transfer_id,
            "file_path": t.file_path,
            "action": t.action,
            "file_size": t.file_size,
            "transferred": t.transferred,
            "speed": speed,
        })
    # Drop samples for transfers that ended so the dict doesn't grow
    # unbounded across sync runs.
    active_ids = set(progress.active_transfers.keys())
    stale = [tid for tid in samples if tid not in active_ids]
    for tid in stale:
        samples.pop(tid, None)
    return out


class CreateTaskRequest(BaseModel):
    name: str
    connection_id: int
    local_path: str
    remote_path: str = "/"
    sync_mode: str = "bidirectional"  # bidirectional, upload_only, download_only
    schedule_type: str = "manual"  # realtime, scheduled, manual
    schedule_interval: int = 0  # seconds, for scheduled type
    filter_include: str = ""
    filter_exclude: str = ""
    max_file_size: int = 0
    delete_after_sync: int = 0


class UpdateTaskRequest(BaseModel):
    name: Optional[str] = None
    local_path: Optional[str] = None
    remote_path: Optional[str] = None
    sync_mode: Optional[str] = None
    schedule_type: Optional[str] = None
    schedule_interval: Optional[int] = None
    filter_include: Optional[str] = None
    filter_exclude: Optional[str] = None
    max_file_size: Optional[int] = None
    delete_after_sync: Optional[int] = None
    status: Optional[str] = None


@router.get("")
async def list_tasks():
    tasks = await db.db_list_tasks()
    result = []
    for task in tasks:
        progress = SyncEngine.get_progress(task["id"])
        task["is_running"] = SyncEngine.is_running(task["id"])
        if progress:
            task["progress"] = {
                "total_files": progress.total_files,
                "processed_files": progress.processed_files,
                "failed_files": progress.failed_files,
                "skipped_files": progress.skipped_files,
            "transferred_bytes": progress.transferred_bytes,
            "speed": progress.speed,
            "active_transfers": _transfers_with_speed(progress),
        }
    result.append(task)
    return {"tasks": result}


@router.get("/{task_id}")
async def get_task(task_id: int):
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task["is_running"] = SyncEngine.is_running(task_id)
    progress = SyncEngine.get_progress(task_id)
    if progress:
        task["progress"] = {
            "total_files": progress.total_files,
            "processed_files": progress.processed_files,
            "failed_files": progress.failed_files,
            "skipped_files": progress.skipped_files,
            "transferred_bytes": progress.transferred_bytes,
            "speed": progress.speed,
            "active_transfers": _transfers_with_speed(progress),
        }
    return task


@router.post("")
async def create_task(req: CreateTaskRequest):
    # Verify connection exists
    conn = await db.db_get_connection(req.connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    task_id = await db.db_create_task(
        name=req.name,
        connection_id=req.connection_id,
        local_path=req.local_path,
        remote_path=req.remote_path,
        sync_mode=req.sync_mode,
        schedule_type=req.schedule_type,
        schedule_interval=req.schedule_interval,
        filter_include=req.filter_include,
        filter_exclude=req.filter_exclude,
        max_file_size=req.max_file_size,
        delete_after_sync=req.delete_after_sync,
    )

    # Schedule the task
    task = await db.db_get_task(task_id)
    if task:
        await scheduler.schedule_task(task)

    return {"id": task_id, "status": "created"}


@router.put("/{task_id}")
async def update_task(task_id: int, req: UpdateTaskRequest):
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if updates:
        await db.db_update_task(task_id, **updates)

    # Reschedule if needed
    if "schedule_type" in updates or "schedule_interval" in updates or "local_path" in updates:
        updated_task = await db.db_get_task(task_id)
        if updated_task:
            await scheduler.schedule_task(updated_task)

    return {"status": "updated"}


@router.delete("/{task_id}")
async def delete_task(task_id: int):
    await scheduler.unschedule_task(task_id)
    await db.db_delete_task(task_id)
    return {"deleted": True}


@router.post("/{task_id}/sync")
async def sync_now(task_id: int):
    """Trigger an immediate sync for a task."""
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if SyncEngine.is_running(task_id):
        raise HTTPException(409, "Task is already running")

    # Run sync in background
    import asyncio
    asyncio.create_task(SyncEngine.sync_task(task_id))
    return {"status": "syncing", "task_id": task_id}


@router.post("/{task_id}/pause")
async def pause_task(task_id: int):
    # If the task is currently running, request it to stop
    if SyncEngine.is_running(task_id):
        SyncEngine.request_stop(task_id)
    await scheduler.unschedule_task(task_id)
    await db.db_update_task(task_id, status="paused")
    return {"status": "paused"}


@router.post("/{task_id}/resume")
async def resume_task(task_id: int):
    task = await db.db_get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await db.db_update_task(task_id, status="idle")
    await scheduler.schedule_task(task)
    return {"status": "resumed"}


class CancelTransferRequest(BaseModel):
    transfer_id: int


@router.post("/{task_id}/cancel-transfer")
async def cancel_transfer(task_id: int, req: CancelTransferRequest):
    """Cancel a specific file transfer within a running sync task."""
    if not SyncEngine.is_running(task_id):
        raise HTTPException(409, "Task is not running")
    SyncEngine.cancel_transfer(task_id, req.transfer_id)
    return {"status": "cancelled", "transfer_id": req.transfer_id}


@router.get("/{task_id}/logs")
async def get_logs(task_id: int, limit: int = 200, offset: int = 0):
    logs = await db.db_list_logs(task_id, limit, offset)
    return {"logs": logs}


@router.get("/{task_id}/history")
async def get_history(task_id: int, limit: int = 20):
    history = await db.db_list_history(task_id, limit)
    return {"history": history}


@router.get("/{task_id}/progress")
async def get_progress(task_id: int):
    progress = SyncEngine.get_progress(task_id)
    if not progress:
        return {"is_running": False}
    return {
        "is_running": progress.is_running,
        "total_files": progress.total_files,
        "processed_files": progress.processed_files,
        "failed_files": progress.failed_files,
        "skipped_files": progress.skipped_files,
        "transferred_bytes": progress.transferred_bytes,
        "speed": progress.speed,
        "active_transfers": [
            {
                "transfer_id": t.transfer_id,
                "file_path": t.file_path,
                "action": t.action,
                "file_size": t.file_size,
                "transferred": t.transferred,
            }
            for t in progress.active_transfers.values()
        ],
        "started_at": progress.started_at,
        "elapsed": time.time() - progress.started_at if progress.started_at else 0,
    }
