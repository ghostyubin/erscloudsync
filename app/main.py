"""BauduSync - Cloud sync application for NAS.

Mimics Synology CloudSync functionality with Baidu Netdisk and 115 Netdisk support.
Designed to run in Docker on x86 and ARM64 (RK3576) OpenWrt systems.
"""

import asyncio
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import WEB_HOST, WEB_PORT, FRONTEND_DIR, LOCAL_SYNC_ROOT, LOG_DIR
from app.database import init_db
from app.services.scheduler import scheduler
from app.api import connections, tasks, system, downloads, logs

# Configure logging — console + rotating file in /config/log/
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _setup_file_logging():
    """Add a rotating file handler that writes to /config/log/baudusync.log."""
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "baudusync.log"

    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            try:
                if Path(h.baseFilename).resolve() == log_file.resolve():
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    # Setup file logging
    _setup_file_logging()

    # Startup
    logger.info("Initializing database...")
    await init_db()

    # Ensure sync root exists (skip if it's already a mount point)
    try:
        LOCAL_SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning(f"Could not create sync root at {LOCAL_SYNC_ROOT} (may be a mount point)")

    logger.info("Starting scheduler...")
    await scheduler.start()

    logger.info(f"BauduSync ready at http://{WEB_HOST}:{WEB_PORT}")
    yield

    # Shutdown
    logger.info("Shutting down scheduler...")
    await scheduler.stop()
    logger.info("BauduSync stopped.")


app = FastAPI(
    title="BauduSync",
    description="Cloud sync for NAS - Baidu Netdisk & 115 Netdisk",
    version="1.0.0",
    lifespan=lifespan,
)

# API routes
app.include_router(connections.router)
app.include_router(tasks.router)
app.include_router(system.router)
app.include_router(downloads.router)
app.include_router(logs.router)


# Serve frontend
static_dir = FRONTEND_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main HTML page."""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return HTMLResponse("<h1>BauduSync</h1><p>Frontend not found.</p>")


@app.get("/api")
async def api_root():
    return {
        "name": "BauduSync API",
        "version": "1.0.0",
        "endpoints": [
            "/api/connections",
            "/api/tasks",
            "/api/system",
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=WEB_HOST,
        port=WEB_PORT,
        reload=False,
        log_level="info",
    )
