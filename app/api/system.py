"""System API endpoints - health, local directory browsing, stats."""

import os
import shutil
from fastapi import APIRouter, HTTPException
from app.config import LOCAL_SYNC_ROOT, LOCAL_ROOTS, DOWNLOAD_DIR, DATA_DIR

router = APIRouter(prefix="/api/system", tags=["system"])

APP_VERSION = "0.1.0"


@router.get("/health")
async def health():
    return {"status": "ok"}


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
