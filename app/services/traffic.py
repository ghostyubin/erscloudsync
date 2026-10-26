"""Cumulative traffic accounting plus a live throughput estimate.

Two different numbers are needed by the UI and they come from two different
places:

* **累计流量** — how many bytes have been pushed to / pulled from the cloud
  since the counters were last reset. This is persisted, so it survives
  restarts, and it is only incremented once per finished transfer (retries
  reset the per-file counter, so a re-uploaded file is never counted twice).
* **实时速度** — bytes per second right now. This is *not* derived from the
  cumulative counters (they only move when a file completes, which would make
  the rate jump from 0 to file_size in one sample). It is derived from the
  in-flight counters the sync engine exposes, sampled on demand whenever the
  API is read, and the UI polls about once a second.

The sync engine imports this module, so this module must not import the sync
engine at module level — ``_live()`` does it lazily.
"""

import time

from app.database import db_get_setting, db_set_setting

KEY_UPLOAD = "traffic_upload"
KEY_DOWNLOAD = "traffic_download"
KEY_SINCE = "traffic_since"

# A sample older than this is not used for the rate calculation: if the UI
# stops polling for a while (tab hidden, app closed) the next read would
# otherwise divide the idle gap into the delta and report a fake average.
SAMPLE_WINDOW = 4.0
# Minimum spacing between two samples; below this the timing noise dominates.
MIN_DT = 0.2
# Persist at most this often while bytes keep flowing.
FLUSH_INTERVAL = 5.0


class TrafficStats:
    upload_total: int = 0
    download_total: int = 0
    # When the counters were last reset (unix ts, 0 = never).
    since: float = 0.0

    _loaded: bool = False
    _dirty: bool = False
    _last_flush: float = 0.0
    # (time, live_upload, live_download) of the previous read
    _prev: tuple | None = None
    _speed_up: float = 0.0
    _speed_down: float = 0.0
    # In-flight bytes from transfers that do not go through the sync engine
    # (the standalone download manager). key -> bytes received so far.
    _live_extra: dict = {}

    # ---- persistence ----

    @classmethod
    async def load(cls):
        """Read the persisted counters; safe to call more than once."""
        if cls._loaded:
            return
        try:
            cls.upload_total = int(await db_get_setting(KEY_UPLOAD, "0") or 0)
            cls.download_total = int(await db_get_setting(KEY_DOWNLOAD, "0") or 0)
            cls.since = float(await db_get_setting(KEY_SINCE, "0") or 0)
        except (ValueError, TypeError):
            cls.upload_total = cls.download_total = 0
            cls.since = 0.0
        cls._loaded = True

    @classmethod
    async def flush(cls):
        """Write the counters to the settings table (no-op when clean)."""
        if not cls._dirty:
            return
        try:
            await db_set_setting(KEY_UPLOAD, str(cls.upload_total))
            await db_set_setting(KEY_DOWNLOAD, str(cls.download_total))
            await db_set_setting(KEY_SINCE, str(cls.since))
            cls._dirty = False
            cls._last_flush = time.time()
        except Exception:  # noqa: BLE001 - accounting must never break a sync
            pass

    # ---- accounting ----

    @classmethod
    def add(cls, direction: str, size: int):
        """Count a completed transfer. Cheap and synchronous by design."""
        if size <= 0:
            return
        if direction == "upload":
            cls.upload_total += size
        else:
            cls.download_total += size
        if not cls.since:
            cls.since = time.time()
        cls._dirty = True

    @classmethod
    async def reset(cls) -> dict:
        cls.upload_total = 0
        cls.download_total = 0
        cls.since = time.time()
        cls._prev = None
        cls._speed_up = cls._speed_down = 0.0
        cls._dirty = True
        await cls.flush()
        return await cls.snapshot()

    # ---- live speed ----

    @classmethod
    def set_live(cls, key: str, transferred: int):
        """Report in-flight bytes for a transfer outside the sync engine."""
        cls._live_extra[key] = max(0, transferred)

    @classmethod
    def clear_live(cls, key: str):
        cls._live_extra.pop(key, None)

    @classmethod
    def _live(cls) -> tuple[int, int]:
        from app.services.sync_engine import SyncEngine  # local: avoid a cycle
        try:
            upload, download = SyncEngine.live_transferred()
        except Exception:  # noqa: BLE001
            upload = download = 0
        return upload, download + sum(cls._live_extra.values())

    @classmethod
    def _update_speed(cls) -> None:
        live_up, live_down = cls._live()
        now = time.time()
        prev = cls._prev

        if prev is None:
            cls._prev = (now, live_up, live_down)
            cls._speed_up = cls._speed_down = 0.0
            return

        dt = now - prev[0]
        if dt < MIN_DT:
            return  # keep the last reading, the sample is too close
        if dt > SAMPLE_WINDOW:
            # Too long since the last read: the delta says nothing about the
            # current rate, so report idle and start a fresh sample.
            cls._prev = (now, live_up, live_down)
            cls._speed_up = cls._speed_down = 0.0
            return

        # A finished task drops out of the live set, so the delta can go
        # negative — never show a negative rate.
        cls._speed_up = max(0.0, (live_up - prev[1]) / dt)
        cls._speed_down = max(0.0, (live_down - prev[2]) / dt)
        cls._prev = (now, live_up, live_down)

    @classmethod
    async def snapshot(cls) -> dict:
        """Current totals + live speeds, suitable for the UI."""
        await cls.load()
        cls._update_speed()

        if cls._dirty and (time.time() - cls._last_flush) >= FLUSH_INTERVAL:
            await cls.flush()

        return {
            "upload_total": cls.upload_total,
            "download_total": cls.download_total,
            "total": cls.upload_total + cls.download_total,
            "since": cls.since,
            "upload_speed": round(cls._speed_up, 1),
            "download_speed": round(cls._speed_down, 1),
            "speed": round(cls._speed_up + cls._speed_down, 1),
        }


traffic = TrafficStats()
