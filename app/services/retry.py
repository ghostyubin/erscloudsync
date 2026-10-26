"""Retry helper for transient network failures.

Cloud APIs fail in two very different ways:

* **Transient** — TCP reset, timeout, 5xx, CDN hiccup. Retrying works and is
  the difference between "a 500MB upload failed at 92%" and "it succeeded".
* **Permanent** — invalid token, no permission, path not allowed. Retrying
  just wastes time and can hammer the API.

``retry_async`` distinguishes the two, backs off between attempts, and never
swallows cancellation (pause / cancel-file must stay responsive).
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional, TypeVar

from app.config import SYNC_MAX_RETRIES, SYNC_RETRY_DELAY

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Substrings that mark an error as permanent. Matching on the message is
# crude but these come from Baidu/PCS JSON bodies that we surface verbatim
# in RuntimeError messages.
_PERMANENT_MARKERS = (
    "invalid_grant",
    "invalid token",
    "access token invalid",
    "expired token",
    "errno\": -6",       # Baidu: invalid access token
    "errno\": 31064",    # Baidu: file not authorized
    "errno\": 31211",    # Baidu: chunked encoding (a bug on our side, not transient)
    "error_code\": -6",
    "error_code\": 31064",
    "file is not authorized",
    "no permission",
    "permission denied",
    "not found",
    "user is not authorized",
)

# Exceptions that should never be retried: local filesystem problems and
# cancellation.
_NEVER_RETRY_EXC = (
    asyncio.CancelledError,
    FileNotFoundError,
    IsADirectoryError,
    NotADirectoryError,
    PermissionError,
)


def is_retryable(exc: BaseException) -> bool:
    """Return True if retrying the operation could plausibly succeed."""
    if isinstance(exc, _NEVER_RETRY_EXC):
        return False
    text = str(exc)
    lowered = text.lower()
    return not any(marker in lowered for marker in _PERMANENT_MARKERS)


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    what: str = "operation",
    attempts: Optional[int] = None,
    delay: Optional[float] = None,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> T:
    """Run ``func()``, retrying transient failures.

    Args:
        func: Zero-argument callable returning an awaitable. It is called
            again from scratch on every attempt, so it must be idempotent
            and must not capture one-shot objects (e.g. an already consumed
            aiohttp payload).
        what: Human-readable label used in log messages.
        attempts: Total attempts (defaults to ``SYNC_MAX_RETRIES``).
        delay: Base backoff delay in seconds (defaults to ``SYNC_RETRY_DELAY``).
            Attempt N waits ``delay * N``.
        on_retry: Called as ``on_retry(attempt_index, exc)`` right before a
            retry, useful for resetting progress counters.

    Returns:
        Whatever ``func()`` returns on the first successful attempt.

    Raises:
        The last exception if every attempt fails, or immediately if the
        error is permanent / the task was cancelled.
    """
    total = max(1, attempts if attempts is not None else SYNC_MAX_RETRIES)
    base_delay = SYNC_RETRY_DELAY if delay is None else delay

    last_exc: Optional[BaseException] = None
    for attempt in range(1, total + 1):
        try:
            return await func()
        except asyncio.CancelledError:
            # Pause / cancel-file must never be retried or delayed.
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last_exc = exc
            if attempt >= total or not is_retryable(exc):
                raise
            wait = base_delay * attempt
            logger.warning(
                "Retry %d/%d for %s in %.1fs: %s: %s",
                attempt, total - 1, what, wait, type(exc).__name__, exc,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt, exc)
                except Exception:  # pragma: no cover - defensive
                    pass
            await asyncio.sleep(wait)

    # Unreachable: the loop either returns or raises.
    assert last_exc is not None
    raise last_exc
