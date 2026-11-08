"""Logs API endpoints - aggregated sync logs with file-based logging."""

import logging
import logging.handlers
from pathlib import Path
from fastapi import APIRouter, HTTPException

from app import database as db
from app.config import LOG_DIR

router = APIRouter(prefix="/api/logs", tags=["logs"])

logger = logging.getLogger(__name__)


def _setup_file_logging():
    """Ensure a rotating file handler writes to /config/log/baudusync.log."""
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "baudusync.log"

    root = logging.getLogger()
    # Avoid adding duplicate file handlers
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            try:
                if Path(h.baseFilename) == log_file:
                    return
            except Exception:
                pass

    handler = logging.handlers.RotatingFileHandler(
        str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(handler)
    logger.info(f"File logging enabled: {log_file}")


@router.on_event("startup")
async def _startup():
    _setup_file_logging()


@router.get("")
async def list_all_logs(limit: int = 1000, offset: int = 0):
    """List all sync logs across all tasks, most recent first."""
    logs = await db.db_list_all_logs(limit=limit, offset=offset)
    return {"logs": logs, "total": len(logs)}


@router.delete("")
async def clear_all_logs():
    """Clear all sync logs from the database."""
    await db.db_clear_all_logs()
    logger.info("All sync logs cleared by user")
    return {"status": "cleared"}


@router.get("/runtime")
async def list_runtime_logs(limit: int = 500, offset: int = 0,
                            level: str = "", category: str = ""):
    """运行告警：风控、连接重置、重试退避、授权失效等，按最近发生倒序。

    这些原本只写在容器日志里，前端日志页看不到；由 ``log_bridge`` 落库。
    """
    logs = await db.db_list_runtime_logs(
        limit=max(1, min(limit, 2000)), offset=max(0, offset),
        level=level, category=category,
    )
    return {"logs": logs, "total": await db.db_count_runtime_logs(),
            "returned": len(logs)}


@router.delete("/runtime")
async def clear_runtime_logs():
    """只清运行告警，不碰每个文件的同步日志。"""
    await db.db_clear_runtime_logs()
    return {"status": "cleared"}


@router.get("/files")
async def list_log_files():
    """List available log files and their sizes."""
    log_dir = LOG_DIR
    if not log_dir.exists():
        return {"files": [], "dir": str(log_dir)}

    files = []
    try:
        for f in sorted(log_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "modify_time": int(stat.st_mtime),
                    "path": str(f),
                })
    except (OSError, PermissionError):
        pass

    return {"files": files, "dir": str(log_dir)}


@router.get("/file/{filename:path}")
async def read_log_file(filename: str, tail: int = 500):
    """Read the last N lines of a specific log file."""
    from fastapi import Response

    log_file = LOG_DIR / filename
    # Prevent path traversal
    try:
        log_file.resolve().relative_to(LOG_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid file path")

    if not log_file.exists() or not log_file.is_file():
        raise HTTPException(404, "Log file not found")

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        # Return last `tail` lines
        content = "".join(lines[-tail:]) if tail > 0 else "".join(lines)
        return Response(content=content, media_type="text/plain")
    except (OSError, PermissionError) as e:
        raise HTTPException(500, f"Failed to read log file: {e}")
