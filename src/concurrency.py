"""Concurrency primitives for multi-agent safety."""

import asyncio
import fcntl
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.config import DB_PATH, DATA_DIR, WORK_DIR


class ConnectionPool:
    """Thread-local SQLite connections with WAL + busy_timeout."""

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._local = threading.local()
        self._schema_ready = False

    def get(self) -> sqlite3.Connection:
        """Get or create a thread-local connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Ensure schema + migrations on first connection
            if not self._schema_ready:
                from src.db import _ensure_schema
                from src.migration import run_migrations
                _ensure_schema(conn)
                run_migrations(conn)
                self._schema_ready = True
            self._local.conn = conn
        return conn

    def close(self):
        """Close the thread-local connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


async def claim_issue(pool: ConnectionPool, issue_id: int, agent_id: str,
                      ttl_minutes: int = 120) -> bool:
    """Atomically claim an issue for an agent.

    Uses INSERT OR IGNORE so only one agent can claim.
    Returns True if claim succeeded (rowcount > 0).
    """
    conn = pool.get()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes)

    # Clean expired and completed/released claims so new agents can work on them
    conn.execute(
        "DELETE FROM issue_claims WHERE status != 'active' OR expires_at < ?",
        (now.isoformat(),),
    )
    conn.commit()

    # Try to claim — INSERT OR IGNORE ensures only one active claim per issue
    cursor = conn.execute(
        """INSERT OR IGNORE INTO issue_claims (issue_id, agent_id, claimed_at, expires_at, status)
           VALUES (?, ?, ?, ?, 'active')""",
        (issue_id, agent_id, now.isoformat(), expires.isoformat()),
    )
    conn.commit()
    return cursor.rowcount > 0


async def release_claim(pool: ConnectionPool, issue_id: int, agent_id: str,
                        status: str = "completed"):
    """Release a claim on an issue."""
    conn = pool.get()
    conn.execute(
        "UPDATE issue_claims SET status = ? WHERE issue_id = ? AND agent_id = ?",
        (status, issue_id, agent_id),
    )
    conn.commit()


class SharedRateLimiter:
    """SQLite-backed rate limiter shared across all agents."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool
        self._lock = asyncio.Lock()

    async def wait_for_slot(self, resource: str = "github_api"):
        """Block until a rate limit slot is available."""
        while True:
            if await self._try_acquire(resource):
                return
            await asyncio.sleep(1.0)

    async def _try_acquire(self, resource: str) -> bool:
        """Try to acquire a rate limit slot. Returns True if successful."""
        async with self._lock:
            conn = self._pool.get()
            row = conn.execute(
                "SELECT * FROM rate_limit_state WHERE resource = ?",
                (resource,),
            ).fetchone()
            if not row:
                return True

            now = datetime.now(timezone.utc)
            window_start = datetime.fromisoformat(row["window_start"])
            # Ensure both datetimes are offset-aware (SQLite CURRENT_TIMESTAMP is naive)
            if window_start.tzinfo is None:
                window_start = window_start.replace(tzinfo=timezone.utc)

            # Determine window duration
            if resource == "github_search":
                window_seconds = 60
            else:
                window_seconds = 3600

            # Reset window if expired
            if (now - window_start).total_seconds() >= window_seconds:
                conn.execute(
                    """UPDATE rate_limit_state
                       SET requests_made = 1, window_start = ?
                       WHERE resource = ?""",
                    (now.isoformat(), resource),
                )
                conn.commit()
                return True

            # Check if under limit
            if row["requests_made"] < row["limit_per_window"]:
                conn.execute(
                    "UPDATE rate_limit_state SET requests_made = requests_made + 1 WHERE resource = ?",
                    (resource,),
                )
                conn.commit()
                return True

            return False


class LogWriter:
    """Concurrent-safe markdown log writer using file locking."""

    def __init__(self, log_path: Path):
        self._log_path = log_path

    def append_entry(self, entry: str):
        """Append a log entry after the header line using file locking."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        if not self._log_path.exists():
            self._log_path.write_text(
                "# Claude Agent - Do-Good GitHub Helper\n\n"
            )

        with open(self._log_path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                content = f.read()
                # Insert after first line (header)
                lines = content.split("\n", 1)
                if len(lines) == 2:
                    new_content = lines[0] + "\n\n" + entry + "\n" + lines[1]
                else:
                    new_content = content + "\n\n" + entry + "\n"
                f.seek(0)
                f.write(new_content)
                f.truncate()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def get_agent_work_dir(agent_id: str, repo_name: str) -> Path:
    """Get isolated work directory for an agent.

    Each agent clones to /tmp/dogood-workdir/agent-{id}/repo/
    """
    work_dir = WORK_DIR / f"agent-{agent_id}" / repo_name
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    return work_dir


def cleanup_agent_work_dir(agent_id: str):
    """Remove an agent's work directory after completion."""
    import shutil
    # Solver uses WORK_DIR / agent_id / repo_name
    agent_dir = WORK_DIR / agent_id
    if agent_dir.exists():
        shutil.rmtree(agent_dir, ignore_errors=True)
    # Also check with agent- prefix (legacy)
    alt_dir = WORK_DIR / f"agent-{agent_id}"
    if alt_dir.exists():
        shutil.rmtree(alt_dir, ignore_errors=True)
