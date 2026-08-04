"""Download management API endpoints.

Supports downloading individual files or entire folders from cloud
storage to the local /downloads directory.
"""

import asyncio
import os
import time
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app import database as db
from app.config import DOWNLOAD_DIR
from app.utils.helpers import get_provider

router = APIRouter(prefix="/api/downloads", tags=["downloads"])
logger = logging.getLogger(__name__)

# In-memory progress tracking: dl_id -> {transferred, total, speed, current_file, ...}
_progress: dict[int, dict] = {}
_running: set[int] = set()


class CreateDownloadRequest(BaseModel):
    connection_id: int
    remote_path: str
    is_dir: bool = False
    file_name: str = ""
    file_size: int = 0


@router.get("")
async def list_downloads():
    """List all download tasks."""
    downloads = await db.db_list_downloads()
    # Merge in-memory progress for active downloads
    for dl in downloads:
        dl_id = dl["id"]
        if dl_id in _progress:
            p = _progress[dl_id]
            dl["downloaded_bytes"] = p["transferred"]
            dl["file_size"] = p.get("total", dl["file_size"])
            dl["current_file"] = p.get("current_file", "")
            dl["speed"] = p.get("speed", 0)
            dl["processed_files"] = p.get("processed_files", 0)
            dl["total_files"] = p.get("total_files", 1)
        else:
            dl["current_file"] = ""
            dl["speed"] = 0
    return {"downloads": downloads}


@router.post("")
async def create_download(req: CreateDownloadRequest):
    """Create a new download task."""
    conn = await db.db_get_connection(req.connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    file_name = req.file_name or os.path.basename(req.remote_path.rstrip("/"))
    local_path = str(DOWNLOAD_DIR / file_name)

    dl_id = await db.db_create_download(
        connection_id=req.connection_id,
        connection_name=conn["name"],
        remote_path=req.remote_path,
        local_path=local_path,
        file_name=file_name,
        file_size=req.file_size,
        is_dir=1 if req.is_dir else 0,
        status="pending",
    )

    # Start download in background
    asyncio.create_task(_execute_download(dl_id, conn, req.remote_path, req.is_dir, local_path))
    return {"id": dl_id, "status": "pending"}


@router.delete("/{dl_id}")
async def delete_download(dl_id: int):
    """Delete a download task record (does not delete the downloaded file)."""
    await db.db_delete_download(dl_id)
    _progress.pop(dl_id, None)
    _running.discard(dl_id)
    return {"deleted": True}


@router.post("/{dl_id}/cancel")
async def cancel_download(dl_id: int):
    """Cancel a running download."""
    _running.discard(dl_id)
    await db.db_update_download(dl_id, status="cancelled", completed_at=time.time())
    _progress.pop(dl_id, None)
    return {"status": "cancelled"}


async def _execute_download(dl_id: int, conn: dict, remote_path: str,
                            is_dir: bool, local_path: str):
    """Execute download in background."""
    _running.add(dl_id)
    await db.db_update_download(dl_id, status="downloading")

    provider = get_provider(conn["type"], conn["credentials"])
    try:
        if is_dir:
            await _download_folder(dl_id, provider, remote_path, local_path)
        else:
            await _download_single(dl_id, provider, remote_path, local_path)

        if dl_id in _running:
            await db.db_update_download(
                dl_id, status="completed", completed_at=time.time()
            )
            logger.info(f"Download {dl_id} completed")
    except Exception as e:
        logger.error(f"Download {dl_id} failed: {e}")
        if dl_id in _running:
            await db.db_update_download(
                dl_id, status="failed", error_message=str(e), completed_at=time.time()
            )
    finally:
        _running.discard(dl_id)
        _progress.pop(dl_id, None)
        await provider.close()


async def _download_single(dl_id: int, provider, remote_path: str, local_path: str):
    """Download a single file."""
    _progress[dl_id] = {
        "transferred": 0,
        "total": 0,
        "speed": 0,
        "current_file": os.path.basename(remote_path),
        "processed_files": 0,
        "total_files": 1,
    }

    # Get file info for size
    info = await provider.get_file_info(remote_path)
    total_size = info.size if info else 0
    _progress[dl_id]["total"] = total_size

    # Update DB with file size
    await db.db_update_download(dl_id, file_size=total_size, total_files=1)

    last_time = time.time()
    last_bytes = 0

    async def progress_cb(transferred, file_size):
        now = time.time()
        dt = now - last_time
        speed = 0
        if dt > 0.3:
            speed = (transferred - last_bytes) / dt
            _progress[dl_id]["transferred"] = transferred
            _progress[dl_id]["speed"] = speed
            _progress[dl_id]["total"] = file_size
            # Persist to DB periodically
            await db.db_update_download(
                dl_id, downloaded_bytes=transferred, file_size=file_size
            )

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    await provider.download_file(remote_path, local_path, progress_cb)

    _progress[dl_id]["transferred"] = total_size
    _progress[dl_id]["processed_files"] = 1
    _progress[dl_id]["speed"] = 0
    await db.db_update_download(
        dl_id,
        downloaded_bytes=total_size,
        processed_files=1,
    )


async def _download_folder(dl_id: int, provider, remote_path: str, local_path: str):
    """Recursively download a folder."""
    _progress[dl_id] = {
        "transferred": 0,
        "total": 0,
        "speed": 0,
        "current_file": "",
        "processed_files": 0,
        "total_files": 0,
    }

    # List all files recursively
    all_files = await provider.list_all_files(remote_path)
    total_files = len(all_files)
    total_size = sum(f.size for f in all_files)

    _progress[dl_id]["total_files"] = total_files
    _progress[dl_id]["total"] = total_size

    await db.db_update_download(
        dl_id, file_size=total_size, total_files=total_files
    )

    processed = 0
    transferred = 0
    last_time = time.time()
    last_bytes = 0

    # remote_path is the folder, e.g. /photos/2024
    # For each file, compute local path relative to the folder
    folder_name = os.path.basename(remote_path.rstrip("/"))

    for f in all_files:
        if dl_id not in _running:
            break

        _progress[dl_id]["current_file"] = f.name

        # Compute local path: /downloads/folder_name/relative_path
        rel = f.path[len(remote_path):].lstrip("/")
        f_local = os.path.join(local_path, rel)
        os.makedirs(os.path.dirname(f_local), exist_ok=True)

        async def progress_cb(transferred_bytes, file_size):
            nonlocal transferred
            now = time.time()
            dt = now - last_time
            if dt > 0.3:
                speed = (transferred - last_bytes) / dt
                _progress[dl_id]["transferred"] = transferred + transferred_bytes
                _progress[dl_id]["speed"] = speed

        await provider.download_file(f.path, f_local, progress_cb)
        transferred += f.size
        processed += 1
        _progress[dl_id]["processed_files"] = processed
        _progress[dl_id]["transferred"] = transferred

        now = time.time()
        dt = now - last_time
        if dt > 0.5:
            speed = (transferred - last_bytes) / dt
            _progress[dl_id]["speed"] = speed
            last_time = now
            last_bytes = transferred
            await db.db_update_download(
                dl_id,
                downloaded_bytes=transferred,
                processed_files=processed,
            )

    _progress[dl_id]["speed"] = 0
    await db.db_update_download(
        dl_id,
        downloaded_bytes=transferred,
        processed_files=processed,
    )
