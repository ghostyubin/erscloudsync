"""Baidu Netdisk (百度网盘) cloud provider.

Implements Baidu Pan Open API for file sync operations.
Supports OAuth2 authorization and manual token entry.
"""

import aiohttp
import asyncio
import hashlib
import io
import json
import logging
import os
import time
import math
from typing import Optional
from urllib.parse import urlencode

from app.providers.base import CloudProvider, FileInfo, AuthResult, ProgressReader
from app.config import BAIDU_APP_KEY, BAIDU_APP_SECRET, BAIDU_REDIRECT_URI, BAIDU_DEFAULT_APP_KEY, BAIDU_DEFAULT_APP_SECRET, BAIDU_OAUTH_TOOL_URL, UPLOAD_CONCURRENCY
from app.services.retry import retry_async
from app import database as db

logger = logging.getLogger(__name__)


class _BlockPayload(aiohttp.Payload):
    """In-memory bytes payload that streams content with progress updates.

    We can't use IOBasePayload for our block bytes because:
    1. BytesIO has no fileno(), so IOBasePayload.size returns None
       and aiohttp falls back to Transfer-Encoding: chunked.
    2. Baidu superfile2 returns errno=31211 "transfer-encoding can't be
       chunked" on chunked uploads.

    We can't use BytesPayload because it writes everything in one shot,
    giving no chance to report progress between chunks.

    This class sets _size explicitly (so aiohttp sends Content-Length) and
    writes data in 64KB chunks, invoking ``on_progress(len(chunk))`` after
    each chunk. A callback (rather than a direct ``transfer`` reference) is
    used because blocks are uploaded concurrently: each block adds its own
    delta into a shared counter, so out-of-order completion can't make the
    reported byte count jump backwards.
    """

    def __init__(self, data: bytes, on_progress=None, filename: str = None,
                 content_type: str = "application/octet-stream"):
        super().__init__(value=data, content_type=content_type,
                         filename=filename)
        self._size = len(data)
        self._on_progress = on_progress
        self._read_bytes = 0
        self._body_data = data

    async def write(self, writer):
        await self.write_with_length(writer, None)

    async def write_with_length(self, writer, content_length):
        limit = self._size if content_length is None else content_length
        pos = 0
        while pos < limit:
            end = pos + 65536
            chunk = self._body_data[pos:end] if end < limit else self._body_data[pos:limit]
            if not chunk:
                break
            await writer.write(bytes(chunk))
            pos += len(chunk)
            self._read_bytes = pos
            if self._on_progress is not None:
                self._on_progress(len(chunk))
            # Yield to event loop so concurrent block uploads and the
            # polling task get a chance to run between our writes.
            await asyncio.sleep(0)

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self._body_data.decode(encoding, errors)


# API endpoints
OAUTH_AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
OAUTH_TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
PAN_API_BASE = "https://pan.baidu.com/rest/2.0/xpan"
PCS_API_BASE = "https://d.pcs.baidu.com/rest/2.0/pcs"

# Upload thresholds
MAX_SIMPLE_UPLOAD_SIZE = 4 * 1024 * 1024  # 4MB - use simple upload below this
BLOCK_SIZE = 4 * 1024 * 1024  # 4MB blocks for chunked upload
DOWNLOAD_CHUNK_SIZE = 256 * 1024  # 256KB chunks for download progress reporting


class BaiduProvider(CloudProvider):
    """Baidu Netdisk provider using Pan Open API."""

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.access_token = credentials.get("access_token", "")
        self.refresh_token = credentials.get("refresh_token", "")
        self.expires_at = credentials.get("expires_at", 0)
        self._session: Optional[aiohttp.ClientSession] = None
        # Set by the caller (API / sync engine) so a token refreshed during a
        # request is written back to the database instead of only living in
        # memory. Without this, every new provider object reloads the stale
        # token from the DB and the refresh is lost.
        self.conn_id: Optional[int] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_token(self):
        """Check if token is expired and refresh if needed."""
        if self.expires_at and time.time() > self.expires_at - 300:
            if self.refresh_token:
                await self._refresh_token()
            else:
                raise RuntimeError("Access token expired and no refresh token available")

    async def _refresh_token(self):
        """Refresh the access token using refresh_token."""
        from app.config import get_baidu_app_credentials
        app_key, app_secret = await get_baidu_app_credentials()
        if not app_key or not app_secret:
            raise RuntimeError("Baidu app key/secret not configured for token refresh")
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": app_key,
            "client_secret": app_secret,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(OAUTH_TOKEN_URL, params=params) as resp:
                data = await resp.json(content_type=None)
                if "access_token" not in data:
                    err = str(data.get("error", ""))
                    desc = str(data.get("error_description", "") or "")
                    low = desc.lower()
                    if err in ("invalid_grant", "invalid_request", "invalid_client"):
                        raise RuntimeError("refresh_token 已失效，需要重新授权")
                    if "security policy" in low or "try again later" in low:
                        raise RuntimeError(
                            "百度风控拦截（Trigger security policy），请稍后重试；"
                            "若持续失败请用授权码重新授权")
                    raise RuntimeError(f"Token refresh failed: {data}")
                self.access_token = data["access_token"]
                self.refresh_token = data.get("refresh_token", self.refresh_token)
                self.expires_at = time.time() + data.get("expires_in", 2592000)
                self.credentials["access_token"] = self.access_token
                self.credentials["refresh_token"] = self.refresh_token
                self.credentials["expires_at"] = self.expires_at
                logger.info("Baidu token refreshed (conn_id=%s, expires_at=%s)",
                            self.conn_id, int(self.expires_at))
                await self.persist_credentials()

    async def persist_credentials(self):
        """Write current credentials back to the database (best effort)."""
        if not self.conn_id:
            return
        try:
            await db.db_update_connection(
                self.conn_id, credentials=dict(self.credentials))
        except Exception as exc:  # never let a DB hiccup break a sync
            logger.warning("Failed to persist refreshed credentials: %s", exc)

    async def force_refresh(self) -> dict:
        """Force a refresh now, ignoring ``expires_at``.

        Used by the "刷新令牌" button so the user can renew a token before it
        expires, or recover a connection whose stored expiry looks stale.
        """
        if not self.refresh_token:
            raise RuntimeError("该连接没有 refresh_token，无法自动刷新，请重新授权")
        await self._refresh_token()
        return dict(self.credentials)

    async def _api_get(self, path: str, params: dict = None) -> dict:
        """Make a GET request to the Pan API.

        errno=-6 means "access token invalid/expired". We try exactly one
        forced refresh there, because ``expires_at`` in the DB can be stale
        (or 0 for manually pasted tokens) and would otherwise make the
        connection look permanently dead.
        """
        await self._ensure_token()
        params = dict(params or {})
        params["access_token"] = self.access_token
        session = await self._get_session()
        url = f"{PAN_API_BASE}{path}"
        for attempt in (1, 2):
            async with session.get(url, params=params) as resp:
                text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"API returned non-JSON: {text[:500]}")
            errno = data.get("errno")
            if errno and errno == -6 and attempt == 1 and self.refresh_token:
                logger.info("Baidu token rejected (errno -6), forcing refresh")
                await self._refresh_token()
                params["access_token"] = self.access_token
                continue
            if errno:
                raise RuntimeError(f"Baidu API error {errno}: {data.get('errmsg', '')}")
            return data
        raise RuntimeError("Baidu API error: token still invalid after refresh")

    async def _api_post(self, path: str, params: dict = None, data=None) -> dict:
        """Make a POST request to the Pan API (same errno -6 handling as GET)."""
        await self._ensure_token()
        params = dict(params or {})
        params["access_token"] = self.access_token
        session = await self._get_session()
        url = f"{PAN_API_BASE}{path}"
        for attempt in (1, 2):
            async with session.post(url, params=params, data=data) as resp:
                text = await resp.text()
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"API returned non-JSON: {text[:500]}")
            errno = result.get("errno")
            if errno and errno == -6 and attempt == 1 and self.refresh_token:
                logger.info("Baidu token rejected (errno -6), forcing refresh")
                await self._refresh_token()
                params["access_token"] = self.access_token
                continue
            if errno:
                raise RuntimeError(f"Baidu API error {errno}: {result.get('errmsg', '')}")
            return result
        raise RuntimeError("Baidu API error: token still invalid after refresh")

    async def test_connection(self) -> bool:
        """Test if the connection is valid by getting user info."""
        try:
            await self._api_get("/nas", {"method": "uinfo"})
            return True
        except Exception:
            return False

    async def test_connection_detailed(self) -> tuple[bool, str]:
        """Test connection and return detailed error info."""
        try:
            data = await self._api_get("/nas", {"method": "uinfo"})
            return True, f"OK: user={data.get('baidu_name', 'unknown')}"
        except Exception as e:
            return False, str(e)

    async def list_files(self, remote_path: str) -> list[FileInfo]:
        """List immediate children of remote_path.

        Uses the /xpan/file endpoint which is supported by the built-in
        credentials (ES File Manager's app id). The /xpan/multimedia
        endpoint returns "unsupported api" with these credentials.
        """
        remote_path = self.normalize_path(remote_path)
        files = []
        start = 0
        limit = 1000
        while True:
            params = {
                "method": "list",
                "dir": remote_path,
                "order": "time",
                "desc": 1,
                "start": start,
                "limit": limit,
            }
            data = await self._api_get("/file", params)
            entries = data.get("list", [])
            if not entries:
                break
            for entry in entries:
                fi = FileInfo(
                    path=self.normalize_path(entry.get("path", "")),
                    name=entry.get("server_filename", ""),
                    is_dir=entry.get("isdir", 0) == 1,
                    size=entry.get("size", 0),
                    modify_time=entry.get("local_mtime", entry.get("server_mtime", 0)),
                    md5=entry.get("md5", ""),
                    fs_id=str(entry.get("fs_id", "")),
                )
                files.append(fi)
            if len(entries) < limit:
                break
            start += limit
        return files

    async def list_all_files(self, remote_path: str) -> list[FileInfo]:
        """Recursively list all files under remote_path."""
        remote_path = self.normalize_path(remote_path)
        result = []
        queue = [remote_path]
        while queue:
            current = queue.pop(0)
            try:
                entries = await self.list_files(current)
            except Exception as e:
                # Skip directories we can't read
                continue
            for entry in entries:
                if entry.is_dir:
                    queue.append(entry.path)
                else:
                    result.append(entry)
        return result

    async def get_file_info(self, remote_path: str) -> Optional[FileInfo]:
        """Get metadata for a single file.

        Uses the /xpan/file?method=list endpoint because the filemetas
        method returns errno=2 (unsupported) with the built-in credentials
        (ES File Manager's app id).
        """
        remote_path = self.normalize_path(remote_path)
        try:
            parent = self.parent_path(remote_path)
            filename = remote_path.rsplit("/", 1)[-1]
            entries = await self.list_files(parent)
            for entry in entries:
                if entry.name == filename:
                    return entry
            return None
        except Exception:
            return None

    async def upload_file(self, local_path: str, remote_path: str,
                          progress_callback=None, _transfer=None) -> bool:
        """Upload a local file to Baidu Pan.

        Uses simple upload for small files and chunked upload for large files.
        """
        remote_path = self.normalize_path(remote_path)
        file_size = os.path.getsize(local_path)

        if progress_callback:
            await progress_callback(0, file_size)

        # Try rapid upload first (秒传)
        if await self._try_rapid_upload(local_path, remote_path, file_size):
            if progress_callback:
                await progress_callback(file_size, file_size)
            return True

        # Always use chunked upload (precreate + superfile2 + create) — this
        # endpoint works with the current credentials, while the legacy
        # /pcs/file simple upload endpoint returns "file is not authorized".
        if file_size <= MAX_SIMPLE_UPLOAD_SIZE:
            success = await self._simple_upload(local_path, remote_path, progress_callback, _transfer=_transfer)
        else:
            success = await self._chunked_upload(local_path, remote_path, file_size, progress_callback, _transfer=_transfer)

        if success and progress_callback:
            await progress_callback(file_size, file_size)
        return success

    async def _try_rapid_upload(self, local_path: str, remote_path: str,
                                 file_size: int) -> bool:
        """Try rapid upload (秒传) by checking if file hash already exists."""
        if file_size < 256:  # Too small for rapid upload
            return False
        try:
            md5_hash, slice_md5 = await asyncio.to_thread(
                self._calc_file_hashes, local_path, file_size
            )
            params = {
                "method": "rapidupload",
                "path": remote_path,
                "content-length": file_size,
                "content-md5": md5_hash,
                "slice-md5": slice_md5,
                "type": "0",
            }
            data = await self._api_post("/file", params)
            return data.get("md5") is not None
        except Exception:
            return False

    def _calc_file_hashes(self, local_path: str, file_size: int) -> tuple[str, str]:
        """Calculate full MD5 and first 256KB slice MD5."""
        md5_full = hashlib.md5()
        md5_slice = hashlib.md5()
        slice_size = 256 * 1024
        with open(local_path, "rb") as f:
            # Read first 256KB for slice MD5
            first_chunk = f.read(slice_size)
            md5_slice.update(first_chunk)
            # Continue for full MD5
            md5_full.update(first_chunk)
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                md5_full.update(chunk)
        return md5_full.hexdigest(), md5_slice.hexdigest()

    async def _simple_upload(self, local_path: str, remote_path: str,
                             progress_callback=None, _transfer=None) -> bool:
        """Upload a small file using the chunked flow (single block).

        The legacy /pcs/file simple upload endpoint returns
        "file is not authorized" with the current credentials, so we use the
        same precreate + superfile2 + create flow as chunked uploads,
        but with just one block.
        """
        file_size = os.path.getsize(local_path)
        return await self._chunked_upload(local_path, remote_path, file_size, progress_callback, _transfer=_transfer)

    async def _chunked_upload(self, local_path: str, remote_path: str,
                               file_size: int, progress_callback=None,
                               _transfer=None) -> bool:
        """Upload a large file using the precreate/upload/create flow."""
        num_blocks = math.ceil(file_size / BLOCK_SIZE)
        upload_concurrency = UPLOAD_CONCURRENCY

        # Calculate block MD5s in a thread to avoid blocking the event loop
        def _calc_md5s():
            md5_list = []
            with open(local_path, "rb") as f:
                for i in range(num_blocks):
                    block_data = f.read(BLOCK_SIZE)
                    block_md5 = hashlib.md5(block_data).hexdigest()
                    md5_list.append(block_md5)
            return md5_list

        block_md5_list = await asyncio.to_thread(_calc_md5s)

        # 1. Precreate (retried — a hiccup here would otherwise fail the
        # whole file before a single byte is sent)
        await self._ensure_token()
        params = {
            "method": "precreate",
            "access_token": self.access_token,
        }
        precreate_data = {
            "path": remote_path,
            "size": file_size,
            "isdir": 0,
            "block_list": json.dumps(block_md5_list),
            "autoinit": 1,
        }
        session = await self._get_session()

        async def do_precreate() -> str:
            async with session.post(f"{PAN_API_BASE}/file", params=params,
                                     data=precreate_data) as resp:
                # Precreate uses xpan endpoint which returns proper JSON
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    raise RuntimeError(f"Precreate returned non-JSON: {text[:200]}")
                if data.get("errno"):
                    raise RuntimeError(f"Precreate failed: {data}")
                created_uploadid = data.get("uploadid")
                if not created_uploadid:
                    raise RuntimeError(f"No uploadid in precreate response: {data}")
                return created_uploadid

        uploadid = await retry_async(
            do_precreate, what=f"precreate {os.path.basename(remote_path)}")

        # 2. Upload blocks — in parallel, each block retried independently.
        # Blocks finish out of order, so progress is aggregated through a
        # shared counter instead of a per-block absolute offset.
        state = {"total": 0}
        written = [0] * num_blocks

        def make_progress_cb(idx: int):
            def cb(delta: int) -> None:
                written[idx] += delta
                state["total"] += delta
                if _transfer is not None:
                    _transfer.transferred = min(state["total"], file_size)
            return cb

        async def upload_block(idx: int, block_data: bytes) -> None:
            """Single attempt at uploading one block."""
            part_params = {
                "method": "upload",
                "access_token": self.access_token,
                "type": "tmpfile",
                "path": remote_path,
                "uploadid": uploadid,
                "partseq": idx,
            }
            # A payload can only be consumed once, so it must be built
            # inside the attempt (not reused across retries).
            block_payload = _BlockPayload(
                block_data, on_progress=make_progress_cb(idx),
                filename=f"block_{idx}",
            )
            form = aiohttp.FormData()
            form.add_field("file", block_payload, filename=f"block_{idx}")
            async with session.post(f"{PCS_API_BASE}/superfile2",
                                     params=part_params, data=form) as resp:
                # /pcs/superfile2 sometimes returns text/html Content-Type for JSON
                text = await resp.text()
                try:
                    block_data_resp = json.loads(text)
                except json.JSONDecodeError:
                    raise RuntimeError(
                        f"Block {idx} upload returned non-JSON: {text[:200]}")
                if block_data_resp.get("error_code"):
                    raise RuntimeError(
                        f"Block {idx} upload failed: {block_data_resp}")
                # /pcs/superfile2 returns {"md5": "..."} on success
                if not block_data_resp.get("md5"):
                    raise RuntimeError(
                        f"Block {idx} upload no md5 in response: {block_data_resp}")

        async def upload_block_with_retry(idx: int, block_data: bytes) -> None:
            def on_retry(_attempt_no: int, _exc: BaseException) -> None:
                # Undo the byte count of the failed attempt so the retry
                # re-adds it instead of double counting.
                state["total"] -= written[idx]
                written[idx] = 0

            await retry_async(
                lambda: upload_block(idx, block_data),
                what=f"block {idx + 1}/{num_blocks} of "
                     f"{os.path.basename(remote_path)}",
                on_retry=on_retry,
            )

        # Bounded worker pool. Each worker opens its own file handle so no
        # handle is ever seeked from two coroutines at once, and memory stays
        # bounded at (workers * BLOCK_SIZE).
        queue: asyncio.Queue = asyncio.Queue()
        for i in range(num_blocks):
            queue.put_nowait(i)

        abort = asyncio.Event()
        errors: list[BaseException] = []

        async def worker() -> None:
            with open(local_path, "rb") as fh:
                while not abort.is_set():
                    try:
                        idx = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    fh.seek(idx * BLOCK_SIZE)
                    block_data = fh.read(BLOCK_SIZE)
                    try:
                        await upload_block_with_retry(idx, block_data)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - collected below
                        errors.append(exc)
                        abort.set()
                        return
                    if progress_callback:
                        # Doubles as the per-block cancellation check.
                        await progress_callback(
                            min(state["total"], file_size), file_size)

        worker_count = max(1, min(upload_concurrency, num_blocks))
        await asyncio.gather(*(worker() for _ in range(worker_count)))

        if errors:
            raise errors[0]

        # 3. Create (finalize) — retried for the same reason as precreate
        create_params = {
            "method": "create",
            "access_token": self.access_token,
        }
        create_data = {
            "path": remote_path,
            "size": file_size,
            "isdir": 0,
            "block_list": json.dumps(block_md5_list),
            "uploadid": uploadid,
        }

        async def do_create() -> bool:
            async with session.post(f"{PAN_API_BASE}/file", params=create_params,
                                     data=create_data) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    raise RuntimeError(f"Create returned non-JSON: {text[:200]}")
                if data.get("errno"):
                    raise RuntimeError(f"Create failed: {data}")
                return True

        return await retry_async(
            do_create, what=f"create {os.path.basename(remote_path)}")

    async def download_file(self, remote_path: str, local_path: str,
                            progress_callback=None) -> bool:
        """Download a file from Baidu Pan.

        Uses the PCS download endpoint directly, which returns a 302
        redirect to the CDN. The /xpan/file?method=filemetas endpoint
        returns errno=2 (unsupported) with the built-in credentials, so
        we cannot obtain a dlink. The PCS download endpoint works fine.
        """
        remote_path = self.normalize_path(remote_path)
        await self._ensure_token()
        session = await self._get_session()

        # Download via PCS endpoint (follows 302 redirect to CDN)
        params = {
            "method": "download",
            "path": remote_path,
            "access_token": self.access_token,
        }
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        downloaded = 0
        async with session.get(
            f"{PCS_API_BASE}/file", params=params, allow_redirects=True
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Download failed: HTTP {resp.status}: {text[:200]}")
            # Get file size from Content-Length header
            content_length = resp.headers.get("Content-Length", "0")
            file_size = int(content_length) if content_length.isdigit() else 0
            with open(local_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        await progress_callback(downloaded, file_size)
        return True

    async def delete_file(self, remote_path: str) -> bool:
        """Delete a file or directory on Baidu Pan."""
        remote_path = self.normalize_path(remote_path)
        filelist = json.dumps([{"path": remote_path}])
        data = await self._api_post("/file", {
            "method": "filemanager",
            "opera": "delete",
        }, data={"filelist": filelist})
        return data.get("errno", -1) == 0

    async def create_directory(self, remote_path: str) -> bool:
        """Create a directory on Baidu Pan."""
        remote_path = self.normalize_path(remote_path)
        try:
            await self._api_post("/create", {
                "method": "create_dir",
                "path": remote_path,
            })
            return True
        except RuntimeError as e:
            if "already exists" in str(e) or "errno" in str(e):
                # Directory might already exist, check
                info = await self.get_file_info(remote_path)
                return info is not None and info.is_dir
            raise

    @staticmethod
    def get_oauth_tool_url() -> str:
        """Get the AList OAuth tool URL for the user to obtain a refresh_token.

        The user visits this URL, authorizes on Baidu, and gets a refresh_token
        from AList's callback page. They then paste the refresh_token into our app.
        """
        return BAIDU_OAUTH_TOOL_URL

    @staticmethod
    async def exchange_refresh_token(refresh_token: str, app_key: str = "",
                                     app_secret: str = "") -> dict:
        """Exchange a refresh_token for a new access_token + refresh_token.

        Uses the built-in AList credentials by default, or custom credentials
        if provided (from database settings).
        """
        if not app_key:
            from app.config import get_baidu_app_credentials
            app_key, app_secret = await get_baidu_app_credentials()
        if not app_key or not app_secret:
            raise RuntimeError("Baidu app key/secret not configured")

        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": app_key,
            "client_secret": app_secret,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(OAUTH_TOKEN_URL, params=params) as resp:
                data = await resp.json()
                if "access_token" not in data:
                    raise RuntimeError(f"Token refresh failed: {data}")
                return {
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token", refresh_token),
                    "expires_at": time.time() + data.get("expires_in", 2592000),
                }

    @staticmethod
    async def exchange_code(code: str, app_key: str = "", app_secret: str = "",
                            redirect_uri: str = "") -> dict:
        """Exchange authorization code for access token (legacy/manual)."""
        if not app_key:
            from app.config import BAIDU_APP_KEY, BAIDU_APP_SECRET, BAIDU_REDIRECT_URI
            app_key = BAIDU_APP_KEY
            app_secret = BAIDU_APP_SECRET
            redirect_uri = BAIDU_REDIRECT_URI
        if not app_key or not app_secret:
            raise RuntimeError("Baidu app key/secret not configured")
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app_key,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(OAUTH_TOKEN_URL, params=params) as resp:
                data = await resp.json()
                if "access_token" not in data:
                    raise RuntimeError(f"Token exchange failed: {data}")
                return {
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token", ""),
                    "expires_at": time.time() + data.get("expires_in", 2592000),
                }

    @staticmethod
    async def validate_token(access_token: str) -> dict:
        """Validate a manually provided access token and get user info."""
        params = {"access_token": access_token}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://pan.baidu.com/rest/2.0/xpan/nas", params={
                    **params, "method": "uinfo"
                }) as resp:
                data = await resp.json()
                if data.get("errno"):
                    raise RuntimeError(f"Invalid token: {data}")
                return data
