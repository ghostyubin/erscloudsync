#!/usr/bin/env python3
"""BauduSync 离线解密工具（bsenc）。

对标 Synology 的 Cloud Sync Decryption Tool：不依赖 BauduSync 本体，只用
标准库 + ``cryptography``，拷到任何机器上都能跑。**两种凭据都能单独解密**：

* ``--password``   只要密码（文件头里自带 salt 和密码封装的密钥）
* ``--key``        创建任务时导出的私钥文件（丢了密码也能解）

    # 只看密钥概况，不写任何文件
    python3 bsenc-decrypt.py info --db /config/baudusync.db

    # 一个密码，把整个目录解密到 ./plain
    python3 bsenc-decrypt.py decrypt --password 'xxx' -o ./plain ~/cloud/照片

    # 用任务私钥（不需要密码）
    python3 bsenc-decrypt.py decrypt --key task-1-key.json -o ./plain ~/cloud

    # 原地解密（用明文覆盖密文）
    python3 bsenc-decrypt.py decrypt --password 'xxx' --in-place ~/cloud/照片

    # 只校验完整性，不产出文件
    python3 bsenc-decrypt.py decrypt --password 'xxx' --verify-only ~/cloud

补充来源（可选，不是必需）：``--db`` 直接读 BauduSync 的数据库、``--keys``
读界面导出的密钥包。它们的作用是「用当前密码解开所有文件」——文件头里的
钥匙牌是加密那一刻的密码，改过密码之后的老文件要用当时的密码；用库里的
密钥则永远是最新的。

文件格式见 README 的「客户端加密」章节。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Iterable, Optional

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
except ImportError:  # pragma: no cover - 环境问题
    sys.stderr.write(
        "缺少依赖：pip install cryptography\n"
        "（Debian/Ubuntu 也可以 apt install python3-cryptography）\n")
    raise SystemExit(2)

# --------------------------------------------------------------------------
# 文件格式（与 app/services/crypto.py 必须一致）
# --------------------------------------------------------------------------
MAGIC = b"BSENC"
VERSION = 2
MIN_VERSION = 1
HEADER = struct.Struct("<5sBBBI8sQ20s")
HEADER_SIZE = HEADER.size  # 48
TAG_SIZE = 16
KEY_SIZE = 32
MAX_CHUNK = 64 * 1024 * 1024

FLAG_KEYBLOCK = 0x02
KEYBLOCK = struct.Struct("<I16sH")
KEYBLOCK_HEAD = 2

KDF_DEFAULTS = {"name": "scrypt", "n": 2 ** 15, "r": 8, "p": 1, "length": KEY_SIZE}

SALT_KEY = "crypto_salt"
VERIFIER_KEY = "crypto_verifier"
SEALED_KEY = "crypto_sealed_kek"


class ToolError(Exception):
    """给用户看的错误。"""


# --------------------------------------------------------------------------
# 文件头
# --------------------------------------------------------------------------
def read_header(path: str) -> Optional[dict]:
    """读头，以及可选的「钥匙牌」（版本 2 才有）。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(HEADER_SIZE)
            if len(raw) != HEADER_SIZE:
                return None
            magic, version, flags, key_id, chunk, prefix, plain_size, _ = \
                HEADER.unpack(raw)
            if magic != MAGIC or not (MIN_VERSION <= version <= VERSION):
                return None
            if not (0 < chunk <= MAX_CHUNK):
                return None
            block = None
            aad = raw
            offset = HEADER_SIZE
            if (flags & FLAG_KEYBLOCK) and version >= 2:
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
                block = {"task_id": task_id, "salt": salt,
                         "wrapped": base64.b64encode(
                             body[KEYBLOCK.size:]).decode()}
                aad = raw + head + body
                offset = HEADER_SIZE + KEYBLOCK_HEAD + blen
    except OSError:
        return None
    return {"version": version, "flags": flags, "key_id": key_id,
            "chunk_size": chunk, "nonce_prefix": prefix,
            "plain_size": plain_size, "raw": raw,
            "block": block, "aad": aad, "data_offset": offset}


def looks_encrypted(path: str) -> bool:
    """和本体一样的判据：魔数 + 版本 + 分块区间 + 最小长度。

    只认 5 字节魔数会把恰好以 ``BSENC`` 开头的普通文件误判成密文。
    """
    try:
        if os.path.getsize(path) < HEADER_SIZE + TAG_SIZE:
            return False
    except OSError:
        return False
    return read_header(path) is not None


def decrypt_file(src: str, dst: str, key: bytes) -> int:
    """解密一个文件，返回明文字节数。任何一处校验失败都会抛错。"""
    header = read_header(src)
    if not header:
        raise ToolError("不是 bsenc 加密文件")
    chunk = header["chunk_size"]
    plain_size = header["plain_size"]
    prefix = header["nonce_prefix"]
    aad = header["aad"]
    aead = AESGCM(key)
    done = 0
    blocks = max(1, (plain_size + chunk - 1) // chunk)

    tmp = dst + ".tmp"
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            fin.seek(header["data_offset"])
            for index in range(blocks):
                take = min(chunk, plain_size - done)
                blob = fin.read(take + TAG_SIZE)
                if len(blob) != take + TAG_SIZE:
                    raise ToolError("文件被截断，密文长度不足")
                try:
                    fout.write(aead.decrypt(prefix + index.to_bytes(4, "big"),
                                            blob, aad))
                except InvalidTag:
                    raise ToolError("认证失败：密钥不对，或者文件被改过")
                done += take
            if fin.read(1):
                raise ToolError("密文尾部有多余数据，文件可能被篡改")
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return plain_size


# --------------------------------------------------------------------------
# 密钥
# --------------------------------------------------------------------------
def _derive(password: str, salt: bytes, kdf: dict) -> bytes:
    return Scrypt(salt=salt, length=int(kdf.get("length", KEY_SIZE)),
                  n=int(kdf.get("n", KDF_DEFAULTS["n"])),
                  r=int(kdf.get("r", KDF_DEFAULTS["r"])),
                  p=int(kdf.get("p", KDF_DEFAULTS["p"]))).derive(
        password.encode("utf-8"))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"密钥数据不是合法的 base64：{exc}") from exc


def _unwrap(kek: bytes, packed: str, task_id: int) -> bytes:
    blob = _unb64(packed)
    if len(blob) < 13:
        raise ToolError(f"任务 {task_id} 的密钥数据损坏")
    try:
        return AESGCM(kek).decrypt(blob[:12], blob[12:], f"task:{task_id}".encode())
    except InvalidTag:
        raise ToolError("密码不对（或者文件里那份钥匙牌用的不是这个密码）")


def _check_verifier(kek: bytes, verifier: str) -> bool:
    if not verifier:
        return True
    try:
        AESGCM(kek).decrypt(b"\x00" * 12, _unb64(verifier), None)
        return True
    except InvalidTag:
        return False


class Credentials:
    """一份或几份凭据：密码 + 若干把现成的任务密钥。"""

    def __init__(self) -> None:
        self.static: list[tuple[str, bytes]] = []
        self.password: Optional[str] = None
        self.kdf: dict = dict(KDF_DEFAULTS)
        self.sources: list[str] = []
        self._cache: dict[tuple, bytes] = {}
        self._password_error: Optional[str] = None
        self._salt: bytes = b""
        self._verifier: str = ""
        self._wrapped: list[tuple[int, str, str]] = []
        self._sealed: str = ""
        self._keyfile: Optional[Path] = None

    # ---- 装载 ----
    def add_key(self, label: str, dek: bytes) -> None:
        if len(dek) != KEY_SIZE:
            raise ToolError(f"{label}：密钥长度不对（应该是 {KEY_SIZE} 字节）")
        if all(dek != k for _l, k in self.static):
            self.static.append((label, dek))

    def add_sqlite(self, path: str) -> None:
        """从 BauduSync 的数据库里取出 salt 和每个任务的包裹密钥。"""
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"找不到数据库文件：{p}")
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ToolError(f"打不开数据库：{exc}") from exc
        try:
            conn.row_factory = sqlite3.Row
            settings = {r["key"]: r["value"] for r in
                        conn.execute("SELECT key, value FROM settings")}
            rows = conn.execute(
                "SELECT id, name, enc_wrapped FROM tasks ORDER BY id").fetchall()
        except sqlite3.Error as exc:
            raise ToolError(f"读数据库失败（这是 BauduSync 的库吗？）：{exc}") from exc
        finally:
            conn.close()

        salt = settings.get(SALT_KEY, "")
        if not salt:
            raise ToolError(f"{p} 里没有加密配置（settings 表里找不到 {SALT_KEY}）")
        self.sources.append(f"数据库 {p.name}")
        self._load_wrapped(
            salt, settings.get(VERIFIER_KEY, ""),
            [{"task_id": int(r["id"]), "task_name": r["name"] or "",
              "wrapped": (r["enc_wrapped"] or "").strip()}
             for r in rows if (r["enc_wrapped"] or "").strip()],
            sealed=settings.get(SEALED_KEY, ""), keyfile=p.parent / "enc.key")

    def add_bundle(self, path: str) -> None:
        p = Path(path).expanduser()
        try:
            data = json.loads(p.read_text("utf-8"))
        except OSError as exc:
            raise ToolError(f"读不到密钥包：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise ToolError(f"密钥包不是合法 JSON：{exc}") from exc
        if data.get("format") != "bsenc-keybundle":
            raise ToolError("这不是 BauduSync 的密钥包（format 字段对不上）")
        kdf = data.get("kdf") or {}
        salt = kdf.get("salt") or ""
        if not salt:
            raise ToolError("密钥包里没有 salt")
        self.sources.append(f"密钥包 {p.name}")
        self.kdf = {**KDF_DEFAULTS,
                    **{k: v for k, v in kdf.items() if v is not None}}
        self._load_wrapped(salt, data.get("verifier") or "",
                           data.get("keys") or [])

    def add_taskkey(self, path: str) -> None:
        """任务私钥文件（对应 Synology 的 private.pem），不需要密码。"""
        p = Path(path).expanduser()
        try:
            data = json.loads(p.read_text("utf-8"))
        except OSError as exc:
            raise ToolError(f"读不到私钥文件：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise ToolError(f"私钥文件不是合法 JSON：{exc}") from exc
        if data.get("format") != "bsenc-taskkey":
            raise ToolError(f"{p.name} 不是 BauduSync 的任务私钥文件")
        label = f"{p.name}（任务 {data.get('task_id')} {data.get('task_name') or ''}）"
        self.add_key(label, _unb64(data.get("dek") or ""))
        self.sources.append(f"私钥 {p.name}")

    def _load_wrapped(self, salt_b64: str, verifier: str, keys: list[dict],
                      sealed: str = "", keyfile: Optional[Path] = None) -> None:
        """记下 salt/验证块，等真正知道密码时再派生 KEK（scrypt 很慢）。"""
        self._salt = _unb64(salt_b64)
        self._verifier = verifier
        self._wrapped = [(int(k["task_id"]), k.get("task_name") or "",
                          k["wrapped"]) for k in keys if k.get("wrapped")]
        self._sealed = sealed
        self._keyfile = keyfile

    # ---- 解锁 ----
    def unlock(self, password: Optional[str]) -> str:
        """用密码（或密钥文件）把库里/包里的包裹密钥解出来。返回怎么解的。"""
        self.password = password or self.password
        if not self._salt:
            return "文件头里的钥匙牌"
        if not self._wrapped:
            return "没有需要解开的包裹密钥"

        how = "密码"
        kek: Optional[bytes] = None
        if self.password:
            kek = _derive(self.password, self._salt, self.kdf)
            if not _check_verifier(kek, self._verifier):
                raise ToolError("密码不正确")
        else:
            if not self._sealed or not self._keyfile or not self._keyfile.is_file():
                return "没有密码，也没有可用的自动解锁密钥文件"
            fk = self._keyfile.read_bytes()
            if len(fk) != KEY_SIZE:
                raise ToolError(f"密钥文件 {self._keyfile} 长度不对，应该是 {KEY_SIZE} 字节")
            blob = _unb64(self._sealed)
            try:
                kek = AESGCM(fk).decrypt(blob[:12], blob[12:], b"bsenc-kek")
            except InvalidTag:
                raise ToolError("密钥文件与数据库不匹配（换过密码或不是同一套？）")
            how = f"密钥文件 {self._keyfile.name}（免密码）"

        for task_id, name, packed in self._wrapped:
            try:
                self.add_key(f"任务 {task_id} {name}".strip(),
                             _unwrap(kek, packed, task_id))
            except ToolError:
                continue
        return how

    # ---- 每文件的候选密钥 ----
    def candidates(self, header: dict) -> list[tuple[str, bytes]]:
        """先试文件头里那份钥匙牌（用密码解），再试现成的密钥。"""
        out: list[tuple[str, bytes]] = []
        block = header.get("block")
        if block and self.password:
            ck = (block["task_id"], block["salt"], block["wrapped"])
            dek = self._cache.get(ck)
            if dek is None:
                try:
                    kek = _derive(self.password, block["salt"], self.kdf)
                    dek = _unwrap(kek, block["wrapped"], block["task_id"])
                    self._cache[ck] = dek
                except ToolError as exc:
                    self._password_error = str(exc)
                    dek = None
            if dek:
                out.append((f"文件头钥匙牌（任务 {block['task_id']}）", dek))
        out.extend(self.static)
        if not out:
            if block and not self.password:
                raise ToolError("这个文件的头里有钥匙牌，需要密码（--password）才能解开")
            if self._password_error:
                raise ToolError(self._password_error)
            raise ToolError("没有可用的密钥：请给 --password，或者 --key / --keys / --db")
        return out

    def describe(self) -> str:
        bits = []
        if self.password:
            bits.append("密码")
        if self.static:
            bits.append(f"{len(self.static)} 把现成密钥")
        if not bits:
            bits.append("未指定")
        return "、".join(bits)


# --------------------------------------------------------------------------
# 批处理
# --------------------------------------------------------------------------
def iter_files(target: Path) -> Iterable[Path]:
    if target.is_file():
        yield target
    elif target.is_dir():
        for root, dirs, files in os.walk(target):
            dirs.sort()
            for name in sorted(files):
                yield Path(root) / name
    else:
        sys.stderr.write(f"跳过（不存在）：{target}\n")


def collect(targets: list[Path]) -> list[tuple[Path, Path]]:
    """[(源文件, 相对路径)]，相对路径含目标自身的目录名。"""
    out: list[tuple[Path, Path]] = []
    for t in targets:
        if t.is_file():
            out.append((t, Path(t.name)))
        elif t.is_dir():
            for f in iter_files(t):
                out.append((f, Path(t.name) / f.relative_to(t)))
    return out


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_info(args) -> int:
    cred = _credentials(args)
    print(f"凭据：{cred.describe()}")
    for s in cred.sources:
        print(f"  来源：{s}")
    if not cred._salt:
        if not cred.password and not cred.static:
            raise ToolError("请至少给出 --password、--key、--keys 或 --db 之一")
        print("没有数据库/密钥包：解密时按每个文件头里的钥匙牌 + 密码来解。")
        print("（这种用法只要密码，和 Synology 的 Cloud Sync Decryption Tool 一致）")
        return 0

    print(f"salt：{_b64(cred._salt)}")
    print(f"KDF：scrypt n={cred.kdf['n']} r={cred.kdf['r']} p={cred.kdf['p']}")
    print(f"验证块：{'有' if cred._verifier else '无'}")
    print(f"自动解锁副本：{'有' if cred._sealed else '无'}"
          + (f"，密钥文件 {cred._keyfile}" if cred._keyfile and cred._keyfile.is_file() else ""))
    print(f"包裹密钥：{len(cred._wrapped)} 个")
    for task_id, name, _packed in cred._wrapped:
        print(f"  - 任务 {task_id} {name}".rstrip())

    if args.password or (cred._sealed and cred._keyfile and cred._keyfile.is_file()) \
            or cred.static:
        how = cred.unlock(args.password)
        if not cred.static:
            print(f"（{how}）没有解出任何任务密钥。")
            return 0
        print(f"解锁成功（{how}），可用密钥 {len(cred.static)} 把：")
        for label, dek in cred.static:
            print(f"  - {label}：{dek[:4].hex()}…{dek[-2:].hex()}")
        return 0
    print("（没给密码，也没有密钥文件，未做解锁验证）")
    return 0


def cmd_decrypt(args) -> int:
    targets = [Path(t).expanduser() for t in args.targets]
    cred = _credentials(args)
    if cred._salt:
        # 先解锁：没有密码时可能是靠 enc.key 解开的，这时才有现成密钥
        cred.unlock(args.password)
    if not cred.password and not cred.static:
        raise ToolError("没有可用的解密凭据")

    pairs = collect(targets)
    if not pairs:
        raise ToolError("目标里没有文件")

    out_dir: Optional[Path] = None
    if not args.in_place and not args.verify_only:
        if not args.out:
            raise ToolError("要写文件就得给 -o 输出目录，或者用 --in-place / --verify-only")
        out_dir = Path(args.out).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

    total = sum(f.stat().st_size for f, _r in pairs if f.exists())
    print(f"凭据：{cred.describe()}；文件：{len(pairs)} 个（{human(total)}）")

    if args.in_place and not args.verify_only:
        print("⚠️  原地解密会用明文覆盖密文，不可撤销。")
        if not args.yes and not confirm("确定继续？"):
            return 1

    ok = skipped = failed = copied = 0
    total_plain = 0
    errors: list[tuple[str, str]] = []
    no_key = 0

    for index, (src, rel) in enumerate(pairs, 1):
        name = f"{rel}"
        try:
            header = read_header(str(src)) if looks_encrypted(str(src)) else None
            if not header:
                skipped += 1
                if out_dir and args.copy_plain:
                    dst = out_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _copy(src, dst)
                    copied += 1
                    _say(args, f"[{index}/{len(pairs)}] {name}  明文，已复制")
                else:
                    _say(args, f"[{index}/{len(pairs)}] {name}  明文，跳过", quiet=True)
                continue

            if out_dir:
                dst = out_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() and not args.overwrite:
                    raise ToolError(f"目标已存在：{dst}（加 --overwrite 覆盖）")
                written = _try_keys(src, str(dst), cred, header)
            elif args.verify_only:
                written = _try_keys(src, None, cred, header)
            else:
                written = _try_keys(src, str(src), cred, header)
            ok += 1
            total_plain += written
            _say(args, f"[{index}/{len(pairs)}] {name}  → {human(written)}")
        except ToolError as exc:
            if "没有可用的密钥" in str(exc) or "需要密码" in str(exc):
                no_key += 1
            failed += 1
            errors.append((name, str(exc)))
            _say(args, f"[{index}/{len(pairs)}] {name}  ✗ {exc}")
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不该中断整批
            failed += 1
            errors.append((name, str(exc)))
            _say(args, f"[{index}/{len(pairs)}] {name}  ✗ {exc}")

    print()
    print(f"完成：成功 {ok}，跳过 {skipped}，失败 {failed}"
          + (f"，附带复制 {copied}" if copied else ""))
    if ok:
        print(f"还原出 {human(total_plain)} 明文")
    for name, msg in errors[:20]:
        print(f"  ✗ {name}：{msg}")
    if len(errors) > 20:
        print(f"  … 还有 {len(errors) - 20} 个错误")
    if no_key and failed == no_key:
        print("\n提示：这些文件都没有可用的凭据。用密码解密需要文件头里带钥匙牌"
              "（BauduSync 默认会带）；早期版本或关掉该选项的文件，请改用"
              " --key 私钥文件、--keys 密钥包或 --db 数据库。")
    return 0 if failed == 0 else 1


def _try_keys(src: str, dst: Optional[str], cred: Credentials,
              header: dict) -> int:
    """依次试候选密钥。dst 为 None 表示只校验。"""
    last: Optional[Exception] = None
    for _label, dek in cred.candidates(header):
        try:
            if dst is None:
                return _verify_file(src, dek, header)
            return decrypt_file(src, dst, dek)
        except ToolError as exc:
            last = exc
            continue
    raise last or ToolError("所有密钥都解不开")


def _verify_file(src: str, key: bytes, header: dict) -> int:
    chunk = header["chunk_size"]
    plain_size = header["plain_size"]
    aead = AESGCM(key)
    done = 0
    blocks = max(1, (plain_size + chunk - 1) // chunk)
    with open(src, "rb") as fin:
        fin.seek(header["data_offset"])
        for index in range(blocks):
            take = min(chunk, plain_size - done)
            blob = fin.read(take + TAG_SIZE)
            if len(blob) != take + TAG_SIZE:
                raise ToolError("文件被截断")
            try:
                aead.decrypt(header["nonce_prefix"] + index.to_bytes(4, "big"),
                             blob, header["aad"])
            except InvalidTag:
                raise ToolError("认证失败：密钥不对，或者文件被改过")
            done += take
        if fin.read(1):
            raise ToolError("尾部有多余数据")
    return plain_size


def _copy(src: Path, dst: Path) -> None:
    import shutil
    shutil.copy2(src, dst)


def _say(args, text: str, quiet: bool = False) -> None:
    if quiet and args.quiet:
        return
    print(text, flush=True)


def _credentials(args) -> Credentials:
    cred = Credentials()
    if args.password:
        cred.password = args.password
    for path in args.key or []:
        cred.add_taskkey(path)
    if args.db:
        cred.add_sqlite(args.db)
        if args.keyfile:
            cred._keyfile = Path(args.keyfile).expanduser()
    elif args.keys:
        cred.add_bundle(args.keys)
    for i, text in enumerate(args.dek or []):
        cred.add_key(f"--dek #{i + 1}", _unb64(text.strip()))
    if args.scrypt_n:
        cred.kdf = {**cred.kdf, "n": int(args.scrypt_n)}
    if not (cred.password or cred.static or cred._salt):
        raise ToolError("请用 --password、--key、--keys 或 --db 指定凭据（详见 --help）")
    return cred


# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bsenc-decrypt",
        description="BauduSync 离线解密工具：用密码或任务私钥把加密过的文件还原。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  bsenc-decrypt decrypt --password 'xxx' -o ./plain ~/cloud/照片\n"
            "  bsenc-decrypt decrypt --key task-1-key.json -o ./plain ~/cloud\n"
            "  bsenc-decrypt decrypt --db /config/baudusync.db -o ./plain ~/cloud\n"
            "  bsenc-decrypt decrypt --password 'xxx' --verify-only ~/cloud\n"
            "  bsenc-decrypt info --db /config/baudusync.db\n"
        ))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--password", help="加密密码；不给会交互式询问")
        p.add_argument("--key", action="append",
                       help="任务私钥文件（可重复），不需要密码")
        p.add_argument("--keys", help="界面导出的密钥包 json")
        p.add_argument("--db", help="BauduSync 的 baudusync.db 路径")
        p.add_argument("--keyfile", help="密钥文件路径（默认取数据库同目录的 enc.key）")
        p.add_argument("--dek", action="append",
                       help="直接给 base64 任务密钥，可重复（调试用）")
        p.add_argument("--scrypt-n", help="老文件用了别的 scrypt N 时手动指定（一般不用）")

    p_info = sub.add_parser("info", help="显示凭据情况，顺便验证密码")
    common(p_info)
    p_info.set_defaults(func=cmd_info)

    p_dec = sub.add_parser("decrypt", help="解密文件或目录")
    common(p_dec)
    p_dec.add_argument("targets", nargs="+", help="文件或目录，可给多个")
    p_dec.add_argument("-o", "--out", help="输出目录（保持原有子目录结构）")
    p_dec.add_argument("--in-place", action="store_true", help="原地解密，覆盖密文")
    p_dec.add_argument("--verify-only", action="store_true", help="只校验，不产出文件")
    p_dec.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的输出文件")
    p_dec.add_argument("--copy-plain", action="store_true",
                       help="不是密文的文件也一起复制到输出目录")
    p_dec.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p_dec.add_argument("-q", "--quiet", action="store_true", help="少显示一些")
    p_dec.set_defaults(func=cmd_decrypt)

    args = ap.parse_args(argv)

    if not args.password and not args.dek and not args.key:
        # 有 enc.key 或者给了私钥文件就不用问密码；否则交互式要一次
        auto = False
        if args.db:
            try:
                auto = (Path(args.db).expanduser().parent / "enc.key").is_file()
            except OSError:
                auto = False
        if not auto and sys.stdin.isatty():
            try:
                typed = getpass.getpass("加密密码（没有就回车，用私钥解）：")
            except (EOFError, KeyboardInterrupt):
                sys.stderr.write("错误：需要密码（也可以用 --password 传入）\n")
                return 2
            args.password = (typed or "").strip() or None

    try:
        return args.func(args)
    except ToolError as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
