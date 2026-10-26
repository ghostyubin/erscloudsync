"""SQLite database management with async support."""

import aiosqlite
import json
import time
from typing import Any, Optional

from app.config import DB_PATH

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
CREATE TABLE IF NOT EXISTS sync_records (
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
    status TEXT NOT NULL DEFAULT 'success'
        CHECK(status IN ('success', 'failed')),
    error TEXT DEFAULT '',
    -- How many times this same failure has been seen. A file that keeps
    -- failing is updated in place instead of inserting one row per retry,
    -- so a permanently broken file can no longer flood the panel.
    retry_count INTEGER DEFAULT 0,
    created_at REAL DEFAULT (strftime('%s','now'))
);

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
        ):
            try:
                await db.execute(stmt)
            except Exception:  # noqa: BLE001 - already applied
                pass
        await db.commit()


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


async def db_upsert_sync_state(task_id: int, file_path: str, file_size: int,
                                file_mtime: float, file_md5: str = "", side: str = "both"):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO sync_state (task_id, file_path, file_size, file_mtime, file_md5, side, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))
               ON CONFLICT(task_id, file_path) DO UPDATE SET
                 file_size=excluded.file_size,
                 file_mtime=excluded.file_mtime,
                 file_md5=excluded.file_md5,
                 side=excluded.side,
                 synced_at=strftime('%s','now')""",
            (task_id, file_path, file_size, file_mtime, file_md5, side),
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
        if status == "failed":
            # A file that cannot be transferred used to append one row per
            # retry, and a realtime task retries every cycle — the panel filled
            # up with identical failures. Collapse repeats into the newest row
            # for the same target and keep a counter instead.
            cur = await db.execute(
                """SELECT id FROM sync_records
                   WHERE task_id = ? AND action = ? AND target = ?
                     AND status = 'failed'
                   ORDER BY id DESC LIMIT 1""",
                (task_id, action, target),
            )
            existing = await cur.fetchone()
            if existing:
                await db.execute(
                    """UPDATE sync_records
                       SET error = ?,
                           retry_count = COALESCE(retry_count, 0) + 1,
                           created_at = strftime('%s','now')
                       WHERE id = ?""",
                    (error or "", existing[0]),
                )
                await db.commit()
                return
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
    if status in ("success", "failed"):
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
    total_bytes = 0
    for r in rows:
        total += r["n"]
        total_bytes += r["b"]
        if r["status"] == "failed":
            failed += r["n"]
        # by_action counts every row, failures included, so the per-direction
        # numbers in the panel header add up to the total. "failed" is an extra
        # dimension on top (a subset, not a fourth bucket).
        key = "delete" if r["action"].startswith("delete") else r["action"]
        by_action[key] = by_action.get(key, 0) + r["n"]
    return {"total": total, "failed": failed, "bytes": total_bytes,
            "by_action": by_action, "tasks": tasks}


async def db_clear_records():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sync_records")
        await db.commit()


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
