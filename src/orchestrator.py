"""Agent factory: spawns concurrent agents as subprocesses, manages lifecycle.

Rate limit strategy:
- Start all agents at sonnet-low (tier 1)
- When ANY agent hits a rate limit, orchestrator bumps 1 agent to the next tier
- This spreads agents across model rate limit pools for maximum throughput
- Track observed rate limits per model to learn caps over time
- At max tier (opus-thinking) + still limited → reduce concurrency
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    MAX_CONCURRENT_AGENTS, AGENT_CLAIM_TTL_MINUTES,
    MIN_STARS_DEFAULT, LOG_FILE, MAX_OPUS_PER_ISSUE, PROJECT_ROOT,
    load_model_tiers,
)
from src.concurrency import (
    ConnectionPool, SharedRateLimiter, LogWriter,
    claim_issue, release_claim, cleanup_agent_work_dir,
)
from src.rate_coordinator import is_in_cooldown, seconds_until_clear
from src.db import (
    get_next_unclaimed_issue, get_next_tagged_issue,
    record_agent_run, update_agent_run,
    get_opus_attempts_for_issue,
    get_next_feedback_revision, update_feedback_status,
)
from src.model_selector import score_complexity, get_next_tier
from src.utils import generate_agent_id, now_iso


RATE_LIMIT_SIGNAL_FILE = Path("/tmp/dogood-rate-limit-signal.json")
RATE_LEARNING_FILE = Path("/tmp/dogood-rate-learning.json")
FACTORY_STATUS_FILE = Path("/tmp/dogood-factory-status.json")


class TierDistributor:
    """Manages the distribution of agents across model tiers.

    Strategy: start everyone at tier 1. When rate limits hit a model,
    promote 1 agent to the next tier to use a different rate limit pool.
    Track what we learn about each model's rate limits.
    """

    def __init__(self):
        self._tier_floor = 1  # minimum tier to assign
        self._active_tiers: dict[str, int] = {}  # agent_id -> tier number
        self._rate_limit_events: list[dict] = []  # recent events for learning
        self._models_saturated: set[str] = set()  # models currently at limit
        self._load_learning()

    def assign_tier(self, agent_id: str, complexity: float) -> dict:
        """Assign a model tier using a fixed distribution pattern.

        Default: 4 agents at sonnet-low (floor), 1 agent at sonnet-high.
        When rate limits raise the floor, the distribution shifts up but
        keeps the same 4:1 ratio (4 at floor, 1 at floor+2 or max).
        """
        # Count how many active agents are at the "high" slot (floor + 2)
        high_tier_num = min(self._tier_floor + 2, len(load_model_tiers()))
        high_count = sum(1 for t in self._active_tiers.values()
                         if t >= high_tier_num)

        # 1 out of every 5 agents gets the high slot
        if high_count < 1:
            target_tier = high_tier_num
        else:
            target_tier = self._tier_floor

        # If the target model is saturated, bump up
        tier_dict = self._get_tier(target_tier)
        while tier_dict and tier_dict["model"] in self._models_saturated:
            next_dict = get_next_tier(tier_dict)
            if next_dict:
                tier_dict = next_dict
                target_tier = tier_dict["tier"]
            else:
                break  # at max, nothing to do

        tier_dict = self._get_tier(target_tier)
        if not tier_dict:
            tier_dict = load_model_tiers()[0].copy()

        self._active_tiers[agent_id] = tier_dict["tier"]
        return tier_dict

    def report_rate_limit(self, model: str):
        """Called when any agent hits a rate limit on a specific model.

        Bumps the tier floor so the NEXT agent uses a different pool.
        """
        now = time.time()
        self._rate_limit_events.append({
            "model": model, "time": now
        })
        # Keep last 50 events
        self._rate_limit_events = self._rate_limit_events[-50:]

        # Mark this model as saturated (clear after 2 minutes)
        self._models_saturated.add(model)

        # Count recent hits on this model (last 5 min)
        recent = [e for e in self._rate_limit_events
                  if e["model"] == model and now - e["time"] < 300]

        # If 2+ hits on this model in 5 min, bump the floor past it
        if len(recent) >= 2:
            for tier in load_model_tiers():
                if tier["model"] == model:
                    if tier["tier"] >= self._tier_floor:
                        new_floor = tier["tier"] + 1
                        if new_floor <= len(load_model_tiers()):
                            self._tier_floor = new_floor
                            print(f"  [TIER SHIFT] {model} saturated — "
                                  f"floor raised to tier {self._tier_floor} "
                                  f"({self._get_tier(self._tier_floor)['label']})",
                                  flush=True)

        self._record_learning(model, now)

    def clear_saturation(self, model: str):
        """Clear saturation flag after successful use of a model."""
        self._models_saturated.discard(model)
        # Also consider lowering the floor if the model is clear
        now = time.time()
        recent = [e for e in self._rate_limit_events
                  if e["model"] == model and now - e["time"] < 300]
        if not recent and self._tier_floor > 1:
            # No recent hits — try stepping the floor back down
            self._tier_floor = max(1, self._tier_floor - 1)
            print(f"  [TIER SHIFT] {model} clear — "
                  f"floor lowered to tier {self._tier_floor}",
                  flush=True)

    def is_at_max_tier(self) -> bool:
        """Check if the floor is already at the highest tier."""
        return self._tier_floor >= len(load_model_tiers())

    def release_agent(self, agent_id: str):
        """Remove an agent from tracking."""
        self._active_tiers.pop(agent_id, None)

    def get_distribution_summary(self) -> str:
        """Summary of current tier distribution for logging."""
        counts: dict[int, int] = {}
        for tier_num in self._active_tiers.values():
            counts[tier_num] = counts.get(tier_num, 0) + 1
        parts = []
        for tier in load_model_tiers():
            n = counts.get(tier["tier"], 0)
            if n > 0:
                parts.append(f"{tier['label']}={n}")
        return ", ".join(parts) if parts else "none active"

    def _get_tier(self, tier_num: int) -> dict | None:
        for t in load_model_tiers():
            if t["tier"] == tier_num:
                return t.copy()
        return None

    def _record_learning(self, model: str, timestamp: float):
        """Write rate limit observations to a learning file."""
        try:
            data = {}
            if RATE_LEARNING_FILE.exists():
                data = json.loads(RATE_LEARNING_FILE.read_text())

            if model not in data:
                data[model] = {"hits": [], "estimated_rpm": None}

            hits = data[model]["hits"]
            hits.append(timestamp)
            # Keep last 100 hits per model
            data[model]["hits"] = hits[-100:]

            # Estimate RPM cap: look at hits in the last 10 minutes
            recent = [h for h in hits if timestamp - h < 600]
            if len(recent) >= 3:
                # Time span between first and last hit
                span_minutes = (recent[-1] - recent[0]) / 60
                if span_minutes > 0:
                    estimated_rpm = len(recent) / span_minutes
                    data[model]["estimated_rpm"] = round(estimated_rpm, 1)
                    data[model]["last_updated"] = datetime.now().isoformat()

            tmp = RATE_LEARNING_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(RATE_LEARNING_FILE)
        except Exception:
            pass

    def _load_learning(self):
        """Load previous rate limit learning data."""
        try:
            if RATE_LEARNING_FILE.exists():
                data = json.loads(RATE_LEARNING_FILE.read_text())
                for model, info in data.items():
                    rpm = info.get("estimated_rpm")
                    if rpm:
                        print(f"  [LEARNED] {model}: ~{rpm} RPM at limit",
                              flush=True)
        except Exception:
            pass


class AgentFactory:
    """Spawns and manages concurrent issue-solving agents as subprocesses."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_AGENTS,
                 min_stars: int = MIN_STARS_DEFAULT,
                 max_cost_usd: float = 0.0):
        self.max_concurrent = max_concurrent
        self.min_stars = min_stars
        self.max_cost_usd = max_cost_usd  # 0 = unlimited
        self.pool = ConnectionPool()
        self.rate_limiter = SharedRateLimiter(self.pool)
        self.log_writer = LogWriter(LOG_FILE)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._stats = {"started": 0, "succeeded": 0, "failed": 0, "skipped": 0, "escalated": 0}
        self._total_cost = 0.0
        self._christian_agent_active = False
        self._tier_distributor = TierDistributor()
        self._active_agents = {}  # agent_id -> {repo, issue, started}

    def _write_status(self, factory_running: bool = True):
        """Write factory status to a file for the status bar app to read."""
        try:
            status = {
                "active_agents": len(self._active_agents),
                "agents": {aid: info for aid, info in self._active_agents.items()},
                "stats": self._stats.copy(),
                "max_concurrent": self.max_concurrent,
                "factory_running": factory_running,
                "updated": datetime.now().isoformat(),
            }
            FACTORY_STATUS_FILE.write_text(json.dumps(status))
        except Exception:
            pass

    async def run(self, max_issues: int = 100):
        """Main factory loop: get issues, spawn agent subprocesses, track results."""
        budget_msg = f", budget=${self.max_cost_usd:.2f}" if self.max_cost_usd else ""
        print(f"Agent Factory starting: max_concurrent={self.max_concurrent}, "
              f"min_stars={self.min_stars}, max_issues={max_issues}{budget_msg}", flush=True)

        tasks = []
        issues_started = 0

        bounty_signal = Path("/tmp/bounty-agent-active.signal")

        while issues_started < max_issues:
            # Auto-throttle when interactive Claude Code is running in terminal
            try:
                import subprocess as _sp
                # Find claude processes, exclude our SDK agents (ENTRYPOINT=sdk-py)
                cc_check = _sp.run(
                    ["bash", "-c",
                     "ps -eo pid,command | grep '[c]laude ' | grep -v sdk-py | grep -v ShipIt | grep -c ."],
                    capture_output=True, text=True, timeout=5
                )
                interactive_count = int(cc_check.stdout.strip()) if cc_check.returncode == 0 else 0
                if interactive_count > 0 and self._semaphore._value > 1:
                    self._semaphore = asyncio.Semaphore(1)
                    print("  [THROTTLE] Claude Code active in terminal — limiting to 1 agent",
                          flush=True)
                elif interactive_count == 0 and self._semaphore._value < self.max_concurrent:
                    self._semaphore = asyncio.Semaphore(self.max_concurrent)
                    print(f"  [RESTORE] Claude Code exited — restoring to {self.max_concurrent} agents",
                          flush=True)
            except Exception:
                pass

            # Check if bounty agent needs priority — pause factory if bounties active
            if bounty_signal.exists():
                try:
                    signal_data = json.loads(bounty_signal.read_text())
                    bounty_count = signal_data.get("count", 0)
                    if bounty_count > 0:
                        print(f"  [PAUSED] Bounty Agent active ({bounty_count} bounties) — "
                              f"yielding rate limits", flush=True)
                        await asyncio.sleep(60)
                        continue
                except Exception:
                    pass

            # Check for rate limit signals from agents
            self._check_rate_limit_signals()

            # If all tiers are saturated and we're at max, reduce concurrency
            if self._tier_distributor.is_at_max_tier() and self.max_concurrent > 1:
                # Check if there was a very recent signal
                try:
                    if RATE_LIMIT_SIGNAL_FILE.exists():
                        sig = json.loads(RATE_LIMIT_SIGNAL_FILE.read_text())
                        if time.time() - sig.get("time", 0) < 120:
                            old = self.max_concurrent
                            self.max_concurrent = max(1, self.max_concurrent - 1)
                            self._semaphore = asyncio.Semaphore(self.max_concurrent)
                            print(f"  [THROTTLE] All tiers saturated — "
                                  f"reducing concurrency {old} → {self.max_concurrent}",
                                  flush=True)
                            RATE_LIMIT_SIGNAL_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

            # Check shared rate limit cooldown — don't spawn during API saturation
            if is_in_cooldown():
                wait_secs = seconds_until_clear()
                print(f"  [COOLDOWN] Anthropic API rate limited — waiting {wait_secs:.0f}s before next spawn",
                      flush=True)
                await asyncio.sleep(wait_secs + 5)
                continue

            # Check budget
            if self.max_cost_usd and self._total_cost >= self.max_cost_usd * 0.8:
                if self._total_cost >= self.max_cost_usd:
                    print(f"  Budget exhausted: ${self._total_cost:.2f} / ${self.max_cost_usd:.2f}",
                          flush=True)
                    break
                print(f"  WARNING: At 80% budget: ${self._total_cost:.2f} / ${self.max_cost_usd:.2f}",
                      flush=True)

            # Wait for a semaphore slot
            await self._semaphore.acquire()

            # PRIORITY #1: Check for feedback that needs revision
            conn = self.pool.get()
            feedback_item = get_next_feedback_revision(conn)
            if feedback_item:
                agent_id = generate_agent_id()
                # Use mandatory_model from DB if set, otherwise opus-high
                tiers = load_model_tiers()
                mandatory = feedback_item.get("mandatory_model", "opus-high")
                model_tier = next((t for t in tiers if t.get("label") == mandatory),
                                  tiers[-1]).copy()
                print(f"  [FEEDBACK] Priority revision: contribution #{feedback_item['id']} "
                      f"— {feedback_item['full_name']} (PR: {feedback_item.get('pr_url', '?')})",
                      flush=True)

                update_feedback_status(conn, feedback_item["id"], "in_progress")
                record_agent_run(conn, {
                    "id": agent_id,
                    "issue_id": feedback_item.get("issue_id"),
                    "repo_id": feedback_item.get("repo_id"),
                    "model": model_tier["model"],
                    "effort": model_tier["effort"],
                    "status": "fixing",
                })

                self._stats["started"] += 1
                issues_started += 1
                self._active_agents[agent_id] = {
                    "repo": feedback_item.get("full_name", "?"),
                    "type": "feedback",
                    "started": now_iso(),
                }
                self._write_status()

                task = asyncio.create_task(
                    self._run_feedback_subprocess(agent_id, feedback_item, model_tier)
                )
                tasks.append(task)
                await asyncio.sleep(10)
                continue

            # Get next issue — reserve 1 slot for Christian repos
            issue = None
            if not self._christian_agent_active:
                issue = get_next_tagged_issue(conn, "christian", min_stars=0)
                if issue:
                    issue["_tagged"] = "christian"
                    print(f"  [CHRISTIAN] Prioritizing Christian repo: "
                          f"{issue['full_name']}#{issue['number']}", flush=True)
            if not issue:
                issue = get_next_unclaimed_issue(conn, self.min_stars)
            if not issue:
                self._semaphore.release()
                if tasks:
                    print("No more issues. Waiting for active agents to finish...")
                    break
                else:
                    print("No eligible issues found.")
                    return self._stats

            agent_id = generate_agent_id()

            # Claim the issue before spawning
            claimed = await claim_issue(self.pool, issue["id"], agent_id,
                                        AGENT_CLAIM_TTL_MINUTES)
            if not claimed:
                self._semaphore.release()
                continue

            # Daniel Tier System: all regular agents run at tier 1 (sonnet-low)
            tiers = load_model_tiers()
            model_tier = tiers[0].copy()

            # Bounty issues: use top tier
            if issue.get("is_bounty"):
                model_tier = tiers[-1].copy()
                print(f"  Agent {agent_id}: BOUNTY detected — using {model_tier['label']}")

            print(f"  Agent {agent_id}: {issue['full_name']}#{issue['number']} "
                  f"[{model_tier['label']}] — {issue.get('title', '')[:50]}",
                  flush=True)

            # Record agent run
            record_agent_run(conn, {
                "id": agent_id,
                "issue_id": issue["id"],
                "repo_id": issue.get("repo_id") or issue.get("rid"),
                "model": model_tier["model"],
                "effort": model_tier["effort"],
                "status": "starting",
            })

            self._stats["started"] += 1
            issues_started += 1
            self._active_agents[agent_id] = {
                "repo": issue.get("full_name", "?"),
                "issue": f"#{issue.get('number', '?')}",
                "type": "fix",
                "started": now_iso(),
            }
            self._write_status()

            # Stagger agent spawns — 60s between each
            await asyncio.sleep(60)

            # Track Christian agent slot
            is_christian = issue.get("_tagged") == "christian"
            if is_christian:
                self._christian_agent_active = True

            # Spawn agent as subprocess
            task = asyncio.create_task(
                self._run_agent_subprocess(agent_id, issue, model_tier,
                                           is_christian=is_christian)
            )
            tasks.append(task)

        # Wait for all tasks to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        print(f"\nFactory complete: {self._stats}")
        return self._stats

    def _check_rate_limit_signals(self):
        """Read rate limit signals from agents and update tier distribution."""
        try:
            if not RATE_LIMIT_SIGNAL_FILE.exists():
                return
            sig = json.loads(RATE_LIMIT_SIGNAL_FILE.read_text())
            model = sig.get("model")
            sig_time = sig.get("time", 0)
            # Only process recent signals (within last 2 min)
            if model and time.time() - sig_time < 120:
                self._tier_distributor.report_rate_limit(model)
                RATE_LIMIT_SIGNAL_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def _run_agent_subprocess(self, agent_id: str, issue: dict,
                                     model_tier: dict, is_christian: bool = False):
        """Run a single agent as a subprocess that calls `dogood solve`."""
        conn = self.pool.get()
        full_name = issue["full_name"]
        issue_number = issue["number"]
        python = sys.executable

        try:
            update_agent_run(conn, agent_id, status="fixing")

            # Wait for rate limit
            await self.rate_limiter.wait_for_slot("github_api")

            # Build the solve command — run as a separate process
            cmd = [
                python, "-m", "src.cli", "solve",
                "--issue-id", str(issue["id"]),
                "--agent-id", agent_id,
                "--model-tier", json.dumps(model_tier),
            ]
            if issue.get("is_bounty"):
                cmd.append("--is-bounty")

            env = os.environ.copy()
            # Strip all Claude Code env vars to avoid "nested session" detection
            for key in list(env):
                if "CLAUDE" in key.upper():
                    env.pop(key)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env,
            )

            stdout, stderr = await proc.communicate()
            stdout_text = stdout.decode() if stdout else ""
            stderr_text = stderr.decode() if stderr else ""

            if proc.returncode == 2:
                # Exit code 2 = skipped (unsupported language, duplicate PR, CLA, etc.)
                skip_reason = stdout_text.strip().split("\n")[-1] if stdout_text.strip() else "skipped"
                update_agent_run(conn, agent_id, status="failed",
                                 error=f"skipped: {skip_reason[:200]}",
                                 finished_at=now_iso())
                self._stats["skipped"] = self._stats.get("skipped", 0) + 1
                print(f"  Agent {agent_id}: skipped — {skip_reason[:80]}")
                # No cost incurred for skips, clear saturation
                self._tier_distributor.clear_saturation(model_tier["model"])
            elif proc.returncode == 0:
                # Check if a PR was created by looking at stdout
                pr_url = ""
                for line in stdout_text.split("\n"):
                    if "https://github.com/" in line and "/pull/" in line:
                        match = re.search(r'(https://github\.com/\S+/pull/\d+)', line)
                        if match:
                            pr_url = match.group(1)
                            break

                if pr_url:
                    update_agent_run(conn, agent_id,
                                     status="pr_created",
                                     pr_url=pr_url,
                                     finished_at=now_iso(),
                                     cost_usd=model_tier.get("max_budget_usd", 0))
                    self._stats["succeeded"] += 1
                    print(f"  Agent {agent_id}: PR created — {pr_url}")

                    # Model worked without rate limit — clear saturation
                    self._tier_distributor.clear_saturation(model_tier["model"])

                    self.log_writer.append_entry(
                        f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — PR SUBMITTED\n"
                        f"**Repo:** {full_name}\n"
                        f"**Issue/PR:** #{issue_number} — {issue.get('title', '')}\n"
                        f"**Model:** {model_tier['label']}\n"
                        f"**PR:** {pr_url}\n"
                        f"**Agent:** {agent_id}\n"
                        f"**Action needed:** No\n"
                        f"---"
                    )
                else:
                    # Process succeeded but no PR URL found — check if it was a no-op
                    error_msg = "No changes made" if "No changes" in stdout_text else stdout_text[-200:]
                    update_agent_run(conn, agent_id, status="failed",
                                     error=error_msg, finished_at=now_iso())
                    self._stats["failed"] += 1
                    print(f"  Agent {agent_id}: no PR — {error_msg[:80]}")

                    # Still succeeded at the API level — clear saturation
                    self._tier_distributor.clear_saturation(model_tier["model"])
            else:
                error_msg = stderr_text[-300:] if stderr_text else f"exit code {proc.returncode}"

                # Check if the failure was a real rate limit (not benign SDK event)
                combined = f"{stdout_text} {stderr_text}".lower()
                is_rate_limited = (
                    "rate_limit" in combined or "rate limit" in combined
                    or "you've hit your limit" in combined
                    or "hit your limit" in combined
                )
                is_benign = "benign sdk" in combined or "unknown message type" in combined
                if is_rate_limited and not is_benign:
                    self._stats["escalated"] += 1
                    reset_match = re.search(r'resets\s+(\d{1,2}(?:am|pm))', combined)
                    reset_info = reset_match.group(1) if reset_match else ""
                    update_agent_run(conn, agent_id, status="failed",
                                     error=f"Rate limited{' — resets ' + reset_info if reset_info else ''}",
                                     finished_at=now_iso())
                    self._stats["failed"] += 1
                    print(f"  Agent {agent_id}: rate limited on {model_tier['label']}"
                          f"{' — resets ' + reset_info if reset_info else ''}")
                else:
                    update_agent_run(conn, agent_id, status="failed",
                                     error=error_msg, finished_at=now_iso())
                    self._stats["failed"] += 1
                    print(f"  Agent {agent_id}: failed — {error_msg[:80]}")

        except Exception as e:
            update_agent_run(conn, agent_id, status="failed",
                             error=str(e)[:500], finished_at=now_iso())
            self._stats["failed"] += 1
            print(f"  Agent {agent_id}: error — {e}")

        finally:
            if is_christian:
                self._christian_agent_active = False
            self._tier_distributor.release_agent(agent_id)
            await release_claim(self.pool, issue["id"], agent_id, "completed")
            cleanup_agent_work_dir(agent_id)
            self._active_agents.pop(agent_id, None)
            self._write_status()
            self._semaphore.release()

    async def _run_feedback_subprocess(self, agent_id: str, contribution: dict,
                                        model_tier: dict):
        """Run a feedback-revision agent as a subprocess calling `dogood solve-feedback`."""
        conn = self.pool.get()
        python = sys.executable
        contribution_id = contribution["id"]

        try:
            cmd = [
                python, "-m", "src.cli", "solve-feedback",
                "--contribution-id", str(contribution_id),
                "--agent-id", agent_id,
                "--model-tier", json.dumps(model_tier),
            ]

            env = os.environ.copy()
            for key in list(env):
                if "CLAUDE" in key.upper():
                    env.pop(key)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env,
            )

            stdout, stderr = await proc.communicate()
            stdout_text = stdout.decode() if stdout else ""
            stderr_text = stderr.decode() if stderr else ""
            combined = (stdout_text + "\n" + stderr_text).strip()

            if proc.returncode == 0:
                update_agent_run(conn, agent_id, status="pr_created",
                                 pr_url=contribution.get("pr_url", ""),
                                 finished_at=now_iso())
                self._stats["succeeded"] += 1
                print(f"  Agent {agent_id}: feedback addressed — {contribution.get('pr_url', '')}")

                self.log_writer.append_entry(
                    f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — FEEDBACK ADDRESSED\n"
                    f"**Repo:** {contribution['full_name']}\n"
                    f"**PR:** {contribution.get('pr_url', '')}\n"
                    f"**Reviewer:** {contribution.get('feedback_reviewer', '?')}\n"
                    f"**Model:** {model_tier['label']}\n"
                    f"**Agent:** {agent_id}\n"
                    f"**Action needed:** No\n"
                    f"---"
                )
            else:
                error_msg = combined[-500:] if combined else f"exit code {proc.returncode}"
                update_agent_run(conn, agent_id, status="failed",
                                 error=error_msg[:500], finished_at=now_iso())
                self._stats["failed"] += 1
                print(f"  Agent {agent_id}: feedback fix failed — {error_msg[:120]}")
                # Track retry count — skip after 3 failures to avoid infinite loops
                retry_key = f"feedback_retries_{contribution_id}"
                self._stats[retry_key] = self._stats.get(retry_key, 0) + 1
                if self._stats[retry_key] >= 3:
                    update_feedback_status(conn, contribution_id, "skipped")
                    print(f"  Skipping contribution #{contribution_id} after {self._stats[retry_key]} failed attempts")
                else:
                    update_feedback_status(conn, contribution_id, "needs_revision")

        except Exception as e:
            update_agent_run(conn, agent_id, status="failed",
                             error=str(e)[:500], finished_at=now_iso())
            self._stats["failed"] += 1
            print(f"  Agent {agent_id}: feedback error — {e}")
            # Reset to needs_revision so it gets retried
            update_feedback_status(conn, contribution_id, "needs_revision")

        finally:
            self._tier_distributor.release_agent(agent_id)
            cleanup_agent_work_dir(agent_id)
            self._active_agents.pop(agent_id, None)
            self._write_status()
            self._semaphore.release()
