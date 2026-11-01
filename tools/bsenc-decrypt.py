#!/usr/bin/env python3
"""BauduSync 离线解密工具（bsenc）。

不依赖 BauduSync 本体，只用标准库 + ``cryptography``，可以直接拷到任何
机器上跑。用来在 NAS 挂了、不想装整套程序、或者只想把云盘里的密文拿回来
解开的场合。

    # 看一眼密钥情况（不写任何文件）
    python3 bsenc-decrypt.py info --db /config/baudusync.db

    # 把整个目录解密到 ./plain，保持原有子目录结构
    python3 bsenc-decrypt.py decrypt --db /config/baudusync.db -o ./plain ~/cloud/照片

    # 原地解密（解开后覆盖掉密文）
    python3 bsenc-decrypt.py decrypt --db /config/baudusync.db --in-place ~/cloud/照片

    # 只校验完整性，不产出任何文件
    python3 bsenc-decrypt.py decrypt --db /config/baudusync.db --verify-only ~/cloud

    # 只有备份的密钥包（界面上「导出解密密钥」得到的 json）
    python3 bsenc-decrypt.py decrypt --keys keys.json -o ./plain ~/cloud

密钥从哪来（三选一，按优先级）：

1. ``--db``：直接读 BauduSync 的 ``baudusync.db``，里面有 salt、验证块和
   每个任务的包裹密钥。有密码就用密码；没给密码但有 ``enc.key`` 时直接
   用密钥文件解开（就是 NAS 上无人值守自动解锁那条路）。
2. ``--keys``：界面导出的密钥包 json。**只备份云端文件是不够的**，密码
   本身推不出任务密钥，必须带上 salt 和包裹后的密钥。
3. ``--dek``：直接给一串 base64 的任务密钥（调试用）。

设计上和本体保持一致：``密码 → scrypt → KEK → 解开 → 每任务 DEK → 文件块``。
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
VERSION = 1
HEADER = struct.Struct("<5sBBBI8sQ20s")
HEADER_SIZE = HEADER.size  # 48
TAG_SIZE = 16
KEY_SIZE = 32
MAX_CHUNK = 64 * 1024 * 1024

KDF_DEFAULTS = {"name": "scrypt", "n": 2 ** 15, "r": 8, "p": 1, "length": KEY_SIZE}
VERIFY_PLAIN = b"bsenc-verify-v1"

SALT_KEY = "crypto_salt"
VERIFIER_KEY = "crypto_verifier"
SEALED_KEY = "crypto_sealed_kek"


class ToolError(Exception):
    """给用户看的错误。"""


# --------------------------------------------------------------------------
# 文件头
# --------------------------------------------------------------------------
def read_header(path: str) -> Optional[dict]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(HEADER_SIZE)
    except OSError:
        return None
    if len(raw) != HEADER_SIZE:
        return None
    magic, version, flags, key_id, chunk, prefix, plain_size, _ = HEADER.unpack(raw)
    if magic != MAGIC or version != VERSION:
        return None
    if not (0 < chunk <= MAX_CHUNK):
        return None
    return {"version": version, "flags": flags, "key_id": key_id,
            "chunk_size": chunk, "nonce_prefix": prefix,
            "plain_size": plain_size, "raw": raw}


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
    raw = header["raw"]
    aead = AESGCM(key)
    done = 0
    blocks = max(1, (plain_size + chunk - 1) // chunk)

    tmp = dst + ".tmp"
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            fin.seek(HEADER_SIZE)
            for index in range(blocks):
                take = min(chunk, plain_size - done)
                blob = fin.read(take + TAG_SIZE)
                if len(blob) != take + TAG_SIZE:
                    raise ToolError("文件被截断，密文长度不足")
                try:
                    fout.write(aead.decrypt(prefix + index.to_bytes(4, "big"),
                                            blob, raw))
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
                  n=int(kdf.get("n", 2 ** 15)), r=int(kdf.get("r", 8)),
                  p=int(kdf.get("p", 1))).derive(password.encode("utf-8"))


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
        raise ToolError(f"任务 {task_id} 的密钥解不开（密码不对，或者该任务换过密码）")


def _check_verifier(kek: bytes, verifier: str) -> bool:
    if not verifier:
        return True
    try:
        AESGCM(kek).decrypt(b"\x00" * 12, _unb64(verifier), None)
        return True
    except InvalidTag:
        return False


class KeySet:
    def __init__(self) -> None:
        self.salt: bytes = b""
        self.kdf: dict = dict(KDF_DEFAULTS)
        self.verifier: str = ""
        self.sealed: str = ""
        self.tasks: list[dict] = []          # [{"id":1,"name":"...","wrapped":"..."}]
        self.source: str = ""
        self.keyfile: Optional[Path] = None
        self.deks: list[tuple[int, str, bytes]] = []

    # ---- 输入 ----
    @classmethod
    def from_db(cls, path: str) -> "KeySet":
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"找不到数据库文件：{p}")
        uri = f"file:{p}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise ToolError(f"打不开数据库：{exc}") from exc
        try:
            conn.row_factory = sqlite3.Row
            settings = {r["key"]: r["value"] for r in
                        conn.execute("SELECT key, value FROM settings")}
            rows = conn.execute(
                "SELECT id, name, enc_wrapped FROM tasks ORDER BY id").fetchall()
        except sqlite3.Error as exc:
            raise ToolError(f"读数据库失败（是 BauduSync 的库吗？）：{exc}") from exc
        finally:
            conn.close()

        ks = cls()
        ks.source = f"数据库 {p}"
        salt = settings.get(SALT_KEY, "")
        if not salt:
            raise ToolError(f"{p} 里没有加密配置（source 表里找不到 {SALT_KEY}）")
        ks.salt = _unb64(salt)
        ks.verifier = settings.get(VERIFIER_KEY, "")
        ks.sealed = settings.get(SEALED_KEY, "")
        ks.tasks = [{"id": int(r["id"]), "name": r["name"] or f"任务 {r['id']}",
                     "wrapped": (r["enc_wrapped"] or "").strip()}
                    for r in rows if (r["enc_wrapped"] or "").strip()]
        ks.keyfile = p.parent / "enc.key"
        return ks

    @classmethod
    def from_bundle(cls, path: str) -> "KeySet":
        p = Path(path).expanduser()
        try:
            data = json.loads(p.read_text("utf-8"))
        except OSError as exc:
            raise ToolError(f"读不到密钥包：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise ToolError(f"密钥包不是合法 JSON：{exc}") from exc
        if data.get("format") != "bsenc-keybundle":
            raise ToolError("这不是 BauduSync 的密钥包（format 字段对不上）")
        ks = cls()
        ks.source = f"密钥包 {p}"
        kdf = data.get("kdf") or {}
        ks.salt = _unb64(kdf.get("salt") or "")
        ks.kdf = {**KDF_DEFAULTS, **{k: v for k, v in kdf.items() if v is not None}}
        ks.verifier = data.get("verifier") or ""
        ks.tasks = [{"id": int(t["task_id"]), "name": t.get("task_name") or "",
                     "wrapped": t["wrapped"]}
                    for t in data.get("keys", []) if t.get("wrapped")]
        return ks

    # ---- 解开 ----
    def unlock(self, password: Optional[str]) -> str:
        """把 KEK 找出来并解出所有 DEK。返回一句说明。"""
        if not self.salt:
            raise ToolError("缺少 salt，解不开")

        kek: Optional[bytes] = None
        how = ""
        if password:
            kek = _derive(password, self.salt, self.kdf)
            if not _check_verifier(kek, self.verifier):
                raise ToolError("密码不正确")
            how = "密码"
        elif self.sealed and self.keyfile and self.keyfile.is_file():
            fk = self.keyfile.read_bytes()
            if len(fk) != KEY_SIZE:
                raise ToolError(f"密钥文件 {self.keyfile} 长度不对，应该是 {KEY_SIZE} 字节")
            blob = _unb64(self.sealed)
            try:
                kek = AESGCM(fk).decrypt(blob[:12], blob[12:], b"bsenc-kek")
            except InvalidTag:
                raise ToolError("密钥文件与数据库不匹配（换过密码或不是同一套？）")
            how = f"密钥文件 {self.keyfile.name}（免密码）"
        else:
            raise ToolError("需要密码：没有可用的自动解锁密钥文件")

        if not self.tasks:
            # 解锁本身是成功的，只是还没有任何任务生成过密钥（加密任务第一次
            # 上传时才生成）。这里不算错误，由调用方决定怎么提示。
            return how
        self.deks = [(t["id"], t["name"] or f"任务 {t['id']}",
                      _unwrap(kek, t["wrapped"], t["id"])) for t in self.tasks]
        return how

    def add_raw_deks(self, values: Iterable[str]) -> None:
        start = len(self.deks)
        for i, text in enumerate(values):
            raw = _unb64(text.strip())
            if len(raw) != KEY_SIZE:
                raise ToolError(f"第 {i + 1} 个 --dek 不是 {KEY_SIZE} 字节")
            self.deks.append((-1, f"--dek #{i + 1}", raw))
        if len(self.deks) == start:
            raise ToolError("没有可用的密钥")

    def labels(self) -> list[str]:
        return [label for _tid, label, _dek in self.deks]


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
    ks = _load_keyset(args, need_unlock=False)
    print(f"密钥来源：{ks.source}")
    print(f"salt：{_b64(ks.salt)}")
    print(f"KDF：scrypt n={ks.kdf['n']} r={ks.kdf['r']} p={ks.kdf['p']}")
    print(f"验证块：{'有' if ks.verifier else '无'}")
    print(f"自动解锁副本：{'有' if ks.sealed else '无'}"
          + (f"，密钥文件 {ks.keyfile}" if ks.keyfile and ks.keyfile.is_file() else ""))
    print(f"任务密钥：{len(ks.tasks)} 个")
    for t in ks.tasks:
        print(f"  - #{t['id']} {t['name']}")
    if args.password or (ks.sealed and ks.keyfile and ks.keyfile.is_file()):
        how = ks.unlock(args.password)
        if not ks.deks:
            print(f"解锁成功（{how}），但还没有任务生成过加密密钥——")
            print("加密任务第一次上传时才会生成，现在没有东西可解。")
            return 0
        print(f"解锁成功（{how}），可用密钥 {len(ks.deks)} 把：")
        for tid, label, dek in ks.deks:
            # 只露指纹，别把密钥打进日志
            print(f"  - {label}：{dek[:4].hex()}…{dek[-2:].hex()}")
    else:
        print("（没给密码，也没有密钥文件，未做解锁验证）")
    return 0


def cmd_decrypt(args) -> int:
    targets = [Path(t).expanduser() for t in args.targets]
    ks = _load_keyset(args, need_unlock=True)
    how = "命令行直接给的密钥"
    if ks.tasks or ks.salt:
        how = ks.unlock(args.password)
    if args.dek:
        ks.add_raw_deks(args.dek)
    if not ks.deks:
        raise ToolError("没有可用的密钥：这套配置里还没有任务生成过加密密钥")

    pairs = collect(targets)
    if not pairs:
        raise ToolError("目标里没有文件")

    out_dir: Optional[Path] = None
    if not args.in_place and not args.verify_only:
        if not args.out:
            raise ToolError("要写文件就得给 -o 输出目录，或者用 --in-place / --verify-only")
        out_dir = Path(args.out).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"密钥：{how}，{len(ks.deks)} 把；文件：{len(pairs)} 个"
          f"（{human(sum(f.stat().st_size for f, _r in pairs if f.exists()))}）")

    if args.in_place and not args.verify_only:
        print("⚠️  原地解密会用明文覆盖密文，不可撤销。")
        if not args.yes and not confirm("确定继续？"):
            return 1

    ok = skipped = failed = copied = 0
    total_plain = 0
    errors: list[tuple[str, str]] = []

    for index, (src, rel) in enumerate(pairs, 1):
        name = f"{rel}"
        try:
            if not looks_encrypted(str(src)):
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

            if args.verify_only:
                written = _try_keys(src, None, ks.deks)
            elif out_dir:
                dst = out_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() and not args.overwrite:
                    raise ToolError(f"目标已存在：{dst}（加 --overwrite 覆盖）")
                written = _try_keys(src, str(dst), ks.deks)
            else:
                written = _try_keys(src, str(src), ks.deks)   # 写临时文件再替换
            ok += 1
            total_plain += written
            _say(args, f"[{index}/{len(pairs)}] {name}  → {human(written)}")
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
    return 0 if failed == 0 else 1


def _try_keys(src: str, dst: Optional[str], deks: list[tuple[int, str, bytes]]) -> int:
    """依次试每一把密钥。dst 为 None 表示只校验。"""
    last: Optional[Exception] = None
    for _tid, _label, dek in deks:
        try:
            if dst is None:
                return _verify_file(src, dek)
            return decrypt_file(src, dst, dek)
        except ToolError as exc:
            last = exc
            continue
    raise last or ToolError("所有密钥都解不开")


def _verify_file(src: str, key: bytes) -> int:
    header = read_header(src)
    if not header:
        raise ToolError("不是 bsenc 加密文件")
    chunk = header["chunk_size"]
    plain_size = header["plain_size"]
    aead = AESGCM(key)
    done = 0
    blocks = max(1, (plain_size + chunk - 1) // chunk)
    with open(src, "rb") as fin:
        fin.seek(HEADER_SIZE)
        for index in range(blocks):
            take = min(chunk, plain_size - done)
            blob = fin.read(take + TAG_SIZE)
            if len(blob) != take + TAG_SIZE:
                raise ToolError("文件被截断")
            try:
                aead.decrypt(header["nonce_prefix"] + index.to_bytes(4, "big"),
                             blob, header["raw"])
            except InvalidTag:
                raise ToolError("认证失败")
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


def _load_keyset(args, need_unlock: bool) -> KeySet:
    if args.db:
        ks = KeySet.from_db(args.db)
    elif args.keys:
        ks = KeySet.from_bundle(args.keys)
    elif args.dek:
        ks = KeySet()
        ks.source = "--dek"
        return ks
    else:
        raise ToolError("请用 --db 或 --keys 指定密钥来源（详见 --help）")
    if args.keyfile:
        ks.keyfile = Path(args.keyfile).expanduser()
    return ks


# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bsenc-decrypt",
        description="BauduSync 离线解密工具：把客户端加密过的文件还原成明文。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  bsenc-decrypt info --db /config/baudusync.db\n"
            "  bsenc-decrypt decrypt --db /config/baudusync.db -o ./plain ~/cloud\n"
            "  bsenc-decrypt decrypt --keys keys.json -o ./plain ~/cloud\n"
            "  bsenc-decrypt decrypt --db /config/baudusync.db --verify-only ~/cloud\n"
        ))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--db", help="BauduSync 的 baudusync.db 路径")
        p.add_argument("--keys", help="界面导出的密钥包 json")
        p.add_argument("--keyfile", help="密钥文件路径（默认取数据库同目录的 enc.key）")
        p.add_argument("--password", help="加密密码；不给会交互式询问")
        p.add_argument("--dek", action="append",
                       help="直接给 base64 任务密钥，可重复（调试用）")

    p_info = sub.add_parser("info", help="显示密钥情况，顺便验证密码")
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
    p_dec.add_argument("-q", "--quiet", action="store_true", help="只显示跳过的文件名以外的信息")
    p_dec.set_defaults(func=cmd_decrypt)

    args = ap.parse_args(argv)

    if not args.password and not args.dek and (args.db or args.keys):
        # 有密钥文件就不问；否则交互式要密码（避免出现在 shell 历史里）
        auto = False
        try:
            if args.db:
                p = Path(args.db).expanduser()
                auto = (p.parent / "enc.key").is_file()
        except OSError:
            pass
        if not auto and sys.stdin.isatty():
            try:
                args.password = getpass.getpass("加密密码：")
            except (EOFError, KeyboardInterrupt):
                sys.stderr.write("错误：需要密码（也可以用 --password 传入）\n")
                return 2

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
