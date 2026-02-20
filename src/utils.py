"""Shared utility functions."""

import fcntl
import json
import time
import uuid
import functools
from datetime import datetime, timezone
from pathlib import Path


def parse_github_url(url: str) -> tuple[str, str]:
    """Extract (owner, name) from a GitHub URL."""
    url = url.rstrip("/")
    parts = url.split("/")
    return parts[-2], parts[-1]


def now_iso() -> str:
    """Current time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def json_dumps(obj) -> str:
    """JSON serialize with default handler for dates."""
    return json.dumps(obj, default=str, ensure_ascii=False)


def truncate(text: str, max_len: int = 200) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def retry_on_rate_limit(max_retries: int = 3, base_delay: float = 60.0):
    """Decorator that retries on GitHub rate limit errors."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "rate limit" in str(e).lower() and attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        print(f"Rate limited. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        raise
        return wrapper
    return decorator


def async_retry_on_rate_limit(max_retries: int = 3, base_delay: float = 60.0):
    """Async decorator that retries on GitHub rate limit errors."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            import asyncio
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if "rate limit" in str(e).lower() and attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        print(f"Rate limited. Retrying in {delay}s...")
                        await asyncio.sleep(delay)
                    else:
                        raise
        return wrapper
    return decorator


def generate_agent_id() -> str:
    """Generate a short unique agent ID."""
    return uuid.uuid4().hex[:12]


def atomic_file_append(path: Path, text: str):
    """Append text to a file atomically using file locking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(text)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
