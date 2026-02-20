"""Telegram two-way communication for the Do-Good GitHub Agent.

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

BOT_TOKEN = "8415315576:AAHwsKk-R6zED0KUh6EQdef3xaCOpbFRFS4"
CHAT_ID = "8450027682"
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
API_URL = f"{BASE_URL}/sendMessage"

PROJECT_DIR = "/Volumes/X10 Pro danielalanbatesatgmail.com /AIcode/github-helper"


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
        print("Telegram daemon started", flush=True)
        print(f"  Messages are piped to Claude Code (claude -p)", flush=True)
        print(f"  Working dir: {PROJECT_DIR}", flush=True)
        print(flush=True)

        while True:
            try:
                updates = get_updates(offset=self.offset, timeout=30)
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
        """Pipe Daniel's message to Claude Code and relay the response."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Daniel: {text[:120]}", flush=True)

        # Record Daniel's message in conversation history
        self.conversation.append({"role": "user", "text": text[:500]})

        # Build context (includes conversation history)
        context = self._build_context(text, reply_to)

        # Build the prompt for Claude
        system_prompt = (
            "You are Claude, Daniel Bates' AI assistant. Daniel is messaging you via Telegram.\n"
            "You manage the Do-Good GitHub Helper — an autonomous agent that finds and fixes bugs "
            "on social-good open-source projects.\n\n"
            "CRITICAL: You are in a MULTI-TURN conversation. The CONVERSATION HISTORY in your "
            "context shows previous messages. When Daniel says things like 'yes', 'do it', "
            "'all of them', etc., refer to the conversation history to understand what he means.\n\n"
            "RULES:\n"
            "- Keep responses concise and Telegram-friendly (under 3000 chars)\n"
            "- If Daniel asks to reply to a GitHub PR/issue, use `gh pr comment` or `gh issue comment`\n"
            "- If Daniel asks to update code, edit the files directly\n"
            "- If Daniel asks about status, query the SQLite DB at data/github_helper.db\n"
            "- If Daniel asks to restart a service, use the appropriate CLI command\n"
            "- You have full access to the project at: " + PROJECT_DIR + "\n"
            "- When posting GitHub comments, delay 60 seconds first to seem natural\n"
            "- Be direct and helpful. No fluff. Short answers for short questions.\n"
        )

        if context:
            system_prompt += f"\n{context}\n"

        full_prompt = f"Daniel: {text}"

        # Run claude -p with the project directory
        try:
            cmd = [
                "claude", "-p", full_prompt,
                "--system-prompt", system_prompt,
                "--model", "haiku",
                "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep",
                "--add-dir", PROJECT_DIR,
                "--no-session-persistence",
            ]

            print(f"[{ts}] -> claude -p (haiku)...", flush=True)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=180,
                cwd=PROJECT_DIR,
                env={
                    **{k: v for k, v in os.environ.items()
                       if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")},
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL": "1",
                },
            )

            response = result.stdout.strip()
            stderr = result.stderr.strip()

            if result.returncode != 0 and not response:
                print(f"[{ts}] Claude error (rc={result.returncode}): {stderr[:200]}", flush=True)
                notify_plain(f"Error: {stderr[:300]}")
                return

            if not response:
                response = "(empty response)"

            print(f"[{ts}] <- {response[:120]}", flush=True)

            # Record Claude's response in conversation history
            self.conversation.append({"role": "assistant", "text": response[:500]})

            # Send response back via Telegram (chunked if needed)
            for i in range(0, len(response), 4000):
                chunk = response[i:i + 4000]
                notify_plain(chunk)

        except subprocess.TimeoutExpired:
            print(f"[{ts}] Claude timed out (180s)", flush=True)
            notify_plain("Timed out (180s). Try a simpler request.")
        except FileNotFoundError:
            print(f"[{ts}] 'claude' CLI not found!", flush=True)
            notify_plain("Error: claude CLI not found.")
        except Exception as e:
            print(f"[{ts}] Error: {e}", flush=True)
            notify_plain(f"Error: {e}")

    def record_notification(self, event_type: str, repo: str, url: str, summary: str):
        """Record an outbound notification for context tracking."""
        self.recent_notifications.append({
            "time": datetime.now().strftime("%H:%M"),
            "type": event_type,
            "repo": repo,
            "url": url,
            "summary": summary,
        })
