"""Sync records API — a queryable history of every file that moved.

Unlike /api/logs (prose lines meant for reading), these rows are structured, so
the UI can filter by direction/task, search by path, and page through them.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from app import database as db
from app.api.tasks import _rel_from_record
from app.services.sync_engine import SyncEngine

router = APIRouter(prefix="/api/records", tags=["records"])

logger = logging.getLogger(__name__)

# A page bigger than this would be pointless on a phone and would let a typo
# in the query string pull the whole table into memory.
MAX_LIMIT = 500


@router.get("")
async def list_records(limit: int = 50, offset: int = 0,
                       action: Optional[str] = None,
                       task_id: Optional[int] = None,
                       status: Optional[str] = None,
                       q: Optional[str] = None,
                       stats: bool = True):
    """List transfer records, newest first.

    ``action`` accepts upload / download / delete (the latter groups both
    delete directions). ``q`` matches the file name or either path.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    q = (q or "").strip() or None
    if action not in ("upload", "download", "delete", "delete_local",
                      "delete_remote"):
        action = None

    records = await db.db_list_records(
        limit=limit, offset=offset, action=action,
        task_id=task_id, status=status, query=q)
    total = await db.db_count_records(
        action=action, task_id=task_id, status=status, query=q)

    payload = {
        "records": records,
        "total": total,
        "has_more": offset + len(records) < total,
        "limit": limit,
        "offset": offset,
    }
    if stats:
        # Always the unfiltered totals: the header shows "共 N 条" plus a per
        # direction breakdown, which must not shrink when a filter is applied.
        payload["stats"] = await db.db_record_stats()
    return payload


@router.delete("")
async def clear_records():
    """Delete the whole transfer history."""
    await db.db_clear_records()
    logger.info("Sync records cleared by user")
    return {"status": "cleared"}


@router.delete("/{record_id}")
async def delete_record(record_id: int):
    """Remove a single row from the transfer history.

    History only — the file itself is untouched on both sides.
    """
    if not await db.db_delete_record(record_id):
        raise HTTPException(404, "Record not found")
    return {"status": "deleted", "record_id": record_id}


@router.post("/retry-all")
async def retry_all():
    """Re-queue every failed and skipped record in one go.

    Same per-file path as the single-row retry: clear the ignore flag and the
    backoff entry, then guarantee one sync per affected task — running tasks
    get a follow-up cycle via ``request_sync`` (a bare spawn would be a no-op
    and the files would wait for a disk event that may never come). Rows are
    left in place until the file actually goes through, at which point the
    success write removes them.
    """
    records = await db.db_list_retryable_records()
    if not records:
        return {"queued": 0, "tasks": [], "skipped_records": 0}

    touched: dict[int, dict] = {}
    resolved = 0
    for rec in records:
        task = touched.get(rec["task_id"])
        if task is None:
            task = await db.db_get_task(rec["task_id"])
            if not task:
                continue
            touched[rec["task_id"]] = task
        rel = _rel_from_record(task, rec)
        if not rel:
            # The task's roots changed and the path can no longer be worked
            # out; leave the row alone rather than guessing.
            continue
        await db.db_remove_ignored(rec["task_id"], rel)
        await db.db_clear_failure(rec["task_id"], rel)
        resolved += 1

    started = []
    follow_up = []
    for task_id, task in touched.items():
        if SyncEngine.request_sync(task_id) == "started":
            started.append(task.get("name") or f"#{task_id}")
        else:
            follow_up.append(task.get("name") or f"#{task_id}")

    logger.info("Retry-all: %d record(s) re-queued across %d task(s)",
                resolved, len(touched))
    return {"queued": resolved, "records": len(records),
            "tasks": list(touched.keys()),
            "started": started, "already_running": follow_up}
