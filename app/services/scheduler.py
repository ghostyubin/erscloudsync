"""Task scheduler - manages scheduled and real-time sync tasks."""

import asyncio
import os
import time
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from app.services.sync_engine import SyncEngine
from app import database as db
from app.config import LOCAL_SYNC_ROOT, DOWNLOAD_DIR
import logging

logger = logging.getLogger(__name__)


class FileChangeHandler(FileSystemEventHandler):
    """Handles local file change events for real-time sync."""

    def __init__(self, task_id: int, local_path: str, debounce_seconds: int = 5):
        self.task_id = task_id
        self.local_path = local_path
        self.debounce_seconds = debounce_seconds
        self._last_trigger = 0
        self._debounce_task: Optional[asyncio.Task] = None
        # Capture the main event loop here (runs in main thread).
        # watchdog's Observer runs callbacks in a separate thread where
        # asyncio.get_event_loop() would fail on Python 3.10+.
        self._loop = asyncio.get_event_loop()

    def on_any_event(self, event):
        if event.is_directory:
            return
        now = time.time()
        self._last_trigger = now
        # Cancel any pending debounce task
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        # Schedule on the main event loop thread-safely
        # (on_any_event is called from a watchdog observer thread)
        self._loop.call_soon_threadsafe(self._schedule_debounced_sync)

    def _schedule_debounced_sync(self):
        """Create the debounce task on the event loop thread."""
        self._debounce_task = self._loop.create_task(self._debounced_sync())

    async def _debounced_sync(self):
        try:
            await asyncio.sleep(self.debounce_seconds)
            if time.time() - self._last_trigger >= self.debounce_seconds - 1:
                logger.info(f"File change detected, triggering sync for task {self.task_id}")
                await SyncEngine.sync_task(self.task_id)
        except asyncio.CancelledError:
            pass


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
            await self.schedule_task(task)

        logger.info(f"Scheduler started with {len(tasks)} tasks")

    async def stop(self):
        """Stop the scheduler and all observers."""
        for observer in self._observers.values():
            observer.stop()
            observer.join()
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
                asyncio.create_task(SyncEngine.sync_task(task_id))

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
            del self._handlers[task_id]


# Global scheduler instance
scheduler = SyncScheduler()
