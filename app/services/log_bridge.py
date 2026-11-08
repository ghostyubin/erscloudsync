"""把 Python logging 的告警桥接到数据库，让前端日志页看得见运行异常。

以前 ``logger.warning`` 只进容器 stdout 和 ``/config/log/baudusync.log``：
百度返回限流页、连接被重置、分片上传重试退避、token 刷新失败这些事，
用户在网页日志页一条都看不到——那边只查 ``sync_logs``（每个文件一条的
业务记录）。排查「上传怎么卡住了」只能 ssh 进容器翻 docker logs。

这里给 root logger 挂一个 handler，把 WARNING 及以上**结构化落库**：
带中文分类（限流/网络/授权…）、同类事件在时间窗内聚合计数。写库由后台
协程批量做，``emit`` 只碰内存和一把线程锁——同步引擎的文件回调、watchdog
的 observer 线程都可能触发日志，绝不能在这里 await 或碰事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from app import database as db

# 批量落库间隔
FLUSH_INTERVAL = 3.0
# 同一条告警在这个窗口内只累加次数，不新增行
AGGREGATE_WINDOW = 60.0
# 单条消息截断长度：百度错误页是整段 HTML，不截断会撑爆列表
MAX_MESSAGE = 600
# 内存里最多攒多少种不同告警（防极端情况下无界增长）
MAX_PENDING = 500

# 分类规则，**顺序敏感**：越具体越靠前。一条
# "Retry 1/2 for block ... : TimeoutError" 同时含 retry 和 timeout，
# 归到「网络连接中断」比「自动重试」更能说明卡住的原因。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("百度网盘限流/风控", (
        "限流", "风控", "error_all", "trigger security policy",
        "返回了错误网页", "返回网页而非 json", "安全策略",
    )),
    ("授权失效/刷新", (
        "invalid_grant", "invalid grant", "重新授权", "token 失效",
        "access token invalid", "errno -6", 'errno": -6', "api error -6",
        "unauthorized",
    )),
    ("网络连接中断", (
        "connection reset", "connection lost", "server disconnected",
        "ssl shutdown timed out", "timeouterror", "clientoserror",
        "clientconnectorerror", "errno 104", "连接被重置", "网络超时",
    )),
    ("网盘接口异常", (
        "non-json", "baidu api error", "precreate", "superfile",
        "pan.baidu", "errno", "pcs",
    )),
    ("自动重试", ("retry ", "重试")),
    ("同步任务异常", (
        "sync_task", "syncengine", "同步失败", "同步出错", "scheduler",
    )),
)


def classify(message: str, source: str = "") -> str:
    """把一条原始日志归到用户看得懂的中文分类。"""
    text = f"{message} {source}".lower()
    for label, markers in _RULES:
        if any(marker in text for marker in markers):
            return label
    return "其他告警"


class RuntimeLogHandler(logging.Handler):
    """收集 WARNING/ERROR，聚合后交给后台协程批量写库。"""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._closed = False

    def emit(self, record: logging.LogRecord) -> None:
        """logging 会在任意线程调用这里：只做内存操作，不 await、不碰 DB。"""
        try:
            if self._closed or record.levelno < logging.WARNING:
                return
            # 写库失败时如果再打 warning，就会「写库→打日志→再写库」无限递归
            if record.name.startswith(("app.services.log_bridge", "aiosqlite")):
                return
            message = record.getMessage().replace("\r", " ").replace("\n", " ⏎ ")
            if len(message) > MAX_MESSAGE:
                message = message[:MAX_MESSAGE] + "…"
            level = "error" if record.levelno >= logging.ERROR else "warning"
            category = classify(message, record.name)
            now = time.time()
            key = f"{level}|{category}|{record.name}|{message}"
            with self._lock:
                cur = self._pending.get(key)
                if cur and now - cur["last_at"] < AGGREGATE_WINDOW:
                    cur["count"] += 1
                    cur["last_at"] = now
                elif len(self._pending) < MAX_PENDING:
                    self._pending[key] = {
                        "level": level,
                        "category": category,
                        "source": record.name,
                        "message": message,
                        "count": 1,
                        "first_at": now,
                        "last_at": now,
                    }
        except Exception:  # noqa: BLE001 - handler 抛异常会污染业务日志
            pass

    async def _loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(FLUSH_INTERVAL)
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 循环不能死
                pass

    async def drain(self) -> None:
        """把攒下的告警批量写库（可在测试里直接调用）。

        不能叫 ``flush``：``logging.Handler.flush()`` 是同步方法，解释器退出时
        ``logging.shutdown()`` 会遍历所有 handler 调它，覆写成协程只会得到
        一个没人 await 的 coroutine 和一条 RuntimeWarning。
        """
        with self._lock:
            batch = list(self._pending.values())
            self._pending.clear()
        if not batch:
            return
        try:
            await db.db_merge_runtime_logs(batch)
        except Exception:  # noqa: BLE001
            # 不能用 logger.warning（会递归回自己）；丢这批也不影响同步
            pass

    async def start(self) -> None:
        """必须在 ``init_db()`` 之后调用，否则表还不存在。"""
        if self._task and not self._task.done():
            return
        self._closed = False
        root = logging.getLogger()
        if self not in root.handlers:
            root.addHandler(self)
        self._task = asyncio.create_task(self._loop(), name="runtime-log-bridge")

    async def stop(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        await self.drain()
        try:
            logging.getLogger().removeHandler(self)
        except Exception:  # noqa: BLE001
            pass


runtime_log_handler = RuntimeLogHandler()
