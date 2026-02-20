"""Feedback loop: polls GitHub notifications, analyzes sentiment, takes action."""

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    GITHUB_USERNAME, FEEDBACK_POLL_INTERVAL_SECONDS,
    HOSTILE_SENTIMENT_THRESHOLD, LOG_FILE, PROJECT_ROOT,
    MAX_OPUS_PER_ISSUE, ANTI_AI_KEYWORDS as ANTI_AI_KEYWORDS_CONFIG,
)
from src.concurrency import ConnectionPool, LogWriter
from src.db import (
    add_to_blacklist, remove_from_blacklist, is_blacklisted,
    add_learned_pattern, add_sponsor, get_opus_usage_for_repo,
)
from src.utils import now_iso
from src.telegram import notify_github_attention


# Polite exit template
POLITE_EXIT = (
    "Thank you for the feedback! I appreciate you taking the time to review. "
    "I'll withdraw this PR. Wishing this project continued success!"
)

# Compassion re-engagement template
COMPASSION_REENGAGEMENT = (
    "No worries at all! Happy to help. Let me take a look at this."
)

# Sentiment keywords for quick classification before AI analysis
HOSTILE_KEYWORDS = {
    "spam", "bot", "garbage", "terrible", "awful", "worst", "useless",
    "stop", "go away", "not welcome", "ban", "block", "unwanted",
    "waste of time", "low quality", "low-quality", "junk",
}
ANTI_AI_KEYWORDS = {
    "no ai", "no llm", "ai-generated", "ban ai", "no bots",
    "ai contributions not accepted", "ai-free", "no machine",
}
POSITIVE_KEYWORDS = {
    "lgtm", "looks good", "great", "nice", "thank", "awesome",
    "well done", "excellent", "approved", "perfect", "wonderful",
    "impressive", "helpful", "appreciate",
}
SPONSOR_KEYWORDS = {
    "sponsor", "sponsoring", "donate", "donation", "fund", "funding",
    "support you", "buy you a coffee", "tip", "patreon", "ko-fi",
}
REGRET_KEYWORDS = {
    "sorry", "apologize", "apologies", "my bad", "overreacted",
    "reconsidered", "changed my mind", "give it another try",
    "come back", "welcome back",
}
# Keywords that mean Daniel needs to be notified via Telegram
CONTACT_KEYWORDS = {
    "email", "contact", "reach out", "get in touch", "message me",
    "dm me", "direct message", "how can i reach", "talk to you",
}
PAYMENT_KEYWORDS = {
    "payment", "pay you", "paypal", "venmo", "bank", "invoice",
    "compensation", "reward", "bounty payout", "send money",
    "wire transfer", "crypto", "wallet address",
}
JOB_KEYWORDS = {
    "hire", "hiring", "job", "position", "work for us", "join us",
    "contract", "freelance", "consulting", "interested in working",
    "opportunity", "role", "full-time", "part-time",
}


class FeedbackLoop:
    """Monitors GitHub notifications and processes review feedback."""

    def __init__(self, pool: ConnectionPool = None):
        self.pool = pool or ConnectionPool()
        self.log_writer = LogWriter(LOG_FILE)
        self.username = GITHUB_USERNAME

    async def run_once(self) -> dict:
        """Run one feedback cycle. Returns stats dict."""
        stats = {"processed": 0, "positive": 0, "constructive": 0,
                 "hostile": 0, "anti_ai": 0, "sponsor": 0, "regretful": 0,
                 "payment_request": 0, "job_inquiry": 0, "contact_request": 0}

        notifications = self._fetch_notifications()
        if not notifications:
            return stats

        for notif in notifications:
            try:
                result = await self._process_notification(notif)
                if result:
                    stats["processed"] += 1
                    stats[result] = stats.get(result, 0) + 1
            except Exception as e:
                print(f"  Error processing notification: {e}")

        return stats

    async def run_continuous(self):
        """Run feedback loop continuously as a background task."""
        while True:
            try:
                stats = await self.run_once()
                if stats["processed"] > 0:
                    print(f"  Feedback cycle: {stats}")
            except Exception as e:
                print(f"  Feedback loop error: {e}")
            await asyncio.sleep(FEEDBACK_POLL_INTERVAL_SECONDS)

    def _fetch_notifications(self) -> list:
        """Fetch GitHub notifications for our PRs."""
        try:
            result = subprocess.run(
                ["gh", "api", "notifications",
                 "--jq", '[.[] | select(.subject.type == "PullRequest")]'],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
        except Exception as e:
            print(f"  Failed to fetch notifications: {e}")
        return []

    async def _process_notification(self, notif: dict) -> str | None:
        """Process a single notification. Returns sentiment category or None."""
        subject = notif.get("subject", {})
        pr_url = subject.get("url", "")
        repo_full_name = notif.get("repository", {}).get("full_name", "")

        if not pr_url:
            return None

        # Fetch PR reviews and comments
        reviews = self._fetch_pr_reviews(pr_url)
        comments = self._fetch_pr_comments(pr_url)

        all_feedback = reviews + comments
        if not all_feedback:
            return None

        for item in all_feedback:
            body = item.get("body", "")
            reviewer = item.get("user", {}).get("login", "")
            if reviewer == self.username:
                continue  # Skip our own comments

            sentiment = self._classify_sentiment(body)

            # Record in DB
            conn = self.pool.get()
            conn.execute(
                """INSERT INTO pr_reviews (pr_url, reviewer, review_type, body, sentiment)
                   VALUES (?, ?, ?, ?, ?)""",
                (pr_url, reviewer, item.get("state", "comment"),
                 body[:2000], sentiment),
            )
            conn.commit()

            # Take action based on sentiment
            comment_id = item.get("id")
            action = await self._take_action(sentiment, pr_url, repo_full_name,
                                             body, reviewer, comment_id=comment_id)
            if action:
                conn.execute(
                    """UPDATE pr_reviews SET action_taken = ?
                       WHERE pr_url = ? AND reviewer = ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (action, pr_url, reviewer),
                )
                conn.commit()

            # Mark notification as read
            notif_id = notif.get("id")
            if notif_id:
                subprocess.run(
                    ["gh", "api", "-X", "PATCH", f"notifications/threads/{notif_id}"],
                    capture_output=True, text=True, timeout=10
                )

            return sentiment

        return None

    def _fetch_pr_reviews(self, pr_api_url: str) -> list:
        """Fetch reviews for a PR."""
        try:
            result = subprocess.run(
                ["gh", "api", f"{pr_api_url}/reviews"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return []

    def _fetch_pr_comments(self, pr_api_url: str) -> list:
        """Fetch issue comments on a PR."""
        try:
            # Convert pulls URL to issues comments URL
            comments_url = pr_api_url.replace("/pulls/", "/issues/") + "/comments"
            result = subprocess.run(
                ["gh", "api", comments_url],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return []

    def _classify_sentiment(self, text: str) -> str:
        """Classify review text sentiment using keyword matching."""
        text_lower = text.lower()

        # Check anti-AI first (highest priority)
        if any(kw in text_lower for kw in ANTI_AI_KEYWORDS):
            return "hostile"  # anti-AI treated as hostile for action purposes

        # Check for payment/job/contact requests (notify Daniel via Telegram)
        if any(kw in text_lower for kw in PAYMENT_KEYWORDS):
            return "payment_request"
        if any(kw in text_lower for kw in JOB_KEYWORDS):
            return "job_inquiry"
        if any(kw in text_lower for kw in CONTACT_KEYWORDS):
            return "contact_request"

        # Check for sponsor mentions
        if any(kw in text_lower for kw in SPONSOR_KEYWORDS):
            return "sponsor"

        # Check for regret/re-engagement
        if any(kw in text_lower for kw in REGRET_KEYWORDS):
            return "regretful"

        # Check hostile
        hostile_count = sum(1 for kw in HOSTILE_KEYWORDS if kw in text_lower)
        if hostile_count >= 2:
            return "hostile"

        # Check positive
        positive_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
        if positive_count >= 1:
            return "positive"

        # Check for constructive feedback (mentions fixes/changes needed)
        constructive_keywords = {"could you", "please", "instead", "should",
                                 "consider", "suggestion", "nit", "minor",
                                 "change", "fix", "update", "modify"}
        if any(kw in text_lower for kw in constructive_keywords):
            return "constructive"

        return "constructive"  # default to constructive

    def _api_url_to_html(self, pr_api_url: str) -> str:
        """Convert API URL like repos/owner/repo/pulls/123 to HTML URL."""
        return pr_api_url.replace("https://api.github.com/repos/", "https://github.com/").replace("/pulls/", "/pull/")

    def _react_to_comment(self, owner_repo: str, comment_id: int, reaction: str = "+1"):
        """Add a reaction (thumbs up etc.) to a comment. Delays 10s to look human."""
        if not comment_id:
            return
        time.sleep(10)
        try:
            subprocess.run(
                ["gh", "api", "-X", "POST",
                 f"repos/{owner_repo}/issues/comments/{comment_id}/reactions",
                 "-f", f"content={reaction}"],
                capture_output=True, text=True, timeout=10
            )
        except Exception:
            pass

    async def _take_action(self, sentiment: str, pr_url: str,
                           repo_full_name: str, body: str,
                           reviewer: str, comment_id: int = None) -> str | None:
        """Take action based on sentiment. Returns action name."""
        conn = self.pool.get()

        # Extract PR number from URL for gh commands
        pr_number = pr_url.split("/")[-1]
        owner_repo = "/".join(pr_url.split("/repos/")[1].split("/pulls/")[0:1]) if "/repos/" in pr_url else repo_full_name
        html_url = self._api_url_to_html(pr_url)

        # --- Telegram notification sentiments ---
        if sentiment == "payment_request":
            self._comment_on_pr(
                owner_repo, pr_number,
                "Thanks for asking! For payment details, please email daniel@batesai.org and I'll get back to you promptly."
            )
            notify_github_attention(
                "payment_request", repo_full_name, html_url,
                f"From @{reviewer}: {body[:200]}"
            )
            return "telegram_notified"

        if sentiment == "job_inquiry":
            self._comment_on_pr(
                owner_repo, pr_number,
                "Thank you for the opportunity! Please reach out to daniel@batesai.org and I'd be happy to discuss further."
            )
            notify_github_attention(
                "job_inquiry", repo_full_name, html_url,
                f"From @{reviewer}: {body[:200]}"
            )
            return "telegram_notified"

        if sentiment == "contact_request":
            self._comment_on_pr(
                owner_repo, pr_number,
                "Thanks for reaching out! The best way to contact me is daniel@batesai.org."
            )
            notify_github_attention(
                "contact_request", repo_full_name, html_url,
                f"From @{reviewer}: {body[:200]}"
            )
            return "telegram_notified"

        if sentiment == "sponsor":
            self._react_to_comment(owner_repo, comment_id, "+1")
            add_sponsor(conn, reviewer, repo_full_name, "comment",
                        json.dumps({"quote": body[:500]}))
            self._comment_on_pr(owner_repo, pr_number,
                                "Thank you so much for the kind words and support! 🙏")
            notify_github_attention(
                "payment_request", repo_full_name, html_url,
                f"Sponsor offer from @{reviewer}: {body[:200]}"
            )
            return "thanked"

        # --- Standard sentiments ---
        if sentiment == "positive":
            self._react_to_comment(owner_repo, comment_id, "+1")
            self._comment_on_pr(owner_repo, pr_number,
                                "Thank you for the review! Glad it helps. 🙏")
            return "thanked"

        elif sentiment == "constructive":
            # Log for potential re-fix
            self.log_writer.append_entry(
                f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — REVIEW RECEIVED\n"
                f"**Repo:** {repo_full_name}\n"
                f"**PR:** #{pr_number}\n"
                f"**Reviewer:** {reviewer}\n"
                f"**Feedback:** {body[:300]}\n"
                f"**Action needed:** Yes — review and potentially re-fix\n"
                f"---"
            )

            # Notify via Telegram if it looks like a question
            body_lower = body.lower()
            if "?" in body and any(w in body_lower for w in ["why", "how", "what", "can you", "could you", "would you"]):
                notify_github_attention(
                    "question", repo_full_name, html_url,
                    f"Question from @{reviewer}: {body[:200]}"
                )

            # Check for learned patterns
            self._check_for_patterns(body, repo_full_name)

            return "fix_pushed"

        elif sentiment == "hostile":
            # Check for anti-AI policy
            body_lower = body.lower()
            is_anti_ai = any(kw in body_lower for kw in ANTI_AI_KEYWORDS)

            # Polite exit
            self._comment_on_pr(owner_repo, pr_number, POLITE_EXIT)

            # Close PR
            self._close_pr(owner_repo, pr_number)

            # Blacklist repo
            reason = "anti_ai_policy" if is_anti_ai else "hostile_maintainer"
            add_to_blacklist(conn, repo_full_name, reason,
                             details=json.dumps({"reviewer": reviewer,
                                                 "quote": body[:500]}))

            self.log_writer.append_entry(
                f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — BLOCKED\n"
                f"**Repo:** {repo_full_name}\n"
                f"**Reason:** {reason}\n"
                f"**Reviewer:** {reviewer}\n"
                f"**Quote:** {body[:200]}\n"
                f"**Action needed:** No — repo blacklisted\n"
                f"---"
            )
            return "repo_blacklisted"

        elif sentiment == "regretful":
            # Un-blacklist and re-engage
            if is_blacklisted(conn, repo_full_name):
                remove_from_blacklist(conn, repo_full_name)
                self._comment_on_pr(owner_repo, pr_number, COMPASSION_REENGAGEMENT)

                self.log_writer.append_entry(
                    f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} — RE-ENGAGED\n"
                    f"**Repo:** {repo_full_name}\n"
                    f"**User:** {reviewer}\n"
                    f"**Details:** Repo un-blacklisted after positive re-engagement\n"
                    f"**Action needed:** No\n"
                    f"---"
                )
                return "re_engaged"

        return None

    def _comment_on_pr(self, owner_repo: str, pr_number: str, comment: str):
        """Post a comment on a PR. Delays 60s to look human."""
        time.sleep(60)
        try:
            subprocess.run(
                ["gh", "pr", "comment", pr_number,
                 "--repo", owner_repo, "--body", comment],
                capture_output=True, text=True, timeout=15
            )
        except Exception as e:
            print(f"  Failed to comment on PR: {e}")

    def _close_pr(self, owner_repo: str, pr_number: str):
        """Close a PR."""
        try:
            subprocess.run(
                ["gh", "pr", "close", pr_number, "--repo", owner_repo],
                capture_output=True, text=True, timeout=15
            )
        except Exception as e:
            print(f"  Failed to close PR: {e}")

    def _check_for_patterns(self, body: str, repo_full_name: str):
        """Extract learned patterns from review feedback."""
        conn = self.pool.get()
        body_lower = body.lower()

        # Check for commit format feedback
        commit_keywords = ["conventional commit", "commit message", "commit format",
                           "please use", "commit style"]
        if any(kw in body_lower for kw in commit_keywords):
            add_learned_pattern(conn, "commit_format", body[:500],
                                repo_full_name, 0.7, "review_feedback")

        # Check for DCO requirement
        if "signed-off-by" in body_lower or "dco" in body_lower:
            add_learned_pattern(conn, "dco_required", "true",
                                repo_full_name, 0.9, "review_feedback")

        # Check for global patterns (update CLAUDE.md if seen in 3+ repos)
        self._maybe_update_claude_md(conn)

    def _maybe_update_claude_md(self, conn):
        """If a pattern appears in 3+ repos, add it to CLAUDE.md as a global rule."""
        patterns = conn.execute("""
            SELECT pattern_type, pattern_value, COUNT(DISTINCT repo_full_name) as repo_count
            FROM learned_patterns
            WHERE repo_full_name IS NOT NULL
            GROUP BY pattern_type, pattern_value
            HAVING repo_count >= 3
        """).fetchall()

        if not patterns:
            return

        claude_md_path = PROJECT_ROOT / "CLAUDE.md"
        if not claude_md_path.exists():
            return

        content = claude_md_path.read_text()

        # Add learned rules section if not present
        if "## Learned Rules" not in content:
            content += "\n\n## Learned Rules\n"
            content += "*Auto-discovered patterns from maintainer feedback:*\n\n"

        for p in patterns:
            rule_text = f"- **{p['pattern_type']}** (seen in {p['repo_count']} repos): {p['pattern_value'][:100]}"
            if rule_text not in content:
                content = content.rstrip() + "\n" + rule_text + "\n"
                # Also record as global pattern
                add_learned_pattern(conn, p["pattern_type"], p["pattern_value"],
                                    None, 0.9, "auto_global")

        claude_md_path.write_text(content)
