"""Base cloud provider interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import time
import io


class ProgressReader(io.IOBase):
    """File-like wrapper that updates transfer.transferred on every read().

    aiohttp's IOBasePayload reads in 64KB chunks, so progress is reported
    at 64KB granularity — frequent enough for real-time speed display
    even when multiple files transfer concurrently.

    Subclasses IOBase and exposes __len__ so aiohttp uses Content-Length
    instead of chunked Transfer-Encoding. Some cloud APIs (Baidu
    superfile2) reject requests with `Transfer-Encoding: chunked`.
    """

    def __init__(self, fileobj, transfer=None, base: int = 0,
                 file_size: int = 0):
        super().__init__()
        self._fileobj = fileobj
        self._transfer = transfer
        self._base = base
        self._file_size = file_size
        self._read_bytes = 0

    def readable(self) -> bool:
        return True

    def __len__(self) -> int:
        # Tell aiohttp the total payload size so it uses Content-Length
        # rather than chunked Transfer-Encoding.
        return self._file_size

    def read(self, size: int = -1):
        chunk = self._fileobj.read(size)
        if chunk:
            self._read_bytes += len(chunk)
            if self._transfer:
                self._transfer.transferred = self._base + self._read_bytes
        return chunk

    def seek(self, *args):
        return self._fileobj.seek(*args)

    def tell(self):
        return self._fileobj.tell()

    def close(self):
        try:
            self._fileobj.close()
        except Exception:
            pass
        super().close()


@dataclass
class FileInfo:
    """Represents a file or directory on either local or cloud storage."""
    path: str          # Full path relative to sync root, e.g. "/photos/2024/img.jpg"
    name: str          # File or directory name
    is_dir: bool       # True if directory
    size: int = 0      # File size in bytes (0 for directories)
    modify_time: float = 0.0  # Modification time as Unix timestamp
    md5: str = ""      # MD5 hash if available (for cloud rapid upload / integrity check)
    fs_id: str = ""    # Baidu fs_id
    cid: str = ""      # 115 category id


@dataclass
class AuthResult:
    """Result of an authentication attempt."""
    success: bool
    credentials: dict = field(default_factory=dict)
    message: str = ""
    need_action: str = ""  # 'qr_code', 'oauth_url', 'token_input'
    action_data: dict = field(default_factory=dict)


class CloudProvider(ABC):
    """Abstract base class for cloud storage providers."""

    def __init__(self, credentials: dict):
        self.credentials = credentials

    @abstractmethod
    async def test_connection(self) -> bool:
        """Test if the connection is valid. Returns True if valid."""
        ...

    @abstractmethod
    async def list_files(self, remote_path: str) -> list[FileInfo]:
        """List files and directories under remote_path.
        
        Args:
            remote_path: Path on the cloud storage, e.g. "/photos" or "/" for root
            
        Returns:
            List of FileInfo objects for immediate children.
        """
        ...

    @abstractmethod
    async def list_all_files(self, remote_path: str) -> list[FileInfo]:
        """Recursively list all files under remote_path.
        
        Returns flat list of all files (not directories) under the path.
        """
        ...

    @abstractmethod
    async def upload_file(self, local_path: str, remote_path: str,
                          progress_callback=None) -> bool:
        """Upload a local file to remote_path.
        
        Args:
            local_path: Local filesystem path
            remote_path: Full remote path including filename
            
        Returns:
            True if upload succeeded.
        """
        ...

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str,
                            progress_callback=None) -> bool:
        """Download a remote file to local_path."""
        ...

    @abstractmethod
    async def delete_file(self, remote_path: str) -> bool:
        """Delete a file on the cloud."""
        ...

    @abstractmethod
    async def create_directory(self, remote_path: str) -> bool:
        """Create a directory on the cloud."""
        ...

    @abstractmethod
    async def get_file_info(self, remote_path: str) -> Optional[FileInfo]:
        """Get metadata for a single file or directory."""
        ...

    @staticmethod
    def normalize_path(path: str) -> str:
        """Ensure path starts with / and doesn't end with / (except root)."""
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return path

    @staticmethod
    def join_paths(*parts) -> str:
        """Join path parts with / separators."""
        result = "/".join(part.strip("/") for part in parts if part)
        return "/" + result if not result.startswith("/") else result

    @staticmethod
    def parent_path(path: str) -> str:
        """Get parent directory path."""
        path = CloudProvider.normalize_path(path)
        if path == "/":
            return "/"
        idx = path.rfind("/")
        if idx <= 0:
            return "/"
        return path[:idx]

    @staticmethod
    def basename(path: str) -> str:
        """Get the filename from a path."""
        path = CloudProvider.normalize_path(path)
        if path == "/":
            return ""
        return path[path.rfind("/") + 1:]
