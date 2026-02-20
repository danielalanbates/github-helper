"""Agent factory: spawns concurrent agents as subprocesses, manages lifecycle."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    MAX_CONCURRENT_AGENTS, AGENT_CLAIM_TTL_MINUTES,
    MIN_STARS_DEFAULT, LOG_FILE, MAX_OPUS_PER_ISSUE, PROJECT_ROOT,
)
from src.concurrency import (
    ConnectionPool, SharedRateLimiter, LogWriter,
    claim_issue, release_claim, cleanup_agent_work_dir,
)
from src.db import (
    get_next_unclaimed_issue, record_agent_run, update_agent_run,
    get_opus_attempts_for_issue,
)
from src.model_selector import score_complexity, select_tier, get_next_tier
from src.utils import generate_agent_id, now_iso


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
        self._stats = {"started": 0, "succeeded": 0, "failed": 0, "escalated": 0}
        self._total_cost = 0.0

    async def run(self, max_issues: int = 100):
        """Main factory loop: get issues, spawn agent subprocesses, track results."""
        budget_msg = f", budget=${self.max_cost_usd:.2f}" if self.max_cost_usd else ""
        print(f"Agent Factory starting: max_concurrent={self.max_concurrent}, "
              f"min_stars={self.min_stars}, max_issues={max_issues}{budget_msg}", flush=True)

        tasks = []
        issues_started = 0

        while issues_started < max_issues:
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

            # Get next issue
            conn = self.pool.get()
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

            # Determine model tier (per-issue opus budget tracking)
            repo_dict = {"language": issue.get("language"), "stars": issue.get("stars"),
                         "open_issues": 0}
            complexity = score_complexity(issue, repo_dict)
            model_tier = select_tier(complexity, issue["id"], conn)

            # Bounty issues always get opus (bypass per-issue limit)
            if issue.get("is_bounty"):
                from src.model_selector import get_tier_by_number
                model_tier = get_tier_by_number(3)  # tier 3 = opus
                print(f"  Agent {agent_id}: BOUNTY detected — forcing opus")

            # Check opus budget for non-bounty issues
            elif "opus" in model_tier["model"]:
                opus_attempts = get_opus_attempts_for_issue(conn, issue["id"])
                if opus_attempts >= MAX_OPUS_PER_ISSUE:
                    print(f"  Agent {agent_id}: opus budget exhausted for issue #{issue['number']}, "
                          f"downgrading to sonnet")
                    model_tier = select_tier(0.6, issue["id"], conn)

            print(f"  Agent {agent_id}: {issue['full_name']}#{issue['number']} "
                  f"[{model_tier['label']}] — {issue.get('title', '')[:50]}")

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

            # Stagger agent spawns — shorter for cheaper models
            stagger = 30 if "haiku" in model_tier["model"] else 60
            await asyncio.sleep(stagger)

            # Spawn agent as subprocess
            task = asyncio.create_task(
                self._run_agent_subprocess(agent_id, issue, model_tier)
            )
            tasks.append(task)

        # Wait for all tasks to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        print(f"\nFactory complete: {self._stats}")
        return self._stats

    async def _run_agent_subprocess(self, agent_id: str, issue: dict,
                                     model_tier: dict):
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

            # Run the subprocess
            import os
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)  # Allow nested Claude Code sessions

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

            if proc.returncode == 0:
                # Check if a PR was created by looking at stdout
                pr_url = ""
                for line in stdout_text.split("\n"):
                    if "https://github.com/" in line and "/pull/" in line:
                        # Extract PR URL
                        import re
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
            else:
                error_msg = stderr_text[-300:] if stderr_text else f"exit code {proc.returncode}"
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
            await release_claim(self.pool, issue["id"], agent_id, "completed")
            cleanup_agent_work_dir(agent_id)
            self._semaphore.release()
