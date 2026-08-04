"""Baidu Netdisk (百度网盘) cloud provider.

Implements Baidu Pan Open API for file sync operations.
Supports OAuth2 authorization and manual token entry.
"""

import aiohttp
import asyncio
import hashlib
import json
import os
import time
import math
from typing import Optional
from urllib.parse import urlencode

from app.providers.base import CloudProvider, FileInfo, AuthResult
from app.config import BAIDU_APP_KEY, BAIDU_APP_SECRET, BAIDU_REDIRECT_URI, BAIDU_DEFAULT_APP_KEY, BAIDU_DEFAULT_APP_SECRET, BAIDU_OAUTH_TOOL_URL, SYNC_CHUNK_SIZE
from app import database as db

# API endpoints
OAUTH_AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
OAUTH_TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
PAN_API_BASE = "https://pan.baidu.com/rest/2.0/xpan"
PCS_API_BASE = "https://d.pcs.baidu.com/rest/2.0/pcs"

# Upload thresholds
MAX_SIMPLE_UPLOAD_SIZE = 4 * 1024 * 1024  # 4MB - use simple upload below this
BLOCK_SIZE = 4 * 1024 * 1024  # 4MB blocks for chunked upload


class BaiduProvider(CloudProvider):
    """Baidu Netdisk provider using Pan Open API."""

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.access_token = credentials.get("access_token", "")
        self.refresh_token = credentials.get("refresh_token", "")
        self.expires_at = credentials.get("expires_at", 0)
        self._session: Optional[aiohttp.ClientSession] = None

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
                data = await resp.json()
                if "access_token" not in data:
                    raise RuntimeError(f"Token refresh failed: {data}")
                self.access_token = data["access_token"]
                self.refresh_token = data.get("refresh_token", self.refresh_token)
                self.expires_at = time.time() + data.get("expires_in", 2592000)
                self.credentials["access_token"] = self.access_token
                self.credentials["refresh_token"] = self.refresh_token
                self.credentials["expires_at"] = self.expires_at

    async def _api_get(self, path: str, params: dict = None) -> dict:
        """Make a GET request to the Pan API."""
        await self._ensure_token()
        params = params or {}
        params["access_token"] = self.access_token
        session = await self._get_session()
        url = f"{PAN_API_BASE}{path}"
        async with session.get(url, params=params) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"API returned non-JSON: {text[:500]}")
            if data.get("errno"):
                raise RuntimeError(f"Baidu API error {data['errno']}: {data.get('errmsg', '')}")
            return data

    async def _api_post(self, path: str, params: dict = None, data=None) -> dict:
        """Make a POST request to the Pan API."""
        await self._ensure_token()
        params = params or {}
        params["access_token"] = self.access_token
        session = await self._get_session()
        url = f"{PAN_API_BASE}{path}"
        async with session.post(url, params=params, data=data) as resp:
            text = await resp.text()
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"API returned non-JSON: {text[:500]}")
            if result.get("errno"):
                raise RuntimeError(f"Baidu API error {result['errno']}: {result.get('errmsg', '')}")
            return result

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
                          progress_callback=None) -> bool:
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
            success = await self._simple_upload(local_path, remote_path)
        else:
            success = await self._chunked_upload(local_path, remote_path, file_size, progress_callback)

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

    async def _simple_upload(self, local_path: str, remote_path: str) -> bool:
        """Upload a small file using the chunked flow (single block).

        The legacy /pcs/file simple upload endpoint returns
        "file is not authorized" with the current credentials, so we use the
        same precreate + superfile2 + create flow as chunked uploads,
        but with just one block.
        """
        file_size = os.path.getsize(local_path)
        return await self._chunked_upload(local_path, remote_path, file_size, None)

    async def _chunked_upload(self, local_path: str, remote_path: str,
                               file_size: int, progress_callback=None) -> bool:
        """Upload a large file using the precreate/upload/create flow."""
        num_blocks = math.ceil(file_size / BLOCK_SIZE)
        block_md5_list = []

        # Calculate block MD5s
        with open(local_path, "rb") as f:
            for i in range(num_blocks):
                block_data = f.read(BLOCK_SIZE)
                block_md5 = hashlib.md5(block_data).hexdigest()
                block_md5_list.append(block_md5)

        # 1. Precreate
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
            uploadid = data.get("uploadid")
            if not uploadid:
                raise RuntimeError(f"No uploadid in precreate response: {data}")

        # 2. Upload blocks
        uploaded = 0
        with open(local_path, "rb") as f:
            for i in range(num_blocks):
                block_data = f.read(BLOCK_SIZE)
                part_params = {
                    "method": "upload",
                    "access_token": self.access_token,
                    "type": "tmpfile",
                    "path": remote_path,
                    "uploadid": uploadid,
                    "partseq": i,
                }
                form = aiohttp.FormData()
                form.add_field("file", block_data,
                               filename=f"block_{i}")
                async with session.post(f"{PCS_API_BASE}/superfile2",
                                         params=part_params, data=form) as resp:
                    # /pcs/superfile2 sometimes returns text/html Content-Type for JSON
                    text = await resp.text()
                    try:
                        block_data_resp = json.loads(text)
                    except json.JSONDecodeError:
                        raise RuntimeError(
                            f"Block {i} upload returned non-JSON: {text[:200]}")
                    if block_data_resp.get("error_code"):
                        raise RuntimeError(
                            f"Block {i} upload failed: {block_data_resp}")
                    # /pcs/superfile2 returns {"md5": "..."} on success
                    if not block_data_resp.get("md5"):
                        raise RuntimeError(
                            f"Block {i} upload no md5 in response: {block_data_resp}")

                uploaded += len(block_data) if isinstance(block_data, bytes) else BLOCK_SIZE
                if progress_callback:
                    await progress_callback(min(uploaded, file_size), file_size)

        # 3. Create (finalize)
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
                async for chunk in resp.content.iter_chunked(SYNC_CHUNK_SIZE):
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
