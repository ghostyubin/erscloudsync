"""同步完成通知 —— 按文件夹聚合，静默一段时间后推送到 Heimdallr 网关。

为什么按「文件夹」而不是按「任务」
------------------------------------
一个 realtime 任务通常挂在 ``/sync`` 这种总目录上，里面是很多互相独立
的文件夹（``/sync/1``、``/sync/2`` …）。用户想收到的是「1 号文件夹传完了」，
而不是所有文件夹混在一条里，也不是每轮扫描各推一条。

为什么要有静默期
----------------
realtime 任务在拷贝大目录时会连续触发几十轮扫描，每轮都推就是几十条消息。
静默期把「这一波」合并成一条：某个文件夹只要有文件在动，就把它的计时器
往后推；连续 ``quiet`` 秒没有新文件才算这一波结束，此时推一条汇总。

线程/协程模型
-------------
``record()`` / ``record_noop()`` 是纯内存同步方法，由同步引擎在文件完成时
直接调用，不 await、不碰网络，绝不会拖慢同步。真正的 HTTP 请求在
``_loop()`` 的后台任务里做。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

from app import database as db
from app.config import (
    NOTIFY_DEFAULT_KEY,
    NOTIFY_DEFAULT_QUIET,
    NOTIFY_DEFAULT_URL,
)

logger = logging.getLogger(__name__)

# settings 表里的键名
ENABLED_KEY = "notify_enabled"
URL_KEY = "notify_url"
TOKEN_KEY = "notify_key"
QUIET_KEY = "notify_quiet_seconds"
INSECURE_KEY = "notify_insecure"

QUIET_MIN = 10
QUIET_MAX = 3600

# 一个文件夹持续不断有文件进来（比如拷几百 GB）时的兜底：从这一波开始
# 计时算起最迟 30 分钟也要推一条，否则用户可能几个小时收不到任何消息。
MAX_BATCH_DELAY = 1800.0

# 发送失败的重试次数与间隔（秒）。轮询本身 5 秒一次，所以 3 次约 15 秒。
MAX_ATTEMPTS = 3
TICK = 5.0

# 配置读库的缓存时间，避免每 5 秒打一次 SQLite
CONFIG_TTL = 30.0


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------

def fmt_bytes(n: int) -> str:
    """3.2 GB / 812.4 MB / 0 B"""
    if not n or n <= 0:
        return "0 B"
    for unit, div in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                      ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            value = n / div
            return f"{value:.2f} {unit}" if value < 10 else f"{value:.1f} {unit}"
    return f"{n} B"


def fmt_duration(seconds: float) -> str:
    """4分12秒 / 38秒 / 1小时4分"""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分"


# 消息里最多点几个文件名，再多就只给数量
NAME_LIMIT = 3


def _basename(rel_path: str) -> str:
    """``1/a/b.mp4`` → ``b.mp4``"""
    rel = (rel_path or "").replace("\\", "/").rstrip("/")
    return rel.rsplit("/", 1)[-1] if rel else ""


def _remember(bucket: list, name: str) -> None:
    """记文件名，超过上限就不再加，避免长任务在内存里攒一个巨长列表。"""
    if name and len(bucket) < NAME_LIMIT and name not in bucket:
        bucket.append(name)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

@dataclass
class NotifyConfig:
    enabled: bool = False
    url: str = ""
    token: str = ""
    quiet: int = NOTIFY_DEFAULT_QUIET
    insecure: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    @property
    def endpoint(self) -> str:
        """网关地址 → 推送端点。

        用户在界面上只填到域名（``https://notify.example.com``），路径由这里
        拼；已经自带 ``/push`` 的也不会拼成 ``/push/push``。
        """
        base = (self.url or "").strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/push"):
            return base
        return base + "/push"


def _as_bool(raw, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _gateway_failure(status: int, text: str) -> str:
    """网关的业务失败也要算失败 —— 返回空串表示成功，否则返回失败原因。

    Heimdallr 即使推送失败（token 不对、渠道没绑定、企业微信报 60020…）
    仍然返回 HTTP 200，真正结果在响应体的 ``code`` 里：成功是
    ``{"code":0,"message":"success"}``。只看状态码会把「没人收到」当成
    「发送成功」，这正是最难查的那种问题。
    """
    if status >= 400:
        return f"HTTP {status}: {text}"
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 - 不是 JSON 就不猜，按状态码算成功
        return ""
    if not isinstance(data, dict) or "code" not in data:
        return ""
    try:
        code = int(data.get("code"))
    except (TypeError, ValueError):
        return ""
    if code == 0:
        return ""
    reason = data.get("message") or data.get("msg") or ""
    return f"网关返回 code={code}：{reason}".rstrip("：")


def _clamp_quiet(raw, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(QUIET_MIN, min(QUIET_MAX, value))


async def load_config() -> NotifyConfig:
    """从 settings 表读通知配置，库里没有值就用环境变量/内置默认。"""
    keys = (ENABLED_KEY, URL_KEY, TOKEN_KEY, QUIET_KEY, INSECURE_KEY)
    raw = {}
    for key in keys:
        try:
            raw[key] = await db.db_get_setting(key)
        except Exception:  # noqa: BLE001 - 通知配置读不到不能让同步挂掉
            raw[key] = None

    url = raw.get(URL_KEY) or NOTIFY_DEFAULT_URL
    token = raw.get(TOKEN_KEY) or NOTIFY_DEFAULT_KEY
    # 显式存过 "0" 才算关闭；没存过时，只要地址和 token 都有就默认开启
    if raw.get(ENABLED_KEY) is None:
        enabled = bool(url and token)
    else:
        enabled = _as_bool(raw.get(ENABLED_KEY))

    return NotifyConfig(
        enabled=enabled,
        url=(url or "").strip(),
        token=(token or "").strip(),
        quiet=_clamp_quiet(raw.get(QUIET_KEY), NOTIFY_DEFAULT_QUIET),
        insecure=_as_bool(raw.get(INSECURE_KEY)),
    )


# --------------------------------------------------------------------------
# 聚合桶
# --------------------------------------------------------------------------

@dataclass
class FolderBatch:
    """某个文件夹「这一波」的待推送汇总。"""
    key: str
    label: str
    task_name: str
    uploads: int = 0
    downloads: int = 0
    deletes: int = 0
    failed: int = 0
    noop: int = 0
    bytes_moved: int = 0
    first_at: float = 0.0
    last_at: float = 0.0
    attempts: int = 0
    # 只有真的传了东西才算「有活动」。纯无变化（noop）的桶不推送，
    # 否则一个安静的文件夹每轮扫描都会冒出一条「同步完成 0 个文件」。
    has_activity: bool = False
    error: str = ""
    # 只留前几个文件名：单个文件时直接显示它，失败时点名是哪个出错。
    # 上限 3 是为了消息不被文件名刷屏，也避免长任务攒出一个大列表。
    files: list = field(default_factory=list)
    failed_files: list = field(default_factory=list)

    @property
    def moved(self) -> int:
        return self.uploads + self.downloads + self.deletes

    def body(self) -> str:
        """正文：前三行讲清「传了什么 / 出没出错 / 多久」，出错再补一句怎么处理。

        纯文本（msg_type=text）—— 微信里看企业微信消息，markdown 会显示
        「不支持的消息类型」。所以排版只靠换行和间隔号，不依赖任何标记语法。
        """
        lines: list[str] = []
        actions = []
        if self.uploads:
            actions.append(f"上传 {self.uploads}")
        if self.downloads:
            actions.append(f"下载 {self.downloads}")
        if self.deletes:
            actions.append(f"删除 {self.deletes}")

        # ---- 第一行：这一波到底传了多少 ----
        if self.failed:
            seg = []
            if self.moved:
                seg.append(f"成功 {self.moved} 个")
            seg.append(f"失败 {self.failed} 个")
            if self.bytes_moved:
                seg.append(fmt_bytes(self.bytes_moved))
            lines.append(" · ".join(seg))
        elif self.moved == 1 and self.files:
            # 只有一个文件时，文件名比「1 个文件」有用得多
            size = f" · {fmt_bytes(self.bytes_moved)}" if self.bytes_moved else ""
            lines.append(f"{self.files[0]}{size}")
        elif self.moved:
            # 纯上传不说动作（默认就是上传），混合或纯下载/删除才说出来
            if self.uploads and not (self.downloads or self.deletes):
                head = f"{self.moved} 个文件"
            elif len(actions) == 1:
                head = f"{actions[0]} 个文件"
            else:
                head = " · ".join(f"{a} 个" for a in actions)
            if self.bytes_moved:
                head += f" · {fmt_bytes(self.bytes_moved)}"
            lines.append(head)

        # ---- 出错时点名：哪个文件、什么原因 ----
        if self.failed_files:
            shown = "、".join(self.failed_files[:3])
            if self.failed > len(self.failed_files[:3]):
                shown += f" 等 {self.failed} 个"
            lines.append(f"失败：{shown}")
        if self.error:
            lines.append(f"原因：{self.error}")

        # ---- 最后一行：多久、几点 ----
        # 整轮扫挂（一个文件都没动）时不给「用时」，那会让人以为传了一会儿才失败
        tail = []
        if (self.moved or self.failed) and self.first_at and self.last_at > self.first_at:
            tail.append(f"用时 {fmt_duration(self.last_at - self.first_at)}")
        tail.append(datetime.now().strftime("%H:%M"))
        lines.append(" · ".join(tail))

        if self.error or self.failed:
            lines.append("修好后可在「同步记录」点重试")
        return "\n".join(lines)

    def title(self) -> str:
        """标题 = 状态 + 文件夹。手机上第一条就看清是哪个文件夹、成没成。"""
        if self.error:
            return f"❌ 同步失败 · {self.label}"
        if self.failed:
            return f"⚠️ {self.failed} 个失败 · {self.label}"
        return f"✅ 传输完成 · {self.label}"


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------

class Notifier:
    def __init__(self) -> None:
        self._pending: dict[str, FolderBatch] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._cfg: Optional[NotifyConfig] = None
        self._cfg_at: float = 0.0
        # 供界面「测试发送」和排查用：最近一次发送的结果
        self.last_result: dict = {}

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._loop(), name="notifier")

    async def stop(self) -> None:
        task, self._loop_task = self._loop_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- 配置 -------------------------------------------------------------

    async def config(self, force: bool = False) -> NotifyConfig:
        now = time.time()
        if force or self._cfg is None or now - self._cfg_at > CONFIG_TTL:
            self._cfg = await load_config()
            self._cfg_at = now
        return self._cfg

    def invalidate_config(self) -> None:
        """设置保存后调用，让下一次读取立刻拿到新值。"""
        self._cfg = None
        self._cfg_at = 0.0

    # -- 采集（同步、纯内存） ---------------------------------------------

    @staticmethod
    def folder_of(local_base: str, rel_path: str) -> tuple[str, str]:
        """把相对路径归到「同步根下的一级文件夹」。

        ``/sync/1/a.mp4`` → ``("1", "/sync/1")``；
        直接放在根下的文件 → ``("", "/sync")``。
        """
        base = (local_base or "").rstrip("/")
        rel = (rel_path or "").replace("\\", "/").strip("/")
        head = rel.split("/", 1)[0] if "/" in rel else ""
        if head:
            return head, f"{base}/{head}"
        return "", base

    def _bucket(self, task_id: int, task_name: str, local_base: str,
                rel_path: str, cycle_started: float) -> FolderBatch:
        folder, label = self.folder_of(local_base, rel_path)
        key = f"{task_id}:{folder}"
        batch = self._pending.get(key)
        now = time.time()
        if batch is None:
            started = cycle_started or now
            batch = FolderBatch(key=key, label=label, task_name=task_name,
                                first_at=started, last_at=now)
            self._pending[key] = batch
        return batch

    def record(self, *, task_id: int, task_name: str, local_base: str,
               rel_path: str, action: str, ok: bool, size: int = 0,
               cycle_started: float = 0.0) -> None:
        """记一个「真的动过」的文件。由同步引擎在文件完成后调用。"""
        try:
            batch = self._bucket(task_id, task_name, local_base, rel_path,
                                 cycle_started)
            now = time.time()
            batch.has_activity = True
            batch.last_at = now
            if cycle_started:
                batch.first_at = min(batch.first_at, cycle_started)
            name = _basename(rel_path)
            if not ok:
                batch.failed += 1
                _remember(batch.failed_files, name)
                return
            if action == "upload":
                batch.uploads += 1
            elif action == "download":
                batch.downloads += 1
            elif action == "delete":
                batch.deletes += 1
            _remember(batch.files, name)
            batch.bytes_moved += max(0, size)
        except Exception:  # noqa: BLE001 - 统计失败绝不能影响同步
            logger.debug("notifier.record failed", exc_info=True)

    def record_noop(self, *, task_id: int, task_name: str, local_base: str,
                    rel_paths, cycle_started: float = 0.0) -> None:
        """记「无变化」的文件数，只用于消息里的计数，不触发推送。"""
        if not rel_paths:
            return
        try:
            counts: dict[str, int] = {}
            labels: dict[str, str] = {}
            for rel in rel_paths:
                folder, label = self.folder_of(local_base, rel)
                counts[folder] = counts.get(folder, 0) + 1
                labels[folder] = label
            for folder, count in counts.items():
                batch = self._pending.get(f"{task_id}:{folder}")
                if batch is None:
                    started = cycle_started or time.time()
                    batch = FolderBatch(key=f"{task_id}:{folder}",
                                        label=labels[folder],
                                        task_name=task_name,
                                        first_at=started, last_at=started)
                    self._pending[batch.key] = batch
                batch.noop += count
        except Exception:  # noqa: BLE001
            logger.debug("notifier.record_noop failed", exc_info=True)

    def abort_task(self, task_id: int, error: str, local_base: str = "",
                   task_name: str = "") -> None:
        """整轮同步报错：把该任务待推送的桶标记错误并立刻发，不再等静默。

        已经传成功的文件也要让用户知道「传到一半就断了」，单纯等静默期
        结束只会让人以为还在跑。

        只有真的动过文件的文件夹才推——一个从头到尾都「无变化」的文件夹
        跟着报「同步失败」纯属噪音。如果整轮在扫描阶段就挂了（比如连接
        不上网盘），没有任何文件夹动过，此时补一条任务级的失败通知，
        否则用户什么消息都收不到。
        """
        touched = False
        for batch in self._pending.values():
            if batch.key.startswith(f"{task_id}:") and batch.has_activity:
                batch.error = error
                batch.has_activity = True
                batch.last_at = 0.0  # 让 flush 立刻判定到期
                touched = True
        if touched or not (local_base or task_name):
            return
        key = f"{task_id}:__error__"
        batch = self._pending.get(key)
        if batch is None:
            batch = FolderBatch(key=key, label=local_base or task_name,
                                task_name=task_name or "同步任务",
                                first_at=time.time(), last_at=time.time(),
                                has_activity=True)
            self._pending[key] = batch
        batch.error = error
        batch.last_at = 0.0

    # -- 后台循环 ---------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK)
                await self.flush_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 循环不能死
                logger.warning("notify loop error", exc_info=True)

    async def flush_due(self) -> None:
        if not self._pending:
            return
        cfg = await self.config()

        if not (cfg.enabled and cfg.configured):
            # 关掉通知后不要把桶留着无限涨
            self._pending.clear()
            return

        now = time.time()
        for key, batch in list(self._pending.items()):
            if not batch.has_activity and not batch.error:
                # 只有无变化记录、从来没传过东西：留着计数，静置 5 分钟后丢掉
                if now - batch.last_at > 300:
                    self._pending.pop(key, None)
                continue

            due = (now - batch.last_at >= cfg.quiet
                   or now - batch.first_at >= MAX_BATCH_DELAY)
            if not due:
                continue

            ok, detail = await self._send(cfg, batch)
            if ok:
                self._pending.pop(key, None)
                self.last_result = {
                    "ok": True, "at": now, "folder": batch.label,
                    "message": detail,
                }
            else:
                batch.attempts += 1
                self.last_result = {
                    "ok": False, "at": now, "folder": batch.label,
                    "message": detail,
                }
                if batch.attempts >= MAX_ATTEMPTS:
                    self._pending.pop(key, None)
                    logger.warning(
                        "Dropping notification for %s after %d attempts: %s",
                        batch.label, batch.attempts, detail)

    # -- 发送 -------------------------------------------------------------

    async def _send(self, cfg: NotifyConfig, batch: FolderBatch
                    ) -> tuple[bool, str]:
        endpoint = cfg.endpoint
        if not endpoint:
            return False, "通知地址为空"
        payload = {
            "key": cfg.token,
            "title": batch.title(),
            "body": batch.body(),
            "msg_type": "text",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        # aiohttp: ssl=False 表示跳过证书校验，None 表示用默认校验
        ssl_arg = False if cfg.insecure else None
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, ssl=ssl_arg) as resp:
                    text = (await resp.text())[:300]
                    failure = _gateway_failure(resp.status, text)
                    if failure:
                        return False, failure
                    return True, f"HTTP {resp.status}: {text}"
        except asyncio.TimeoutError:
            return False, "请求网关超时（15 秒）"
        except aiohttp.ClientConnectorCertificateError as exc:
            return False, (f"证书校验失败，可在设置里勾选「跳过证书校验」：{exc}")
        except aiohttp.ClientError as exc:
            return False, f"连接网关失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"发送异常：{exc}"

    async def send_test(self, title: str = "BauduSync 测试通知",
                        body: str = "如果你在手机上看到这条消息，说明通知已经能正常送达。",
                        ) -> dict:
        """测试发送：直接用当前配置发一条，不经过聚合队列。"""
        cfg = await self.config(force=True)
        if not cfg.configured:
            return {"ok": False, "message": "请先填写网关地址和分组 Token"}
        batch = FolderBatch(key="test", label=title, task_name="BauduSync",
                            uploads=1, bytes_moved=0,
                            first_at=time.time(), last_at=time.time(),
                            has_activity=True)
        endpoint = cfg.endpoint
        payload = {"key": cfg.token, "title": title, "body": body,
                   "msg_type": "text"}
        timeout = aiohttp.ClientTimeout(total=15)
        ssl_arg = False if cfg.insecure else None
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, ssl=ssl_arg) as resp:
                    text = (await resp.text())[:300]
                    failure = _gateway_failure(resp.status, text)
                    return {"ok": not failure, "endpoint": endpoint,
                            "status": resp.status,
                            "message": failure or text}
        except asyncio.TimeoutError:
            return {"ok": False, "endpoint": endpoint,
                    "message": "请求网关超时（15 秒）"}
        except aiohttp.ClientConnectorCertificateError as exc:
            return {"ok": False, "endpoint": endpoint,
                    "message": f"证书校验失败，勾选「跳过证书校验」再试：{exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "endpoint": endpoint, "message": f"{exc}"}

    def snapshot(self) -> dict:
        """给界面/排查用：当前排队的桶。"""
        return {
            "pending": [
                {"folder": b.label, "task": b.task_name,
                 "uploads": b.uploads, "downloads": b.downloads,
                 "failed": b.failed, "noop": b.noop,
                 "bytes": b.bytes_moved, "attempts": b.attempts,
                 "queued_for": round(time.time() - b.last_at, 1)}
                for b in self._pending.values()
            ],
            "last_result": self.last_result,
        }


notifier = Notifier()
