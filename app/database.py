"""SQLite database management with async support."""

import aiosqlite
import json
import logging
import time
from typing import Any, Optional

from app.config import DB_PATH

logger = logging.getLogger(__name__)

# DDL for sync_records, kept as its own constant because the upgrade path has
# to reuse it verbatim: SQLite cannot ALTER a CHECK constraint, so adding a
# status value means rebuilding the table, and the rebuilt table must not
# drift from the one a fresh install gets.
_RECORDS_TABLE_SQL = """CREATE TABLE IF NOT EXISTS sync_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL DEFAULT 0,
    task_name TEXT NOT NULL DEFAULT '',
    connection TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL
        CHECK(action IN ('upload', 'download', 'delete_local', 'delete_remote')),
    file_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    file_size INTEGER DEFAULT 0,
    -- 'skipped' is a user decision (the Skip button), not a failure: nothing
    -- was transferred and nothing went wrong, so it gets its own state.
    status TEXT NOT NULL DEFAULT 'success'
        CHECK(status IN ('success', 'failed', 'skipped')),
    error TEXT DEFAULT '',
    -- How many times this same failure has been seen. A file that keeps
    -- failing is updated in place instead of inserting one row per retry,
    -- so a permanently broken file can no longer flood the panel.
    retry_count INTEGER DEFAULT 0,
    created_at REAL DEFAULT (strftime('%s','now'))
)"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('baidu', '115')),
    credentials TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT 'disconnected',
    created_at REAL DEFAULT (strftime('%s','now')),
    updated_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    connection_id INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    sync_mode TEXT NOT NULL DEFAULT 'bidirectional'
        CHECK(sync_mode IN ('bidirectional', 'upload_only', 'download_only')),
    schedule_type TEXT DEFAULT 'manual'
        CHECK(schedule_type IN ('realtime', 'scheduled', 'manual')),
    schedule_interval INTEGER DEFAULT 0,
    filter_include TEXT DEFAULT '',
    filter_exclude TEXT DEFAULT '',
    max_file_size INTEGER DEFAULT 0,
    delete_after_sync INTEGER DEFAULT 0,
    -- 客户端加密：只加密内容，文件名和路径不变。enc_wrapped 是用全局 KEK
    -- 包裹后的任务密钥（base64），第一次上传时才生成。
    enc_enabled INTEGER DEFAULT 0,
    enc_wrapped TEXT DEFAULT '',
    enc_key_id INTEGER DEFAULT 0,
    -- 密文格式：bsenc = 自研的 AES-256-GCM（带认证）；syno = 群晖 Cloud Sync
    -- 兼容格式，可以用群晖官方的解密工具打开。两者按任务切换。
    enc_format TEXT DEFAULT 'bsenc',
    -- 群晖格式需要一对 RSA 密钥：公钥明文存（本来就不保密），私钥用 KEK
    -- 包裹后存（用户随时可以重新导出 private.pem）。
    syno_pubkey TEXT DEFAULT '',
    syno_priv_wrapped TEXT DEFAULT '',
    status TEXT DEFAULT 'idle'
        CHECK(status IN ('idle', 'running', 'paused', 'error', 'success')),
    last_sync_at REAL,
    last_sync_status TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    created_at REAL DEFAULT (strftime('%s','now')),
    updated_at REAL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    file_mtime REAL DEFAULT 0,
    file_md5 TEXT DEFAULT '',
    -- 加密任务里云端是密文，大小和本地明文对不上。单存一列记实际观测到的
    -- 云端大小，否则每轮都会判成「云端变了」→ 双向模式无限下载。
    remote_size INTEGER DEFAULT 0,
    side TEXT DEFAULT 'both'
        CHECK(side IN ('local', 'remote', 'both')),
    synced_at REAL DEFAULT (strftime('%s','now')),
    UNIQUE(task_id, file_path),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    action TEXT NOT NULL
        CHECK(action IN ('upload', 'download', 'delete_local', 'delete_remote',
                         'skip', 'mkdir_local', 'mkdir_remote', 'error', 'info')),
    file_path TEXT NOT NULL DEFAULT '',
    detail TEXT DEFAULT '',
    timestamp REAL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    start_time REAL,
    end_time REAL,
    status TEXT DEFAULT 'running',
    files_synced INTEGER DEFAULT 0,
    files_failed INTEGER DEFAULT 0,
    files_skipped INTEGER DEFAULT 0,
    bytes_transferred INTEGER DEFAULT 0,
    error_message TEXT DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_state_task ON sync_state(task_id);
CREATE INDEX IF NOT EXISTS idx_sync_logs_task ON sync_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_sync_logs_time ON sync_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_sync_history_task ON sync_history(task_id);

-- One row per transferred/deleted file, written by the sync engine and by the
-- download manager. sync_logs holds human-readable prose ("Uploaded /sync/x"),
-- which is fine for a log view but cannot be filtered by direction, sorted, or
-- joined against a task reliably — every consumer would have to re-parse the
-- message text. This table stores the pieces separately: who (task + cloud
-- connection), what (file name + size), from where, to where, and whether it
-- worked.
--
-- No FOREIGN KEY on purpose: task_name / connection are snapshot strings, so
-- deleting a task must not wipe its transfer history.
""" + _RECORDS_TABLE_SQL + """;

CREATE INDEX IF NOT EXISTS idx_sync_records_time ON sync_records(created_at);
CREATE INDEX IF NOT EXISTS idx_sync_records_action ON sync_records(action);
CREATE INDEX IF NOT EXISTS idx_sync_records_task ON sync_records(task_id);

-- Per-file failure memory: without this, an upload_only task re-attempts a
-- file it cannot upload on *every* realtime cycle, and writes a fresh failed
-- record each time. next_retry_at is an exponential backoff deadline.
CREATE TABLE IF NOT EXISTS sync_failures (
    task_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,          -- path relative to the task root
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_error_at REAL NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_sync_failures_retry
    ON sync_failures(task_id, next_retry_at);

-- Files the user explicitly skipped. "Skip" in the UI means "stop offering
-- this file", not "abort the transfer that is running right now", so it has
-- to survive the cycle that produced it.
CREATE TABLE IF NOT EXISTS sync_ignored (
    task_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,          -- path relative to the task root
    action TEXT NOT NULL DEFAULT '',  -- upload / download, for the listing
    created_at REAL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (task_id, file_path)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at REAL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL,
    connection_name TEXT DEFAULT '',
    remote_path TEXT NOT NULL,
    local_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending'
        CHECK(status IN ('pending', 'downloading', 'completed', 'failed', 'cancelled')),
    is_dir INTEGER DEFAULT 0,
    total_files INTEGER DEFAULT 1,
    processed_files INTEGER DEFAULT 0,
    error_message TEXT DEFAULT '',
    created_at REAL DEFAULT (strftime('%s','now')),
    completed_at REAL,
    FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at);
"""


async def init_db():
    """Initialize the database schema."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # WAL is persistent in the database file, so every later connection
        # benefits. Without it the frontend polling /api/tasks while a sync
        # writes logs serialises readers against writers and can surface as
        # "database is locked" on the NAS.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript(SCHEMA)
        # Columns added after a table already exists. CREATE TABLE IF NOT
        # EXISTS does nothing on an existing table, so upgrades need the ALTER
        # path. "duplicate column name" is the expected, harmless outcome on a
        # database that already has it.
        for stmt in (
            "ALTER TABLE sync_records ADD COLUMN retry_count INTEGER DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN enc_enabled INTEGER DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN enc_wrapped TEXT DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN enc_key_id INTEGER DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN enc_format TEXT DEFAULT 'bsenc'",
            "ALTER TABLE tasks ADD COLUMN syno_pubkey TEXT DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN syno_priv_wrapped TEXT DEFAULT ''",
            "ALTER TABLE sync_state ADD COLUMN remote_size INTEGER DEFAULT 0",
        ):
            try:
                await db.execute(stmt)
            except Exception:  # noqa: BLE001 - already applied
                pass
        await _migrate_records_status(db)
        await db.commit()


async def _migrate_records_status(db):
    """Rebuild sync_records if its CHECK still only allows success/failed.

    ``CREATE TABLE IF NOT EXISTS`` silently does nothing on an existing table,
    so a database created before the "skipped" status existed would reject
    every skip record with an IntegrityError. SQLite has no ALTER ... CHECK,
    hence the copy-through-rebuild dance (the recommended 12-step procedure).
    """
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_records'")
    row = await cursor.fetchone()
    ddl = (row[0] if row else "") or ""
    if "'skipped'" in ddl:
        return

    # The old indexes follow the renamed table, so they are dropped together
    # with it below and have to be recreated afterwards — creating them while
    # the old names are still taken would be a silent no-op.
    await db.executescript(f"""
        ALTER TABLE sync_records RENAME TO sync_records_old;
        {_RECORDS_TABLE_SQL};
        INSERT INTO sync_records (id, task_id, task_name, connection, action,
                                  file_name, source, target, file_size,
                                  status, error, retry_count, created_at)
            SELECT id, task_id, task_name, connection, action,
                   file_name, source, target, file_size,
                   status, error, COALESCE(retry_count, 0), created_at
              FROM sync_records_old;
        DROP TABLE sync_records_old;
        CREATE INDEX IF NOT EXISTS idx_sync_records_time ON sync_records(created_at);
        CREATE INDEX IF NOT EXISTS idx_sync_records_action ON sync_records(action);
        CREATE INDEX IF NOT EXISTS idx_sync_records_task ON sync_records(task_id);
    """)
    logger.info("Migrated sync_records: added 'skipped' to the status CHECK")


async def get_db() -> aiosqlite.Connection:
    """Get a database connection."""
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


# ---- Connection CRUD ----

async def db_create_connection(name: str, conn_type: str, credentials: dict, status: str = "connected"):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT INTO connections (name, type, credentials, status) VALUES (?, ?, ?, ?)",
            (name, conn_type, json.dumps(credentials), status),
        )
        await db.commit()
        return cursor.lastrowid


async def db_update_connection(conn_id: int, **kwargs):
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k == "credentials":
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    sets.append("updated_at = strftime('%s','now')")
    vals.append(conn_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE connections SET {', '.join(sets)} WHERE id = ?", vals
        )
        await db.commit()


async def db_get_connection(conn_id: int) -> Optional[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM connections WHERE id = ?", (conn_id,))
        row = await cursor.fetchone()
        if row:
            d = dict(row)
            d["credentials"] = json.loads(d.get("credentials", "{}"))
            return d
    return None


async def db_list_connections() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM connections ORDER BY id")
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["credentials"] = json.loads(d.get("credentials", "{}"))
            result.append(d)
        return result


async def db_delete_connection(conn_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
        await db.commit()


# ---- Task CRUD ----

async def db_create_task(**kwargs):
    allowed = {
        "name", "connection_id", "local_path", "remote_path", "sync_mode",
        "schedule_type", "schedule_interval", "filter_include", "filter_exclude",
        "max_file_size", "delete_after_sync",
        "enc_enabled", "enc_wrapped", "enc_key_id",
        "enc_format", "syno_pubkey", "syno_priv_wrapped",
    }
    cols = []
    vals = []
    for k, v in kwargs.items():
        if k in allowed:
            cols.append(k)
            vals.append(v)
    placeholders = ", ".join(["?"] * len(cols))
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        await db.commit()
        return cursor.lastrowid


async def db_update_task(task_id: int, **kwargs):
    sets = []
    vals = []
    for k, v in kwargs.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    sets.append("updated_at = strftime('%s','now')")
    vals.append(task_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals
        )
        await db.commit()


async def db_get_task(task_id: int) -> Optional[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT t.*, c.name as connection_name, c.type as connection_type
               FROM tasks t JOIN connections c ON t.connection_id = c.id
               WHERE t.id = ?""",
            (task_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def db_list_tasks() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT t.*, c.name as connection_name, c.type as connection_type
               FROM tasks t JOIN connections c ON t.connection_id = c.id
               ORDER BY t.id"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def db_delete_task(task_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        # These two have no foreign key (a task can be deleted while its rows
        # still carry useful history), so clean them up explicitly — otherwise
        # a re-created task id would inherit stale backoff/ignore state.
        await db.execute("DELETE FROM sync_failures WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM sync_ignored WHERE task_id = ?", (task_id,))
        await db.commit()


# ---- Sync state ----

async def db_get_sync_state(task_id: int) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_state WHERE task_id = ?", (task_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def db_get_sync_state_entry(task_id: int, file_path: str) -> Optional[dict]:
    """The stored "this file was successfully transferred" row, if any."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_state WHERE task_id = ? AND file_path = ?",
            (task_id, file_path),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def db_upsert_sync_state(task_id: int, file_path: str, file_size: int,
                                file_mtime: float, file_md5: str = "",
                                side: str = "both",
                                remote_size: Optional[int] = None):
    """``remote_size=None`` 表示「这一轮没看到云端，别动已记的值」。

    上传完成时会立刻写一次准确的云端大小，紧接着周期末的状态回写用的却是
    传输前的快照——那一轮云端还没有这个文件。如果这里无条件写 0，就会把刚
    记下的值冲掉。所以用 COALESCE 让 None 变成「保留原值」。
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO sync_state (task_id, file_path, file_size, file_mtime,
                                       file_md5, side, remote_size, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 0), strftime('%s','now'))
               ON CONFLICT(task_id, file_path) DO UPDATE SET
                 file_size=excluded.file_size,
                 file_mtime=excluded.file_mtime,
                 file_md5=excluded.file_md5,
                 side=excluded.side,
                 remote_size=COALESCE(?, sync_state.remote_size),
                 synced_at=strftime('%s','now')""",
            (task_id, file_path, file_size, file_mtime, file_md5, side,
             remote_size, remote_size),
        )
        await db.commit()


async def db_delete_sync_state(task_id: int, file_path: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "DELETE FROM sync_state WHERE task_id = ? AND file_path = ?",
            (task_id, file_path),
        )
        await db.commit()


async def db_clear_sync_state(task_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sync_state WHERE task_id = ?", (task_id,))
        await db.commit()


# ---- Logs ----

async def db_add_log(task_id: int, action: str, file_path: str = "", detail: str = ""):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO sync_logs (task_id, action, file_path, detail) VALUES (?, ?, ?, ?)",
            (task_id, action, file_path, detail),
        )
        await db.commit()


async def db_list_logs(task_id: int, limit: int = 200, offset: int = 0) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_logs WHERE task_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def db_list_all_logs(limit: int = 1000, offset: int = 0) -> list[dict]:
    """List logs from all tasks, most recent first, with task name."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT l.*, t.name as task_name
               FROM sync_logs l
               LEFT JOIN tasks t ON l.task_id = t.id
               ORDER BY l.id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def db_clear_all_logs():
    """Delete all sync logs."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sync_logs")
        await db.commit()


async def db_clear_task_logs(task_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sync_logs WHERE task_id = ?", (task_id,))
        await db.commit()


# ---- Sync records (one row per file that moved) ----

# Hard cap so the table cannot grow without bound on a NAS that syncs for
# years. Trimming happens every _RECORD_TRIM_EVERY inserts instead of on every
# write: a DELETE with a subquery per transferred file would double the cost of
# the write path for no benefit.
_RECORD_LIMIT = 20000
_RECORD_TRIM_EVERY = 200
_record_writes = 0


async def db_add_record(task_id: int, action: str, *,
                        task_name: str = "", connection: str = "",
                        file_name: str = "", source: str = "", target: str = "",
                        file_size: int = 0, status: str = "success",
                        error: str = ""):
    """Record a file that was uploaded, downloaded or deleted."""
    global _record_writes
    async with aiosqlite.connect(str(DB_PATH)) as db:
        if status in ("failed", "skipped"):
            # A file that cannot be transferred used to append one row per
            # retry, and a realtime task retries every cycle — the panel filled
            # up with identical failures. Collapse repeats into the newest row
            # for the same target. Skips are collapsed the same way: a file
            # skipped, restored and skipped again is one row, not three.
            cur = await db.execute(
                """SELECT id FROM sync_records
                   WHERE task_id = ? AND action = ? AND target = ?
                     AND status = ?
                   ORDER BY id DESC LIMIT 1""",
                (task_id, action, target, status),
            )
            existing = await cur.fetchone()
            if existing:
                # retry_count is "how many times this failed again", so a
                # repeated skip must not inflate it.
                bump = 1 if status == "failed" else 0
                await db.execute(
                    """UPDATE sync_records
                       SET error = ?,
                           retry_count = COALESCE(retry_count, 0) + ?,
                           created_at = strftime('%s','now')
                       WHERE id = ?""",
                    (error or "", bump, existing[0]),
                )
                await db.commit()
                return
        elif status == "success":
            # The file made it through, so any earlier failure / skip row for
            # the very same target is now stale — and it still carries a Retry
            # button, which reads as "this is still broken". Drop it: the
            # success row above it is the current truth. Only rows strictly
            # older than this one go, so nothing is deleted before the insert
            # below exists.
            await db.execute(
                """DELETE FROM sync_records
                   WHERE task_id = ? AND action = ? AND target = ?
                     AND status IN ('failed', 'skipped')""",
                (task_id, action, target),
            )
        await db.execute(
            """INSERT INTO sync_records
               (task_id, task_name, connection, action, file_name,
                source, target, file_size, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, task_name, connection, action, file_name,
             source, target, int(file_size or 0), status, error or ""),
        )
        _record_writes += 1
        if _record_writes % _RECORD_TRIM_EVERY == 0:
            await db.execute(
                """DELETE FROM sync_records WHERE id NOT IN
                   (SELECT id FROM sync_records ORDER BY id DESC LIMIT ?)""",
                (_RECORD_LIMIT,),
            )
        await db.commit()


def _records_where(action: Optional[str] = None, task_id: Optional[int] = None,
                   status: Optional[str] = None,
                   query: Optional[str] = None) -> tuple[str, list]:
    """Build the shared WHERE clause for the record list / count queries."""
    where: list[str] = []
    params: list = []
    if action:
        # "delete" is a UI-level grouping of the two delete actions.
        if action == "delete":
            where.append("action LIKE 'delete%'")
        elif action in ("upload", "download", "delete_local", "delete_remote"):
            where.append("action = ?")
            params.append(action)
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if status in ("success", "failed", "skipped"):
        where.append("status = ?")
        params.append(status)
    if query:
        like = f"%{query}%"
        where.append("(file_name LIKE ? OR source LIKE ? OR target LIKE ?)")
        params.extend([like, like, like])
    return ("WHERE " + " AND ".join(where)) if where else "", params


async def db_list_records(limit: int = 50, offset: int = 0, *,
                          action: Optional[str] = None,
                          task_id: Optional[int] = None,
                          status: Optional[str] = None,
                          query: Optional[str] = None) -> list[dict]:
    clause, params = _records_where(action, task_id, status, query)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT * FROM sync_records {clause}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def db_count_records(*, action: Optional[str] = None,
                           task_id: Optional[int] = None,
                           status: Optional[str] = None,
                           query: Optional[str] = None) -> int:
    clause, params = _records_where(action, task_id, status, query)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM sync_records {clause}", params)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


def _digest_folder(root: str, action: str, source: str, target: str) -> str:
    """把一条记录归到「任务本地根下的一级目录」，口径和实时通知的分桶一致。

    上传和「删远端」时 ``source`` 是本地路径；下载和「删本地」时 ``target``
    才是本地路径（见 `sync_engine._add_record` 的调用）。相对任务的
    ``local_path`` 取第一段：``/sync/1/a.jpg`` → ``/sync/1``，直接放在
    根下的 → ``/sync``。任务已被删除（拿不到根）时退化成按路径第一段
    归并，不会算丢。
    """
    local_side = source if action in ("upload", "delete_remote") else target
    path = (local_side or "").replace("\\", "/")
    root = (root or "").replace("\\", "/").rstrip("/")
    if root and path.startswith(root + "/"):
        rel = path[len(root) + 1:]
    elif root and path.startswith(root):
        rel = path[len(root):].lstrip("/")
    else:
        rel = path.lstrip("/")
    head, sep, _ = rel.partition("/")
    if root:
        return f"{root}/{head}" if sep else root
    return head or "/"


async def db_digest_stats(since: float, until: float) -> dict:
    """统计一段时间内的传输情况，供「每隔 N 小时」的汇总通知使用。

    以 ``sync_records`` 为准而不是内存计数器：进程重启、重建容器都不会让
    统计归零，也不会因为通知开关被关掉一段时间就少算。记录表本身有 2 万
    行上限，窗口再长也不会捞出巨量数据。
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, local_path FROM tasks")
        roots = {int(r["id"]): (r["local_path"] or "")
                 for r in await cursor.fetchall()}
        cursor = await db.execute(
            """SELECT task_id, action, status, source, target, file_name,
                      COALESCE(file_size, 0) AS size
               FROM sync_records
               WHERE created_at >= ? AND created_at <= ?""",
            (float(since), float(until)),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    folders: dict[str, int] = {}
    files = uploads = downloads = deletes = 0
    failed = skipped = 0
    total_bytes = 0
    failed_names: list[str] = []

    for row in rows:
        action = row["action"] or ""
        status = row["status"] or "success"
        if status == "failed":
            failed += 1
            # 只留前三个文件名，消息里点名用
            if len(failed_names) < 3 and row["file_name"]:
                failed_names.append(row["file_name"])
            continue
        if status == "skipped":
            # 用户主动跳过的既不算传输也不算失败
            skipped += 1
            continue
        if action == "upload":
            uploads += 1
        elif action == "download":
            downloads += 1
        elif action.startswith("delete"):
            deletes += 1
        else:
            continue
        files += 1
        # 目录分布按「完成的事」算（含删除，不含失败）：这样各目录的数字
        # 加起来正好等于「共 N 个文件」，不会对不上账
        label = _digest_folder(roots.get(int(row["task_id"] or 0), ""),
                               action, row["source"] or "", row["target"] or "")
        folders[label] = folders.get(label, 0) + 1
        if action.startswith("delete"):
            continue                      # 删除不计流量
        total_bytes += int(row["size"] or 0)

    ordered = sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "files": files, "uploads": uploads, "downloads": downloads,
        "deletes": deletes, "failed": failed, "skipped": skipped,
        "bytes": total_bytes,
        # 只回前 8 个目录，够消息展示也够排查
        "folders": ordered[:8], "folder_count": len(ordered),
        "failed_names": failed_names,
        "since": float(since), "until": float(until),
    }


async def db_get_record(record_id: int) -> Optional[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_records WHERE id = ?", (record_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def db_record_stats() -> dict:
    """Totals for the panel header and the task filter dropdown."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT action, status, COUNT(*) AS n, COALESCE(SUM(file_size),0) AS b
               FROM sync_records GROUP BY action, status""")
        rows = [dict(r) for r in await cursor.fetchall()]
        cursor = await db.execute(
            """SELECT task_id, task_name, COUNT(*) AS n
               FROM sync_records GROUP BY task_id, task_name
               ORDER BY n DESC LIMIT 100""")
        tasks = [dict(r) for r in await cursor.fetchall()]

    by_action: dict[str, int] = {}
    total = 0
    failed = 0
    skipped = 0
    total_bytes = 0
    for r in rows:
        total += r["n"]
        total_bytes += r["b"]
        if r["status"] == "failed":
            failed += r["n"]
        elif r["status"] == "skipped":
            skipped += r["n"]
        # by_action counts every row, failures included, so the per-direction
        # numbers in the panel header add up to the total. "failed" / "skipped"
        # are an extra dimension on top (subsets, not extra buckets).
        key = "delete" if r["action"].startswith("delete") else r["action"]
        by_action[key] = by_action.get(key, 0) + r["n"]
    return {"total": total, "failed": failed, "skipped": skipped,
            "bytes": total_bytes, "by_action": by_action, "tasks": tasks}


async def db_clear_records():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sync_records")
        await db.commit()


async def db_delete_record(record_id: int) -> bool:
    """Remove one row from the transfer history (user asked to)."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cur = await db.execute("DELETE FROM sync_records WHERE id = ?",
                               (record_id,))
        await db.commit()
        return cur.rowcount > 0


async def db_list_retryable_records() -> list[dict]:
    """Every failed / skipped row, oldest first, for the Retry-all button.

    Deliberately unpaginated: a backlog the user wants to clear in one click
    should not silently stop at some limit. Oldest first so the re-sync queue
    matches the order things originally went wrong.
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM sync_records
               WHERE status IN ('failed', 'skipped')
               ORDER BY created_at ASC, id ASC""")
        return [dict(r) for r in await cursor.fetchall()]


# ---- Per-file failure memory (exponential backoff) ----

# Seconds to wait before the next attempt, indexed by attempt count - 1. The
# last value is the ceiling: a file that never succeeds settles at one attempt
# per hour instead of one per realtime cycle.
FAILURE_BACKOFF = (60.0, 300.0, 900.0, 3600.0)


def failure_backoff(attempts: int) -> float:
    """Delay before retrying a file that has failed ``attempts`` times."""
    if attempts <= 0:
        return 0.0
    return FAILURE_BACKOFF[min(attempts - 1, len(FAILURE_BACKOFF) - 1)]


async def db_mark_failure(task_id: int, file_path: str, error: str) -> float:
    """Remember one failed attempt; returns the timestamp of the next retry."""
    now = time.time()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT attempts FROM sync_failures WHERE task_id = ? AND file_path = ?",
            (task_id, file_path),
        )
        row = await cursor.fetchone()
        attempts = (int(row["attempts"]) if row else 0) + 1
        next_retry = now + failure_backoff(attempts)
        await db.execute(
            """INSERT INTO sync_failures
                   (task_id, file_path, attempts, last_error, last_error_at,
                    next_retry_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id, file_path) DO UPDATE SET
                   attempts = excluded.attempts,
                   last_error = excluded.last_error,
                   last_error_at = excluded.last_error_at,
                   next_retry_at = excluded.next_retry_at""",
            (task_id, file_path, attempts, error or "", now, next_retry),
        )
        await db.commit()
    return next_retry


async def db_clear_failure(task_id: int, file_path: str):
    """Forget the failure record — called when the file finally goes through."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "DELETE FROM sync_failures WHERE task_id = ? AND file_path = ?",
            (task_id, file_path),
        )
        await db.commit()


async def db_get_failures(task_id: int) -> dict[str, dict]:
    """All failure records of a task, keyed by relative path."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_failures WHERE task_id = ?", (task_id,))
        return {r["file_path"]: dict(r) for r in await cursor.fetchall()}


async def db_clear_failures(task_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sync_failures WHERE task_id = ?", (task_id,))
        await db.commit()


# ---- Ignored files ----

async def db_add_ignored(task_id: int, file_path: str, action: str = ""):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO sync_ignored (task_id, file_path, action)
               VALUES (?, ?, ?)
               ON CONFLICT(task_id, file_path) DO UPDATE SET action = excluded.action""",
            (task_id, file_path, action or ""),
        )
        await db.commit()


async def db_remove_ignored(task_id: int, file_path: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "DELETE FROM sync_ignored WHERE task_id = ? AND file_path = ?",
            (task_id, file_path),
        )
        await db.commit()


async def db_get_ignored(task_id: int) -> set[str]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT file_path FROM sync_ignored WHERE task_id = ?", (task_id,))
        return {r[0] for r in await cursor.fetchall()}


async def db_list_ignored(task_id: int) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM sync_ignored WHERE task_id = ?
               ORDER BY created_at DESC""", (task_id,))
        return [dict(r) for r in await cursor.fetchall()]


async def db_clear_ignored(task_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sync_ignored WHERE task_id = ?", (task_id,))
        await db.commit()


# ---- History ----

async def db_create_history(task_id: int) -> int:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT INTO sync_history (task_id, start_time, status) VALUES (?, strftime('%s','now'), 'running')",
            (task_id,),
        )
        await db.commit()
        return cursor.lastrowid


async def db_update_history(history_id: int, **kwargs):
    sets = []
    vals = []
    for k, v in kwargs.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(history_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE sync_history SET {', '.join(sets)} WHERE id = ?", vals
        )
        await db.commit()


async def db_list_history(task_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sync_history WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ---- Settings ----

async def db_get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default


async def db_set_setting(key: str, value: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, strftime('%s','now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%s','now')""",
            (key, value),
        )
        await db.commit()


async def db_rewrap_crypto(salt_key: str, salt_b64: str,
                           verifier_key: str, verifier_b64: str,
                           wraps: list[tuple[int, str]],
                           extra_settings: Optional[list[tuple[str, str]]] = None,
                           privs: Optional[list[tuple[int, str]]] = None):
    """换密码时一次性落库：新 salt、新验证块、所有任务重新包裹的密钥。

    必须在**同一个事务**里写完。分开写的话，中途崩溃/断电会留下「新 salt
    配上部分旧 wrapped」这种半新半旧的状态——旧密码推导出的 KEK 解不开新的，
    新密码推导出的 KEK 解不开旧的，那部分任务的云端文件就永久打不开了。

    两个 settings 的键名由调用方传进来，免得这里再抄一份常量和 crypto.py 漂移。

    ``extra_settings`` 用来带别的也要换的键（群晖格式要存一份用 KEK 包裹的
    密码明文），``privs`` 是群晖格式任务的 RSA 私钥——它们同样是 KEK 包裹的，
    换密码时必须一起重包，否则那些任务的新文件就加不了密了。
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            rows = [(salt_key, salt_b64), (verifier_key, verifier_b64)]
            rows.extend(extra_settings or [])
            for key, value in rows:
                await db.execute(
                    """INSERT INTO settings (key, value, updated_at)
                       VALUES (?, ?, strftime('%s','now'))
                       ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=strftime('%s','now')""",
                    (key, value))
            for task_id, packed in wraps:
                await db.execute(
                    """UPDATE tasks SET enc_wrapped = ?, enc_key_id = 1,
                       updated_at = strftime('%s','now') WHERE id = ?""",
                    (packed, task_id))
            for task_id, packed in (privs or []):
                await db.execute(
                    """UPDATE tasks SET syno_priv_wrapped = ?,
                       updated_at = strftime('%s','now') WHERE id = ?""",
                    (packed, task_id))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def db_get_all_settings() -> dict:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}


# ---- Downloads ----

async def db_create_download(**kwargs) -> int:
    cols = []
    vals = []
    for k, v in kwargs.items():
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join(["?"] * len(cols))
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"INSERT INTO downloads ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        await db.commit()
        return cursor.lastrowid


async def db_update_download(dl_id: int, **kwargs):
    sets = []
    vals = []
    for k, v in kwargs.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(dl_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE downloads SET {', '.join(sets)} WHERE id = ?", vals
        )
        await db.commit()


async def db_get_download(dl_id: int) -> Optional[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM downloads WHERE id = ?", (dl_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def db_list_downloads(limit: int = 200) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def db_delete_download(dl_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM downloads WHERE id = ?", (dl_id,))
        await db.commit()
