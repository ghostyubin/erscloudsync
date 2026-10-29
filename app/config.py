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

# Retry policy for transient network failures.
# SYNC_MAX_RETRIES = total attempts per operation (1 means "no retry").
# SYNC_RETRY_DELAY = base delay in seconds; attempt N waits N * base.
SYNC_MAX_RETRIES = int(os.environ.get("BAUDUSYNC_MAX_RETRIES", "3"))
SYNC_RETRY_DELAY = float(os.environ.get("BAUDUSYNC_RETRY_DELAY", "3"))

# How many blocks of a single file are uploaded in parallel.
# 1 restores the old sequential behaviour.
UPLOAD_CONCURRENCY = int(os.environ.get("BAUDUSYNC_UPLOAD_CONCURRENCY", "4"))

# Real-time sync: how long the file watcher waits for the tree to go quiet
# before starting a cycle. A long copy (hundreds of GB) keeps emitting events,
# which would postpone the sync indefinitely, so once a batch has been pending
# for this many seconds the cycle runs anyway and the rest is picked up later.
# 0 = always wait for the tree to settle (the old behaviour).
REALTIME_MAX_DELAY = float(os.environ.get("BAUDUSYNC_REALTIME_MAX_DELAY", "30"))

# Files modified more recently than this (seconds) are treated as "still being
# written" and skipped by real-time cycles. Without it, the max-delay path
# above would upload a half-copied photo and push it again once it is complete.
REALTIME_WRITE_GRACE = float(os.environ.get("BAUDUSYNC_WRITE_GRACE", "3"))

# --- 同步完成通知（默认值，界面里保存过的以数据库为准） ---
# 网关地址填到域名即可，代码会拼上 /push（兼容 Heimdallr 的 POST /push）。
NOTIFY_DEFAULT_URL = os.environ.get("BAUDUSYNC_NOTIFY_URL", "")
NOTIFY_DEFAULT_KEY = os.environ.get("BAUDUSYNC_NOTIFY_KEY", "")
# 文件夹静默多少秒才算「这一波传完了」。太短会在拷贝过程中推送，
# 太长则拷完要等很久才收到消息。
NOTIFY_DEFAULT_QUIET = int(os.environ.get("BAUDUSYNC_NOTIFY_QUIET", "60"))

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
# ``or`` rather than ``get(key, default)``: a compose file that lists
# ``BAIDU_APP_KEY=`` with an empty value sets the variable to "" and would then
# shadow the built-in credentials, so every Baidu authorisation would go out
# with an empty client_id. Empty means "not configured".
BAIDU_APP_KEY = os.environ.get("BAIDU_APP_KEY") or BAIDU_DEFAULT_APP_KEY
BAIDU_APP_SECRET = os.environ.get("BAIDU_APP_SECRET") or BAIDU_DEFAULT_APP_SECRET
# Use oob redirect — Baidu shows the auth code on a page, user copies it manually
# This avoids depending on any third-party callback server
BAIDU_OAUTH_TOOL_URL = (
    "https://openapi.baidu.com/oauth/2.0/authorize"
    "?response_type=code"
    # Reference the variable, not a second copy of the literal: with the key
    # hardcoded here, overriding BAIDU_APP_KEY (env or DB setting) silently
    # left this link pointing at the old app id.
    f"&client_id={BAIDU_APP_KEY}"
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
