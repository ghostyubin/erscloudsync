"""Application configuration."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BAUDUSYNC_DATA_DIR", "/config"))
FRONTEND_DIR = BASE_DIR / "frontend"

# Database
DB_PATH = DATA_DIR / "baudusync.db"

# Web server
WEB_HOST = os.environ.get("BAUDUSYNC_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("BAUDUSYNC_PORT", "5566"))

# Sync defaults
SYNC_CONCURRENCY = int(os.environ.get("BAUDUSYNC_CONCURRENCY", "3"))
SYNC_CHUNK_SIZE = int(os.environ.get("BAUDUSYNC_CHUNK_SIZE", str(8 * 1024 * 1024)))  # 8MB
SYNC_MAX_RETRIES = int(os.environ.get("BAUDUSYNC_MAX_RETRIES", "3"))

# Local sync root (mounted volume in Docker, fallback to local dir for dev)
LOCAL_SYNC_ROOT = Path(os.environ.get("BAUDUSYNC_SYNC_ROOT", str(BASE_DIR / "sync")))

# Downloads directory (for files downloaded from cloud, separate from sync root)
DOWNLOAD_DIR = Path(os.environ.get("BAUDUSYNC_DOWNLOAD_DIR", "/downloads"))

# Log directory (application + sync logs written to files)
LOG_DIR = Path(os.environ.get("BAUDUSYNC_LOG_DIR", str(DATA_DIR / "log")))

# All browsable local roots for directory selection in UI
LOCAL_ROOTS = [str(LOCAL_SYNC_ROOT), str(DOWNLOAD_DIR)]

# Baidu OAuth — using ES File Manager's publicly known credentials
# These have Pan API permissions (unlike some other public keys)
# Source: OpenList team (AList community fork)
# https://github.com/orgs/OpenListTeam/discussions/19
BAIDU_DEFAULT_APP_KEY = "NqOMXF6XGhGRIGemsQ9nG0Na"
BAIDU_DEFAULT_APP_SECRET = "SVT6xpMdLcx6v4aCR4wT8BBOTbzFO8LM"
BAIDU_APP_KEY = os.environ.get("BAIDU_APP_KEY", BAIDU_DEFAULT_APP_KEY)
BAIDU_APP_SECRET = os.environ.get("BAIDU_APP_SECRET", BAIDU_DEFAULT_APP_SECRET)
# Use oob redirect — Baidu shows the auth code on a page, user copies it manually
# This avoids depending on any third-party callback server
BAIDU_OAUTH_TOOL_URL = (
    "https://openapi.baidu.com/oauth/2.0/authorize"
    "?response_type=code"
    "&client_id=NqOMXF6XGhGRIGemsQ9nG0Na"
    "&redirect_uri=oob"
    "&scope=basic,netdisk"
    "&display=popup"
)
BAIDU_REDIRECT_URI = os.environ.get("BAIDU_REDIRECT_URI", "oob")

# In-memory OAuth session store: state -> {name, status, credentials, error, created_at}
OAUTH_SESSIONS: dict = {}

# Ensure data directory exists
DATA_DIR.mkdir(parents=True, exist_ok=True)


async def get_baidu_app_credentials() -> tuple[str, str]:
    """Get Baidu app key and secret, preferring database over env vars."""
    from app import database as db
    db_key = await db.db_get_setting("baidu_app_key", "")
    db_secret = await db.db_get_setting("baidu_app_secret", "")
    return db_key or BAIDU_APP_KEY, db_secret or BAIDU_APP_SECRET
