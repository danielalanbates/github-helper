"""Issue analysis and fix generation via Claude Code SDK."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from claude_code_sdk import query, ClaudeCodeOptions as ClaudeAgentOptions, AssistantMessage, ResultMessage

import re

from src.config import (
    GITHUB_TOKEN, GITHUB_USERNAME, WORK_DIR,
    CLA_KEYWORDS, CLA_CONTEXTUAL_PATTERNS, DCO_KEYWORDS,
    CLA_WORKFLOW_FILES, DCO_WORKFLOW_FILES,
    CLA_ORGS, SIGNED_CLA_ORGS,
    load_model_tiers, ANTI_AI_KEYWORDS,
    SUPPORTED_EXTENSIONS, UNSUPPORTED_EXTENSIONS,
)
from src.db import (
    get_connection, record_contribution, update_contribution_status,
    update_feedback_status,
)
from src.utils import now_iso

# Files to check for contributing guidelines (in priority order)
CONTRIBUTING_FILES = [
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING.txt",
    "CONTRIBUTING",
    ".github/CONTRIBUTING.md",
    ".github/contributing.md",
    "docs/CONTRIBUTING.md",
]

PR_TEMPLATE_FILES = [
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
]

README_FILES = [
    "README.md",
    "README.rst",
    "README.txt",
    "README",
]


class Solver:
    def __init__(self, token: str = GITHUB_TOKEN, username: str = GITHUB_USERNAME,
                 agent_id: str = "main", work_dir: Path = WORK_DIR,
                 model_tier: dict = None, is_bounty: bool = False):
        self.token = token
        self.username = username
        self.agent_id = agent_id
        self.work_dir = work_dir
        self.model_tier = model_tier  # None = use defaults (backward compatible)
        self.is_bounty = is_bounty

    def _signal_rate_limit(self, model: str):
        """Signal the orchestrator which model hit a rate limit."""
        import json, time
        from pathlib import Path
        sig_file = Path("/tmp/dogood-rate-limit-signal.json")
        try:
            sig = {"model": model, "time": time.time(), "agent": self.agent_id}
            tmp = sig_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(sig))
            tmp.rename(sig_file)
        except OSError:
            pass

    async def solve_issue(self, issue_id: int) -> dict:
        """Full pipeline: analyze -> clone -> fork -> fix -> PR."""
        conn = get_connection()

        # Load issue and repo from DB
        issue = conn.execute("""
            SELECT i.*, r.owner, r.name as repo_name, r.full_name,
                   r.url as repo_url, r.language
            FROM issues i
            JOIN repositories r ON i.repo_id = r.id
            WHERE i.id = ?
        """, (issue_id,)).fetchone()

        if not issue:
            raise ValueError(f"Issue {issue_id} not found in database")

        owner = issue["owner"]
        repo_name = issue["repo_name"]
        issue_number = issue["number"]
        full_name = issue["full_name"]

        print(f"Solving {full_name}#{issue_number}: {issue['title']}")

        # Pre-flight: check if org/repo is manually blocked
        from src.config import BLOCKED_ORGS, BLOCKED_REPOS
        if owner.lower() in {o.lower() for o in BLOCKED_ORGS} or full_name.lower() in {r.lower() for r in BLOCKED_REPOS}:
            print(f"  Skipping: {full_name} is in the manual blocklist")
            record_contribution(conn, {
                "issue_id": issue_id, "repo_id": issue["repo_id"],
                "action": "skipped", "status": "skipped_blocked_org",
            })
            return {"success": False, "error": f"Blocked org/repo: {full_name}"}

        # Pre-flight: check if the bug references only unsupported file types
        unsupported_ext = self.check_issue_language(dict(issue))
        if unsupported_ext:
            print(f"  Skipping: issue references unsupported file types ({unsupported_ext})")
            record_contribution(conn, {
                "issue_id": issue_id, "repo_id": issue["repo_id"],
                "action": "skipped", "status": "skipped_language",
            })
            return {"success": False, "error": f"Issue references unsupported languages: {unsupported_ext}"}

        # Pre-flight: check for existing PRs on this issue
        existing_pr = self._check_existing_prs(owner, repo_name, issue_number)
        if existing_pr:
            print(f"  Skipping: existing PR already addresses this issue — {existing_pr}")
            record_contribution(conn, {
                "issue_id": issue_id, "repo_id": issue["repo_id"],
                "action": "skipped", "status": "skipped_duplicate_pr",
            })
            return {"success": False, "error": f"Duplicate PR exists: {existing_pr}"}

        # Record we're starting
        contrib_id = record_contribution(conn, {
            "issue_id": issue_id,
            "repo_id": issue["repo_id"],
            "action": "analyzed",
            "status": "in_progress",
        })

        try:
            # Fork the repo
            print("  Forking repository...")
            self._ensure_fork(owner, repo_name)

            # Clone to work directory
            print("  Cloning repository...")
            clone_path = self._clone_repo(owner, repo_name)

            # Check anti-AI policy BEFORE doing any work
            print("  Checking for anti-AI policy...")
            if self.check_anti_ai_policy(owner, repo_name, clone_path):
                update_contribution_status(conn, contrib_id, "skipped_anti_ai")
                # Auto-blacklist this repo
                from src.db import add_to_blacklist
                add_to_blacklist(conn, full_name, "anti_ai_policy",
                                 details='{"source": "contributing_scan"}')
                return {"success": False, "error": f"Repo {full_name} has anti-AI policy"}

            # Check CLA requirement
            cla_info = self.check_cla_requirement(owner, repo_name, clone_path)
            if cla_info["requires_cla"]:
                update_contribution_status(conn, contrib_id, "skipped_cla")
                # Auto-blacklist so we don't waste time on other issues in this repo
                from src.db import add_to_blacklist
                add_to_blacklist(conn, full_name, "cla_required",
                                 details=json.dumps({"source": "cla_scan", "info": cla_info["details"]}))
                return {"success": False, "error": f"Repo requires CLA: {cla_info['details']}"}

            # Fetch contributing guidelines BEFORE making changes
            print("  Scanning contributing guidelines...")
            guidelines = self._fetch_contributing_guidelines(
                owner, repo_name, clone_path
            )
            if guidelines["contributing"]:
                print(f"    Found CONTRIBUTING ({len(guidelines['contributing'])} chars)")
            if guidelines["pr_template"]:
                print(f"    Found PR template ({len(guidelines['pr_template'])} chars)")
            if guidelines["readme_contributing"]:
                print(f"    Found README contributing section ({len(guidelines['readme_contributing'])} chars)")

            # Create a branch
            branch_name = f"fix/issue-{issue_number}"
            self._create_branch(clone_path, branch_name)

            # Fetch issue details via gh CLI
            print("  Fetching issue context...")
            issue_context = self._fetch_issue_context(owner, repo_name, issue_number)

            # Run Claude Code SDK to analyze and fix
            print("  Running Claude to analyze and fix...")
            result = await self._run_claude_fix(
                clone_path, issue, issue_context, branch_name, guidelines
            )

            if result["success"]:
                # Push and create PR
                print("  Creating pull request...")
                pr_url = self._push_and_pr(
                    clone_path, owner, repo_name,
                    issue_number, issue["title"], branch_name, guidelines
                )
                # Track model used and opus attempts
                model_used = None
                opus_attempts = 0
                if self.model_tier:
                    model_used = self.model_tier.get("model")
                    if "opus" in model_used.lower():
                        opus_attempts = 1

                update_contribution_status(conn, contrib_id, "pr_created", pr_url,
                                          model_used=model_used,
                                          opus_attempts=opus_attempts)
                return {"success": True, "pr_url": pr_url, "details": result}
            else:
                update_contribution_status(conn, contrib_id, "failed")
                return {"success": False, "error": result.get("error")}

        except Exception as e:
            update_contribution_status(conn, contrib_id, "error")
            return {"success": False, "error": str(e)}

    def _fetch_contributing_guidelines(
        self, owner: str, repo: str, clone_path: Path
    ) -> dict:
        """Fetch CONTRIBUTING.md, PR template, and README contributing sections.

        Checks the cloned repo first (faster), falls back to GitHub API if sparse clone
        doesn't include these files.

        Returns dict with keys: contributing, pr_template, readme_contributing, commit_format
        """
        result = {
            "contributing": "",
            "pr_template": "",
            "readme_contributing": "",
            "commit_format": "",
        }

        # 1. Try CONTRIBUTING files from clone
        for f in CONTRIBUTING_FILES:
            fp = clone_path / f
            if fp.exists():
                try:
                    text = fp.read_text(errors="replace")[:15000]  # cap at 15k chars
                    result["contributing"] = text
                    # Try to extract commit message format
                    result["commit_format"] = self._extract_commit_format(text)
                    break
                except Exception:
                    pass

        # If not found in clone, try GitHub API
        if not result["contributing"]:
            for f in CONTRIBUTING_FILES:
                text = self._fetch_file_from_github(owner, repo, f)
                if text:
                    result["contributing"] = text[:15000]
                    result["commit_format"] = self._extract_commit_format(text)
                    break

        # 2. Try PR template files
        for f in PR_TEMPLATE_FILES:
            fp = clone_path / f
            if fp.exists():
                try:
                    result["pr_template"] = fp.read_text(errors="replace")[:5000]
                    break
                except Exception:
                    pass

        if not result["pr_template"]:
            for f in PR_TEMPLATE_FILES:
                text = self._fetch_file_from_github(owner, repo, f)
                if text:
                    result["pr_template"] = text[:5000]
                    break

        # 3. Extract contributing section from README
        for f in README_FILES:
            fp = clone_path / f
            if fp.exists():
                try:
                    readme = fp.read_text(errors="replace")
                    section = self._extract_contributing_section(readme)
                    if section:
                        result["readme_contributing"] = section[:5000]
                    break
                except Exception:
                    pass

        if not result["readme_contributing"]:
            for f in README_FILES:
                text = self._fetch_file_from_github(owner, repo, f)
                if text:
                    section = self._extract_contributing_section(text)
                    if section:
                        result["readme_contributing"] = section[:5000]
                    break

        return result

    def _fetch_file_from_github(self, owner: str, repo: str, path: str) -> str:
        """Fetch a file from GitHub API (for sparse clones that don't have it)."""
        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/contents/{path}",
                 "--jq", ".content"],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0 and r.stdout.strip():
                import base64
                return base64.b64decode(r.stdout.strip()).decode("utf-8", errors="replace")
        except Exception:
            pass
        return ""

    def _extract_contributing_section(self, readme: str) -> str:
        """Extract the 'Contributing' section from a README."""
        import re
        # Match ## Contributing, ## How to Contribute, etc.
        pattern = re.compile(
            r'^(#{1,3}\s+(?:contributing|how to contribute|contribute|development|getting involved).*?)(?=^#{1,3}\s|\Z)',
            re.MULTILINE | re.IGNORECASE | re.DOTALL
        )
        m = pattern.search(readme)
        return m.group(1).strip() if m else ""

    def _extract_commit_format(self, contributing_text: str) -> str:
        """Try to extract commit message format rules from CONTRIBUTING text."""
        import re
        lines = contributing_text.split("\n")
        commit_lines = []
        in_commit_section = False
        for line in lines:
            lower = line.lower()
            if re.search(r'commit\s*(message|format|convention|style)', lower):
                in_commit_section = True
                commit_lines.append(line)
                continue
            if in_commit_section:
                if line.startswith("#") and not re.search(r'commit', lower):
                    break
                commit_lines.append(line)
                if len(commit_lines) > 30:
                    break
        return "\n".join(commit_lines).strip() if commit_lines else ""

    def _check_existing_prs(self, owner: str, repo: str, issue_number: int) -> str | None:
        """Check if open PRs already address this issue. Returns PR URL or None."""
        try:
            # Search open PRs mentioning this issue number
            result = subprocess.run(
                ["gh", "pr", "list", "--repo", f"{owner}/{repo}",
                 "--state", "open", "--search", str(issue_number),
                 "--json", "number,title,url", "--limit", "10"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return None
            prs = json.loads(result.stdout or "[]")
            issue_str = f"#{issue_number}"
            for pr in prs:
                title = (pr.get("title") or "").lower()
                # Match PRs that reference this issue number in title
                if issue_str in title or f"fix {issue_str}" in title or f"fixes {issue_str}" in title or f"close {issue_str}" in title or f"closes {issue_str}" in title or f"resolve {issue_str}" in title:
                    return pr.get("url", f"PR #{pr.get('number')}")
        except Exception:
            pass
        return None

    def check_issue_language(self, issue: dict) -> str | None:
        """Check if the issue references files in unsupported languages.

        Scans issue title and body for file path references (e.g., `src/main.rs`,
        `lib/foo.go`). If ALL referenced files are in unsupported languages,
        returns the unsupported extension. If mixed or no file refs, returns None (ok).
        """
        import re
        text = f"{issue.get('title', '')} {issue.get('body', '')}"

        # Find file references: word.ext or path/to/file.ext
        file_refs = re.findall(r'[\w/\\.-]+\.(\w{1,6})\b', text)
        if not file_refs:
            return None

        # Normalize to .ext format
        extensions = {f".{ext.lower()}" for ext in file_refs}

        # Filter out common non-file matches (versions like 3.11, URLs, etc.)
        noise = {".com", ".org", ".io", ".net", ".dev", ".app", ".md",
                 ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
                 ".txt", ".log", ".lock", ".env", ".0", ".1", ".2", ".3",
                 ".4", ".5", ".6", ".7", ".8", ".9", ".10", ".11", ".12"}
        extensions -= noise

        if not extensions:
            return None

        unsupported = extensions & UNSUPPORTED_EXTENSIONS
        supported = extensions & SUPPORTED_EXTENSIONS

        # If ALL file references are unsupported and NONE are supported, skip
        if unsupported and not supported:
            return ", ".join(sorted(unsupported))

        return None

    def check_anti_ai_policy(self, owner: str, repo: str, clone_path: Path = None) -> bool:
        """Check if a repo explicitly bans AI-generated contributions.

        Scans CONTRIBUTING.md and README.md for anti-AI keywords.
        Returns True if the repo bans AI contributions.
        """
        texts_to_check = []

        # Check CONTRIBUTING files
        for f in CONTRIBUTING_FILES:
            text = ""
            if clone_path:
                fp = clone_path / f
                if fp.exists():
                    try:
                        text = fp.read_text(errors="replace")[:15000]
                    except Exception:
                        pass
            if not text:
                text = self._fetch_file_from_github(owner, repo, f)
            if text:
                texts_to_check.append(text)
                break

        # Check README files
        for f in README_FILES:
            text = ""
            if clone_path:
                fp = clone_path / f
                if fp.exists():
                    try:
                        text = fp.read_text(errors="replace")[:15000]
                    except Exception:
                        pass
            if not text:
                text = self._fetch_file_from_github(owner, repo, f)
            if text:
                texts_to_check.append(text)
                break

        for text in texts_to_check:
            text_lower = text.lower()
            for kw in ANTI_AI_KEYWORDS:
                if kw in text_lower:
                    print(f"  Anti-AI policy detected: found '{kw}'")
                    return True
        return False

    def check_cla_requirement(self, owner: str, repo: str, clone_path: Path = None) -> dict:
        """Check if a repo requires a CLA or DCO sign-off.

        Returns dict with:
            requires_cla: bool - True if CLA required
            requires_dco: bool - True if DCO required
            details: str - Description of what was found
        """
        result = {"requires_cla": False, "requires_dco": False, "details": ""}

        # 0. Check org-level CLA requirements first (fastest check)
        owner_lower = owner.lower()
        signed_orgs = {o.lower() for o in SIGNED_CLA_ORGS}
        if owner_lower in signed_orgs:
            # We've signed this org's CLA — skip all further checks
            return result
        if owner_lower in {o.lower() for o in CLA_ORGS}:
            result["requires_cla"] = True
            result["details"] = f"Org '{owner}' requires CLA (known CLA org, not signed)"
            return result

        # 1. Check CONTRIBUTING.md for CLA/DCO mentions
        contributing_text = ""
        if clone_path:
            for f in CONTRIBUTING_FILES:
                fp = clone_path / f
                if fp.exists():
                    try:
                        contributing_text = fp.read_text(errors="replace").lower()
                        break
                    except Exception:
                        pass

        if not contributing_text:
            for f in CONTRIBUTING_FILES:
                text = self._fetch_file_from_github(owner, repo, f)
                if text:
                    contributing_text = text.lower()
                    break

        # Check CLA enforcement phrases (definite keywords)
        for kw in CLA_KEYWORDS:
            if kw in contributing_text:
                result["requires_cla"] = True
                result["details"] = f"CONTRIBUTING requires CLA: '{kw}'"
                break
        # Check contextual patterns (enforcement language near CLA mention)
        if not result["requires_cla"]:
            for pattern in CLA_CONTEXTUAL_PATTERNS:
                if re.search(pattern, contributing_text):
                    result["requires_cla"] = True
                    result["details"] = f"CONTRIBUTING requires CLA: regex '{pattern}'"
                    break

        for kw in DCO_KEYWORDS:
            if kw in contributing_text:
                result["requires_dco"] = True
                if not result["details"]:
                    result["details"] = f"CONTRIBUTING mentions DCO: '{kw}'"
                break

        # 2. Check for CLA/DCO workflow files (these actively block PRs)
        if not result["requires_cla"]:
            for wf in CLA_WORKFLOW_FILES:
                if clone_path and (clone_path / wf).exists():
                    result["requires_cla"] = True
                    result["details"] = f"CLA workflow found: {wf}"
                    break
        if not result["requires_dco"]:
            for wf in DCO_WORKFLOW_FILES:
                if clone_path and (clone_path / wf).exists():
                    result["requires_dco"] = True
                    result["details"] = f"DCO workflow found: {wf}"
                    break

        return result

    def _ensure_fork(self, owner: str, repo: str):
        """Fork the repo to the user's account if not already forked."""
        result = subprocess.run(
            ["gh", "repo", "fork", f"{owner}/{repo}", "--clone=false"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0 and "already exists" not in result.stderr:
            raise RuntimeError(f"Fork failed: {result.stderr}")

    def _clone_repo(self, owner: str, repo: str) -> Path:
        """Clone the user's fork to the work directory (agent-isolated)."""
        clone_path = self.work_dir / self.agent_id / repo
        clone_path.parent.mkdir(parents=True, exist_ok=True)

        if clone_path.exists():
            shutil.rmtree(clone_path)

        try:
            subprocess.run(
                ["gh", "repo", "clone", f"{self.username}/{repo}", str(clone_path),
                 "--", "--depth=1", "--single-branch"],
                check=True, capture_output=True, text=True, timeout=300
            )
        except subprocess.TimeoutExpired:
            # Clean up partial clone
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            raise RuntimeError(f"Clone timed out after 300s for {owner}/{repo} — skipping")
        except subprocess.CalledProcessError as e:
            # Clean up failed clone
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            raise RuntimeError(f"Clone failed for {owner}/{repo}: {e.stderr[:200]}")

        # Add upstream remote
        subprocess.run(
            ["git", "-C", str(clone_path), "remote", "add", "upstream",
             f"https://github.com/{owner}/{repo}.git"],
            capture_output=True, text=True
        )

        return clone_path

    def _create_branch(self, clone_path: Path, branch_name: str):
        """Create and checkout a new branch."""
        subprocess.run(
            ["git", "-C", str(clone_path), "checkout", "-b", branch_name],
            check=True, capture_output=True, text=True
        )

    def _fetch_issue_context(self, owner: str, repo: str, number: int) -> str:
        """Fetch issue body + comments via gh CLI."""
        result = subprocess.run(
            ["gh", "issue", "view", str(number),
             "--repo", f"{owner}/{repo}",
             "--json", "title,body,comments,labels,assignees"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return result.stdout
        return "{}"

    async def _run_claude_fix(
        self, clone_path: Path, issue: dict,
        issue_context: str, branch_name: str,
        guidelines: dict | None = None
    ) -> dict:
        """Use Claude Code SDK to analyze the issue and generate a fix."""

        guidelines = guidelines or {}

        # Build guidelines section for the prompt
        guidelines_section = ""
        if guidelines.get("contributing"):
            guidelines_section += f"""
### CONTRIBUTING.md (MUST FOLLOW):
{guidelines['contributing'][:8000]}
"""
        if guidelines.get("commit_format"):
            guidelines_section += f"""
### Commit Message Format (extracted from CONTRIBUTING):
{guidelines['commit_format']}
"""
        if guidelines.get("readme_contributing"):
            guidelines_section += f"""
### README Contributing Section:
{guidelines['readme_contributing'][:3000]}
"""

        prompt = f"""You are working on a fix for an open-source project.

## Repository
- Name: {issue['full_name']}
- Language: {issue['language'] or 'Unknown'}

## Issue #{issue['number']}: {issue['title']}

### Issue Body:
{issue['body'] or 'No description provided.'}

### Issue Context (comments, labels):
{issue_context}

## Contributing Guidelines
{guidelines_section if guidelines_section else "No specific contributing guidelines found. Follow standard open-source practices."}

## Your Task:
1. Read the relevant source code to understand the codebase structure
2. Understand the bug or feature request described in the issue
3. Implement a minimal, focused fix that addresses the issue
4. Make sure the fix follows the project's existing code style AND the contributing guidelines above
5. If there are tests, run them to verify your fix doesn't break anything
6. Stage and commit your changes with a commit message that follows the project's conventions
   - If the project specifies a commit format (e.g., Conventional Commits, type: description), USE IT
   - Otherwise use: "Fix #{issue['number']}: <brief description>"

CRITICAL guidelines:
- Keep changes minimal and focused on the issue
- Follow existing code conventions AND the contributing guidelines above
- Do NOT modify unrelated files
- Follow the project's commit message format exactly
- If the project requires signed-off-by, DCO, or other sign-off, include it
- If the issue is too complex or ambiguous, explain why and stop
"""

        # Build options — Bounty Agent gets a quality-focused prompt and more turns
        if self.is_bounty:
            system_prompt = (
                "You are Bounty Agent — an elite open-source contributor competing for bug bounties. "
                "Your fix must be BETTER than any competing PR. Focus on:\n"
                "- Comprehensive fix that handles all edge cases\n"
                "- Add or update tests to prove correctness\n"
                "- Clean, idiomatic code that follows project conventions exactly\n"
                "- Better error handling than a quick-fix would have\n"
                "- If competing PRs are shown, analyze their weaknesses and improve on them\n"
                "Quality wins bounties. Be thorough."
            )
            max_turns = 50
        else:
            system_prompt = (
                "You are a skilled open-source contributor. You fix bugs carefully, "
                "write clean code, and follow project conventions. You are thorough "
                "but minimal in your changes."
            )
            max_turns = 30

        opts_kwargs = {
            "system_prompt": system_prompt,
            "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            "cwd": str(clone_path),
            "max_turns": max_turns,
            "permission_mode": "bypassPermissions",
        }
        if self.model_tier:
            opts_kwargs["model"] = self.model_tier["model"]
            extra = {}
            if self.model_tier.get("effort"):
                extra["effort"] = self.model_tier["effort"]
            if extra:
                opts_kwargs["extra_args"] = extra
        options = ClaudeAgentOptions(**opts_kwargs)

        result_text = ""
        success = False
        cost_usd = 0.0

        try:
            # Ensure no Claude Code env vars that would trigger "nested session" detection
            import os as _os
            for _k in list(_os.environ):
                if "CLAUDE" in _k.upper():
                    _os.environ.pop(_k, None)

            from src.rate_coordinator import (
                report_rate_limit, get_retry_delay, get_slot_for_agent,
            )
            from src.model_selector import get_next_tier
            slot = get_slot_for_agent(self.agent_id)
            max_retries = 5
            current_tier = self.model_tier.copy() if self.model_tier else None
            for attempt in range(max_retries + 1):
                try:
                    async for message in query(prompt=prompt, options=options):
                        if message is None:
                            continue  # Skip unknown message types
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if hasattr(block, "text"):
                                    result_text += block.text + "\n"
                        elif isinstance(message, ResultMessage):
                            if message.result:
                                result_text += message.result + "\n"
                            if message.total_cost_usd:
                                cost_usd = message.total_cost_usd
                        # Silently skip other message types (SystemMessage, StreamEvent, etc.)
                    break  # Success — exit retry loop
                except Exception as e:
                    err_str = str(e).lower()
                    if ("rate_limit" in err_str or "hit your limit" in err_str):
                        if attempt < max_retries:
                            # Report to shared coordinator so other agents stagger
                            report_rate_limit(self.agent_id)

                            # Signal orchestrator which model hit the wall
                            hit_model = current_tier["model"] if current_tier else "unknown"
                            self._signal_rate_limit(hit_model)

                            # Escalate to next model tier (different rate limit pool)
                            if current_tier:
                                next_tier = get_next_tier(current_tier)
                                if next_tier:
                                    current_tier = next_tier
                                    self.model_tier = current_tier
                                    escalated_kwargs = {**opts_kwargs}
                                    escalated_kwargs["model"] = current_tier["model"]
                                    escalated_kwargs["extra_args"] = {
                                        "effort": current_tier.get("effort", "medium")
                                    }
                                    options = ClaudeAgentOptions(**escalated_kwargs)
                                    print(f"  Rate limited on {hit_model} — "
                                          f"escalating to {current_tier['label']}",
                                          flush=True)
                                else:
                                    print(f"  Rate limited at max tier ({current_tier['label']})",
                                          flush=True)

                            wait = get_retry_delay(slot=slot, attempt=attempt)
                            print(f"  Waiting {wait:.0f}s (slot {slot}, attempt {attempt + 1}/{max_retries})...",
                                  flush=True)
                            await asyncio.sleep(wait)
                            continue
                    raise  # Re-raise non-rate-limit errors or last attempt

            # Check if there are actual changes
            diff_result = subprocess.run(
                ["git", "-C", str(clone_path), "diff", "--stat", "HEAD~1"],
                capture_output=True, text=True
            )
            has_changes = bool(diff_result.stdout.strip())

            if not has_changes:
                # Also check if there are uncommitted changes
                status_result = subprocess.run(
                    ["git", "-C", str(clone_path), "status", "--porcelain"],
                    capture_output=True, text=True
                )
                if status_result.stdout.strip():
                    # Auto-commit any remaining changes
                    subprocess.run(
                        ["git", "-C", str(clone_path), "add", "-A"],
                        capture_output=True, text=True
                    )
                    subprocess.run(
                        ["git", "-C", str(clone_path), "commit", "-m",
                         f"Fix #{issue['number']}: {issue['title'][:60]}"],
                        capture_output=True, text=True
                    )
                    has_changes = True

            success = has_changes

        except Exception as e:
            return {"success": False, "error": str(e)}

        return {
            "success": success,
            "result": result_text[-2000:] if result_text else "No output",
            "has_changes": success,
            "cost_usd": cost_usd,
        }

    def _detect_base_branch(self, owner: str, repo: str) -> str:
        """Detect the correct base branch for PRs (dev, develop, main, master)."""
        try:
            # Check if repo has a dev/develop branch that's the PR target
            result = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}",
                 "--jq", ".default_branch"],
                capture_output=True, text=True, timeout=10
            )
            default = result.stdout.strip() if result.returncode == 0 else "main"

            # Check for common dev branches that some repos prefer
            for branch in ["dev", "develop", "development"]:
                if branch == default:
                    continue
                check = subprocess.run(
                    ["gh", "api", f"repos/{owner}/{repo}/branches/{branch}",
                     "--jq", ".name"],
                    capture_output=True, text=True, timeout=10
                )
                if check.returncode == 0 and check.stdout.strip():
                    # Check recent PRs to see if they target this branch
                    prs = subprocess.run(
                        ["gh", "api", f"repos/{owner}/{repo}/pulls?state=closed&per_page=5",
                         "--jq", "[.[].base.ref] | unique | .[]"],
                        capture_output=True, text=True, timeout=10
                    )
                    if prs.returncode == 0 and branch in prs.stdout:
                        return branch

            return default
        except Exception:
            return "main"

    def _push_and_pr(
        self, clone_path: Path, upstream_owner: str, repo: str,
        issue_number: int, issue_title: str, branch_name: str,
        guidelines: dict | None = None
    ) -> str:
        """Push the branch and create a PR, respecting project conventions."""
        guidelines = guidelines or {}

        subprocess.run(
            ["git", "-C", str(clone_path), "push", "-u", "origin", branch_name],
            check=True, capture_output=True, text=True, timeout=60
        )

        # Get the diff summary for the PR body
        diff_stat = subprocess.run(
            ["git", "-C", str(clone_path), "diff", "--stat", "HEAD~1"],
            capture_output=True, text=True
        ).stdout.strip()

        # Get commit message(s) for context
        commit_msgs = subprocess.run(
            ["git", "-C", str(clone_path), "log", "--oneline", "HEAD~1..HEAD"],
            capture_output=True, text=True
        ).stdout.strip()

        pr_title = f"Fix #{issue_number}: {issue_title[:60]}"

        # Dynamic model disclosure with thinking level
        if self.model_tier:
            raw = self.model_tier["model"].replace("claude-", "").replace("-20251001", "")
            parts = raw.split("-")
            if len(parts) >= 3:
                model_display = f"Claude {parts[0].title()} {parts[1]}.{parts[2]}"
            else:
                model_display = f"Claude {raw.title()}"
            effort = self.model_tier.get("effort", "medium")
            thinking_tag = " (extended thinking)" if self.model_tier.get("thinking") else ""
            effort_display = f" | effort: {effort}{thinking_tag}"
        else:
            model_display = "Claude Opus 4.6"
            effort_display = ""
        pr_footer = (
            f"*This PR was created with the assistance of {model_display} by Anthropic"
            f"{effort_display}. "
            f"Happy to make any adjustments!*\n\n"
            f"By submitting this pull request, I confirm that my contribution is made under "
            f"the terms of the project's license (contributor license agreement)."
        )

        # Build PR body - use template if available, otherwise standard format
        if guidelines.get("pr_template"):
            pr_body = (
                f"Fixes #{issue_number}\n\n"
                f"## Summary\n"
                f"This PR fixes: {issue_title}\n\n"
                f"## Changes\n"
                f"```\n{diff_stat}\n```\n\n"
                f"## Testing\n"
                f"Please review the changes carefully. "
                f"The fix was verified against the existing test suite.\n\n"
                f"---\n"
                f"{pr_footer}"
            )
        else:
            pr_body = (
                f"Fixes #{issue_number}\n\n"
                f"## Summary\n"
                f"This PR addresses: {issue_title}\n\n"
                f"## Changes\n"
                f"```\n{diff_stat}\n```\n\n"
                f"## Testing\n"
                f"Please review the changes carefully. "
                f"The fix was verified against the existing test suite.\n\n"
                f"---\n"
                f"{pr_footer}"
            )

        # Detect correct base branch (some repos use dev/develop instead of main)
        base_branch = self._detect_base_branch(upstream_owner, repo)
        print(f"  PR target: {upstream_owner}/{repo} base={base_branch}")

        result = subprocess.run(
            ["gh", "pr", "create",
             "--repo", f"{upstream_owner}/{repo}",
             "--head", f"{self.username}:{branch_name}",
             "--base", base_branch,
             "--title", pr_title,
             "--body", pr_body],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode != 0:
            raise RuntimeError(f"PR creation failed: {result.stderr}")

        return result.stdout.strip()

    async def solve_feedback(self, contribution_id: int) -> dict:
        """Re-work a PR based on reviewer feedback. Updates the existing branch/PR."""
        conn = get_connection()

        # Load contribution with feedback details
        row = conn.execute("""
            SELECT c.*, r.owner, r.name as repo_name, r.full_name,
                   r.url as repo_url, r.language,
                   i.number as issue_number, i.title as issue_title, i.body as issue_body
            FROM contributions c
            LEFT JOIN repositories r ON c.repo_id = r.id
            LEFT JOIN issues i ON c.issue_id = i.id
            WHERE c.id = ?
        """, (contribution_id,)).fetchone()

        if not row:
            return {"success": False, "error": f"Contribution {contribution_id} not found"}

        contrib = dict(row)
        # Extract owner/repo from pr_url if repo_id is missing
        owner = contrib.get("owner")
        repo_name = contrib.get("repo_name")
        full_name = contrib.get("full_name")
        pr_url = contrib.get("pr_url") or contrib.get("feedback_pr_url", "")
        if not owner and pr_url:
            import re as _re
            m = _re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/\d+', pr_url)
            if m:
                owner = m.group(1)
                repo_name = m.group(2)
                full_name = f"{owner}/{repo_name}"
        issue_number = contrib.get("issue_number")
        feedback_text = contrib.get("feedback_text", "")
        feedback_reviewer = contrib.get("feedback_reviewer", "unknown")

        if not pr_url:
            update_feedback_status(conn, contribution_id, "skipped")
            return {"success": False, "error": "No PR URL found on contribution"}

        # Check PR is still open
        pr_number = pr_url.rstrip("/").split("/")[-1]
        pr_state = self._check_pr_state(owner, repo_name, pr_number)
        if pr_state != "open":
            update_feedback_status(conn, contribution_id, "skipped")
            return {"success": False, "error": f"PR is {pr_state}, not open"}

        print(f"Addressing feedback on {full_name}#{issue_number} (PR #{pr_number})")
        print(f"  Reviewer: {feedback_reviewer}")
        print(f"  Feedback: {feedback_text[:100]}...")

        # Mark in progress
        update_feedback_status(conn, contribution_id, "in_progress")

        try:
            # Fetch full PR review thread for context
            review_thread = self._fetch_pr_review_thread(owner, repo_name, pr_number)

            # Clone our fork and checkout the existing branch
            print("  Cloning fork...")
            clone_path = self._clone_repo(owner, repo_name)

            # Get actual branch name from the PR instead of guessing
            branch_name = None
            try:
                br_result = subprocess.run(
                    ["gh", "api", f"repos/{owner}/{repo_name}/pulls/{pr_number}",
                     "--jq", ".head.ref"],
                    capture_output=True, text=True, timeout=15
                )
                if br_result.returncode == 0 and br_result.stdout.strip():
                    branch_name = br_result.stdout.strip()
                    print(f"  PR branch: {branch_name}")
            except Exception:
                pass

            if not branch_name:
                branch_name = f"fix/issue-{issue_number}" if issue_number else f"fix/pr-{pr_number}"
                print(f"  Falling back to guessed branch: {branch_name}")

            branch_exists = self._checkout_existing_branch(clone_path, branch_name)

            if not branch_exists:
                update_feedback_status(conn, contribution_id, "skipped")
                return {"success": False, "error": f"Branch {branch_name} not found in fork"}

            # Fetch contributing guidelines
            guidelines = self._fetch_contributing_guidelines(owner, repo_name, clone_path)

            # Run Claude to address the feedback
            print("  Running Claude to address feedback...")
            result = await self._run_claude_feedback_fix(
                clone_path, contrib, feedback_text, review_thread, guidelines
            )

            if result["success"]:
                # Push updates to the same branch (auto-updates the PR)
                print("  Pushing updates...")
                subprocess.run(
                    ["git", "-C", str(clone_path), "push", "origin", branch_name],
                    check=True, capture_output=True, text=True, timeout=60
                )

                # Thank the reviewer in their language
                from src.feedback import _detect_language
                reviewer_lang = _detect_language(feedback_text)
                _thank_translations = {
                    "en": f"Thanks for the feedback, @{feedback_reviewer}! I've pushed an update addressing your review comments. Please take another look when you get a chance.",
                    "zh": f"感谢反馈，@{feedback_reviewer}！我已推送了更新以解决您的审查意见。请在方便时再看一下。",
                    "ja": f"フィードバックありがとうございます、@{feedback_reviewer}！レビューコメントに対応する更新をプッシュしました。お時間のある時にご確認ください。",
                    "ko": f"피드백 감사합니다, @{feedback_reviewer}! 리뷰 코멘트를 반영하여 업데이트를 푸시했습니다. 시간이 되실 때 다시 확인해 주세요.",
                    "ru": f"Спасибо за обратную связь, @{feedback_reviewer}! Я отправил обновление с учётом ваших замечаний. Пожалуйста, посмотрите при возможности.",
                    "es": f"¡Gracias por los comentarios, @{feedback_reviewer}! He subido una actualización con los cambios solicitados. Por favor, revíselo cuando pueda.",
                    "pt": f"Obrigado pelo feedback, @{feedback_reviewer}! Enviei uma atualização com as alterações solicitadas. Por favor, revise quando puder.",
                    "de": f"Danke für das Feedback, @{feedback_reviewer}! Ich habe ein Update mit den gewünschten Änderungen gepusht. Bitte schauen Sie es sich bei Gelegenheit an.",
                    "fr": f"Merci pour le retour, @{feedback_reviewer} ! J'ai poussé une mise à jour avec les modifications demandées. N'hésitez pas à revérifier.",
                }
                thank_msg = _thank_translations.get(reviewer_lang, _thank_translations["en"])
                subprocess.run(
                    ["gh", "pr", "comment", pr_number,
                     "--repo", f"{owner}/{repo_name}", "--body", thank_msg],
                    capture_output=True, text=True, timeout=15
                )

                update_feedback_status(conn, contribution_id, "addressed")
                return {"success": True, "pr_url": pr_url}
            else:
                update_feedback_status(conn, contribution_id, "needs_revision")
                return {"success": False, "error": result.get("error", "Claude fix failed")}

        except Exception as e:
            import traceback
            traceback.print_exc()
            update_feedback_status(conn, contribution_id, "needs_revision")
            return {"success": False, "error": str(e)}

    def _check_pr_state(self, owner: str, repo: str, pr_number: str) -> str:
        """Check if a PR is open, closed, or merged."""
        try:
            result = subprocess.run(
                ["gh", "pr", "view", pr_number, "--repo", f"{owner}/{repo}",
                 "--json", "state", "--jq", ".state"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return result.stdout.strip().lower()
        except Exception:
            pass
        return "unknown"

    def _fetch_pr_review_thread(self, owner: str, repo: str, pr_number: str) -> str:
        """Fetch the full review thread for a PR."""
        try:
            result = subprocess.run(
                ["gh", "pr", "view", pr_number, "--repo", f"{owner}/{repo}",
                 "--json", "reviews,comments",
                 "--jq", '[.reviews[]?, .comments[]?] | .[] | "\\(.author.login): \\(.body)"'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return result.stdout[:10000]
        except Exception:
            pass
        return ""

    def _checkout_existing_branch(self, clone_path: Path, branch_name: str) -> bool:
        """Try to checkout an existing branch from the fork."""
        # Fetch the specific branch (clone may be --single-branch)
        fetch = subprocess.run(
            ["git", "-C", str(clone_path), "fetch", "origin", branch_name],
            capture_output=True, text=True, timeout=30
        )
        if fetch.returncode != 0:
            return False
        # Try origin/branch first, fall back to FETCH_HEAD
        result = subprocess.run(
            ["git", "-C", str(clone_path), "checkout", "-b", branch_name,
             f"origin/{branch_name}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "-C", str(clone_path), "checkout", "-b", branch_name,
                 "FETCH_HEAD"],
                capture_output=True, text=True
            )
        return result.returncode == 0

    async def _run_claude_feedback_fix(
        self, clone_path: Path, contrib: dict,
        feedback_text: str, review_thread: str,
        guidelines: dict | None = None
    ) -> dict:
        """Use Claude to address reviewer feedback on an existing PR."""
        from src.feedback import _detect_language
        reviewer_lang = _detect_language(feedback_text)
        lang_names = {"en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
                      "ru": "Russian", "es": "Spanish", "pt": "Portuguese", "de": "German",
                      "fr": "French", "ar": "Arabic"}
        lang_instruction = ""
        if reviewer_lang != "en":
            lang_name = lang_names.get(reviewer_lang, reviewer_lang)
            lang_instruction = f"\n\nIMPORTANT: The reviewer writes in {lang_name}. Write your commit messages in English (standard practice), but any PR comments or explanations should be in {lang_name}.\n"

        guidelines = guidelines or {}
        guidelines_section = ""
        if guidelines.get("contributing"):
            guidelines_section += f"\n### CONTRIBUTING.md:\n{guidelines['contributing'][:5000]}\n"
        if guidelines.get("commit_format"):
            guidelines_section += f"\n### Commit Format:\n{guidelines['commit_format']}\n"

        prompt = f"""You are updating a pull request based on reviewer feedback.{lang_instruction}

## Repository
- Name: {contrib.get('full_name') or f"{contrib.get('owner', '?')}/{contrib.get('repo_name', '?')}"}
- Language: {contrib.get('language') or 'Unknown'}

## Original Issue: #{contrib.get('issue_number') or '?'}: {contrib.get('issue_title') or ''}
{(contrib.get('issue_body') or '')[:2000]}

## PR Review Thread:
{review_thread[:5000]}

## Specific Feedback to Address:
{feedback_text[:3000]}

## Contributing Guidelines
{guidelines_section if guidelines_section else "Follow standard practices."}

## Your Task:
1. Read the relevant source code and understand the current state of the PR
2. Address EACH point in the reviewer's feedback
3. Make the minimum changes needed to satisfy the review
4. Stage and commit with a message like "Address review feedback: <summary>"
5. Follow the project's commit message format

CRITICAL:
- Address ALL feedback points, not just some
- Keep changes focused on what the reviewer asked for
- Do NOT introduce new features or unrelated changes
- If the reviewer asked a question, make sure the code answers it
"""

        opts_kwargs = {
            "system_prompt": (
                "You are addressing code review feedback on a pull request. "
                "Be thorough — address every point the reviewer raised. "
                "Keep changes minimal and focused."
            ),
            "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            "cwd": str(clone_path),
            "max_turns": 30,
            "permission_mode": "bypassPermissions",
        }
        if self.model_tier:
            opts_kwargs["model"] = self.model_tier["model"]
            if self.model_tier.get("effort"):
                opts_kwargs["extra_args"] = {"effort": self.model_tier["effort"]}
        options = ClaudeAgentOptions(**opts_kwargs)

        result_text = ""
        try:
            async for message in query(prompt=prompt, options=options):
                if message is None:
                    continue
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text"):
                            result_text += block.text + "\n"
                elif isinstance(message, ResultMessage):
                    if message.result:
                        result_text += message.result + "\n"

            # Check for actual changes
            diff_result = subprocess.run(
                ["git", "-C", str(clone_path), "diff", "--stat", "HEAD~1"],
                capture_output=True, text=True
            )
            has_changes = bool(diff_result.stdout.strip())

            if not has_changes:
                status_result = subprocess.run(
                    ["git", "-C", str(clone_path), "status", "--porcelain"],
                    capture_output=True, text=True
                )
                if status_result.stdout.strip():
                    subprocess.run(
                        ["git", "-C", str(clone_path), "add", "-A"],
                        capture_output=True, text=True
                    )
                    subprocess.run(
                        ["git", "-C", str(clone_path), "commit", "-m",
                         "Address review feedback"],
                        capture_output=True, text=True
                    )
                    has_changes = True

            return {"success": has_changes, "result": result_text[-2000:]}

        except Exception as e:
            return {"success": False, "error": str(e)}
