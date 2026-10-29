"""Core sync engine - handles file synchronization between local and cloud storage.

Supports three sync modes:
- bidirectional: two-way sync
- upload_only: local -> cloud only
- download_only: cloud -> local only
"""

import asyncio
import logging
import os
import fnmatch
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

from app.providers.base import CloudProvider, FileInfo
from app import database as db
from app.config import LOCAL_SYNC_ROOT, DOWNLOAD_DIR, SYNC_CONCURRENCY
from app.services.notify import notifier
from app.services.retry import retry_async
from app.services.traffic import traffic
from app.utils.helpers import get_provider

# Filenames that are never worth syncing, regardless of what the task filters
# say. macOS drops .DS_Store / ._* on every folder it browses over SMB, Office
# leaves ~$ lock files behind, and browsers/our own downloader leave .part and
# .tmp files around. Pushing those to the cloud is pure noise and, in the case
# of .part, would upload a half-downloaded file.
# How many files one task transfers at the same time. The value lives in the
# settings table so the UI can change it without a restart; the environment
# variable only supplies the default for a fresh install.
CONCURRENCY_SETTING_KEY = "sync_concurrency"
CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 16


def parse_concurrency(raw, fallback: int = None) -> int:
    """Turn whatever is stored in the settings table into a usable number.

    Anything unparsable or out of range falls back to the configured default
    instead of raising: a bad row must never stop every sync from running.
    """
    default = SYNC_CONCURRENCY if fallback is None else fallback
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, default))
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, value))


DEFAULT_EXCLUDE_PATTERNS = (
    ".DS_Store", "._*", ".Spotlight-V100", ".Trashes", ".fseventsd",
    "Thumbs.db", "desktop.ini", "ehthumbs.db",
    "~$*", ".~lock.*",
    "*.tmp", "*.part", "*.crdownload", "*.partial",
)


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
    # Path relative to the sync root. Only used for bookkeeping (per-folder
    # notification counters): SKIP placeholders carry no local_path, so without
    # this they could not be attributed to the folder they belong to.
    rel_path: str = ""


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
    # total_files counts only files that actually need work (upload/download/
    # delete). Files that are already in sync are reported separately as
    # noop_files, otherwise the overall progress bar would start at 99% on any
    # incremental run and the "N/M" label would contradict the percentage.
    total_files: int = 0
    noop_files: int = 0
    processed_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    total_bytes: int = 0
    transferred_bytes: int = 0
    # Same bytes split by direction, so the dashboard can show an upload and a
    # download rate independently. ``transferred_bytes`` stays the sum of the
    # two and is what the progress bar / history use.
    transferred_upload: int = 0
    transferred_download: int = 0
    speed: float = 0.0
    started_at: float = 0.0
    is_running: bool = False
    # "scanning" while walking the local tree / listing the cloud,
    # "executing" once the operation list is known, "done" afterwards.
    phase: str = "idle"
    # Concurrent transfer tracking: transfer_id -> ActiveTransfer
    active_transfers: dict = field(default_factory=dict)
    _next_transfer_id: int = 0
    # Speed tracking internals
    _speed_last_time: float = 0.0
    _speed_last_bytes: int = 0

    @property
    def completed_files(self) -> int:
        """Files whose operation is over (synced + failed + skipped)."""
        return self.processed_files + self.failed_files + self.skipped_files

    @property
    def percent(self) -> int:
        if not self.is_running:
            return 100
        if self.total_files <= 0:
            return 0
        return min(100, round(self.completed_files / self.total_files * 100))


logger = logging.getLogger(__name__)


async def _add_record(ctx: Optional[dict], action: str, source: str,
                      target: str, file_size: int = 0,
                      status: str = "success", error: str = "") -> None:
    """Write one row to sync_records.

    Deliberately best-effort: the history panel is a convenience, so a failed
    insert (disk full, schema from an older image, ...) must never abort the
    transfer that just succeeded.
    """
    try:
        await db.db_add_record(
            (ctx or {}).get("task_id", 0), action,
            task_name=(ctx or {}).get("task_name", ""),
            connection=(ctx or {}).get("connection", ""),
            file_name=os.path.basename((target or source).rstrip("/")),
            source=source or "",
            target=target or "",
            file_size=file_size,
            status=status,
            error=error,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not write sync record for %s", target, exc_info=True)


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
    # Strong references to fire-and-forget sync tasks. asyncio only keeps a
    # weak reference to a task, so without this a background sync can be
    # garbage collected mid-execution.
    _bg_tasks: set = set()

    @classmethod
    def spawn(cls, task_id: int, **kwargs) -> "asyncio.Task":
        """Start a sync in the background, keeping a reference to the task."""
        t = asyncio.create_task(cls.sync_task(task_id, **kwargs))
        cls._bg_tasks.add(t)
        t.add_done_callback(cls._bg_tasks.discard)
        return t

    # Tasks that must get another full cycle once the current one finishes.
    # A "retry this file" click during a running realtime sync used to assume
    # the file would join the cycle in flight — but that cycle computed its
    # operation list before the click, and a quiet directory never generates
    # another event, so the file waited indefinitely for the next one.
    _pending_rescan: set[int] = set()

    @classmethod
    def request_sync(cls, task_id: int, **kwargs) -> str:
        """Guarantee one full scan of this task, sooner or later.

        Returns "started" when a cycle was spawned right away, "queued" when
        it will run right after the current one finishes.
        """
        if task_id not in cls._running_tasks:
            cls.spawn(task_id, **kwargs)
            return "started"

        if task_id in cls._pending_rescan:
            return "queued"  # someone already asked; one extra cycle suffices

        cls._pending_rescan.add(task_id)

        async def _waiter():
            try:
                while task_id in cls._running_tasks:
                    await asyncio.sleep(0.5)
                cls._pending_rescan.discard(task_id)
                # sync_task refuses concurrent runs; if something else snuck
                # in during the handover it returns immediately and the file
                # rides that cycle instead — same outcome either way.
                await cls.sync_task(task_id, **kwargs)
            except Exception:  # noqa: BLE001 - never crash the waiter
                cls._pending_rescan.discard(task_id)

        t = asyncio.create_task(_waiter())
        cls._bg_tasks.add(t)
        t.add_done_callback(cls._bg_tasks.discard)
        return "queued"

    @classmethod
    def get_progress(cls, task_id: int) -> Optional[SyncProgress]:
        return cls._progress.get(task_id)

    @classmethod
    def is_running(cls, task_id: int) -> bool:
        return task_id in cls._running_tasks

    @classmethod
    async def get_concurrency(cls) -> int:
        """Files transferred in parallel, per task.

        Read at the start of every cycle, so changing it in the UI applies to
        the next run without restarting the container.
        """
        raw = await db.db_get_setting(CONCURRENCY_SETTING_KEY, "")
        return parse_concurrency(raw)

    @classmethod
    def live_transferred(cls) -> tuple[int, int]:
        """Bytes sent / received so far by all running tasks, retries excluded.

        Includes in-flight transfers so the aggregate speed moves smoothly
        instead of jumping by a whole file each time one completes.
        """
        upload = download = 0
        for progress in cls._progress.values():
            if not progress.is_running:
                continue
            upload += progress.transferred_upload
            download += progress.transferred_download
            for transfer in progress.active_transfers.values():
                if transfer.action == "upload":
                    upload += transfer.transferred
                else:
                    download += transfer.transferred
        return upload, download

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
    async def sync_task(cls, task_id: int, write_grace: float = 0.0):
        """Execute a full sync cycle for a task.

        ``write_grace`` is forwarded to the scanner; only the real-time watcher
        sets it, because only that path can start a cycle while files are still
        being copied in.
        """
        if task_id in cls._running_tasks:
            return  # Already running

        cls._running_tasks.add(task_id)

        # Initialize progress
        progress = SyncProgress(task_id=task_id, started_at=time.time(),
                                is_running=True, phase="scanning")
        cls._progress[task_id] = progress

        task = await db.db_get_task(task_id)
        if not task:
            cls._running_tasks.discard(task_id)
            return

        history_id = await db.db_create_history(task_id)
        await db.db_update_task(task_id, status="running", last_error="")
        await db.db_add_log(task_id, "info", detail=f"Sync started: {task['name']}")

        try:
            # 提前给默认值：下面除了连接失败还会在更早的地方抛异常，
            # except 分支里的通知要用到它
            local_base = ""

            # Get provider
            connection = await db.db_get_connection(task["connection_id"])
            if not connection:
                raise RuntimeError("Connection not found")

            provider = get_provider(connection["type"], connection["credentials"],
                                    conn_id=connection["id"])
            if not await provider.test_connection():
                raise RuntimeError(f"Connection to {connection['name']} failed")

            # Snapshot of the labels every sync_records row carries, so the
            # history panel still reads correctly after the task is deleted.
            record_ctx = {
                "task_id": task_id,
                "task_name": task["name"],
                "connection": connection["name"],
            }

            sync_mode = task["sync_mode"]
            local_base = cls._resolve_local_base(task["local_path"])
            remote_base = task["remote_path"]

            # Ensure local directory exists
            os.makedirs(local_base, exist_ok=True)

            # Make sure the remote root exists BEFORE listing it. Without this,
            # a cloud-side deleted root makes list_all_files come back empty,
            # and "not in remote_map" then means "upload everything" on every
            # single run. Baidu also auto-creates the directory on upload, so a
            # failure here is logged but not fatal.
            if sync_mode in ("upload_only", "bidirectional"):
                try:
                    ok = await provider.create_directory(remote_base)
                    if not ok:
                        await db.db_add_log(task_id, "info",
                            detail=f"Remote dir {remote_base} could not be ensured; "
                                   f"uploads will create it on demand")
                except Exception as e:
                    await db.db_add_log(task_id, "info",
                        detail=f"Ensure remote dir {remote_base} skipped: {e}")

            # Get filters
            include_patterns = [p.strip() for p in task.get("filter_include", "").split(",") if p.strip()]
            exclude_patterns = [p.strip() for p in task.get("filter_exclude", "").split(",") if p.strip()]
            max_size = task.get("max_file_size", 0)

            # Scan local and remote files
            await db.db_add_log(task_id, "info", detail="Scanning local files...")
            local_files = await cls._scan_local(
                local_base, include_patterns, exclude_patterns, max_size,
                write_grace=write_grace)

            await db.db_add_log(task_id, "info", detail="Listing remote files...")
            remote_files = await provider.list_all_files(remote_base)

            # Build file maps keyed by relative path
            local_map = {cls._rel_path(f.path, local_base): f for f in local_files}
            remote_map = {cls._rel_path(f.path, remote_base): f for f in remote_files}

            # Get last sync state
            sync_state = await db.db_get_sync_state(task_id)
            last_state = {s["file_path"]: s for s in sync_state}

            # Files the user told us to leave alone, and files that are still
            # inside their retry backoff window. Both are decided per cycle, so
            # a realtime task stops re-offering them every few seconds.
            ignored = await db.db_get_ignored(task_id)
            failures = await db.db_get_failures(task_id)
            if failures:
                # A file edited after its last failure is a different problem
                # (or the same one fixed), so it deserves an immediate retry
                # instead of waiting out a backoff that can be an hour long.
                for rel, row in list(failures.items()):
                    lf = local_map.get(rel)
                    if lf and lf.modify_time > (row.get("last_error_at") or 0):
                        await db.db_clear_failure(task_id, rel)
                        failures.pop(rel, None)

            # Compute operations
            operations = cls._compute_operations(
                local_map, remote_map, last_state, sync_mode, local_base, remote_base,
                ignored=ignored, failures=failures,
            )

            # Only files that actually need work count towards the progress
            # bar. "No changes" placeholders are reported separately as
            # noop_files and are NOT executed: running them would bump
            # skipped_files for every unchanged file, which is how the label
            # ended up reading "63/9 · 100% · 无变化 83".
            work_ops = cls._plan_operations(operations, progress)

            # 「无变化」的文件只计入通知里的数字，永远不会触发推送——否则
            # 一个安静的文件夹每轮扫描都会冒一条「同步完成 0 个文件」。
            # 这些是 SKIP 占位符、没有 local_path，靠 rel_path 归类到文件夹。
            notifier.record_noop(
                task_id=task_id, task_name=task["name"], local_base=local_base,
                rel_paths=[op.rel_path for op in operations
                           if op.action == SyncAction.SKIP
                           and op.reason == "No changes" and op.rel_path],
                cycle_started=progress.started_at)

            progress.phase = "executing"
            await db.db_add_log(task_id, "info", detail=(
                f"Found {progress.total_files} operations to execute "
                f"({progress.noop_files} unchanged)"))

            # Execute operations with a concurrency limit. Counters are bumped
            # per finished operation instead of after asyncio.gather returns:
            # gather only resolves when *every* operation is done, which used
            # to leave the overall progress frozen at 0% for the whole run and
            # then jump straight to 100%.
            # Files are transferred with a per-task concurrency limit. The
            # number comes from the settings table (UI), so a change applies
            # to the next cycle without restarting the container.
            concurrency = await cls.get_concurrency()
            semaphore = asyncio.Semaphore(concurrency)

            def _track(op: SyncOperation, ok: bool) -> None:
                """把一个文件的结果计入通知汇总。

                纯内存操作，同步引擎照常往下跑；真正的 HTTP 推送在通知服务
                的后台任务里做，网关挂了也不会影响同步。
                """
                if op.action in (SyncAction.MKDIR_LOCAL, SyncAction.MKDIR_REMOTE):
                    return  # 建目录不算「传了东西」
                try:
                    if op.local_path:
                        rel = cls._rel_path(op.local_path, local_base)
                    elif op.remote_path:
                        rel = cls._rel_path(op.remote_path, remote_base)
                    else:
                        return
                    notifier.record(
                        task_id=task_id, task_name=task["name"],
                        local_base=local_base, rel_path=rel,
                        action=op.action.value, ok=ok,
                        size=(op.file_info.size or 0) if op.file_info else 0,
                        cycle_started=progress.started_at)
                except Exception:  # noqa: BLE001 - 统计不能拖垮同步
                    logger.debug("notify track failed for %s", op.local_path,
                                 exc_info=True)

            async def _run_one(op: SyncOperation):
                try:
                    result = await cls._execute_op(
                        op, provider, task_id, semaphore, progress,
                        local_base, remote_base, record_ctx)
                except asyncio.CancelledError:
                    progress.skipped_files += 1
                    raise
                except Exception as exc:  # noqa: BLE001 - _execute_op already logged it
                    progress.failed_files += 1
                    _track(op, ok=False)
                    return exc
                if result == "success":
                    progress.processed_files += 1
                    _track(op, ok=True)
                elif result == "skipped":
                    progress.skipped_files += 1
                return result

            await asyncio.gather(
                *[_run_one(op) for op in work_ops],
                return_exceptions=True
            )

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
                # "skipped" for the history keeps its old meaning: everything
                # that was not transferred, unchanged files included.
                files_skipped=progress.skipped_files + progress.noop_files,
                bytes_transferred=progress.transferred_bytes
            )
            await db.db_add_log(task_id, "info", detail=
                f"Sync completed: {progress.processed_files} synced, "
                f"{progress.failed_files} failed, {progress.skipped_files} skipped, "
                f"{progress.noop_files} unchanged"
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
                files_skipped=progress.skipped_files + progress.noop_files,
                bytes_transferred=progress.transferred_bytes
            )
            await db.db_add_log(task_id, "error", detail=f"Sync error: {error_msg}")
            # 整轮报错：这个任务待推送的文件夹立刻发出去（带上错误信息），
            # 不再等静默期——传到一半断了更要马上让人知道。
            try:
                notifier.abort_task(task_id, error_msg,
                                    local_base=local_base or task["local_path"],
                                    task_name=task["name"])
            except Exception:  # noqa: BLE001
                logger.debug("notify abort failed", exc_info=True)

        finally:
            progress.is_running = False
            progress.phase = "done"
            cls._running_tasks.discard(task_id)
            cls._stop_requested.discard(task_id)
            cls._cancelled_transfers.pop(task_id, None)
            cls._transfer_samples.pop(task_id, None)
            try:
                await provider.close()
            except Exception:
                pass

    @classmethod
    def _plan_operations(cls, operations: list[SyncOperation],
                         progress: SyncProgress) -> list[SyncOperation]:
        """Split the operation list into real work and "no changes" placeholders.

        Fills ``total_files`` / ``noop_files`` / ``total_bytes`` on ``progress``
        and returns only the operations that need to be executed.

        Anything that is not executed must stay out of ``total_files`` *and*
        out of the per-op counters, otherwise ``completed_files`` outgrows the
        denominator and the UI shows nonsense like "63/9 · 100%".
        """
        work_ops = [op for op in operations if op.action != SyncAction.SKIP]
        progress.total_files = len(work_ops)
        progress.noop_files = len(operations) - len(work_ops)
        progress.total_bytes = sum(
            (op.file_info.size or 0) for op in work_ops
            if op.file_info is not None
            and op.action in (SyncAction.UPLOAD, SyncAction.DOWNLOAD))
        return work_ops

    @classmethod
    def _compute_operations(cls, local_map: dict, remote_map: dict,
                            last_state: dict, sync_mode: str,
                            local_base: str, remote_base: str,
                            ignored: Optional[set] = None,
                            failures: Optional[dict] = None) -> list[SyncOperation]:
        """Compute sync operations based on file comparison.

        ``ignored`` are relative paths the user explicitly skipped, and
        ``failures`` holds the backoff state per relative path. Both make the
        file a SKIP instead of an operation, which is what stops a realtime
        task from retrying a doomed file on every cycle.
        """
        operations = []
        all_paths = set(local_map.keys()) | set(remote_map.keys())
        ignored = ignored or set()
        failures = failures or {}
        now = time.time()

        for rel_path in sorted(all_paths):
            local_file = local_map.get(rel_path)
            remote_file = remote_map.get(rel_path)
            last = last_state.get(rel_path)

            if rel_path in ignored:
                operations.append(SyncOperation(
                    action=SyncAction.SKIP, reason="Ignored by user"))
                continue

            fail = failures.get(rel_path)
            if fail and float(fail.get("next_retry_at") or 0) > now:
                operations.append(SyncOperation(
                    action=SyncAction.SKIP, reason="Waiting for retry"))
                continue

            local_changed = False
            remote_changed = False

            if local_file and last:
                if local_file.size != last.get("file_size", 0) or \
                   abs(local_file.modify_time - last.get("file_mtime", 0)) > 2:
                    local_changed = True
            elif local_file and not last:
                local_changed = True  # New file

            if remote_file and last:
                if remote_file.size != last.get("file_size", 0):
                    remote_changed = True
                else:
                    last_md5 = (last.get("file_md5") or "").strip()
                    remote_md5 = (getattr(remote_file, "md5", "") or "").strip()
                    if last_md5 and remote_md5:
                        # md5 wins over mtime: the cloud stamps the upload time
                        # on the file, so a freshly uploaded file would always
                        # look "changed" if we compared timestamps.
                        remote_changed = remote_md5 != last_md5
                    elif abs(remote_file.modify_time - last.get("file_mtime", 0)) > 2:
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
                        operations.append(SyncOperation(action=SyncAction.SKIP, reason="No changes", rel_path=rel_path))
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
                        operations.append(SyncOperation(action=SyncAction.SKIP, reason="No changes", rel_path=rel_path))

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
                        operations.append(SyncOperation(action=SyncAction.SKIP, reason="No changes", rel_path=rel_path))

        return operations

    @classmethod
    async def _execute_op(cls, op: SyncOperation, provider: CloudProvider,
                          task_id: int, semaphore: asyncio.Semaphore,
                          progress: SyncProgress, local_base: str,
                          remote_base: str,
                          record_ctx: Optional[dict] = None) -> str:
        """Execute a single sync operation.

        Returns "success" / "skipped" (or raises). Progress counters are bumped
        by the caller from that return value — never here, or every operation
        would be counted twice (a 29-file cycle used to report 58 skipped).
        """
        async with semaphore:
            # Check if stop was requested (e.g. user paused the task)
            if task_id in cls._stop_requested:
                return "skipped"

            if op.action == SyncAction.SKIP:
                return "skipped"

            try:
                if op.action == SyncAction.UPLOAD:
                    try:
                        file_size = os.path.getsize(op.local_path)
                    except OSError:
                        # The file vanished between the scan and now (moved,
                        # deleted, or a temp file that got cleaned up).
                        await db.db_add_log(task_id, "skip", op.remote_path,
                            "Skipped (file no longer exists)")
                        return "skipped"
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
                            ok = await provider.create_directory(remote_dir)
                            if not ok:
                                await db.db_add_log(task_id, "info", remote_dir,
                                    "Remote directory could not be created; "
                                    "the upload below may fail")
                        except Exception as e:
                            await db.db_add_log(task_id, "info", remote_dir,
                                f"create_directory failed: {e}")

                    try:
                        # Check if this transfer was cancelled before starting
                        if cls.is_transfer_cancelled(task_id, transfer_id):
                            await db.db_add_log(task_id, "skip", op.remote_path,
                                "Skipped (cancelled by user)")
                            return "skipped"

                        def _reset_progress(_attempt_no, _exc):
                            # The retry starts the file from zero, so the
                            # speed/progress display must not keep bytes
                            # counted from the failed attempt.
                            transfer.transferred = 0

                        await retry_async(
                            lambda: provider.upload_file(
                                op.local_path, op.remote_path,
                                progress_callback=cls._make_callback(
                                    progress, transfer_id, task_id),
                                _transfer=transfer),
                            what=f"upload {os.path.basename(op.local_path)}",
                            on_retry=_reset_progress,
                        )
                        # Pop before counting: the live per-direction totals
                        # must never hold the same bytes twice in one sample.
                        progress.active_transfers.pop(transfer_id, None)
                        progress.transferred_bytes += file_size
                        progress.transferred_upload += file_size
                        traffic.add("upload", file_size)
                        await db.db_add_log(task_id, "upload", op.remote_path,
                            f"Uploaded {op.local_path}")
                        await _add_record(record_ctx, "upload", op.local_path,
                                          op.remote_path, file_size)
                        try:
                            await db.db_clear_failure(
                                task_id, cls._rel_path(op.local_path, local_base))
                        except Exception:  # noqa: BLE001 - bookkeeping only
                            logger.debug("Could not clear failure for %s",
                                         op.local_path, exc_info=True)
                        # Record state right away: the maps used at the end of
                        # the cycle are pre-transfer snapshots, so a completed
                        # upload would otherwise stay unrecorded if the run is
                        # interrupted or the remote listing lags behind.
                        try:
                            st = os.stat(op.local_path)
                            await db.db_upsert_sync_state(
                                task_id, cls._rel_path(op.local_path, local_base),
                                st.st_size, st.st_mtime, "", "local")
                        except OSError:
                            pass
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
                            await db.db_add_log(task_id, "skip", op.remote_path,
                                "Skipped (cancelled by user)")
                            return "skipped"

                        def _reset_progress(_attempt_no, _exc):
                            transfer.transferred = 0

                        await retry_async(
                            lambda: provider.download_file(
                                op.remote_path, op.local_path,
                                progress_callback=cls._make_callback(
                                    progress, transfer_id, task_id)),
                            what=f"download {os.path.basename(op.remote_path)}",
                            on_retry=_reset_progress,
                        )
                        done_size = os.path.getsize(op.local_path)
                        progress.active_transfers.pop(transfer_id, None)
                        progress.transferred_bytes += done_size
                        progress.transferred_download += done_size
                        traffic.add("download", done_size)
                        await db.db_add_log(task_id, "download", op.remote_path,
                            f"Downloaded to {op.local_path}")
                        await _add_record(record_ctx, "download", op.remote_path,
                                          op.local_path, done_size)
                        try:
                            await db.db_clear_failure(
                                task_id, cls._rel_path(op.remote_path, remote_base))
                        except Exception:  # noqa: BLE001 - bookkeeping only
                            logger.debug("Could not clear failure for %s",
                                         op.remote_path, exc_info=True)
                        # Same reasoning as the upload path: persist the state
                        # immediately and carry the remote md5 over, so the
                        # next cycle can tell "already downloaded" apart from
                        # "changed on the cloud" without relying on mtimes.
                        try:
                            st = os.stat(op.local_path)
                            await db.db_upsert_sync_state(
                                task_id, cls._rel_path(op.local_path, local_base),
                                st.st_size, st.st_mtime,
                                (op.file_info.md5 if op.file_info else "") or "",
                                "both")
                        except OSError:
                            pass
                        return "success"
                    finally:
                        progress.active_transfers.pop(transfer_id, None)

                elif op.action == SyncAction.DELETE_LOCAL:
                    size = 0
                    if os.path.exists(op.local_path):
                        try:
                            size = os.path.getsize(op.local_path)
                        except OSError:
                            size = 0
                        os.remove(op.local_path)
                    await db.db_add_log(task_id, "delete_local", op.local_path,
                        "Deleted locally (was deleted on remote)")
                    await _add_record(record_ctx, "delete_local", op.remote_path,
                                      op.local_path, size)
                    return "success"

                elif op.action == SyncAction.DELETE_REMOTE:
                    await provider.delete_file(op.remote_path)
                    await db.db_add_log(task_id, "delete_remote", op.remote_path,
                        "Deleted on remote (was deleted locally)")
                    await _add_record(record_ctx, "delete_remote", op.local_path,
                                      op.remote_path,
                                      op.file_info.size if op.file_info else 0)
                    return "success"

            except asyncio.CancelledError as e:
                # Transfer was cancelled (by user or by stop request)
                await db.db_add_log(task_id, "skip", op.remote_path or op.local_path,
                    f"Cancelled: {e}")
                return "skipped"
            except Exception as e:
                await db.db_add_log(task_id, "error", op.remote_path or op.local_path,
                    f"{op.action.value} failed: {e}")
                if op.action in (SyncAction.UPLOAD, SyncAction.DOWNLOAD,
                                 SyncAction.DELETE_LOCAL, SyncAction.DELETE_REMOTE):
                    if op.action in (SyncAction.UPLOAD, SyncAction.DELETE_REMOTE):
                        src, dst = op.local_path, op.remote_path
                    else:
                        src, dst = op.remote_path, op.local_path
                    await _add_record(record_ctx, op.action.value, src, dst,
                                      status="failed", error=str(e))
                # Remember the failure so the next cycle can back off instead
                # of hammering the same file (and the same record) again.
                if op.action in (SyncAction.UPLOAD, SyncAction.DOWNLOAD):
                    rel = cls._rel_path(
                        op.local_path if op.action == SyncAction.UPLOAD else op.remote_path,
                        local_base if op.action == SyncAction.UPLOAD else remote_base)
                    try:
                        await db.db_mark_failure(task_id, rel, str(e))
                    except Exception:  # noqa: BLE001 - bookkeeping only
                        logger.debug("Could not record failure for %s", rel,
                                     exc_info=True)
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
                    # A file finishing moves its bytes from active to
                    # transferred_bytes a moment later, which can make the
                    # aggregate dip below the previous sample. Never show a
                    # negative rate.
                    progress.speed = max(0.0, delta / dt) if dt > 0 else 0
                    progress._speed_last_time = now
                    progress._speed_last_bytes = total_now
            elif progress._speed_last_time == 0:
                progress._speed_last_time = now
                active_total = sum(t.transferred for t in progress.active_transfers.values())
                progress._speed_last_bytes = progress.transferred_bytes + active_total
        return callback

    @classmethod
    async def _scan_local(cls, base_path: str, include: list[str],
                          exclude: list[str], max_size: int,
                          write_grace: float = 0.0) -> list[FileInfo]:
        """Scan local directory and return list of FileInfo objects.

        ``write_grace``: skip files modified within the last N seconds. Used by
        real-time cycles that are allowed to start before a big copy has
        settled — a partially written photo would otherwise be uploaded and
        then re-uploaded once it is complete.
        """
        result = []
        cutoff = (time.time() - write_grace) if write_grace > 0 else 0.0
        for root, dirs, files in os.walk(base_path):
            for filename in files:
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, base_path)
                rel_path = "/" + rel_path.replace(os.sep, "/")

                # Apply filters
                if any(fnmatch.fnmatch(filename, pat) for pat in DEFAULT_EXCLUDE_PATTERNS):
                    continue
                if exclude and any(fnmatch.fnmatch(filename, pat) for pat in exclude):
                    continue
                if include and not any(fnmatch.fnmatch(filename, pat) for pat in include):
                    continue

                try:
                    stat = os.stat(full_path)
                    if max_size and stat.st_size > max_size:
                        continue
                    if cutoff and stat.st_mtime > cutoff:
                        continue  # still being written
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
        """Persist the state of every path seen this cycle.

        Iterating only ``local_map`` used to leave remote-only files
        unrecorded, which made download_only tasks re-download everything on
        every run. ``file_size`` / ``file_mtime`` describe the local side
        (used to detect local edits), ``file_md5`` describes the remote side
        (used to detect remote edits without trusting server mtimes).
        """
        for rel_path in sorted(set(local_map) | set(remote_map)):
            local_file = local_map.get(rel_path)
            remote_file = remote_map.get(rel_path)
            remote_md5 = (getattr(remote_file, "md5", "") or "") if remote_file else ""
            if local_file and remote_file:
                size, mtime = local_file.size, local_file.modify_time
                # The maps are pre-transfer snapshots, so a file that was
                # (re)downloaded during this cycle still carries its old
                # size/mtime here. Re-stat it, otherwise the next run thinks
                # the local copy changed and uploads it again.
                try:
                    st = os.stat(cls._join_local(local_base, rel_path))
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    pass
                await db.db_upsert_sync_state(
                    task_id, rel_path, size, mtime, remote_md5, "both")
            elif local_file:
                size, mtime = local_file.size, local_file.modify_time
                try:
                    st = os.stat(cls._join_local(local_base, rel_path))
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    pass
                await db.db_upsert_sync_state(
                    task_id, rel_path, size, mtime, "", "local")
            elif remote_file:
                await db.db_upsert_sync_state(
                    task_id, rel_path, remote_file.size, remote_file.modify_time,
                    remote_md5, "remote")

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
