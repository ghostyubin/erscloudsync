"""115 Netdisk (115网盘) cloud provider.

Implements 115 cloud storage API with QR code login support.
Uses cookie-based authentication.
"""

import aiohttp
import asyncio
import hashlib
import json
import os
import time
from typing import Optional
from urllib.parse import urlencode

from app.providers.base import CloudProvider, FileInfo, AuthResult
from app.config import SYNC_CHUNK_SIZE

# API endpoints
QR_LOGIN_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/poll"
QR_IMAGE_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode"
FILES_API = "https://proapi.115.com/android/2.0/files"
UPLOAD_INIT_URL = "https://proapi.115.com/android/2.0/upload/init"
DOWNLOAD_URL = "https://proapi.115.com/android/2.0/files/download"

# Cookie keys we need
COOKIE_KEYS = ["UID", "CID", "SEID", "KID"]


class Provider115(CloudProvider):
    """115 Netdisk provider using web API with cookie authentication."""

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.cookies = credentials.get("cookies", {})
        self.uid = self.cookies.get("UID", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._cid_cache: dict[str, str] = {"/": "0"}  # path -> CID cache

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300),
                headers={
                    "Cookie": self.cookie_header,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36",
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def test_connection(self) -> bool:
        """Test connection by listing root directory."""
        try:
            files = await self.list_files("/")
            return True
        except Exception:
            return False

    def _parse_file_entry(self, entry: dict, parent_path: str) -> FileInfo:
        """Parse a file entry from 115 API response, handling multiple formats."""
        name = entry.get("n") or entry.get("file_name") or entry.get("name", "")
        size = entry.get("s") or entry.get("file_size") or entry.get("size", 0)
        mtime = entry.get("t") or entry.get("update_time") or entry.get("modify_time", 0)
        sha1 = entry.get("sha1") or entry.get("sha", "")
        fid = str(entry.get("fid") or entry.get("file_id", ""))
        pickcode = entry.get("pc") or entry.get("pickcode", "")

        # Determine if directory
        # 115 API uses various indicators: no sha1, fc == -1, or explicit is_dir
        is_dir = (
            entry.get("is_dir") is True
            or entry.get("fc") == -1
            or (not sha1 and not pickcode and not entry.get("s"))
            or entry.get("pid") == ""
        )
        # More reliable check: directories don't have pickcode
        if not pickcode and entry.get("fc") is not None:
            is_dir = True
        if entry.get("fc") == 0 and pickcode:
            is_dir = False

        path = self.join_paths(parent_path, name)

        return FileInfo(
            path=path,
            name=name,
            is_dir=is_dir,
            size=int(size) if not is_dir else 0,
            modify_time=float(mtime) if mtime else 0,
            md5=sha1,  # 115 uses SHA1, not MD5, but we store it in the same field
            cid=fid if is_dir else "",
            fs_id=fid,
        )

    async def _resolve_cid(self, remote_path: str) -> str:
        """Resolve a remote path to a 115 CID (category ID)."""
        remote_path = self.normalize_path(remote_path)
        if remote_path in self._cid_cache:
            return self._cid_cache[remote_path]
        if remote_path == "/":
            return "0"

        parts = [p for p in remote_path.split("/") if p]
        current_cid = "0"
        current_path = ""

        for part in parts:
            current_path = self.join_paths(current_path, part)
            if current_path in self._cid_cache:
                current_cid = self._cid_cache[current_path]
                continue

            # List files in current CID to find the directory
            found = False
            offset = 0
            while True:
                files = await self._list_by_cid(current_cid, offset=offset)
                if not files:
                    break
                for entry in files:
                    fi = self._parse_file_entry(entry, current_path)
                    if fi.name == part:
                        if fi.is_dir:
                            current_cid = fi.cid or fi.fs_id
                            self._cid_cache[current_path] = current_cid
                            found = True
                            break
                        else:
                            # It's a file, not a directory
                            return ""
                if found:
                    break
                offset += len(files)
                if len(files) < 100:
                    break

            if not found:
                return ""

        return current_cid

    async def _list_by_cid(self, cid: str, offset: int = 0,
                            limit: int = 100) -> list[dict]:
        """List files in a directory by CID."""
        params = {
            "cid": cid,
            "o": "user_ptime",
            "asc": 0,
            "offset": offset,
            "show_dir": 1,
            "limit": limit,
            "format": "json",
        }
        session = await self._get_session()
        async with session.get(FILES_API, params=params) as resp:
            data = await resp.json()
            if not data.get("state", True):
                raise RuntimeError(f"115 API error: {data}")
            # Handle different response formats
            if "data" in data and isinstance(data["data"], dict):
                return data["data"].get("list", [])
            elif "data" in data and isinstance(data["data"], list):
                return data["data"]
            elif "list" in data:
                return data["list"]
            return []

    async def list_files(self, remote_path: str) -> list[FileInfo]:
        """List immediate children of remote_path."""
        remote_path = self.normalize_path(remote_path)
        cid = await self._resolve_cid(remote_path)
        if not cid and remote_path != "/":
            return []

        result = []
        offset = 0
        while True:
            entries = await self._list_by_cid(cid or "0", offset=offset)
            if not entries:
                break
            for entry in entries:
                fi = self._parse_file_entry(entry, remote_path)
                if fi.is_dir and fi.cid:
                    self._cid_cache[fi.path] = fi.cid
                result.append(fi)
            if len(entries) < 100:
                break
            offset += 100
        return result

    async def list_all_files(self, remote_path: str) -> list[FileInfo]:
        """Recursively list all files under remote_path."""
        remote_path = self.normalize_path(remote_path)
        result = []
        queue = [(remote_path, None)]
        while queue:
            path, _ = queue.pop(0)
            try:
                entries = await self.list_files(path)
            except Exception:
                continue
            for entry in entries:
                if entry.is_dir:
                    queue.append((entry.path, None))
                else:
                    result.append(entry)
        return result

    async def get_file_info(self, remote_path: str) -> Optional[FileInfo]:
        """Get metadata for a single file."""
        remote_path = self.normalize_path(remote_path)
        parent = self.parent_path(remote_path)
        name = self.basename(remote_path)
        try:
            entries = await self.list_files(parent)
            for entry in entries:
                if entry.name == name:
                    return entry
        except Exception:
            pass
        return None

    async def upload_file(self, local_path: str, remote_path: str,
                          progress_callback=None) -> bool:
        """Upload a local file to 115."""
        remote_path = self.normalize_path(remote_path)
        parent_path = self.parent_path(remote_path)
        filename = self.basename(remote_path)
        file_size = os.path.getsize(local_path)

        if progress_callback:
            await progress_callback(0, file_size)

        # Resolve target CID
        cid = await self._resolve_cid(parent_path)
        if not cid and parent_path != "/":
            # Create the directory
            await self.create_directory(parent_path)
            cid = await self._resolve_cid(parent_path)
        cid = cid or "0"

        # Calculate SHA1 for rapid upload check
        sha1 = await asyncio.to_thread(self._calc_sha1, local_path)

        # Initialize upload
        preid = await asyncio.to_thread(self._calc_preid, local_path)

        params = {
            "userid": self.uid,
            "filename": filename,
            "filesize": file_size,
            "target": cid,
            "fileid": sha1,
            "preid": preid,
        }
        session = await self._get_session()
        async with session.post(UPLOAD_INIT_URL, params=params) as resp:
            data = await resp.json()
            if not data.get("state", True):
                raise RuntimeError(f"Upload init failed: {data}")

            # Check for rapid upload (秒传)
            upload_data = data.get("data", data)
            if upload_data.get("rapid_upload") or upload_data.get("exist") or \
               upload_data.get("file_id"):
                # Rapid upload succeeded
                if progress_callback:
                    await progress_callback(file_size, file_size)
                return True

            # Get OSS upload URL and parameters
            upload_url = upload_data.get("upload_url") or upload_data.get("url")
            oss_params = upload_data.get("params") or upload_data.get("oss_params", {})

            if not upload_url:
                raise RuntimeError(f"No upload URL in response: {data}")

        # Upload to OSS
        with open(local_path, "rb") as f:
            form = aiohttp.FormData()
            for k, v in oss_params.items():
                form.add_field(k, str(v))
            form.add_field("file", f, filename=filename)

            async with session.post(upload_url, data=form) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    raise RuntimeError(f"OSS upload failed: HTTP {resp.status}: {text[:500]}")

        if progress_callback:
            await progress_callback(file_size, file_size)
        return True

    def _calc_sha1(self, local_path: str) -> str:
        """Calculate SHA1 hash of a file."""
        sha1 = hashlib.sha1()
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha1.update(chunk)
        return sha1.hexdigest().upper()

    def _calc_preid(self, local_path: str) -> str:
        """Calculate preid (first 128KB SHA1) for 115 upload."""
        sha1 = hashlib.sha1()
        with open(local_path, "rb") as f:
            data = f.read(128 * 1024)
            sha1.update(data)
        return sha1.hexdigest().upper()

    async def download_file(self, remote_path: str, local_path: str,
                            progress_callback=None) -> bool:
        """Download a file from 115."""
        info = await self.get_file_info(remote_path)
        if not info:
            raise RuntimeError(f"File not found: {remote_path}")

        file_id = info.fs_id
        file_size = info.size

        if progress_callback:
            await progress_callback(0, file_size)

        session = await self._get_session()

        # Get download URL
        params = {"file_id": file_id, "user_id": self.uid}
        async with session.get(DOWNLOAD_URL, params=params, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                download_url = resp.headers.get("Location", "")
            else:
                data = await resp.json()
                download_data = data.get("data", data)
                download_url = download_data.get("download_url") or \
                               download_data.get("url", "")

        if not download_url:
            raise RuntimeError(f"Could not get download URL for: {remote_path}")

        # Download file
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        downloaded = 0
        async with session.get(download_url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Download failed: HTTP {resp.status}")
            with open(local_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(SYNC_CHUNK_SIZE):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        await progress_callback(downloaded, file_size)
        return True

    async def delete_file(self, remote_path: str) -> bool:
        """Delete a file or directory on 115."""
        info = await self.get_file_info(remote_path)
        if not info:
            raise RuntimeError(f"File not found: {remote_path}")

        file_id = info.fs_id
        params = {"file_id": file_id, "user_id": self.uid}
        session = await self._get_session()
        async with session.post(f"{FILES_API}/delete", params=params) as resp:
            data = await resp.json()
            return data.get("state", False)

    async def create_directory(self, remote_path: str) -> bool:
        """Create a directory on 115."""
        remote_path = self.normalize_path(remote_path)
        if remote_path == "/" or remote_path in self._cid_cache:
            return True

        parent_path = self.parent_path(remote_path)
        dir_name = self.basename(remote_path)

        # Ensure parent exists
        parent_cid = await self._resolve_cid(parent_path)
        if not parent_cid and parent_path != "/":
            await self.create_directory(parent_path)
            parent_cid = await self._resolve_cid(parent_path)
        parent_cid = parent_cid or "0"

        params = {
            "user_id": self.uid,
            "cid": parent_cid,
            "dir_name": dir_name,
        }
        session = await self._get_session()
        async with session.post(f"{FILES_API}/add_dir", params=params) as resp:
            data = await resp.json()
            if data.get("state", False):
                # Cache the new CID
                file_id = data.get("data", {}).get("file_id") or \
                         data.get("data", {}).get("cid", "")
                if file_id:
                    self._cid_cache[remote_path] = str(file_id)
                return True
            # Directory might already exist
            if "exist" in str(data).lower() or "already" in str(data).lower():
                return True
            raise RuntimeError(f"Create directory failed: {data}")

    # ---- QR Code Login ----

    @staticmethod
    async def get_qr_token() -> dict:
        """Get QR code login token and UID."""
        async with aiohttp.ClientSession() as session:
            async with session.post(QR_LOGIN_URL) as resp:
                data = await resp.json()
                if not data.get("state", False):
                    raise RuntimeError(f"Failed to get QR token: {data}")
                qr_data = data.get("data", {})
                return {
                    "uid": str(qr_data.get("uid", "")),
                    "token": str(qr_data.get("token", "")),
                    "sign": str(qr_data.get("sign", "")),
                    "time": qr_data.get("time", int(time.time())),
                }

    @staticmethod
    async def get_qr_image(token: str) -> bytes:
        """Get QR code image bytes."""
        params = {"token": token}
        async with aiohttp.ClientSession() as session:
            async with session.get(QR_IMAGE_URL, params=params) as resp:
                return await resp.read()

    @staticmethod
    async def poll_qr_login(uid: str, token: str, sign: str,
                             t: int) -> dict:
        """Poll QR code login status.
        
        Returns:
            dict with 'status' (-1=waiting, -2=expired, 1=scanned, 2=confirmed)
            and 'cookies' (when confirmed).
        """
        params = {
            "uid": uid,
            "token": token,
            "sign": sign,
            "time": t,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(QR_LOGIN_URL, data=params) as resp:
                data = await resp.json()
                qr_data = data.get("data", {})

                status = qr_data.get("status", -1)
                result = {"status": status, "message": qr_data.get("msg", "")}

                if status == 2:
                    # Login confirmed, extract cookies
                    cookies = qr_data.get("cookie", {})
                    if not cookies:
                        # Try to extract from response headers
                        cookies = {}
                        for key in COOKIE_KEYS:
                            val = resp.cookies.get(key)
                            if val:
                                cookies[key] = str(val.value)
                    result["cookies"] = cookies

                return result

    @staticmethod
    async def validate_cookies(cookies: dict) -> bool:
        """Validate cookies by making a test API call."""
        provider = Provider115({"cookies": cookies})
        try:
            result = await provider.test_connection()
            await provider.close()
            return result
        except Exception:
            return False
