"""Task scheduler - manages scheduled and real-time sync tasks."""

import asyncio
import fnmatch
import os
import time
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from app.services.sync_engine import SyncEngine, DEFAULT_EXCLUDE_PATTERNS
from app import database as db
from app.config import LOCAL_SYNC_ROOT, DOWNLOAD_DIR, REALTIME_MAX_DELAY, REALTIME_WRITE_GRACE
import logging

logger = logging.getLogger(__name__)


class FileChangeHandler(FileSystemEventHandler):
    """Handles local file change events for real-time sync."""

    def __init__(self, task_id: int, local_path: str, debounce_seconds: int = 5,
                 max_delay: Optional[float] = None,
                 write_grace: Optional[float] = None):
        self.task_id = task_id
        self.local_path = local_path
        self.debounce_seconds = debounce_seconds
        # Longest time a batch may be postponed by a never-ending stream of
        # events (copying hundreds of GB). 0 = wait for the tree to settle.
        self.max_delay = REALTIME_MAX_DELAY if max_delay is None else max_delay
        self.write_grace = REALTIME_WRITE_GRACE if write_grace is None else write_grace
        self._last_trigger = 0.0
        self._batch_started = 0.0
        self._debounce_task: Optional[asyncio.Task] = None
        # Capture the main event loop here (runs in main thread).
        # watchdog's Observer runs callbacks in a separate thread where
        # asyncio.get_event_loop() would fail on Python 3.10+.
        self._loop = asyncio.get_event_loop()

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Ignore the files the sync engine never uploads anyway. Our own
        # downloads write "<name>.part" before the atomic rename, so without
        # this a failing download would keep re-triggering itself in a loop.
        name = os.path.basename(event.src_path or "")
        if any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_EXCLUDE_PATTERNS):
            return
        self._last_trigger = time.time()
        # Called from a watchdog observer thread -> hop onto the event loop.
        self._loop.call_soon_threadsafe(self._schedule_debounced_sync)

    def _schedule_debounced_sync(self):
        """Runs on the event loop thread.

        At most one debounce task exists. We deliberately do NOT cancel it:
        copying a large folder fires events every second, and cancelling the
        task would abort the sync it already started, so the transfer would
        restart forever and never finish. The debounce loop below re-checks
        the last event time instead, which coalesces the burst just as well.
        """
        if self._debounce_task and not self._debounce_task.done():
            return
        self._batch_started = time.time()
        self._debounce_task = self._loop.create_task(self._debounced_sync())

    def _quiet(self) -> bool:
        """True when no file event has arrived for the debounce window."""
        return time.time() - self._last_trigger >= self.debounce_seconds - 1

    def _settled(self) -> bool:
        """True when the current batch is allowed to run."""
        if self._quiet():
            return True
        # The tree is still changing. Copying a few hundred GB keeps firing
        # events for hours, so the sync would never start; cap the wait and
        # sync what has landed so far. Files still being written are skipped
        # by the scanner (write_grace), the rest is picked up next cycle.
        if self.max_delay > 0 and time.time() - self._batch_started >= self.max_delay:
            return True
        return False

    async def _debounced_sync(self):
        try:
            # After a cycle that started mid-copy we owe one catch-up pass,
            # because the scanner skipped files that were still being written.
            # That pass waits for the tree to go quiet instead of firing on the
            # max_delay cap again — otherwise the cap would start a fresh cycle
            # every 30s even when there is nothing left to do.
            catchup = False
            catchup_deadline = 0.0
            while True:
                if catchup:
                    def ready():
                        # Quiet, or a new copy showed up and the cap expired.
                        return self._quiet() or (
                            self.max_delay > 0 and time.time() >= catchup_deadline)
                else:
                    def ready():
                        return self._settled()
                # Wait until the burst of file events settles down (or the
                # batch has been pending longer than max_delay).
                while not ready():
                    await asyncio.sleep(1)
                # A sync may already be running for this task; let it finish so
                # the changes made meanwhile are picked up by the next cycle
                # instead of racing with it.
                while SyncEngine.is_running(self.task_id):
                    await asyncio.sleep(2)
                    while not ready():
                        await asyncio.sleep(1)
                forced = not self._quiet()
                self._batch_started = time.time()
                started = self._batch_started
                logger.info(f"File change detected, triggering sync for task {self.task_id}")
                await SyncEngine.sync_task(self.task_id, write_grace=self.write_grace)
                # Events that arrive while the sync runs are not queued
                # anywhere (_schedule_debounced_sync returns early because this
                # task is still alive). If the copy was still writing during
                # the cycle, run once more so those files are not missed.
                if self._last_trigger > started:
                    catchup = False
                    continue
                if forced:
                    catchup = True
                    catchup_deadline = time.time() + self.max_delay
                    continue
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Realtime sync for task {self.task_id} failed: {e}")

    def cancel(self):
        """Stop the debounce loop (called when the task is unscheduled)."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()


class SyncScheduler:
    """Manages scheduled and real-time sync tasks."""

    def __init__(self):
        self._scheduler = AsyncIOScheduler()
        self._observers: dict[int, Observer] = {}
        self._handlers: dict[int, FileChangeHandler] = {}
        self._started = False

    async def start(self):
        """Start the scheduler and load all tasks."""
        if self._started:
            return
        self._scheduler.start()
        self._started = True

        # Load all tasks and schedule them
        tasks = await db.db_list_tasks()
        for task in tasks:
            # Respect the paused state: without this, every container restart
            # silently re-enables tasks the user had explicitly paused.
            if task.get("status") == "paused":
                logger.info(f"Task {task['id']} is paused, not scheduling")
                continue
            await self.schedule_task(task)

        logger.info(f"Scheduler started with {len(tasks)} tasks")

    async def stop(self):
        """Stop the scheduler and all observers."""
        for observer in self._observers.values():
            observer.stop()
            # Never join without a timeout: a wedged observer thread would
            # otherwise block application shutdown forever.
            try:
                observer.join(timeout=2)
            except Exception:
                pass
        self._observers.clear()
        self._handlers.clear()
        if self._started:
            self._scheduler.shutdown()
            self._started = False

    async def schedule_task(self, task: dict):
        """Schedule or reschedule a task based on its configuration."""
        task_id = task["id"]
        schedule_type = task.get("schedule_type", "manual")

        # Remove existing schedule
        await self.unschedule_task(task_id)

        if schedule_type == "scheduled":
            interval = task.get("schedule_interval", 0)
            if interval > 0:
                self._scheduler.add_job(
                    SyncEngine.sync_task,
                    IntervalTrigger(seconds=interval),
                    args=[task_id],
                    id=f"task_{task_id}",
                    replace_existing=True,
                )
                logger.info(f"Task {task_id} scheduled every {interval}s")

        elif schedule_type == "realtime":
            local_base = self._resolve_local_base(task["local_path"])
            if os.path.exists(local_base):
                handler = FileChangeHandler(task_id, local_base)
                observer = Observer()
                observer.schedule(handler, local_base, recursive=True)
                observer.start()
                self._observers[task_id] = observer
                self._handlers[task_id] = handler
                logger.info(f"Task {task_id} watching {local_base} for changes")

                # Run initial sync in background — don't block app startup.
                # If we await here, uvicorn stays stuck on "Waiting for
                # application startup" and never starts listening on the port.
                # No write_grace here: the startup pass is the catch-up pass,
                # and a file skipped now might never get another event.
                SyncEngine.spawn(task_id)

    @staticmethod
    def _resolve_local_base(local_path: str) -> str:
        """Resolve local path to absolute filesystem path.

        Supports /sync and /downloads prefixes.
        """
        if not local_path:
            return str(LOCAL_SYNC_ROOT)
        if local_path.startswith("/downloads"):
            sub = local_path[len("/downloads"):]
            return os.path.join(str(DOWNLOAD_DIR), sub.lstrip("/"))
        elif local_path.startswith("/sync"):
            sub = local_path[len("/sync"):]
            return os.path.join(str(LOCAL_SYNC_ROOT), sub.lstrip("/"))
        else:
            return os.path.join(str(LOCAL_SYNC_ROOT), local_path.lstrip("/"))

    async def unschedule_task(self, task_id: int):
        """Remove a task from the scheduler."""
        # Remove scheduled job
        try:
            self._scheduler.remove_job(f"task_{task_id}")
        except Exception:
            pass

        # Stop file observer
        if task_id in self._observers:
            self._observers[task_id].stop()
            try:
                self._observers[task_id].join(timeout=2)
            except Exception:
                pass
            del self._observers[task_id]
        if task_id in self._handlers:
            self._handlers[task_id].cancel()
            del self._handlers[task_id]
        # A sync may still be running for this task; ask it to wind down
        # instead of letting it continue against an unscheduled task.
        if SyncEngine.is_running(task_id):
            SyncEngine.request_stop(task_id)


# Global scheduler instance
scheduler = SyncScheduler()
