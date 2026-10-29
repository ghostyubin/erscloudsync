"""System API endpoints - health, local directory browsing, stats."""

import os
import shutil
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import LOCAL_SYNC_ROOT, LOCAL_ROOTS, DOWNLOAD_DIR, DATA_DIR, SYNC_CONCURRENCY
from app import database as db
from app.services.sync_engine import (
    CONCURRENCY_MAX, CONCURRENCY_MIN, CONCURRENCY_SETTING_KEY, SyncEngine,
)
from app.services.traffic import traffic
from app.version import APP_VERSION

router = APIRouter(prefix="/api/system", tags=["system"])


class RuntimeSettings(BaseModel):
    """Globals that can be changed without editing the compose file."""
    sync_concurrency: Optional[int] = None


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/settings")
async def get_settings():
    """Runtime-tunable settings plus the bounds the UI should enforce."""
    return {
        "sync_concurrency": await SyncEngine.get_concurrency(),
        "defaults": {"sync_concurrency": SYNC_CONCURRENCY},
        "limits": {"sync_concurrency": {"min": CONCURRENCY_MIN,
                                        "max": CONCURRENCY_MAX}},
    }


@router.put("/settings")
async def update_settings(req: RuntimeSettings):
    """Persist runtime settings.

    The value is clamped rather than rejected: the UI already limits the input,
    and a stale browser tab should not make every sync fail with a 400.
    """
    result = {}
    if req.sync_concurrency is not None:
        value = max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, int(req.sync_concurrency)))
        await db.db_set_setting(CONCURRENCY_SETTING_KEY, str(value))
        result["sync_concurrency"] = value
    if not result:
        raise HTTPException(400, "No setting provided")
    return result


@router.get("/traffic")
async def traffic_stats():
    """Cumulative upload/download bytes plus the current throughput.

    Reads are cheap and intended to be polled about once a second: the live
    speed is sampled from the sync engine's in-flight counters on each call.
    """
    return await traffic.snapshot()


@router.post("/traffic/reset")
async def traffic_reset():
    """Zero the cumulative counters (the live speed keeps working)."""
    await traffic.reset()
    return await traffic.snapshot()


@router.get("/info")
async def system_info():
    """Get system information."""
    total, used, free = shutil.disk_usage(str(LOCAL_SYNC_ROOT))

    # Active counts - safely handle ImportError / startup order
    active_tasks = 0
    active_downloads = 0
    try:
        from app.database import db_list_tasks
        tasks = await db_list_tasks()
        active_tasks = sum(1 for t in tasks if t.get("status") == "running")
    except Exception:
        pass
    try:
        from app.database import db_list_downloads
        downloads = await db_list_downloads()
        active_downloads = sum(1 for d in downloads if d.get("status") in ("pending", "downloading"))
    except Exception:
        pass

    return {
        "version": APP_VERSION,
        "data_dir": str(DATA_DIR),
        "sync_dir": str(LOCAL_SYNC_ROOT),
        "download_dir": str(DOWNLOAD_DIR),
        "sync_root": str(LOCAL_SYNC_ROOT),
        "active_tasks": active_tasks,
        "active_downloads": active_downloads,
        "disk": {
            "total": total,
            "used": used,
            "free": free,
            "usage_percent": round(used / total * 100, 1) if total else 0,
        },
    }


@router.get("/local-dirs")
async def list_local_dirs(path: str = "", root: str = ""):
    """List local directories for task configuration.

    Args:
        path: relative path within the selected root
        root: which root to browse - "sync" (default) or "downloads"
    """
    # Select the base root
    if root == "downloads":
        base = DOWNLOAD_DIR
    else:
        base = LOCAL_SYNC_ROOT

    if path:
        # Ensure path is within the selected root
        target = os.path.join(str(base), path.lstrip("/"))
        target = os.path.normpath(target)
        if not target.startswith(str(base)):
            raise HTTPException(400, "Path outside root")
    else:
        target = str(base)

    if not os.path.exists(target):
        raise HTTPException(404, "Directory not found")

    entries = []
    try:
        for name in sorted(os.listdir(target)):
            full_path = os.path.join(target, name)
            is_dir = os.path.isdir(full_path)
            rel = os.path.relpath(full_path, str(base))
            entry = {
                "name": name,
                "path": "/" + rel.replace(os.sep, "/"),
                "full_path": full_path,
                "is_dir": is_dir,
            }
            if not is_dir:
                try:
                    stat = os.stat(full_path)
                    entry["size"] = stat.st_size
                    entry["modify_time"] = int(stat.st_mtime)
                except (OSError, PermissionError):
                    entry["size"] = 0
                    entry["modify_time"] = 0
            entries.append(entry)
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    rel = os.path.relpath(target, str(base))
    current_path = "/" + rel.replace(os.sep, "/") if rel != "." else "/"
    return {
        "current": current_path,
        "root": root or "sync",
        "root_label": "下载目录" if root == "downloads" else "同步目录",
        "entries": entries,
    }
