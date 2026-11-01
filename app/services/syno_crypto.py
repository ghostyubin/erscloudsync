"""Synology Cloud Sync 兼容格式（``__CLOUDSYNC_ENC__``）的编解码。

为什么要有这个模块
------------------
我们自己的 ``BSENC`` 格式（见 ``crypto.py``）安全性更好，但只有本项目的
工具能解。想用群晖官方的 **Cloud Sync Decryption Tool** 直接打开，就必须
逐字节符合它的格式。这里实现的就是那个格式。

格式（逆向自 Synology 的样本文件 + marnix/synology-decrypt 的描述）
-----------------------------------------------------------------
::

    "__CLOUDSYNC_ENC__"          17 字节魔数
    md5(魔数) 的 hex             32 字节 ASCII
    0x42 ...entries... 0x40      第一个字典：加密参数
    0x42 ...entries... 0x40      数据字典 × N（每块一条）
    0x42 ...entries... 0x40      末尾元数据字典（file_md5）

    类型标记：0x10 = 字符串（2 字节大端长度 + UTF-8）
              0x11 = 字节串（2 字节大端长度 + 原始字节）
              0x01 = 整数（1 字节长度 + 大端数值）
              0x42 / 0x40 = 字典开始 / 结束

密钥层级（两层，和我们的 BSENC 完全不同）::

    密码 --OpenSSL-KDF--> key1 --解密 enc_key1--> session_key（64 字符 hex）
    session_key --OpenSSL-KDF(无盐, 迭代 1 次)--> 数据密钥

    私钥 --RSA-OAEP-SHA1--> 解 enc_key2 --> 同一个 session_key

也就是说**每个文件都有一个随机 session key**，它同时被密码和 RSA 公钥
各包裹一份。这就是为什么「密码」和「private.pem」能各自单独解密——两者
指向的都是同一个 session key，解出来的明文一模一样。

数据本身：先 LZ4 压缩（frame 格式），再 AES-256-CBC + PKCS7 填充，按
16384 字节切成若干块。每个文件头里还记着明文的 MD5，解密后校验。

安全性的代价（写在这里，也写进 README）
------------------------------------
* KDF 是 OpenSSL 的 MD5 迭代 1000 次——按今天的标准非常弱，一个弱密码的
  离线爆破成本远低于我们的 scrypt(N=2^15)。
* AES-CBC 没有消息认证。改一位、换一块都不会在解密时报错，只有末尾的
  file_md5 能发现（而那个 MD5 在明文里可算，攻击者改完能重算）。
* 每个文件头里带 ``key1_hash``，等于给了一份可离线验证密码猜想的样本。

所以：需要强保护时用 ``BSENC``；需要「任何一台机器上拿群晖的工具就能解」
时用本格式。两者可以在不同任务上并存。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import struct
from typing import BinaryIO, Optional

import lz4.frame
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"__CLOUDSYNC_ENC__"
MAGIC_HASH = hashlib.md5(MAGIC).hexdigest().encode("ascii")   # 32 字节 ASCII
PREAMBLE = len(MAGIC) + len(MAGIC_HASH)                       # 49

DICT_START = 0x42
DICT_END = 0x40
KIND_STRING = 0x10
KIND_BYTES = 0x11
KIND_INT = 0x01

# 群晖当前版本就是 16384（早期文档写 8192，实测样本是 16384）。
CHUNK_SIZE = 16384
# 数据密钥固定 32 字节（AES-256），IV 16 字节。
KEY_SIZE = 32
IV_SIZE = 16
# session key 以大写十六进制字符串的形式在两处被包裹，长度 64。
SESSION_HEX_LEN = KEY_SIZE * 2

RSA_BITS = 2048

# 头里的字典字段（顺序与群晖生成的文件一致，便于肉眼比对）
INFO_KEYS = ("compress", "digest", "enc_key1", "enc_key2", "encrypt",
             "file_name", "key1_hash", "key2_hash", "salt",
             "session_key_hash", "type", "version")


class SynoError(Exception):
    """格式层的错误，上层统一翻译成 CryptoError。"""


class SynoDecryptError(SynoError):
    """密码/密钥不对，或者文件被改过。"""


# --------------------------------------------------------------------------
# 字典编码
# --------------------------------------------------------------------------
def _enc_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise SynoError("字段太长，超出格式上限")
    return bytes([KIND_STRING]) + struct.pack(">H", len(raw)) + raw


def _enc_bytes(value: bytes) -> bytes:
    if len(value) > 0xFFFF:
        raise SynoError("数据块太大，超出格式上限（单块最长 65535 字节）")
    return bytes([KIND_BYTES]) + struct.pack(">H", len(value)) + value


def _enc_int(value: int) -> bytes:
    if value < 0:
        raise SynoError("不支持负数")
    length = max(1, (value.bit_length() + 7) // 8)
    return bytes([KIND_INT, length]) + value.to_bytes(length, "big")


def _enc_dict(items: list[tuple[str, object]]) -> bytes:
    """items 里的值可以是 str / bytes / int / list（嵌套字典）。"""
    out = bytearray([DICT_START])
    for key, value in items:
        out += _enc_string(key)
        if isinstance(value, str):
            out += _enc_string(value)
        elif isinstance(value, (bytes, bytearray)):
            out += _enc_bytes(bytes(value))
        elif isinstance(value, int):
            out += _enc_int(value)
        elif isinstance(value, list):
            out += _enc_dict(value)
        else:  # pragma: no cover - 编程错误
            raise SynoError(f"不支持的字段类型 {type(value)}")
    out.append(DICT_END)
    return bytes(out)


class _Reader:
    """带一个字节回推能力的读取器（流式解密要用它判断是否最后一块）。"""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh
        self._pending: Optional[bytes] = None

    def read(self, n: int) -> bytes:
        if n <= 0:
            return b""
        if self._pending is None:
            return self._fh.read(n)
        head, self._pending = self._pending, None
        if len(head) >= n:
            rest = head[n:]
            if rest:                     # 别留空串：peek 会把它当成「已读完」
                self._pending = rest
            return head[:n]
        return head + self._fh.read(n - len(head))

    def peek(self) -> Optional[int]:
        if self._pending is None:
            self._pending = self._fh.read(1)
        return self._pending[0] if self._pending else None


def _read_value(r: _Reader):
    tag = r.read(1)
    if not tag:
        raise SynoError("文件在解析字典时意外结束")
    tag = tag[0]
    if tag == DICT_START:
        return _read_dict(r)
    if tag == DICT_END:
        raise SynoError("字典未配对")
    if tag in (KIND_STRING, KIND_BYTES):
        raw_len = r.read(2)
        if len(raw_len) != 2:
            raise SynoError("文件在读取字段长度时意外结束")
        raw = r.read(struct.unpack(">H", raw_len)[0])
        return raw.decode("utf-8", "replace") if tag == KIND_STRING else raw
    if tag == KIND_INT:
        size = r.read(1)
        if not size:
            raise SynoError("文件在读取整数长度时意外结束")
        n = size[0]
        return int.from_bytes(r.read(n), "big") if n else 0
    raise SynoError(f"未知的字段类型 0x{tag:02X}")


def _read_dict(r: _Reader) -> dict:
    result: dict = {}
    while True:
        b = r.peek()
        if b is None:
            raise SynoError("字典没有结束标记，文件可能被截断")
        if b == DICT_END:
            r.read(1)
            return result
        key = _read_value(r)
        if not isinstance(key, str):
            raise SynoError("字典的键必须是字符串")
        result[key] = _read_value(r)


# --------------------------------------------------------------------------
# OpenSSL 的 EVP_BytesToKey（群晖用的就是它）
# --------------------------------------------------------------------------
def _openssl_kdf(password: bytes, salt: bytes,
                 key_size: int = KEY_SIZE, iv_size: int = IV_SIZE) -> tuple[bytes, bytes]:
    """按照 openssl enc 的老算法派生 key+iv。

    有 salt 时每一轮迭代 1000 次 MD5，没有 salt 时只算 1 次——群晖故意
    留了这个差异：第一层（密码 → enc_key1）有盐，第二层（session key →
    数据密钥）无盐。
    """
    rounds = 1 if salt == b"" else 1000
    material = b""
    prev = b""
    while len(material) < key_size + iv_size:
        block = prev + password + salt
        for _ in range(rounds):
            block = hashlib.md5(block).digest()
        prev = block
        material += block
    return material[:key_size], material[key_size:key_size + iv_size]


def _aes_cbc(key: bytes, iv: bytes):
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise SynoDecryptError("解密结果长度异常")
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        raise SynoDecryptError("填充校验失败：密码不对，或者文件被改过")
    return data[:-pad]


def _salted_hash(prefix: str, data: bytes) -> str:
    """群晖的 42 字符串哈希：10 字符盐 + md5(盐 + 数据)。"""
    return prefix + hashlib.md5(prefix.encode("ascii") + data).hexdigest()


def _rand_tag() -> str:
    """10 字符的盐/前缀，和样本一样是 urlsafe base64 风格。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(8)).decode("ascii")[:10]


def _rand_salt() -> str:
    """文件头里的 salt 字段：6 字节 base64，正好 8 个字符。"""
    return base64.b64encode(secrets.token_bytes(6)).decode("ascii")


# --------------------------------------------------------------------------
# RSA 密钥对
# --------------------------------------------------------------------------
def generate_keypair() -> tuple[bytes, bytes]:
    """生成一对 PEM。格式必须是 PKCS#1——群晖的 key.zip 里就是 ``RSA PUBLIC KEY``
    和 ``RSA PRIVATE KEY``，用 SPKI 那套（``PUBLIC KEY``）它对不上 ``key2_hash``。
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.PKCS1)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption())
    return pub, priv


def _oaep() -> padding.OAEP:
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(),
                        label=None)


# --------------------------------------------------------------------------
# 判断与头部读取
# --------------------------------------------------------------------------
def is_syno(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_info(path: str) -> Optional[dict]:
    """只读第一个字典，用来展示/判断，不做解密。"""
    try:
        with open(path, "rb") as fh:
            if fh.read(len(MAGIC)) != MAGIC:
                return None
            if fh.read(len(MAGIC_HASH)) != MAGIC_HASH:
                return None
            r = _Reader(fh)
            info = _read_value(r)
    except (OSError, SynoError):
        return None
    if not isinstance(info, dict) or "enc_key1" not in info:
        return None
    return info


# --------------------------------------------------------------------------
# 加密
# --------------------------------------------------------------------------
def _md5_of(path: str) -> str:
    digest = hashlib.md5()
    total = 0
    with open(path, "rb") as fh:
        while True:
            data = fh.read(1024 * 1024)
            if not data:
                break
            total += len(data)
            digest.update(data)
    return digest.hexdigest(), total


def encrypt_file(src: str, dst: str, password: str, public_pem: bytes,
                 file_name: Optional[str] = None, compress: bool = True) -> int:
    """把 src 加密成群晖格式写到 dst，返回密文大小。

    阻塞调用，调用方用 ``asyncio.to_thread`` 包起来。

    流式处理：只压缩/加密一遍，中间不把整个文件读进内存——NAS 上同步几个
    GB 的镜像也不会爆。
    """
    file_name = file_name or os.path.basename(src)
    password_bytes = password.encode("utf-8")

    # 明文 md5 要先算：它写在文件尾的元数据里，而那时源文件已经读完了。
    file_md5, plain_size = _md5_of(src)

    session = secrets.token_bytes(KEY_SIZE)
    session_hex = session.hex().upper().encode("ascii")     # 64 字节
    salt = _rand_salt()

    # 第一层：密码 + 盐 → key1，用它把 session_hex 包起来
    key1, iv1 = _openssl_kdf(password_bytes, salt.encode("ascii"))
    enc = _aes_cbc(key1, iv1).encryptor()
    enc_key1 = base64.b64encode(enc.update(_pkcs7_pad(session_hex)) + enc.finalize())

    # 第二层（私钥那条路）：RSA-OAEP 直接把 session_hex 加密
    pub = serialization.load_pem_public_key(public_pem)
    enc_key2 = base64.b64encode(pub.encrypt(session_hex, _oaep()))

    # 数据密钥：session key 的原始字节当密码，无盐、只迭代一次
    data_key, data_iv = _openssl_kdf(session, b"")

    tag1, tag2, tag3 = _rand_tag(), _rand_tag(), _rand_tag()
    header = _enc_dict([
        ("compress", 1 if compress else 0),
        ("digest", "md5"),
        ("enc_key1", enc_key1.decode("ascii")),
        ("enc_key2", enc_key2.decode("ascii")),
        ("encrypt", 1),
        ("file_name", file_name),
        ("key1_hash", _salted_hash(tag1, password_bytes)),
        ("key2_hash", _salted_hash(tag2, public_pem)),
        ("salt", salt),
        ("session_key_hash", _salted_hash(tag3, session_hex)),
        ("type", "metadata"),
        ("version", [("major", 3), ("minor", 1)]),
    ])

    enc_data = _aes_cbc(data_key, data_iv).encryptor()
    comp = lz4.frame.LZ4FrameCompressor() if compress else None
    written = 0
    pending = bytearray()

    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC + MAGIC_HASH + header)
        written += PREAMBLE + len(header)

        def pump(blob: bytes) -> None:
            nonlocal written
            if not blob:
                return
            pending.extend(blob)
            while len(pending) >= CHUNK_SIZE:
                block = bytes(pending[:CHUNK_SIZE])
                del pending[:CHUNK_SIZE]
                piece = _enc_dict([("data", block), ("type", "data")])
                fout.write(piece)
                written += len(piece)

        read_total = 0
        fed = 0

        def push_plain(blob: bytes) -> None:
            """把「压缩后的明文」喂给加密器。填充长度要按它的总字节数算，
            不是按原始大小——LZ4 的输出长度和输入完全无关。"""
            nonlocal fed
            if not blob:
                return
            fed += len(blob)
            pump(enc_data.update(blob))

        if comp is not None:
            push_plain(comp.begin())        # frame 头也要算进填充基数
        while True:
            data = fin.read(1024 * 1024)
            if not data:
                break
            read_total += len(data)
            push_plain(comp.compress(data) if comp is not None else data)
        if comp is not None:
            push_plain(comp.flush())
        pad_len = 16 - fed % 16
        pump(enc_data.update(bytes([pad_len]) * pad_len) + enc_data.finalize())
        if pending:
            block = bytes(pending)
            pending.clear()
            piece = _enc_dict([("data", block), ("type", "data")])
            fout.write(piece)
            written += len(piece)

        meta = _enc_dict([("file_md5", file_md5), ("type", "metadata")])
        fout.write(meta)
        written += len(meta)

    if read_total != plain_size:
        # 实时同步可能正好在加密途中往源文件追加，密文和头里记的 md5 就对不上。
        # 与其产出解不出来的文件，不如报错让它下一轮重来。
        raise SynoError("源文件在加密过程中发生了变化，请等它写完再同步")
    return written


# --------------------------------------------------------------------------
# 解密
# --------------------------------------------------------------------------
def _session_key(info: dict, password: Optional[str],
                 private_pem: Optional[bytes]) -> bytes:
    """拿到 session key（64 字节 hex 文本）。密码优先，其次私钥。"""
    if password is not None:
        salt = (info.get("salt") or "").encode("ascii")
        enc_key1 = base64.b64decode(info["enc_key1"])
        key1, iv1 = _openssl_kdf(password.encode("utf-8"), salt)
        dec = _aes_cbc(key1, iv1).decryptor()
        session_hex = _pkcs7_unpad(dec.update(enc_key1) + dec.finalize())
        check = info.get("key1_hash")
        if check and len(check) >= 42:
            if _salted_hash(check[:10], password.encode("utf-8")) != check:
                raise SynoDecryptError("密码不正确")
        return session_hex

    if private_pem is None:
        raise SynoDecryptError("需要密码或者 private.pem")
    if "enc_key2" not in info:
        raise SynoDecryptError("这个文件里没有私钥可解的那一份")
    priv = serialization.load_pem_private_key(private_pem, password=None)
    try:
        return priv.decrypt(base64.b64decode(info["enc_key2"]), _oaep())
    except ValueError as exc:
        raise SynoDecryptError("私钥与这个文件不匹配") from exc


def decrypt_file(src: str, dst: str, password: Optional[str] = None,
                 private_pem: Optional[bytes] = None) -> int:
    """把群晖格式的密文解密写到 dst，返回明文字节数。

    格式解析阶段的错误统一翻译成 ``SynoDecryptError``：对调用方来说
    「文件坏了」和「密码不对」都是同一件事——这个文件解不开。任何失败
    都不留半成品。

    实现上不去读完整份密文再统一去填充，而是「读到下一块才知道上一块
    是不是最后一块」，所以只多占一个块的缓冲。
    """
    try:
        return _decrypt_impl(src, dst, password, private_pem)
    except SynoError as exc:
        try:
            os.remove(dst)
        except OSError:
            pass
        if isinstance(exc, SynoDecryptError):
            raise
        raise SynoDecryptError(str(exc)) from exc


def _decrypt_impl(src: str, dst: str, password: Optional[str] = None,
                  private_pem: Optional[bytes] = None) -> int:
    with open(src, "rb") as fin:
        if fin.read(len(MAGIC)) != MAGIC:
            raise SynoDecryptError("不是群晖 Cloud Sync 的加密文件")
        if fin.read(len(MAGIC_HASH)) != MAGIC_HASH:
            raise SynoDecryptError("魔数校验失败，文件可能损坏")
        r = _Reader(fin)
        info = _read_value(r)
        if not isinstance(info, dict) or "enc_key1" not in info:
            raise SynoDecryptError("文件头缺少加密参数")

        version = info.get("version") or {}
        if int(version.get("major", 0) or 0) < 1:
            raise SynoDecryptError(f"不支持的格式版本 {version}")

        session_hex = _session_key(info, password, private_pem)
        expected_session = info.get("session_key_hash")
        if expected_session and len(expected_session) >= 42:
            if _salted_hash(expected_session[:10], session_hex) != expected_session:
                raise SynoDecryptError("会话密钥校验失败")

        try:
            # v1（无盐）时代 session key 直接就是原始字节；v3 起才变成
            # 64 个字符的十六进制文本。两种都要认，否则老文件打不开。
            session_raw = (bytes.fromhex(session_hex.decode("ascii"))
                           if info.get("salt") else session_hex)
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            raise SynoDecryptError("会话密钥格式异常") from exc
        data_key, data_iv = _openssl_kdf(session_raw, b"")
        dec = _aes_cbc(data_key, data_iv).decryptor()

        compress = bool(int(info.get("compress", 0) or 0))
        decomp = lz4.frame.LZ4FrameDecompressor() if compress else None
        digest = hashlib.md5()
        written = 0
        meta: dict = {}
        carry = b""            # 还没去填充的最后一块解密结果

        with open(dst, "wb") as fout:
            def emit(plain: bytes) -> None:
                nonlocal written
                if not plain:
                    return
                digest.update(plain)
                fout.write(plain)
                written += len(plain)

            def flush_block(plain: bytes) -> None:
                emit(decomp.decompress(plain) if decomp is not None else plain)

            while True:
                nxt = r.peek()
                # 群晖的格式没有顶层结束标记：读完最后一个字典就是文件末尾。
                # 真被截断的话，下面的解析或末尾的 MD5 校验会报出来。
                if nxt is None:
                    break
                if nxt == DICT_END:
                    r.read(1)
                    break
                block_dict = _read_value(r)
                if not isinstance(block_dict, dict):
                    raise SynoDecryptError("数据块格式异常")
                if block_dict.get("type") == "metadata":
                    meta = block_dict
                    continue
                payload = block_dict.get("data")
                if not isinstance(payload, (bytes, bytearray)):
                    raise SynoDecryptError("数据块里没有内容")
                if len(payload) % 16:
                    raise SynoDecryptError("密文块长度不是 16 的倍数")
                if carry:
                    flush_block(carry)
                carry = dec.update(bytes(payload))

            final = dec.finalize()
            if carry or final:
                tail = _pkcs7_unpad(carry + final)
                if tail:
                    flush_block(tail)
            # 收尾：把解压器里残留的数据吐出来（frame 尾部可能有积压的块）
            if decomp is not None:
                try:
                    rest = decomp.decompress(b"")
                except Exception:  # noqa: BLE001 - 已到流尾时个别实现会抛错
                    rest = b""
                if rest:
                    emit(rest)

        # 直接用边写边算的那份摘要，不必把文件再读一遍
        expected = meta.get("file_md5") or info.get("file_md5")
        if expected and digest.hexdigest() != expected:
            try:
                os.remove(dst)
            except OSError:
                pass
            raise SynoDecryptError("MD5 校验失败，文件不完整或被改过")

    return written
