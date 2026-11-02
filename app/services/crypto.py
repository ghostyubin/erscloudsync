"""客户端加密：文件在本地加密后才上传，云端只存密文。

**文件名和路径不变**，只有内容被加密。所以云端看到的仍是 ``/照片/2024/a.jpg``，
但用任何客户端打开都是乱码。这是刻意的——换成乱码名字之后，出问题没法排查，
也没法在网盘客户端里认出是哪个文件。

几个不显然但必须遵守的设计约束：

* **必须先落临时文件再上传**，不能流式加密边传。百度 ``precreate`` 要求每个
  4MB 分片的 md5、115 要求整文件 SHA1，两者都得先有一个完整的密文文件才能算
  出哈希。临时文件用 ``源文件 + .bsenc.part``，同目录（同一次写入，不跨盘），
  而且 ``.part`` 本来就在 ``DEFAULT_EXCLUDE_PATTERNS`` 里，扫描器不会把它当成
  新文件同步上去。
* **分块大小对齐百度分片（4MiB）**，密文分块边界和上传分块边界重合，以后想
  改成边加密边算 md5 时不用改文件格式。
* **加密文件自带魔数头**，下载时只读头就能判断要不要解密（魔数 + 版本 + 分块
  大小都要合法）。因为文件名不变，没法靠后缀区分明文和密文——「只加密新文件」
  意味着云端必然是混合的。
* **密码忘了就永远打不开**，没有找回途径。所以密码要经 scrypt 派生（不是直接
  当密钥用），数据库里只存 salt、验证块和被包裹的 KEK，明文密码不落库。
* **改密码只重包裹 DEK**，不重传文件。salt / 验证块 / 所有任务的新包裹必须在
  同一个事务里落地，否则崩溃会留下半新半旧、旧密码新密码都解不开的状态。

密钥层级：``密码 → scrypt → KEK → 包裹 → 每任务 DEK → 加密文件块``。
为了让 NAS 无人值守，KEK 另有一份用密钥文件（``/config/enc.key``，0600）包裹的
副本存在库里——所以备份数据库时如果没带上这个文件，光有库是解不开的；反过来，
不想要自动解锁就把这个文件移走。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import struct
import time
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app import database as db
from app.config import DATA_DIR
from app.version import APP_VERSION

logger = logging.getLogger(__name__)

MAGIC = b"BSENC"
VERSION = 2
MIN_VERSION = 1
# <magic 5s><ver B><flags B><key_id B><chunk I><nonce prefix 8s><plain size Q><保留 20s>
HEADER = struct.Struct("<5sBBBI8sQ20s")
HEADER_SIZE = HEADER.size  # 48
CHUNK_SIZE = 4 * 1024 * 1024
TAG_SIZE = 16
KEY_SIZE = 32
MAX_CHUNK = 64 * 1024 * 1024

# 文件头后面可选的一段「钥匙牌」，见 build_key_block() 的说明。
FLAG_KEYBLOCK = 0x02
# <task id I><salt 16s><wrapped len H>
KEYBLOCK = struct.Struct("<I16sH")
KEYBLOCK_HEAD = 2
KEYBLOCK_SIZE = KEYBLOCK_HEAD + KEYBLOCK.size + 12 + KEY_SIZE + TAG_SIZE  # 84

# 中转文件后缀都以 .part 结尾，扫描器会忽略它们
SUFFIX_CIPHER = ".bsenc.part"
SUFFIX_PLAIN = ".bsenc.dec.part"

SALT_KEY = "crypto_salt"
VERIFIER_KEY = "crypto_verifier"
SEALED_KEY = "crypto_sealed_kek"
VERIFY_PLAIN = b"bsenc-verify-v1"
KEYFILE = DATA_DIR / "enc.key"

# scrypt 参数：约 32MB 内存、单次 0.1s 左右。NAS 上只在启动/解锁时跑一次，
# 每次同步不需要重新派生。
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1

MIN_PASSWORD = 8


class CryptoError(Exception):
    """加密相关的可预期错误，消息直接给用户看。"""


class KeyLocked(CryptoError):
    """还没解锁（没设过密码，或者 enc.key 和数据库对不上）。"""


class NotEncrypted(CryptoError):
    """这个文件不是我们加密的。"""


class DecryptError(CryptoError):
    """能认出是加密文件，但解不开（密钥不对或内容被改过）。"""


# --------------------------------------------------------------------------
# 文件格式
# --------------------------------------------------------------------------
def encrypted_size(plain_size: int, key_block: bool = True) -> int:
    """密文大小 = 头 +（钥匙牌）+ 明文 + 每块 16 字节认证标签。

    空文件也会写一个「0 长度明文」的标签，所以空文件也有一块。那个标签
    唯一的用处是拿头当 AAD 认证一遍——否则空文件的头谁都能改，而不被察觉。
    """
    blocks = max(1, (plain_size + CHUNK_SIZE - 1) // CHUNK_SIZE)
    extra = KEYBLOCK_SIZE if key_block else 0
    return HEADER_SIZE + extra + plain_size + TAG_SIZE * blocks


def build_key_block(task_id: int, salt: bytes, wrapped: str) -> bytes:
    """拼出文件头后面那段「钥匙牌」：任务号 + salt + 被 KEK 包裹的任务密钥。

    放这一段的唯一目的，是让**只凭密码**就能解密——和 Synology 的
    Cloud Sync Decryption Tool 一样，不需要数据库、也不需要私钥文件。
    密码 → scrypt(salt) → KEK → 解开这里的 wrapped → 任务密钥 → 解文件。

    代价必须说清楚：云端密文里因此带了一份「可以离线验证密码猜想」的样本。
    每猜一次都要跑一遍 scrypt（32MB / 0.1 秒级），强密码下不构成实际威胁，
    但这确实比「云端什么都不给」弱一档。**这个代价是固定的，没有开关**：
    去掉它跨机器解密就不成立，密码本身要够强。
    """
    raw = _unb64(wrapped)
    body = KEYBLOCK.pack(task_id, salt, len(raw)) + raw
    return struct.pack("<H", len(body)) + body


def _parse_key_block(fh) -> Optional[dict]:
    head = fh.read(KEYBLOCK_HEAD)
    if len(head) != KEYBLOCK_HEAD:
        return None
    (blen,) = struct.unpack("<H", head)
    if blen < KEYBLOCK.size or blen > 4096:
        return None
    body = fh.read(blen)
    if len(body) != blen:
        return None
    task_id, salt, wlen = KEYBLOCK.unpack(body[:KEYBLOCK.size])
    if KEYBLOCK.size + wlen != blen:
        return None
    return {"task_id": task_id, "salt": salt,
            "wrapped": _b64(body[KEYBLOCK.size:]), "raw": head + body}


def is_encrypted(path: str) -> bool:
    """判断这个文件是不是我们加密的。

    不能只看魔数——文件名不变意味着云端是明文密文混合的，而一个恰好以
    ``BSENC`` 开头的普通文件（二进制格式碰巧撞上，或者用户自己压了个包）
    会被误判成密文，然后解密失败、下载失败。所以魔数之外还要求版本号和
    分块大小都落在合法区间，长度至少够一个空文件的密文。
    """
    try:
        if os.path.getsize(path) < HEADER_SIZE + TAG_SIZE:
            return False
    except OSError:
        return False
    header = read_header(path)
    if not header or not (MIN_VERSION <= header["version"] <= VERSION):
        return False
    chunk = header["chunk_size"]
    return 0 < chunk <= MAX_CHUNK


def read_header(path: str) -> Optional[dict]:
    """读头（含可选的钥匙牌）。返回的 aad / data_offset 解密时要用。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(HEADER_SIZE)
            if len(raw) != HEADER_SIZE:
                return None
            magic, version, flags, key_id, chunk, prefix, plain_size, _ = \
                HEADER.unpack(raw)
            if magic != MAGIC or version > VERSION:
                return None
            block = None
            aad = raw
            offset = HEADER_SIZE
            if (flags & FLAG_KEYBLOCK) and version >= 2:
                block = _parse_key_block(fh)
                if not block:
                    return None
                aad = raw + block["raw"]
                offset = HEADER_SIZE + len(block["raw"])
    except OSError:
        return None
    return {"version": version, "flags": flags, "key_id": key_id,
            "chunk_size": chunk, "nonce_prefix": prefix,
            "plain_size": plain_size, "raw": raw,
            "block": block, "aad": aad, "data_offset": offset}


def dek_from_block(block: dict, password: str) -> bytes:
    """用密码解开文件头里的「钥匙牌」，拿到这个文件的数据密钥。

    这是「只凭密码解密」的唯一支点：密码 → scrypt(salt) → KEK → 解开包裹的
    DEK。整个过程不需要数据库、不需要本机配置，所以任何一台部署了本程序的
    机器都能解别人的密文——这也是「解密」页面的工作原理。

    失败一律抛 ``DecryptError``：密码不对和文件被改过在这里是同一件事，
    AES-GCM 的认证标签分不出来，也不该分。
    """
    kek = _derive((password or "").strip(), block["salt"])
    return _unwrap(kek, block["wrapped"], block["task_id"])


def encrypt_file(src: str, dst: str, key: bytes, key_block: bytes = b"") -> int:
    """把 src 加密写到 dst，返回密文字节数。

    阻塞调用（纯磁盘 + CPU），调用方用 ``asyncio.to_thread`` 包起来。

    按 ``plain_size`` 精确读，不用「读到 EOF 为止」：实时同步可能正好在这
    几秒里往源文件追加内容，多读的部分会被加密进密文，但头里记的还是旧
    长度，解密时就只解前 ``plain_size`` 字节、后面的块被静默丢弃——不报错、
    也没人发现。宁可这里报错重试。
    """
    plain_size = os.path.getsize(src)
    prefix = secrets.token_bytes(8)
    flags = FLAG_KEYBLOCK if key_block else 0
    header = HEADER.pack(MAGIC, VERSION, flags, 1, CHUNK_SIZE, prefix,
                         plain_size, b"\x00" * 20)
    # 钥匙牌也进 AAD：它是承重的（里面有 salt 和包裹的密钥），不能让人改。
    aad = header + key_block
    aead = AESGCM(key)
    written = HEADER_SIZE + len(key_block)
    index = 0
    remaining = plain_size
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(aad)
        while remaining > 0:
            want = min(CHUNK_SIZE, remaining)
            data = fin.read(want)
            if len(data) != want:
                raise CryptoError("源文件在加密过程中被截断，请等它写完再同步")
            # IV = 文件级随机前缀 + 块序号，所以每块不用单独存 nonce，
            # 同一文件内也不会重复（同密钥下 IV 复用会直接毁掉 GCM）。
            blob = aead.encrypt(prefix + index.to_bytes(4, "big"), data, aad)
            fout.write(blob)
            written += len(blob)
            index += 1
            remaining -= len(data)
        if plain_size == 0:
            # 空也要一个标签，把头绑上去（否则空文件的头完全不受保护）
            blob = aead.encrypt(prefix + (0).to_bytes(4, "big"), b"", aad)
            fout.write(blob)
            written += len(blob)
    expected = encrypted_size(plain_size, bool(key_block))
    if written != expected:
        raise CryptoError(f"密文长度异常（{written} != {expected}），源文件在加密时被改过")
    return written



def decrypt_file(src: str, dst: str, key: bytes) -> int:
    """把 src 解密写到 dst，返回明文字节数。"""
    header = read_header(src)
    if not header:
        raise NotEncrypted("不是加密文件")
    if not (MIN_VERSION <= header["version"] <= VERSION):
        raise CryptoError(f"不支持的加密版本 {header['version']}")
    chunk = header["chunk_size"]
    if chunk <= 0 or chunk > MAX_CHUNK:
        raise CryptoError("加密文件头里的分块大小异常")
    plain_size = header["plain_size"]
    prefix = header["nonce_prefix"]
    raw = header["aad"]          # 头 + 钥匙牌，两者都参与认证
    aead = AESGCM(key)
    done = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fin.seek(header["data_offset"])
        # 空文件这里也要走一遍：blocks 至少为 1，take 为 0，只校验那个标签。
        blocks = max(1, (plain_size + chunk - 1) // chunk)
        for index in range(blocks):
            take = min(chunk, plain_size - done)
            blob = fin.read(take + TAG_SIZE)
            if len(blob) != take + TAG_SIZE:
                raise DecryptError("文件被截断，密文长度不足")
            try:
                fout.write(aead.decrypt(prefix + index.to_bytes(4, "big"),
                                        blob, raw))
            except InvalidTag:
                raise DecryptError("解密校验失败：密钥不对，或者文件被改过")
            done += take
        # 头里写的长度之外的字节一律不认：追加数据不该被无声忽略。
        if fin.read(1):
            raise DecryptError("密文尾部有多余数据，文件可能被篡改")
    return plain_size


# --------------------------------------------------------------------------
# 密钥
# --------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("密钥数据损坏") from exc


def _derive(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=KEY_SIZE, n=SCRYPT_N, r=SCRYPT_R,
                  p=SCRYPT_P).derive(password.encode("utf-8"))


def _wrap(kek: bytes, dek: bytes, task_id: int) -> str:
    """用 KEK 包裹任务的 DEK。AAD 绑任务号，防止把一个任务的密钥挪到另一个。"""
    nonce = secrets.token_bytes(12)
    blob = AESGCM(kek).encrypt(nonce, dek, f"task:{task_id}".encode())
    return _b64(nonce + blob)


def _unwrap(kek: bytes, packed: str, task_id: int) -> bytes:
    blob = _unb64(packed)
    if len(blob) < 13:
        raise CryptoError("任务密钥数据损坏")
    try:
        return AESGCM(kek).decrypt(blob[:12], blob[12:],
                                   f"task:{task_id}".encode())
    except InvalidTag:
        raise DecryptError("这个任务的密钥解不开（可能换过密码但没重包裹）")


class Keyring:
    """全局密码 + 每任务 DEK 的管理与内存缓存。"""

    def __init__(self) -> None:
        self._kek: Optional[bytes] = None
        self._deks: dict[int, bytes] = {}
        self._blocks: dict[int, bytes] = {}
        # 第一次上传时多个文件会同时进来要密钥。不串行化的话每个协程都会
        # 生成一把不同的 DEK 并写库，后写的覆盖先写的——先写那批文件就永远
        # 解不开了（实测并发 3 时会随机坏一个文件）。
        self._lock = asyncio.Lock()

    # ---- 状态 ----
    async def initialized(self) -> bool:
        return bool(await db.db_get_setting(SALT_KEY, ""))

    async def status(self) -> dict:
        salt = await db.db_get_setting(SALT_KEY, "")
        sealed = await db.db_get_setting(SEALED_KEY, "")
        if not salt:
            return {"initialized": False, "unlocked": False,
                    "keyfile": KEYFILE.exists(),
                    "message": "还没有设置加密密码"}
        if not self._kek:
            # 「有封存副本 + 有密钥文件」不等于「能解开」：换过机器、恢复过
            # 数据库、或者改过密码导致两边对不上，都会让它们同时在却解不开。
            # 真的试一次，别在界面上报假状态。
            try:
                await self.kek()
            except CryptoError:
                pass
        return {
            "initialized": True,
            "unlocked": bool(self._kek),
            "keyfile": KEYFILE.exists(),
            "message": "" if self._kek or not sealed else "需要输入密码解锁",
        }

    def _read_keyfile(self, create: bool = False) -> Optional[bytes]:
        try:
            data = KEYFILE.read_bytes()
            if len(data) == KEY_SIZE:
                return data
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("读不到密钥文件 %s: %s", KEYFILE, exc)
            return None
        if not create:
            return None
        data = secrets.token_bytes(KEY_SIZE)
        KEYFILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(KEYFILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        logger.info("已生成密钥文件 %s（备份数据库时记得一起备份）", KEYFILE)
        return data

    async def _seal(self, kek: bytes, password: Optional[str] = None) -> None:
        """把 KEK 用密钥文件里的 FK 包一份存库，实现无人值守自动解密。"""
        fk = self._read_keyfile(create=True)
        if not fk:
            return
        nonce = secrets.token_bytes(12)
        blob = AESGCM(fk).encrypt(nonce, kek, b"bsenc-kek")
        await db.db_set_setting(SEALED_KEY, _b64(nonce + blob))

    # ---- 生命周期 ----
    async def setup(self, password: str) -> dict:
        if await self.initialized():
            raise CryptoError("已经设置过加密密码，要改请用「更改密码」")
        password = (password or "").strip()
        if len(password) < MIN_PASSWORD:
            raise CryptoError(f"密码至少 {MIN_PASSWORD} 位")

        salt = secrets.token_bytes(16)
        kek = await asyncio.to_thread(_derive, password, salt)
        verifier = AESGCM(kek).encrypt(b"\x00" * 12, VERIFY_PLAIN, None)
        await db.db_set_setting(SALT_KEY, _b64(salt))
        await db.db_set_setting(VERIFIER_KEY, _b64(verifier))
        self._kek = kek
        self._deks.clear()
        self._blocks.clear()
        await self._seal(kek, password)
        return {"ok": True, "message": "加密密码已设置"}

    async def unlock(self, password: str) -> dict:
        salt = await db.db_get_setting(SALT_KEY, "")
        if not salt:
            raise CryptoError("还没有设置加密密码")
        kek = await asyncio.to_thread(_derive, (password or "").strip(),
                                      _unb64(salt))
        verifier = await db.db_get_setting(VERIFIER_KEY, "")
        if not verifier:
            raise CryptoError("验证块丢失，无法校验密码")
        try:
            AESGCM(kek).decrypt(b"\x00" * 12, _unb64(verifier), None)
        except InvalidTag:
            return {"ok": False, "message": "密码不正确"}
        self._kek = kek
        self._deks.clear()
        self._blocks.clear()
        await self._seal(kek, (password or "").strip())
        return {"ok": True, "message": "已解锁，之后重启会自动运行"}

    async def export_bundle(self, password: str) -> dict:
        """导出离线解密密钥包（给 tools/bsenc-decrypt.py 用）。

        **光有密码和密文是解不开的**：密码只能推出 KEK，真正加密每个文件的
        是随机的 DEK，它只以「被 KEK 包裹」的形式存在库里。所以要把 salt 和
        包裹后的密钥一起带走，否则 NAS 一旦没了，密码也就废了。

        必须重新输密码确认：这个文件等于把所有任务的解密权打包带走，不能让
        一个开着页面的浏览器顺手导走。
        """
        salt = await db.db_get_setting(SALT_KEY, "")
        if not salt:
            raise CryptoError("还没有设置加密密码")
        kek = await asyncio.to_thread(_derive, (password or "").strip(),
                                      _unb64(salt))
        verifier = await db.db_get_setting(VERIFIER_KEY, "")
        if verifier:
            try:
                AESGCM(kek).decrypt(b"\x00" * 12, _unb64(verifier), None)
            except InvalidTag:
                raise CryptoError("密码不正确")

        keys = []
        for task in await db.db_list_tasks():
            packed = (task.get("enc_wrapped") or "").strip()
            if not packed:
                continue
            _unwrap(kek, packed, int(task["id"]))   # 解不开就别导出坏包
            keys.append({"task_id": int(task["id"]),
                         "task_name": task.get("name") or "",
                         "wrapped": packed})
        if not keys:
            raise CryptoError("还没有任务生成过加密密钥，导出的密钥包会是空的")

        return {
            "format": "bsenc-keybundle",
            "version": 1,
            "app_version": APP_VERSION,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R,
                    "p": SCRYPT_P, "length": KEY_SIZE, "salt": _b64(_unb64(salt))},
            "verifier": verifier,
            "keys": keys,
            "note": "配合 tools/bsenc-decrypt.py 使用。里面是包裹后的任务密钥，"
                    "仍需密码才能解开——但泄漏了它 + 密码就等于泄漏全部文件。",
        }

    async def change_password(self, old: str, new: str) -> dict:
        async with self._lock:          # 别和正在生成密钥的同步撞上
            return await self._change_password(old, new)

    async def _change_password(self, old: str, new: str) -> dict:
        salt = await db.db_get_setting(SALT_KEY, "")
        if not salt:
            raise CryptoError("还没有设置加密密码")
        new = (new or "").strip()
        if len(new) < MIN_PASSWORD:
            raise CryptoError(f"新密码至少 {MIN_PASSWORD} 位")

        old_kek = await asyncio.to_thread(_derive, (old or "").strip(),
                                          _unb64(salt))
        verifier = await db.db_get_setting(VERIFIER_KEY, "")
        try:
            AESGCM(old_kek).decrypt(b"\x00" * 12, _unb64(verifier), None)
        except InvalidTag:
            return {"ok": False, "message": "原密码不正确"}

        # 先把所有任务的 DEK 解出来，再换 salt 重包。任何一把解不开都在
        # 这里就失败，此时还没写库，改密码等于没发生——比写了一半强得多。
        unwrapped: list[tuple[int, bytes]] = []
        for task in await db.db_list_tasks():
            packed = (task.get("enc_wrapped") or "").strip()
            if not packed:
                continue
            unwrapped.append((int(task["id"]), _unwrap(old_kek, packed, int(task["id"]))))

        new_salt = secrets.token_bytes(16)
        new_kek = await asyncio.to_thread(_derive, new, new_salt)
        verifier = AESGCM(new_kek).encrypt(b"\x00" * 12, VERIFY_PLAIN, None)

        # salt / 验证块 / 所有 wrapped 必须在同一个事务里落地，否则中途崩溃
        # 会留下「新 salt + 部分旧 wrapped」，那部分任务就永久解不开了。
        await db.db_rewrap_crypto(
            SALT_KEY, _b64(new_salt),
            VERIFIER_KEY, _b64(verifier),
            [(task_id, _wrap(new_kek, dek, task_id)) for task_id, dek in unwrapped])

        self._kek = new_kek
        self._deks.clear()
        self._blocks.clear()
        await self._seal(new_kek)
        return {"ok": True,
                "message": f"密码已更换（{len(unwrapped)} 个任务的密钥已重包裹，文件没有重传）"}

    # ---- 取密钥 ----
    async def kek(self) -> bytes:
        if self._kek:
            return self._kek
        sealed = await db.db_get_setting(SEALED_KEY, "")
        if not sealed:
            raise KeyLocked("还没有可用的自动解锁密钥，请先在「全局设置 → 客户端加密」输入密码")
        fk = self._read_keyfile()
        if not fk:
            raise KeyLocked(f"密钥文件 {KEYFILE.name} 不见了，请重新输入密码解锁")
        blob = _unb64(sealed)
        try:
            self._kek = AESGCM(fk).decrypt(blob[:12], blob[12:], b"bsenc-kek")
        except InvalidTag:
            raise KeyLocked("密钥文件与数据库不匹配，请重新输入密码解锁")
        return self._kek

    async def dek_for_task(self, task: dict) -> bytes:
        """取任务的数据密钥，第一次用时生成并落库。

        整个过程加锁，并且拿到锁之后重新读一次任务行：并发的第一个文件生成
        密钥并写库后，其余文件必须解开这把已有的密钥，而不是再生成一把。
        """
        task_id = int(task["id"])
        cached = self._deks.get(task_id)
        if cached:
            return cached
        async with self._lock:
            cached = self._deks.get(task_id)
            if cached:
                return cached
            fresh = await db.db_get_task(task_id) or task
            kek = await self.kek()
            packed = (fresh.get("enc_wrapped") or "").strip()
            if not packed:
                dek = secrets.token_bytes(KEY_SIZE)
                await db.db_update_task(task_id,
                                        enc_wrapped=_wrap(kek, dek, task_id),
                                        enc_key_id=1)
                logger.info("任务 %s 已生成新的数据密钥", task_id)
            else:
                dek = _unwrap(kek, packed, task_id)
            self._deks[task_id] = dek
            self._blocks.pop(task_id, None)     # 密钥变了，钥匙牌要重拼
            return dek

    async def block_for(self, task: dict) -> Optional[bytes]:
        """拼出写进文件头的「钥匙牌」，让只凭密码也能解密。

        这里**恒定**附带，没有开关。曾经有过一个 ``crypto_include_keyblock``
        设置可以关掉它，但那样一来密文就只有本机数据库（或拿着私钥文件的人）
        能解，换台机器输同样的密码会报「密钥不对」——跨机器解密直接废掉，
        省下的那 84 字节毫无意义。所以开关连同设置项一起删了。

        返回 None 只在一种情况下发生：任务还没有包裹好的 DEK，或者还没设过
        密码（没有 salt），此时本来就无从拼装。
        """
        task_id = int(task["id"])
        cached = self._blocks.get(task_id)
        if cached:
            return cached
        async with self._lock:
            cached = self._blocks.get(task_id)
            if cached:
                return cached
            fresh = await db.db_get_task(task_id) or task
            packed = (fresh.get("enc_wrapped") or "").strip()
            salt = await db.db_get_setting(SALT_KEY, "")
            if not packed or not salt:
                return None
            block = build_key_block(task_id, _unb64(salt), packed)
            self._blocks[task_id] = block
            return block

    def task_keyfile(self, task: dict, dek: bytes) -> dict:
        """一个任务的「私钥文件」内容，交给用户离线保管。

        拿到它就不需要密码、不需要数据库，能解开这个任务上传过的所有文件。
        """
        return {
            "format": "bsenc-taskkey",
            "version": 1,
            "task_id": int(task["id"]),
            "task_name": task.get("name") or "",
            "key_id": 1,
            "dek": _b64(dek),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "app_version": APP_VERSION,
            "note": "这个文件等于该任务全部云端文件的解密权，和密码同等重要。"
                    "请离线保存；丢了密码时它是唯一的出路。",
        }

    def forget_task(self, task_id: int) -> None:
        self._deks.pop(int(task_id), None)
        self._blocks.pop(int(task_id), None)

    def forget_blocks(self) -> None:
        """设置改了之后让钥匙牌缓存失效（不用重新派生 KEK）。"""
        self._blocks.clear()

    async def verify_password(self, password: str) -> bool:
        """只校验密码，不改任何状态（导出密钥前用）。"""
        salt = await db.db_get_setting(SALT_KEY, "")
        verifier = await db.db_get_setting(VERIFIER_KEY, "")
        if not salt or not verifier:
            return False
        kek = await asyncio.to_thread(_derive, (password or "").strip(),
                                      _unb64(salt))
        try:
            AESGCM(kek).decrypt(b"\x00" * 12, _unb64(verifier), None)
            return True
        except InvalidTag:
            return False


keyring = Keyring()


# --------------------------------------------------------------------------
# 给同步引擎用的高层接口
# --------------------------------------------------------------------------
async def task_key(task: dict) -> Optional[bytes]:
    """任务开了加密就返回它的 DEK，否则 None。"""
    if not int(task.get("enc_enabled") or 0):
        return None
    return await keyring.dek_for_task(task)


async def download_keys(task: Optional[dict] = None) -> list[bytes]:
    """下载时可能用到的密钥，按可能性排序。

    文件名不变，所以「这个文件是哪个任务加密的」只能靠试。自己的任务排第一，
    其它任务的密钥兜底——两个任务指向同一个云端目录时不会解不开。
    """
    keys: list[bytes] = []
    seen: set[bytes] = set()

    def _add(key: bytes) -> None:
        if key not in seen:
            seen.add(key)
            keys.append(key)

    if task and int(task.get("enc_enabled") or 0):
        try:
            _add(await keyring.dek_for_task(task))
        except (KeyLocked, CryptoError):
            raise
    try:
        for other in await db.db_list_tasks():
            if task and int(other["id"]) == int(task["id"]):
                continue
            if not (other.get("enc_wrapped") or "").strip():
                continue
            try:
                _add(await keyring.dek_for_task(other))
            except CryptoError:
                continue
    except Exception:  # noqa: BLE001 - 兜底逻辑不该拖垮下载
        logger.debug("枚举其它任务密钥失败", exc_info=True)
    return keys


async def encrypt_for_upload(task: dict, src: str) -> Optional[str]:
    """加密到 ``src + .bsenc.part``，返回中转文件路径；没开加密返回 None。"""
    if not int(task.get("enc_enabled") or 0):
        return None

    key = await task_key(task)
    if key is None:
        return None
    block = await keyring.block_for(task) or b""
    staged = src + SUFFIX_CIPHER
    try:
        size = await asyncio.to_thread(encrypt_file, src, staged, key, block)
    except Exception:
        _remove(staged)
        raise
    logger.info("已加密 %s → %d 字节", os.path.basename(src), size)
    return staged


async def settle_download(staged: str, final: str,
                          task: Optional[dict] = None,
                          keys: Optional[list[bytes]] = None) -> bool:
    """把下载下来的中转文件变成目标文件。

    是密文就解密，不是就直接改名。返回是否做过解密。

    密钥是「读到头是密文之后」才去取的：绝大多数下载都是明文（没开加密的
    任务、加密前就在云端的旧文件），每个文件都枚举一遍任务密钥纯属浪费。
    """
    if not await asyncio.to_thread(is_encrypted, staged):
        os.replace(staged, final)
        return False

    if keys is None:
        keys = await download_keys(task)
    if not keys:
        _remove(staged)
        raise KeyLocked("云端的文件是加密的，请先在「全局设置 → 客户端加密」设置密码")

    last: Optional[Exception] = None
    for key in keys:
        plain_tmp = final + SUFFIX_PLAIN
        try:
            await asyncio.to_thread(decrypt_file, staged, plain_tmp, key)
            os.replace(plain_tmp, final)
            _remove(staged)
            return True
        except DecryptError as exc:
            _remove(plain_tmp)
            last = exc
            continue
    _remove(staged)
    raise last or DecryptError("解密失败")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def remove_temp(path: Optional[str]) -> None:
    """删中转文件。调用方在 finally 里用，重复删也无所谓。"""
    if path:
        _remove(path)


def cleanup_stale(roots: list[str], max_age: float = 24 * 3600) -> int:
    """清理上次被 kill 打断留下的中转文件。

    只删我们自己那两个后缀，绝不会碰用户自己的 ``.part``。
    """
    removed = 0
    cutoff = time.time() - max_age
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith((SUFFIX_CIPHER, SUFFIX_PLAIN)):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(full) > cutoff:
                        continue
                    os.remove(full)
                    removed += 1
                except OSError:
                    continue
    if removed:
        logger.info("清理了 %d 个加密中转残留文件", removed)
    return removed
