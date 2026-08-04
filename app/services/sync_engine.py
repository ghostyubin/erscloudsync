"""Core sync engine - handles file synchronization between local and cloud storage.

Supports three sync modes:
- bidirectional: two-way sync
- upload_only: local -> cloud only
- download_only: cloud -> local only
"""

import asyncio
import os
import fnmatch
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

from app.providers.base import CloudProvider, FileInfo
from app import database as db
from app.config import LOCAL_SYNC_ROOT, DOWNLOAD_DIR, SYNC_CONCURRENCY
from app.utils.helpers import get_provider


class SyncAction(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE_LOCAL = "delete_local"
    DELETE_REMOTE = "delete_remote"
    SKIP = "skip"
    MKDIR_LOCAL = "mkdir_local"
    MKDIR_REMOTE = "mkdir_remote"


@dataclass
class SyncOperation:
    action: SyncAction
    local_path: str = ""
    remote_path: str = ""
    file_info: Optional[FileInfo] = None
    reason: str = ""


@dataclass
class ActiveTransfer:
    """Tracks a single concurrent file transfer."""
    transfer_id: int
    file_path: str
    action: str  # "upload" or "download"
    file_size: int = 0
    transferred: int = 0
    started_at: float = 0.0


@dataclass
class SyncProgress:
    task_id: int
    total_files: int = 0
    processed_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    total_bytes: int = 0
    transferred_bytes: int = 0
    speed: float = 0.0
    started_at: float = 0.0
    is_running: bool = False
    # Concurrent transfer tracking: transfer_id -> ActiveTransfer
    active_transfers: dict = field(default_factory=dict)
    _next_transfer_id: int = 0
    # Speed tracking internals
    _speed_last_time: float = 0.0
    _speed_last_bytes: int = 0


class SyncEngine:
    """Core synchronization engine."""

    # Class-level progress tracking for each task
    _progress: dict[int, SyncProgress] = {}
    _running_tasks: set[int] = set()
    # Stop flags: when set, the running sync will abort remaining operations
    _stop_requested: set[int] = set()
    # Cancelled transfers: {task_id: set(transfer_id, ...)}
    _cancelled_transfers: dict[int, set[int]] = {}
    # Per-transfer speed sampling: {task_id: {transfer_id: (bytes, time)}}
    # Sampled on each API call to compute per-file speed without relying
    # on the frontend's polling delta (frontend state is unreliable when
    # multiple transfers of identical size are uploaded concurrently).
    _transfer_samples: dict[int, dict[int, tuple]] = {}

    @classmethod
    def get_progress(cls, task_id: int) -> Optional[SyncProgress]:
        return cls._progress.get(task_id)

    @classmethod
    def is_running(cls, task_id: int) -> bool:
        return task_id in cls._running_tasks

    @classmethod
    def request_stop(cls, task_id: int):
        """Request a running sync to stop after current operations."""
        cls._stop_requested.add(task_id)

    @classmethod
    def is_stop_requested(cls, task_id: int) -> bool:
        return task_id in cls._stop_requested

    @classmethod
    def cancel_transfer(cls, task_id: int, transfer_id: int):
        """Cancel a specific file transfer within a running sync."""
        if task_id not in cls._cancelled_transfers:
            cls._cancelled_transfers[task_id] = set()
        cls._cancelled_transfers[task_id].add(transfer_id)

    @classmethod
    def is_transfer_cancelled(cls, task_id: int, transfer_id: int) -> bool:
        return transfer_id in cls._cancelled_transfers.get(task_id, set())

    @classmethod
    async def sync_task(cls, task_id: int):
        """Execute a full sync cycle for a task."""
        if task_id in cls._running_tasks:
            return  # Already running

        cls._running_tasks.add(task_id)

        # Initialize progress
        progress = SyncProgress(task_id=task_id, started_at=time.time(), is_running=True)
        cls._progress[task_id] = progress

        task = await db.db_get_task(task_id)
        if not task:
            cls._running_tasks.discard(task_id)
            return

        history_id = await db.db_create_history(task_id)
        await db.db_update_task(task_id, status="running", last_error="")
        await db.db_add_log(task_id, "info", detail=f"Sync started: {task['name']}")

        try:
            # Get provider
            connection = await db.db_get_connection(task["connection_id"])
            if not connection:
                raise RuntimeError("Connection not found")

            provider = get_provider(connection["type"], connection["credentials"])
            if not await provider.test_connection():
                raise RuntimeError(f"Connection to {connection['name']} failed")

            sync_mode = task["sync_mode"]
            local_base = cls._resolve_local_base(task["local_path"])
            remote_base = task["remote_path"]

            # Ensure local directory exists
            os.makedirs(local_base, exist_ok=True)

            # Get filters
            include_patterns = [p.strip() for p in task.get("filter_include", "").split(",") if p.strip()]
            exclude_patterns = [p.strip() for p in task.get("filter_exclude", "").split(",") if p.strip()]
            max_size = task.get("max_file_size", 0)

            # Scan local and remote files
            await db.db_add_log(task_id, "info", detail="Scanning local files...")
            local_files = await cls._scan_local(local_base, include_patterns, exclude_patterns, max_size)

            await db.db_add_log(task_id, "info", detail="Listing remote files...")
            remote_files = await provider.list_all_files(remote_base)

            # Build file maps keyed by relative path
            local_map = {cls._rel_path(f.path, local_base): f for f in local_files}
            remote_map = {cls._rel_path(f.path, remote_base): f for f in remote_files}

            # Get last sync state
            sync_state = await db.db_get_sync_state(task_id)
            last_state = {s["file_path"]: s for s in sync_state}

            # Compute operations
            operations = cls._compute_operations(
                local_map, remote_map, last_state, sync_mode, local_base, remote_base
            )

            progress.total_files = len(operations)
            await db.db_add_log(task_id, "info", detail=f"Found {len(operations)} operations to execute")

            # Execute operations with concurrency limit
            semaphore = asyncio.Semaphore(SYNC_CONCURRENCY)
            results = await asyncio.gather(
                *[cls._execute_op(op, provider, task_id, semaphore, progress, local_base, remote_base)
                  for op in operations],
                return_exceptions=True
            )

            # Count results
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    progress.failed_files += 1
                    await db.db_add_log(task_id, "error", operations[i].remote_path, str(result))
                elif result == "success":
                    progress.processed_files += 1
                elif result == "skipped":
                    progress.skipped_files += 1

            # Update sync state
            await cls._update_sync_state(task_id, local_map, remote_map, local_base, remote_base)

            # Cleanup: handle deleted files in sync state
            all_paths = set(local_map.keys()) | set(remote_map.keys())
            state_paths = set(last_state.keys())
            deleted_paths = state_paths - all_paths
            for dp in deleted_paths:
                await db.db_delete_sync_state(task_id, dp)

            # Success
            await db.db_update_task(task_id,
                status="success",
                last_sync_at=time.time(),
                last_sync_status="success",
                last_error=""
            )
            await db.db_update_history(history_id,
                status="success",
                end_time=time.time(),
                files_synced=progress.processed_files,
                files_failed=progress.failed_files,
                files_skipped=progress.skipped_files,
                bytes_transferred=progress.transferred_bytes
            )
            await db.db_add_log(task_id, "info", detail=
                f"Sync completed: {progress.processed_files} synced, "
                f"{progress.failed_files} failed, {progress.skipped_files} skipped"
            )

        except Exception as e:
            error_msg = str(e)
            await db.db_update_task(task_id,
                status="error",
                last_sync_at=time.time(),
                last_sync_status="error",
                last_error=error_msg
            )
            await db.db_update_history(history_id,
                status="error",
                end_time=time.time(),
                error_message=error_msg,
                files_synced=progress.processed_files,
                files_failed=progress.failed_files,
                files_skipped=progress.skipped_files,
                bytes_transferred=progress.transferred_bytes
            )
            await db.db_add_log(task_id, "error", detail=f"Sync error: {error_msg}")

        finally:
            progress.is_running = False
            cls._running_tasks.discard(task_id)
            cls._stop_requested.discard(task_id)
            cls._cancelled_transfers.pop(task_id, None)
            cls._transfer_samples.pop(task_id, None)
            try:
                await provider.close()
            except Exception:
                pass

    @classmethod
    def _compute_operations(cls, local_map: dict, remote_map: dict,
                            last_state: dict, sync_mode: str,
                            local_base: str, remote_base: str) -> list[SyncOperation]:
        """Compute sync operations based on file comparison."""
        operations = []
        all_paths = set(local_map.keys()) | set(remote_map.keys())

        for rel_path in sorted(all_paths):
            local_file = local_map.get(rel_path)
            remote_file = remote_map.get(rel_path)
            last = last_state.get(rel_path)

            local_changed = False
            remote_changed = False

            if local_file and last:
                if local_file.size != last.get("file_size", 0) or \
                   abs(local_file.modify_time - last.get("file_mtime", 0)) > 2:
                    local_changed = True
            elif local_file and not last:
                local_changed = True  # New file

            if remote_file and last:
                if remote_file.size != last.get("file_size", 0) or \
                   abs(remote_file.modify_time - last.get("file_mtime", 0)) > 2:
                    remote_changed = True
            elif remote_file and not last:
                remote_changed = True  # New file

            # Compute operations based on sync mode
            if sync_mode == "bidirectional":
                if local_file and remote_file:
                    if local_changed and not remote_changed:
                        operations.append(SyncOperation(
                            action=SyncAction.UPLOAD, file_info=local_file,
                            local_path=cls._join_local(local_base, rel_path),
                            remote_path=cls._join_remote(remote_base, rel_path),
                            reason="Local file modified"
                        ))
                    elif remote_changed and not local_changed:
                        operations.append(SyncOperation(
                            action=SyncAction.DOWNLOAD, file_info=remote_file,
                            local_path=cls._join_local(local_base, rel_path),
                            remote_path=cls._join_remote(remote_base, rel_path),
                            reason="Remote file modified"
                        ))
                    elif local_changed and remote_changed:
                        # Conflict: use newer file
                        if local_file.modify_time >= remote_file.modify_time:
                            operations.append(SyncOperation(
                                action=SyncAction.UPLOAD, file_info=local_file,
                                local_path=cls._join_local(local_base, rel_path),
                                remote_path=cls._join_remote(remote_base, rel_path),
                                reason="Conflict resolved: local is newer"
                            ))
                        else:
                            operations.append(SyncOperation(
                                action=SyncAction.DOWNLOAD, file_info=remote_file,
                                local_path=cls._join_local(local_base, rel_path),
                                remote_path=cls._join_remote(remote_base, rel_path),
                                reason="Conflict resolved: remote is newer"
                            ))
                    else:
                        operations.append(SyncOperation(action=SyncAction.SKIP, reason="No changes"))
                elif local_file and not remote_file:
                    if last:
                        # Deleted remotely -> delete locally
                        operations.append(SyncOperation(
                            action=SyncAction.DELETE_LOCAL,
                            local_path=cls._join_local(local_base, rel_path),
                            reason="Deleted on remote"
                        ))
                    else:
                        # New local file -> upload
                        operations.append(SyncOperation(
                            action=SyncAction.UPLOAD, file_info=local_file,
                            local_path=cls._join_local(local_base, rel_path),
                            remote_path=cls._join_remote(remote_base, rel_path),
                            reason="New local file"
                        ))
                elif remote_file and not local_file:
                    if last:
                        # Deleted locally -> delete remotely
                        operations.append(SyncOperation(
                            action=SyncAction.DELETE_REMOTE,
                            remote_path=cls._join_remote(remote_base, rel_path),
                            file_info=remote_file,
                            reason="Deleted locally"
                        ))
                    else:
                        # New remote file -> download
                        operations.append(SyncOperation(
                            action=SyncAction.DOWNLOAD, file_info=remote_file,
                            local_path=cls._join_local(local_base, rel_path),
                            remote_path=cls._join_remote(remote_base, rel_path),
                            reason="New remote file"
                        ))

            elif sync_mode == "upload_only":
                if local_file:
                    if not remote_file or local_changed:
                        operations.append(SyncOperation(
                            action=SyncAction.UPLOAD, file_info=local_file,
                            local_path=cls._join_local(local_base, rel_path),
                            remote_path=cls._join_remote(remote_base, rel_path),
                            reason="Upload sync"
                        ))
                    else:
                        operations.append(SyncOperation(action=SyncAction.SKIP, reason="No changes"))

            elif sync_mode == "download_only":
                if remote_file:
                    if not local_file or remote_changed:
                        operations.append(SyncOperation(
                            action=SyncAction.DOWNLOAD, file_info=remote_file,
                            local_path=cls._join_local(local_base, rel_path),
                            remote_path=cls._join_remote(remote_base, rel_path),
                            reason="Download sync"
                        ))
                    else:
                        operations.append(SyncOperation(action=SyncAction.SKIP, reason="No changes"))

        return operations

    @classmethod
    async def _execute_op(cls, op: SyncOperation, provider: CloudProvider,
                          task_id: int, semaphore: asyncio.Semaphore,
                          progress: SyncProgress, local_base: str,
                          remote_base: str) -> str:
        """Execute a single sync operation."""
        async with semaphore:
            # Check if stop was requested (e.g. user paused the task)
            if task_id in cls._stop_requested:
                return "skipped"

            if op.action == SyncAction.SKIP:
                progress.skipped_files += 1
                return "skipped"

            try:
                if op.action == SyncAction.UPLOAD:
                    file_size = os.path.getsize(op.local_path)
                    transfer_id = progress._next_transfer_id
                    progress._next_transfer_id += 1
                    transfer = ActiveTransfer(
                        transfer_id=transfer_id,
                        file_path=op.local_path,
                        action="upload",
                        file_size=file_size,
                        started_at=time.time(),
                    )
                    progress.active_transfers[transfer_id] = transfer

                    # Ensure remote directory exists
                    remote_dir = provider.parent_path(op.remote_path)
                    if remote_dir and remote_dir != "/" and remote_dir != remote_base:
                        try:
                            await provider.create_directory(remote_dir)
                        except Exception:
                            pass  # Directory might already exist

                    try:
                        # Check if this transfer was cancelled before starting
                        if cls.is_transfer_cancelled(task_id, transfer_id):
                            progress.skipped_files += 1
                            await db.db_add_log(task_id, "skip", op.remote_path,
                                "Skipped (cancelled by user)")
                            return "skipped"

                        await provider.upload_file(op.local_path, op.remote_path,
                            progress_callback=cls._make_callback(progress, transfer_id, task_id),
                            _transfer=transfer)
                        progress.transferred_bytes += file_size
                        await db.db_add_log(task_id, "upload", op.remote_path,
                            f"Uploaded {op.local_path}")
                        return "success"
                    finally:
                        progress.active_transfers.pop(transfer_id, None)

                elif op.action == SyncAction.DOWNLOAD:
                    file_size = op.file_info.size if op.file_info else 0
                    transfer_id = progress._next_transfer_id
                    progress._next_transfer_id += 1
                    transfer = ActiveTransfer(
                        transfer_id=transfer_id,
                        file_path=op.remote_path,
                        action="download",
                        file_size=file_size,
                        started_at=time.time(),
                    )
                    progress.active_transfers[transfer_id] = transfer

                    # Ensure local directory exists
                    local_dir = os.path.dirname(op.local_path)
                    if local_dir:
                        os.makedirs(local_dir, exist_ok=True)

                    try:
                        # Check if this transfer was cancelled before starting
                        if cls.is_transfer_cancelled(task_id, transfer_id):
                            progress.skipped_files += 1
                            await db.db_add_log(task_id, "skip", op.remote_path,
                                "Skipped (cancelled by user)")
                            return "skipped"

                        await provider.download_file(op.remote_path, op.local_path,
                            progress_callback=cls._make_callback(progress, transfer_id, task_id))
                        progress.transferred_bytes += os.path.getsize(op.local_path)
                        await db.db_add_log(task_id, "download", op.remote_path,
                            f"Downloaded to {op.local_path}")
                        return "success"
                    finally:
                        progress.active_transfers.pop(transfer_id, None)

                elif op.action == SyncAction.DELETE_LOCAL:
                    if os.path.exists(op.local_path):
                        os.remove(op.local_path)
                    await db.db_add_log(task_id, "delete_local", op.local_path,
                        "Deleted locally (was deleted on remote)")
                    return "success"

                elif op.action == SyncAction.DELETE_REMOTE:
                    await provider.delete_file(op.remote_path)
                    await db.db_add_log(task_id, "delete_remote", op.remote_path,
                        "Deleted on remote (was deleted locally)")
                    return "success"

            except asyncio.CancelledError as e:
                # Transfer was cancelled (by user or by stop request)
                await db.db_add_log(task_id, "skip", op.remote_path or op.local_path,
                    f"Cancelled: {e}")
                progress.skipped_files += 1
                return "skipped"
            except Exception as e:
                await db.db_add_log(task_id, "error", op.remote_path or op.local_path,
                    f"{op.action.value} failed: {e}")
                progress.failed_files += 1
                raise

            return "skipped"

    @classmethod
    def _make_callback(cls, progress: SyncProgress, transfer_id: int,
                       task_id: int = 0) -> Callable:
        """Create a progress callback for a specific concurrent file transfer.

        If task_id is provided, the callback will check for cancellation
        and stop requests, raising asyncio.CancelledError to abort the transfer.
        """
        async def callback(current: int, total: int):
            # Check if this transfer was cancelled by the user
            if task_id and cls.is_transfer_cancelled(task_id, transfer_id):
                raise asyncio.CancelledError(f"Transfer {transfer_id} cancelled by user")
            # Check if the entire sync was stopped (e.g. task paused)
            if task_id and task_id in cls._stop_requested:
                raise asyncio.CancelledError(f"Task {task_id} stopped")

            transfer = progress.active_transfers.get(transfer_id)
            if transfer:
                transfer.transferred = current
                if total > 0:
                    transfer.file_size = total
            # Calculate aggregate speed from all active transfers
            now = time.time()
            if progress._speed_last_time > 0:
                dt = now - progress._speed_last_time
                if dt >= 0.5:  # Update speed every 0.5s
                    active_total = sum(t.transferred for t in progress.active_transfers.values())
                    total_now = progress.transferred_bytes + active_total
                    delta = total_now - progress._speed_last_bytes
                    progress.speed = delta / dt if dt > 0 else 0
                    progress._speed_last_time = now
                    progress._speed_last_bytes = total_now
            elif progress._speed_last_time == 0:
                progress._speed_last_time = now
                active_total = sum(t.transferred for t in progress.active_transfers.values())
                progress._speed_last_bytes = progress.transferred_bytes + active_total
        return callback

    @classmethod
    async def _scan_local(cls, base_path: str, include: list[str],
                          exclude: list[str], max_size: int) -> list[FileInfo]:
        """Scan local directory and return list of FileInfo objects."""
        result = []
        for root, dirs, files in os.walk(base_path):
            for filename in files:
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, base_path)
                rel_path = "/" + rel_path.replace(os.sep, "/")

                # Apply filters
                if exclude and any(fnmatch.fnmatch(filename, pat) for pat in exclude):
                    continue
                if include and not any(fnmatch.fnmatch(filename, pat) for pat in include):
                    continue

                try:
                    stat = os.stat(full_path)
                    if max_size and stat.st_size > max_size:
                        continue
                    result.append(FileInfo(
                        path=full_path,
                        name=filename,
                        is_dir=False,
                        size=stat.st_size,
                        modify_time=stat.st_mtime,
                    ))
                except OSError:
                    continue
        return result

    @classmethod
    async def _update_sync_state(cls, task_id: int, local_map: dict,
                                  remote_map: dict, local_base: str,
                                  remote_base: str):
        """Update the sync state database with current file states."""
        for rel_path, local_file in local_map.items():
            remote_file = remote_map.get(rel_path)
            file_info = local_file if local_file else remote_file
            if file_info:
                await db.db_upsert_sync_state(
                    task_id, rel_path,
                    file_info.size, file_info.modify_time,
                    file_info.md5, "both" if (local_file and remote_file) else
                    ("local" if local_file else "remote")
                )

    @staticmethod
    def _resolve_local_base(local_path: str) -> str:
        """Resolve the local base directory from a task's local_path.

        Supports paths starting with /sync (default) or /downloads.
        Returns the absolute filesystem path.
        """
        if not local_path:
            return str(LOCAL_SYNC_ROOT)
        # Check if path starts with /downloads
        if local_path.startswith("/downloads"):
            sub = local_path[len("/downloads"):]
            return os.path.join(str(DOWNLOAD_DIR), sub.lstrip("/"))
        elif local_path.startswith("/sync"):
            sub = local_path[len("/sync"):]
            return os.path.join(str(LOCAL_SYNC_ROOT), sub.lstrip("/"))
        else:
            # Default: treat as relative to sync root
            return os.path.join(str(LOCAL_SYNC_ROOT), local_path.lstrip("/"))

    @staticmethod
    def _rel_path(full_path: str, base: str) -> str:
        """Get relative path from full path, normalized with / prefix."""
        rel = os.path.relpath(full_path, base)
        rel = rel.replace(os.sep, "/")
        if not rel.startswith("/"):
            rel = "/" + rel
        return rel

    @staticmethod
    def _join_remote(base: str, rel: str) -> str:
        """Join remote base path with relative path."""
        if not rel or rel == "/":
            return base
        rel = rel.lstrip("/")
        base = base.rstrip("/")
        return f"{base}/{rel}"

    @staticmethod
    def _join_local(base: str, rel: str) -> str:
        """Join local base path with relative path.

        Unlike os.path.join, this function correctly handles rel paths that
        start with '/' (which os.path.join treats as absolute, dropping base).
        """
        if not rel or rel == "/":
            return base
        rel = rel.lstrip("/").replace("/", os.sep)
        base = base.rstrip(os.sep)
        return os.path.join(base, rel)
