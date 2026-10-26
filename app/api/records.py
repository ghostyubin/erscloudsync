"""Sync records API — a queryable history of every file that moved.

Unlike /api/logs (prose lines meant for reading), these rows are structured, so
the UI can filter by direction/task, search by path, and page through them.
"""

import logging
from typing import Optional

from fastapi import APIRouter

from app import database as db

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
