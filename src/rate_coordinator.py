"""Shared Anthropic API rate limit coordinator.

All agents (factory helpers + bounty agent) use this to avoid thundering herd.
State is stored in a JSON file so separate processes can coordinate.

How it works:
- When ANY agent hits a rate limit, it calls `report_rate_limit()`
- This records the timestamp in a shared file
- Before retrying, agents call `get_retry_delay(slot)` which returns
  a staggered delay unique to their slot number
- The orchestrator calls `is_in_cooldown()` before spawning new agents
"""

import json
import os
import random
import time
from pathlib import Path

RATE_LIMIT_FILE = Path("/tmp/anthropic-rate-limit.json")
NOTIFY_COOLDOWN_FILE = Path("/tmp/anthropic-rate-limit-notified.json")

# Per-minute rate limit budget — be conservative
# Anthropic Tier 1 ≈ 50 RPM, but each agent turn is multiple requests.
# With 4 agents, budget ~12 RPM each with headroom.
COOLDOWN_SECONDS = 75  # wait at least 75s after a rate limit event
SLOT_SPACING_SECONDS = 15  # 15s between each agent's retry
MAX_SLOTS = 6  # factory(4) + bounty(1) + manual(1)
NOTIFY_THRESHOLD = 3  # send Telegram alert after this many hits in 10 min
NOTIFY_COOLDOWN_MINUTES = 30  # don't spam — max 1 alert per 30 min


def _read_state() -> dict:
    """Read the shared rate limit state."""
    try:
        if RATE_LIMIT_FILE.exists():
            return json.loads(RATE_LIMIT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {"last_hit": 0, "hit_count": 0, "reporters": []}


def _write_state(state: dict):
    """Write the shared rate limit state (atomic via temp file)."""
    tmp = RATE_LIMIT_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state))
        tmp.rename(RATE_LIMIT_FILE)
    except OSError:
        pass


def report_rate_limit(agent_id: str = "unknown"):
    """Called by any agent when it hits an Anthropic API rate limit.

    Records the timestamp so other agents can coordinate their retries.
    Sends a Telegram alert if hits exceed the threshold.
    """
    state = _read_state()
    now = time.time()

    # Only update if this is a new event (not duplicate reports within 10s)
    if now - state.get("last_hit", 0) > 10:
        state["last_hit"] = now
        state["hit_count"] = state.get("hit_count", 0) + 1
        # Track recent reporters (last 10)
        reporters = state.get("reporters", [])
        reporters.append({"agent": agent_id, "time": now})
        state["reporters"] = reporters[-10:]
        _write_state(state)

        # Check if we should alert Daniel
        _maybe_notify(state, agent_id)


def _maybe_notify(state: dict, agent_id: str):
    """Send Telegram alert if rate limits are getting excessive."""
    now = time.time()

    # Count hits in the last 10 minutes
    recent_reporters = [r for r in state.get("reporters", [])
                        if now - r["time"] < 600]
    if len(recent_reporters) < NOTIFY_THRESHOLD:
        return

    # Check notification cooldown — don't spam
    try:
        if NOTIFY_COOLDOWN_FILE.exists():
            last_notified = json.loads(NOTIFY_COOLDOWN_FILE.read_text()).get("time", 0)
            if now - last_notified < NOTIFY_COOLDOWN_MINUTES * 60:
                return
    except (json.JSONDecodeError, OSError):
        pass

    # Send the alert
    try:
        import subprocess
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            return

        agents_hit = set(r["agent"] for r in recent_reporters)
        msg = (
            f"⚠️ Rate Limit Alert\n"
            f"{len(recent_reporters)} hits in last 10 min\n"
            f"Agents affected: {len(agents_hit)}\n"
            f"Latest: {agent_id}\n"
            f"Total hits: {state.get('hit_count', 0)}\n"
            f"Agents are coordinating retries."
        )
        subprocess.run(
            ["curl", "-s", f"https://api.telegram.org/bot{bot_token}/sendMessage",
             "-d", f"chat_id={chat_id}",
             "-d", f"text={msg}"],
            capture_output=True, text=True, timeout=10
        )
        # Record notification time
        NOTIFY_COOLDOWN_FILE.write_text(json.dumps({"time": now}))
    except Exception:
        pass


def is_in_cooldown() -> bool:
    """Check if we're in a rate limit cooldown period.

    Used by the orchestrator to avoid spawning new agents during cooldown.
    """
    state = _read_state()
    if not state.get("last_hit"):
        return False
    elapsed = time.time() - state["last_hit"]
    return elapsed < COOLDOWN_SECONDS


def seconds_until_clear() -> float:
    """How many seconds until cooldown ends. Returns 0 if clear."""
    state = _read_state()
    if not state.get("last_hit"):
        return 0.0
    remaining = COOLDOWN_SECONDS - (time.time() - state["last_hit"])
    return max(0.0, remaining)


def get_retry_delay(slot: int = 0, attempt: int = 0) -> float:
    """Get the staggered retry delay for a specific agent slot.

    Each agent gets a unique slot (0-5). Retries are spread out so
    agents don't all hit the API at the same time.

    Args:
        slot: Agent's unique slot number (0-based)
        attempt: Retry attempt number (0-based), for escalating backoff

    Returns:
        Seconds to wait before retrying
    """
    state = _read_state()
    now = time.time()

    # Base: wait until cooldown is over
    elapsed = now - state.get("last_hit", 0)
    base_wait = max(0, COOLDOWN_SECONDS - elapsed)

    # Stagger: each slot retries at a different offset
    slot_offset = (slot % MAX_SLOTS) * SLOT_SPACING_SECONDS

    # Escalate: on repeated failures, add more time (but linear, not exponential)
    # attempt 0 = 0s extra, attempt 1 = 60s, attempt 2 = 120s, etc.
    escalation = attempt * 60

    # Jitter: ±5s random to break any remaining synchronization
    jitter = random.uniform(-5, 5)

    total = base_wait + slot_offset + escalation + jitter
    return max(10.0, min(total, 600.0))  # clamp 10s - 10min


def get_slot_for_agent(agent_id: str) -> int:
    """Derive a consistent slot number from agent ID.

    Uses hash so the same agent always gets the same slot.
    """
    return hash(agent_id) % MAX_SLOTS


CONCURRENCY_REDUCTION_FILE = Path("/tmp/dogood-reduce-concurrency.json")


def request_concurrency_reduction():
    """Signal the factory to reduce concurrency.

    Called when an agent is already at the max model tier (opus-thinking)
    and still hitting rate limits. The factory reads this on each spawn cycle.
    """
    now = time.time()
    try:
        state = {}
        if CONCURRENCY_REDUCTION_FILE.exists():
            state = json.loads(CONCURRENCY_REDUCTION_FILE.read_text())
        state["requested_at"] = now
        state["reduction_count"] = state.get("reduction_count", 0) + 1
        tmp = CONCURRENCY_REDUCTION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.rename(CONCURRENCY_REDUCTION_FILE)
    except OSError:
        pass


def check_concurrency_reduction() -> int | None:
    """Check if agents have requested a concurrency reduction.

    Returns the number of reductions requested (since last clear), or None
    if no reduction is needed. The factory should reduce max_concurrent by 1
    for each reduction, down to a minimum of 1.
    """
    try:
        if not CONCURRENCY_REDUCTION_FILE.exists():
            return None
        state = json.loads(CONCURRENCY_REDUCTION_FILE.read_text())
        requested_at = state.get("requested_at", 0)
        # Only honor recent requests (within last 5 minutes)
        if time.time() - requested_at > 300:
            CONCURRENCY_REDUCTION_FILE.unlink(missing_ok=True)
            return None
        count = state.get("reduction_count", 0)
        if count > 0:
            # Clear after reading so we don't keep reducing
            CONCURRENCY_REDUCTION_FILE.unlink(missing_ok=True)
            return count
    except (json.JSONDecodeError, OSError):
        pass
    return None
