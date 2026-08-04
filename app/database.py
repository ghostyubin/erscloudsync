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
        await db.executescript(SCHEMA)
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
