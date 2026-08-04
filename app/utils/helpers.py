"""Helper utilities."""

from app.providers.base import CloudProvider
from app.providers.baidu import BaiduProvider
from app.providers.p115 import Provider115


def get_provider(conn_type: str, credentials: dict) -> CloudProvider:
    """Factory method to get a cloud provider instance."""
    if conn_type == "baidu":
        return BaiduProvider(credentials)
    elif conn_type == "115":
        return Provider115(credentials)
    else:
        raise ValueError(f"Unknown provider type: {conn_type}")


def format_size(size: int) -> str:
    """Format file size in human-readable format."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


def format_time(ts: float) -> str:
    """Format Unix timestamp to readable string."""
    if not ts:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"
