"""Telegram two-way communication for dogood.

Outbound: notify Daniel about GitHub events.
Inbound:  pipe Daniel's messages to Claude Code for intelligent processing.
"""

import subprocess
import json
import time
import re
import os
from datetime import datetime
from collections import deque
from pathlib import Path

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
API_URL = f"{BASE_URL}/sendMessage"

PROJECT_DIR = "/Volumes/X10 Pro danielalanbatesatgmail.com /AIcode/dogood"

TELEGRAM_INBOX = "/tmp/telegram-inbox.jsonl"
TELEGRAM_OUTBOX = "/tmp/telegram-outbox.jsonl"


# ---------------------------------------------------------------------------
# Outbound: send messages TO Daniel
# ---------------------------------------------------------------------------

def notify(message: str) -> bool:
    """Send a Telegram message to Daniel. Returns True on success."""
    try:
        result = subprocess.run(
            ["curl", "-s", API_URL,
             "-d", f"chat_id={CHAT_ID}",
             "-d", f"text={message}",
             "-d", "parse_mode=Markdown"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout)
            return resp.get("ok", False)
    except Exception:
        pass
    return False


def notify_plain(message: str) -> bool:
    """Send a plain-text Telegram message (no Markdown parsing issues)."""
    try:
        result = subprocess.run(
            ["curl", "-s", API_URL,
             "-d", f"chat_id={CHAT_ID}",
             "-d", f"text={message}"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout)
            return resp.get("ok", False)
    except Exception:
        pass
    return False


def notify_github_attention(event_type: str, repo: str, url: str, summary: str):
    """Notify Daniel about a GitHub event that needs his attention."""
    emoji = {
        "payment_request": "\U0001f4b0",
        "job_inquiry": "\U0001f4bc",
        "question": "\u2753",
        "review_needs_human": "\U0001f440",
        "contact_request": "\U0001f4e7",
        "bounty_found": "\U0001f3af",
        "pr_merged": "\u2705",
    }.get(event_type, "\U0001f4e2")

    msg = f"{emoji} *{event_type.replace('_', ' ').title()}*\n"
    msg += f"Repo: `{repo}`\n"
    msg += f"{summary}\n"
    msg += f"[View on GitHub]({url})"

    return notify(msg)


# ---------------------------------------------------------------------------
# Inbound: receive messages FROM Daniel
# ---------------------------------------------------------------------------

def get_updates(offset: int = 0, timeout: int = 30) -> list:
    """Long-poll Telegram for new messages from Daniel."""
    try:
        result = subprocess.run(
            ["curl", "-s", f"{BASE_URL}/getUpdates",
             "-d", f"offset={offset}",
             "-d", f"timeout={timeout}",
             "-d", "allowed_updates=[\"message\"]"],
            capture_output=True, text=True, timeout=timeout + 10
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout)
            if resp.get("ok"):
                return resp.get("result", [])
    except Exception:
        pass
    return []


def extract_github_url(text: str) -> str | None:
    """Extract a GitHub PR/issue URL from text."""
    match = re.search(r'https://github\.com/[\w\-]+/[\w\-]+/(?:pull|issues)/\d+', text)
    return match.group(0) if match else None


class TelegramDaemon:
    """Polls Telegram for messages and pipes them to Claude Code."""

    def __init__(self):
        self.offset = 0
        # Ring buffer of recent outbound notifications for context
        self.recent_notifications = deque(maxlen=20)
        # Conversation history — passed to Claude each call so it has memory
        self.conversation = deque(maxlen=20)

    def run(self, poll_interval: int = 5):
        """Run the Telegram daemon — long-polls for messages."""
        print("Telegram daemon started (bridge mode)", flush=True)
        print(f"  Inbox:  {TELEGRAM_INBOX}", flush=True)
        print(f"  Outbox: {TELEGRAM_OUTBOX}", flush=True)
        print(flush=True)

        while True:
            try:
                # Poll for incoming Telegram messages
                updates = get_updates(offset=self.offset, timeout=5)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    # Only process messages from Daniel
                    if chat_id != CHAT_ID:
                        continue

                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    reply_to = msg.get("reply_to_message", {})
                    self._handle_message(text, reply_to)

                # Poll outbox for responses from Claude Code session
                self._poll_outbox()

            except KeyboardInterrupt:
                print("\nTelegram daemon stopped.", flush=True)
                break
            except Exception as e:
                print(f"  Telegram poll error: {e}", flush=True)
                time.sleep(poll_interval)

    def _build_context(self, text: str, reply_to: dict) -> str:
        """Build context string for Claude from conversation history and notifications."""
        parts = []

        # Conversation history — this is the critical part for continuity
        if self.conversation:
            history = "\n".join(
                f"{'Daniel' if m['role'] == 'user' else 'Claude'}: {m['text']}"
                for m in self.conversation
            )
            parts.append(f"CONVERSATION HISTORY (most recent messages):\n{history}")

        # If replying to a specific notification, include it
        reply_text = reply_to.get("text", "")
        if reply_text:
            github_url = extract_github_url(reply_text)
            parts.append(f"Daniel is replying to this notification:\n---\n{reply_text[:600]}\n---")
            if github_url:
                parts.append(f"GitHub URL from notification: {github_url}")

        # Include recent notifications for context
        if self.recent_notifications:
            recent = "\n".join(
                f"  [{n['time']}] {n['type']}: {n['summary'][:100]}"
                for n in self.recent_notifications
            )
            parts.append(f"Recent notifications sent to Daniel:\n{recent}")

        return "\n\n".join(parts)

    def _handle_message(self, text: str, reply_to: dict):
        """Respond to Daniel via claude -p, with inbox fallback for active CLI sessions."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Daniel: {text[:200]}", flush=True)

        reply_context = ""
        if reply_to:
            reply_context = reply_to.get("text", "")[:500]
            print(f"[{ts}]   (replying to: {reply_context[:100]})", flush=True)

        # Always write to inbox so CLI session can see it too
        msg = {
            "timestamp": datetime.now().isoformat(),
            "text": text,
            "reply_to": reply_context,
        }
        try:
            inbox = Path(TELEGRAM_INBOX)
            with inbox.open("a") as f:
                f.write(json.dumps(msg) + "\n")
        except Exception:
            pass

        # Record in conversation history
        self.conversation.append({"role": "user", "text": text[:500]})

        # Build context
        context = self._build_context(text, reply_to)

        system_prompt = (
            "You are Claude, Daniel Bates' AI assistant. Daniel is messaging you via Telegram.\n"
            "You manage dogood — an autonomous agent factory that finds and fixes "
            "bugs on open-source projects.\n\n"
            "RULES:\n"
            "- Keep responses concise and Telegram-friendly (under 2000 chars)\n"
            "- If Daniel asks about status, query the SQLite DB at data/github_helper.db\n"
            "- If Daniel asks to reply to a GitHub PR/issue, use `gh pr comment` or `gh issue comment`\n"
            "- You have full access to the project at: " + PROJECT_DIR + "\n"
            "- Be direct. No fluff.\n"
        )
        if context:
            system_prompt += f"\n{context}\n"

        full_prompt = f"Daniel: {text}"

        try:
            cmd = [
                "claude", "-p", full_prompt,
                "--system-prompt", system_prompt,
                "--model", "haiku",
                "--allowedTools", "Bash,Read,Glob,Grep",
                "--add-dir", PROJECT_DIR,
                "--no-session-persistence",
            ]

            print(f"[{ts}] -> claude -p (haiku)...", flush=True)
            env = {k: v for k, v in os.environ.items()
                   if "CLAUDE" not in k.upper()}
            env["PATH"] = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
            env["HOME"] = os.environ.get("HOME", "/Users/daniel")

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                cwd=PROJECT_DIR, env=env,
            )

            response = result.stdout.strip()
            if result.returncode != 0 or not response:
                stderr = result.stderr.strip()
                print(f"[{ts}] Claude error (rc={result.returncode}): {stderr[:200]}", flush=True)
                # Don't send error to user — just log it
                return

            print(f"[{ts}] <- {response[:120]}", flush=True)
            self.conversation.append({"role": "assistant", "text": response[:500]})

            for i in range(0, len(response), 4000):
                notify_plain(response[i:i + 4000])

        except subprocess.TimeoutExpired:
            print(f"[{ts}] Claude timed out (120s)", flush=True)
        except Exception as e:
            print(f"[{ts}] Error: {e}", flush=True)

    def _poll_outbox(self):
        """Check outbox for responses from Claude Code session and send them."""
        outbox = Path(TELEGRAM_OUTBOX)
        if not outbox.exists() or outbox.stat().st_size == 0:
            return
        try:
            lines = outbox.read_text().strip().split("\n")
            outbox.write_text("")  # Clear after reading
            for line in lines:
                if not line.strip():
                    continue
                msg = json.loads(line)
                text = msg.get("text", "")
                if text:
                    for i in range(0, len(text), 4000):
                        notify_plain(text[i:i + 4000])
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sent response: {text[:80]}", flush=True)
        except Exception as e:
            print(f"Outbox error: {e}", flush=True)

    def record_notification(self, event_type: str, repo: str, url: str, summary: str):
        """Record an outbound notification for context tracking."""
        self.recent_notifications.append({
            "time": datetime.now().strftime("%H:%M"),
            "type": event_type,
            "repo": repo,
            "url": url,
            "summary": summary,
        })
