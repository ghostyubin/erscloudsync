"""独立解密：给一个本地目录 + 凭据，把里面的密文还原到另一个目录。

对标 Synology 的 Cloud Sync Decryption Tool——**不依赖本机数据库**。

认两种密文：

* ``BSENC``（本程序现在的格式）：密钥藏在每个文件头里（「钥匙牌」），所以
  任何一台部署了本程序的机器，输对密码就能解别的机器加密的文件；
* ``__CLOUDSYNC_ENC__``（群晖 Cloud Sync 格式）：早先上传到网盘的历史数据。
  本程序已不再生成这种格式，但用户手里还有存量，所以保留解码。

凭据也认两种，可以单用也可以一起用：

* **密码**——当场解开文件头里的钥匙牌；改过密码之后，老文件要用当时的密码；
* **导出的密钥文件**——任务里的 ``baudusync-task-N-key.json`` 里是明文数据
  密钥，**不需要密码**；全局设置里导出的 ``bsenc-keybundle`` 里是仍被密码
  包着的密钥，得配密码。这是「密码忘了」和「文件头没钥匙牌」的出路。

和同步引擎里的 ``settle_download`` 的分工：

* 那边是自动化的、走数据库里的 DEK、针对单个中转文件；
* 这里是手动的、走文件头钥匙牌或导入的密钥文件、针对一整棵目录树。

两边的文件格式完全一样，只是密钥来源不同。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app import config
from app.services import crypto as ck
from app.services import syno_crypto as sk

logger = logging.getLogger(__name__)

# 只留最近这么多条历史，内存里放着就够——这是个一次性动作，不是长期记录。
MAX_JOBS = 20
MAX_ERRORS = 30

# 扫描时忽略的临时文件后缀（我们自己产生的中转文件，解它就是解一半的东西）
_IGNORE_SUFFIX = (ck.SUFFIX_CIPHER, ck.SUFFIX_PLAIN)


class DecryptJobError(Exception):
    """参数不合法、路径越界、没有可解的文件——都归到这里。"""


def allowed_roots() -> list[str]:
    """允许读写的根目录。界面上只能在这两个挂载点里选。"""
    return [str(config.LOCAL_SYNC_ROOT), str(config.DOWNLOAD_DIR)]


def _inside(path: str, root: str) -> bool:
    p = os.path.realpath(path)
    r = os.path.realpath(root)
    return p == r or p.startswith(r + os.sep)


def check_source(path: str) -> str:
    """校验「要解密的目录」：必须在允许的根目录里，且是个目录。"""
    if not path or not path.strip():
        raise DecryptJobError("请选择要解密的目录")
    full = os.path.realpath(path.strip())
    if not any(_inside(full, r) for r in allowed_roots()):
        raise DecryptJobError(
            f"要解密的目录必须在 {' 或 '.join(allowed_roots())} 之下")
    if not os.path.isdir(full):
        raise DecryptJobError("要解密的目录不存在")
    return full


def check_output(src: str, path: str) -> str:
    """校验「解密输出目录」。

    除了同样限制在挂载点内，还**不允许输出到源目录里面**：那样解出来的明文
    会混进待解密的目录树，下次再跑就会把明文当输入，越解越乱。要原地覆盖请
    明确用「覆盖已存在」并换一个平级目录。
    """
    if not path or not path.strip():
        raise DecryptJobError("请选择解密输出目录")
    full = os.path.realpath(path.strip())
    if not any(_inside(full, r) for r in allowed_roots()):
        raise DecryptJobError(
            f"解密输出目录必须在 {' 或 '.join(allowed_roots())} 之下")
    if full == src or _inside(full, src):
        raise DecryptJobError("解密输出目录不能是（或位于）要解密的目录里面")
    return full


def detect_format(path: str) -> Optional[str]:
    """这是谁的密文：``"bsenc"`` / ``"syno"`` / ``None``（明文）。

    BSENC 先认：它的头部校验最严（魔数 + 版本 + 分块大小），几乎不会误判。
    群晖格式只看 17 字节魔数，所以放在后面。
    """
    if ck.is_encrypted(path):
        return "bsenc"
    if sk.is_syno(path):
        return "syno"
    return None


def scan(src: str) -> dict:
    """先数一遍，让用户在真跑之前看到规模，也能提前撞上「凭据不对」。

    只统计不写盘，随便调。

    ``samples``/``task_ids`` 是给「上传密钥文件」那条路用的：密钥是按任务
    编号匹配的，得先知道这堆密文属于哪几个任务，才能在开跑前判定「你给的
    密钥是不是这堆文件的」。
    """
    total = encrypted = plain = 0
    bsenc = syno = 0
    total_bytes = 0
    keyed = 0          # 有多少 BSENC 密文带着钥匙牌（没带就只能靠私钥文件）
    first_with_block: Optional[str] = None
    first_syno: Optional[str] = None
    first_bsenc: Optional[str] = None
    samples: dict[int, str] = {}      # 任务号 → 该任务的第一个密文
    for dirpath, _dirs, files in os.walk(src):
        for name in files:
            if name.endswith(_IGNORE_SUFFIX):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            total += 1
            fmt = detect_format(full)
            if fmt is None:
                plain += 1
                continue
            encrypted += 1
            total_bytes += size
            if fmt == "syno":
                syno += 1
                if first_syno is None:
                    first_syno = full
                continue
            bsenc += 1
            if first_bsenc is None:
                first_bsenc = full
            header = ck.read_header(full)
            if not header:
                continue
            block = header.get("block")
            if block:
                samples.setdefault(int(block["task_id"]), full)
                keyed += 1
                if first_with_block is None:
                    first_with_block = full
    return {"total": total, "encrypted": encrypted, "plain": plain,
            "bsenc": bsenc, "syno": syno, "bytes": total_bytes,
            "with_keyblock": keyed,
            "sample": first_with_block, "sample_syno": first_syno,
            "first_bsenc": first_bsenc,
            "task_ids": sorted(samples), "samples": samples}


def no_cipher_message(info: dict) -> str:
    """扫完一个密文都没有时说什么。

    单纯一句「没有加密文件」会让人以为功能坏了——最常见的情况恰恰是这个
    目录里全是明文：加密只作用于云端，同步到本地的文件在落盘前就解密了，
    所以 /sync 下面永远是明文。把这句话说清楚，比让他自己猜有用。
    """
    n = int(info.get("total") or 0)
    if n == 0:
        return "这个目录里没有文件，没有可解密的密文"
    return (f"这个目录里的 {n} 个文件都是明文，没有可解密的密文。"
            "加密只作用于云端：从云端同步到本地的文件会自动解密，"
            "所以本地的同步目录里不会出现密文。"
            "要解密，得先把云端那份密文（用网盘客户端、rclone 之类）"
            "下载到 /sync 或 /downloads 下的某个目录，再来这里解")


KEYFILE_TASK = "bsenc-taskkey"
KEYFILE_BUNDLE = "bsenc-keybundle"


def parse_keyfiles(items, password: str) -> dict[int, bytes]:
    """把界面上传的密钥文件解析成 ``{任务号: 数据密钥}``。

    认两种格式，都是本程序自己导出的：

    * ``bsenc-taskkey``——任务详情里「重新导出本任务私钥」得到的
      ``baudusync-task-N-key.json``。里面就是明文数据密钥，**不要密码**；
    * ``bsenc-keybundle``——全局设置里「导出解密密钥包」得到的。里面是仍被
      密码包着的密钥，**必须配密码**才能展开。

    密钥包里个别密钥解不开就跳过，不让整份作废——「导出之后又改过密码」的
    库就是这种情形。
    """
    out: dict[int, bytes] = {}
    for idx, data in enumerate(items or [], 1):
        if not isinstance(data, dict):
            raise DecryptJobError(f"第 {idx} 个密钥文件不是 JSON 对象")
        fmt = str(data.get("format") or "").strip()
        if fmt == KEYFILE_TASK:
            try:
                task_id = int(data["task_id"])
                dek = ck._unb64(str(data["dek"]))
            except (KeyError, TypeError, ValueError, ck.CryptoError) as exc:
                raise DecryptJobError(
                    f"第 {idx} 个任务私钥文件内容不完整（{exc}）") from exc
            if len(dek) != ck.KEY_SIZE:
                raise DecryptJobError(
                    f"第 {idx} 个任务私钥长度不对（{len(dek)} 字节），"
                    f"文件可能被改过")
            out[task_id] = dek
        elif fmt == KEYFILE_BUNDLE:
            if not (password or "").strip():
                raise DecryptJobError(
                    "密钥包里的密钥还套着密码，请一并填写当时的加密密码；"
                    "想完全不输密码，改用任务里导出的 baudusync-task-N-key.json")
            salt_b64 = str((data.get("kdf") or {}).get("salt") or "")
            try:
                kek = ck._derive(password, ck._unb64(salt_b64))
            except ck.CryptoError as exc:
                raise DecryptJobError(f"第 {idx} 个密钥包的参数损坏") from exc
            for entry in data.get("keys") or []:
                try:
                    task_id = int(entry["task_id"])
                    out[task_id] = ck._unwrap(kek, str(entry["wrapped"]), task_id)
                except Exception:      # noqa: BLE001 - 单把解不开不影响别的
                    continue
        else:
            raise DecryptJobError(
                f"第 {idx} 个文件不是本程序导出的密钥文件"
                f"（format={fmt or '空'}）。要的是 baudusync-task-N-key.json，"
                f"或者设置里「导出解密密钥包」得到的文件")
    return out


def verify_credentials(src: str, password: str, info: Optional[dict] = None,
                       deks: Optional[dict] = None) -> None:
    """开跑之前先验一次凭据，错了立刻报。

    手里有密钥文件时它优先：按密文里的任务号挑一把，真去解第一块。这一步
    才是关键——密钥文件的 ``task_id`` 对得上不代表密钥对，而「跑到一半才
    发现全批失败」是最难受的失败方式。

    没有可用密钥时退回密码路径：BSENC 解一把钥匙牌（一次 scrypt），群晖
    格式核对文件头里的 ``key1_hash``（一次 MD5 迭代）。
    """
    if info is None:
        info = scan(src)
    if not info["encrypted"]:
        raise DecryptJobError(no_cipher_message(info))

    deks = dict(deks or {})
    probe = info.get("sample") or info.get("first_bsenc") or ""
    have = [t for t in (info.get("task_ids") or []) if t in deks]
    if have:
        task_id = have[0]
        path = (info.get("samples") or {}).get(task_id) or probe
        if path and not ck.verify_key(path, deks[task_id]):
            raise DecryptJobError(
                f"这份密钥打不开任务 {task_id} 的密文：密钥文件属于别的任务，"
                f"或者文件被改过")
        return

    if deks and probe and any(ck.verify_key(probe, dek) for dek in deks.values()):
        # 老格式密文的头里既没有任务号也没有钥匙牌，没法按编号匹配，
        # 只能拿手上的密钥挨个试第一块。通常只有一两把，代价可忽略。
        return

    if deks and not (password or "").strip():
        if not info.get("bsenc"):
            raise DecryptJobError(
                "这批是群晖 Cloud Sync 格式的密文，密钥文件帮不上忙"
                "（它的钥匙是从密码现推的），请填写当时的密码")
        if info.get("task_ids"):
            raise DecryptJobError(
                f"这些密文属于任务 {'、'.join(str(t) for t in info['task_ids'])}，"
                f"和你给的密钥文件对不上。请换成对应任务导出的私钥，"
                f"或者填密码直接解")
        raise DecryptJobError(
            "给的密钥文件打不开这批密文，而且没填密码。请确认这个密钥文件是"
            "加密这些文件的那台机器导出的")

    if info.get("with_keyblock"):
        header = ck.read_header(info["sample"])
        try:
            ck.dek_from_block(header["block"], password)
        except ck.CryptoError as exc:
            raise DecryptJobError(f"密码不正确，或文件已被改动（{exc}）") from exc
        return

    if info.get("syno"):
        # 群晖格式的文件头里带 key1_hash，直接就能判密码
        s_info = sk.read_info(info["sample_syno"])
        if not s_info:
            raise DecryptJobError("群晖格式的文件头解析失败，文件可能损坏")
        try:
            sk.check_password(s_info, password)
        except sk.SynoDecryptError as exc:
            raise DecryptJobError(f"密码不正确（{exc}）") from exc
        return

    raise DecryptJobError(
        "这些文件头里没有钥匙牌（是很早以前的版本加密的），只能靠任务私钥"
        "文件或密钥包解密，不能用密码直接解")


# 兼容旧名字：外部（含测试）还有按名字调用的地方
verify_password = verify_credentials


@dataclass
class Job:
    id: str
    src: str
    dst: str
    copy_plain: bool = False
    overwrite: bool = False
    key_count: int = 0              # 这次用了几把导出的密钥（0=只靠密码）
    status: str = "queued"          # queued | running | done | failed | cancelled
    total: int = 0
    processed: int = 0
    decrypted: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    current: str = ""
    message: str = ""
    errors: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancelled: bool = False

    def snapshot(self) -> dict:
        elapsed = 0.0
        if self.started_at:
            end = self.finished_at or time.time()
            elapsed = round(end - self.started_at, 1)
        pct = round(self.processed * 100 / self.total, 1) if self.total else 0.0
        # 密码和任务私钥都不出现在快照里，前端也就不可能把它回显到界面上
        return {
            "id": self.id, "src": self.src, "dst": self.dst,
            "copy_plain": self.copy_plain, "overwrite": self.overwrite,
            "keys": self.key_count,
            "status": self.status, "total": self.total,
            "processed": self.processed, "percent": pct,
            "decrypted": self.decrypted, "copied": self.copied,
            "skipped": self.skipped, "failed": self.failed,
            "bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
            "current": self.current, "message": self.message,
            "errors": self.errors[:MAX_ERRORS],
            "elapsed": elapsed,
            "created_at": int(self.created_at),
        }


_JOBS: dict[str, Job] = {}
_TASKS: dict[str, asyncio.Task] = {}
# 同一时间只跑一个：解密是重 IO + 重 CPU 的活，并发跑只会互相拖慢，
# 而且两个任务写同一个输出目录时没有任何好处。
_current: Optional[str] = None


def list_jobs() -> list[dict]:
    return [j.snapshot() for j in sorted(_JOBS.values(),
                                         key=lambda x: x.created_at,
                                         reverse=True)]


def get_job(job_id: str) -> Optional[dict]:
    job = _JOBS.get(job_id)
    return job.snapshot() if job else None


def remove_job(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job:
        return False
    if job.status in ("queued", "running"):
        raise DecryptJobError("这个任务还在跑，先取消再删除")
    _JOBS.pop(job_id, None)
    return True


async def cancel_job(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if not job or job.status not in ("queued", "running"):
        return False
    job.cancelled = True
    task = _TASKS.get(job_id)
    if task and not task.done():
        task.cancel()
    return True


async def start(src: str, dst: str, password: str, copy_plain: bool = False,
                overwrite: bool = False, keyfiles=None) -> str:
    """校验、扫描、起后台任务，立刻返回 job id。

    ``keyfiles`` 是界面上传的密钥文件内容（已经解析好的 JSON 列表），可以只
    给密钥不给密码——任务私钥里就是明文密钥，不用密码也能解。

    校验和扫描（可能要走一整棵树、还要跑一次 scrypt）都丢到线程里，别把
    HTTP 事件循环卡住——其它请求会一起变慢。
    """
    global _current
    src = check_source(src)
    dst = check_output(src, dst)
    if not (password or "").strip() and not keyfiles:
        raise DecryptJobError("请输入加密密码，或者上传导出的密钥文件")

    deks = await asyncio.to_thread(parse_keyfiles, keyfiles or [], password)
    info = await asyncio.to_thread(scan, src)
    if not info["encrypted"]:
        raise DecryptJobError(no_cipher_message(info))
    await asyncio.to_thread(verify_credentials, src, password, info, deks)

    if _current:
        running = _JOBS.get(_current)
        if running and running.status in ("queued", "running"):
            raise DecryptJobError(
                f"已经有一个解密任务在跑（{os.path.basename(running.src)}），等它结束再开始")

    job = Job(id=uuid.uuid4().hex[:12], src=src, dst=dst,
              copy_plain=bool(copy_plain), overwrite=bool(overwrite),
              key_count=len(deks))
    _JOBS[job.id] = job
    _current = job.id
    _prune()
    _TASKS[job.id] = asyncio.create_task(_run(job, password, deks))
    logger.info("开始解密：%s → %s（密钥 %d 把）", src, dst, len(deks))
    return job.id


def _prune() -> None:
    if len(_JOBS) <= MAX_JOBS:
        return
    for job in sorted(_JOBS.values(), key=lambda x: x.created_at):
        if len(_JOBS) <= MAX_JOBS:
            break
        if job.status in ("queued", "running"):
            continue
        _JOBS.pop(job.id, None)


def _collect(src: str) -> list[tuple[str, str, int]]:
    """(绝对路径, 相对路径, 大小)。一次列全，边跑边改目录不影响这次的结果。"""
    out: list[tuple[str, str, int]] = []
    for dirpath, _dirs, files in os.walk(src):
        for name in files:
            if name.endswith(_IGNORE_SUFFIX):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            out.append((full, os.path.relpath(full, src), size))
    return out


async def _run(job: Job, password: str, deks: Optional[dict] = None) -> None:
    global _current
    job.status = "running"
    job.started_at = time.time()
    cache: dict[tuple, bytes] = {}
    deks = deks or {}
    try:
        files = await asyncio.to_thread(_collect, job.src)
        job.total = len(files)
        for full, rel, size in files:
            if job.cancelled:
                break
            job.current = rel
            try:
                await _one(job, full, rel, size, password, deks, cache)
            except asyncio.CancelledError:
                raise
            except Exception as exc:          # noqa: BLE001 - 单个文件失败不中断整批
                job.failed += 1
                if len(job.errors) < MAX_ERRORS:
                    job.errors.append({"path": rel, "error": str(exc)})
                logger.warning("解密失败 %s: %s", rel, exc)
            job.processed += 1

        if job.cancelled:
            job.status = "cancelled"
            job.message = f"已取消（已完成 {job.decrypted + job.copied} 个文件）"
        else:
            job.status = "done"
            job.message = (f"解密 {job.decrypted} 个，复制明文 {job.copied} 个，"
                           f"跳过 {job.skipped} 个，失败 {job.failed} 个")
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.message = "已取消"
    except Exception as exc:                  # noqa: BLE001
        job.status = "failed"
        job.message = str(exc)
        logger.exception("解密任务 %s 失败", job.id)
    finally:
        job.finished_at = time.time()
        job.current = ""
        if _current == job.id:
            _current = None
        _TASKS.pop(job.id, None)
        logger.info("解密任务 %s 结束：%s", job.id, job.message)


async def _one(job: Job, full: str, rel: str, size: int,
               password: str, deks: dict, cache: dict) -> None:
    target = os.path.join(job.dst, rel)

    fmt = await asyncio.to_thread(detect_format, full)
    if fmt is None:
        if not job.copy_plain:
            job.skipped += 1
            return
        if os.path.exists(target) and not job.overwrite:
            job.skipped += 1
            return
        await asyncio.to_thread(_ensure_parent, target)
        await asyncio.to_thread(_copy_file, full, target)
        job.copied += 1
        logger.debug("已复制明文 %s", rel)
        return

    if os.path.exists(target) and not job.overwrite:
        job.skipped += 1
        return

    await asyncio.to_thread(_ensure_parent, target)
    tmp = target + ck.SUFFIX_PLAIN
    try:
        if fmt == "syno":
            # 群晖格式每个文件自带随机会话密钥，被密码包在 enc_key1 里，
            # 没有「整个目录共用一份」的说法，缓不上。
            written = await asyncio.to_thread(
                sk.decrypt_file, full, tmp, password, None)
        else:
            header = await asyncio.to_thread(ck.read_header, full)
            if not header:
                raise ck.DecryptError("文件头损坏")
            block = header.get("block")
            task_id = int(block["task_id"]) if block else 0
            # 手上有这个任务的密钥就直接用：省掉每个文件一次 scrypt，
            # 也是「文件头没钥匙牌」和「密码忘了」时唯一能走的路。
            dek = deks.get(task_id) if task_id else None
            if dek is None and not task_id and deks:
                # 老格式的密文头里没有任务号，没法按编号挑密钥，只能拿手上的
                # 密钥挨个试第一块。正常情况下第一把就中。
                for candidate in deks.values():
                    if await asyncio.to_thread(ck.verify_key, full, candidate):
                        dek = candidate
                        break
            if dek is None:
                if not block:
                    raise ck.DecryptError(
                        "文件头里没有钥匙牌，手上的密钥也解不开它——"
                        "请换成加密这台机器导出私钥（baudusync-task-N-key.json）")
                if not (password or "").strip():
                    raise ck.DecryptError(f"没有任务 {task_id} 的密钥文件，也没给密码")
                ck_key = (task_id, block["salt"], block["wrapped"])
                dek = cache.get(ck_key)
                if dek is None:
                    # scrypt 一次 32MB / 0.1 秒，同一任务的文件共用同一个钥匙牌，
                    # 所以整个目录通常只在这里花一次。
                    dek = await asyncio.to_thread(ck.dek_from_block, block, password)
                    cache[ck_key] = dek
            written = await asyncio.to_thread(ck.decrypt_file, full, tmp, dek)
        await asyncio.to_thread(os.replace, tmp, target)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    job.decrypted += 1
    job.bytes_in += size
    job.bytes_out += written


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _copy_file(src: str, dst: str) -> None:
    tmp = dst + ck.SUFFIX_PLAIN
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def forget_all() -> None:
    """进程退出时收尾，主要是让测试能干净重来。"""
    for task in list(_TASKS.values()):
        if not task.done():
            task.cancel()
    _TASKS.clear()
    _JOBS.clear()
