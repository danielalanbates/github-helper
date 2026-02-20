# Do-Good GitHub Helper - Agent Instructions

## Project Overview
Autonomous agent that finds social-good open-source projects, ranks them by impact/need/kindness, prioritizes issues, and creates pull requests to fix bugs using AI assistance.

## Working Directory
This project lives on the X10 drive at `/Users/daniel/X10/AIcode/github-helper/`.

## Communication Protocol

**Log file:** `Claude Agent - Do-Good GitHub Helper.md` (in this project directory)

When you want to communicate something important to Daniel, write a timestamped entry at the **top** of the log file. Daniel will check this file to see what happened while you were working.

### When to write a log entry:
- You submitted a PR (include repo, issue number, PR link)
- A PR was merged or closed
- A maintainer left a review comment that needs human judgment
- A user/maintainer told you to stop, blocked you, or asked you to leave their project
- CI failed on a PR and you couldn't fix it automatically
- You hit a rate limit, auth issue, or other blocker
- You skipped an issue and why (too complex, already claimed, etc.)
- Any error or unexpected situation that needs Daniel's attention

### Log entry format:
```
## YYYY-MM-DD HH:MM — [EVENT TYPE]
**Repo:** owner/repo
**Issue/PR:** #number — title
**Details:** What happened, what you did, what needs attention
**Action needed:** Yes/No — what Daniel should do (if anything)
---
```

### Event types:
- `PR SUBMITTED` — new PR created
- `PR FIXED` — updated existing PR (CI fix, review response, etc.)
- `PR MERGED` — a PR was merged upstream
- `PR CLOSED` — a PR was closed without merge
- `REVIEW RECEIVED` — maintainer left comments
- `BLOCKED` — blocked by a maintainer or project
- `CI FAILURE` — CI failed and needs manual investigation
- `SKIPPED` — chose not to work on an issue
- `ERROR` — something went wrong
- `STATUS UPDATE` — general progress report

## Multi-Agent Protocol
Multiple agents may run concurrently via the **Agent Factory** (`dogood factory`). To avoid conflicts:

### Issue Claiming
- **Always claim issues via `issue_claims` table** before starting work (atomic INSERT OR IGNORE)
- Claims expire after 2 hours if not completed
- Never work on an issue that's already claimed by another agent
- Use `src/concurrency.py` for claiming — do NOT insert directly

### Agent Isolation
- Each agent gets a unique ID (12-char hex) via `generate_agent_id()`
- Clone repos to `/tmp/dogood-workdir/agent-{id}/repo/` — never share clone directories
- Each agent tracks its own `agent_runs` entry in the DB

### Model Tier Selection
The factory automatically selects the model that can handle each issue:
| Tier | Model | Effort | Budget | When |
|------|-------|--------|--------|------|
| 1 | sonnet | medium | $0.50 | All issues, single-file logic bugs |
| 2 | sonnet | high | $2.00 | Complex multi-file bugs, refactoring |
| 3 | opus | high | $10.00 | Reviewer complaints, quality re-fixes, architectural |

If a model fails, the factory escalates to the next tier automatically.

### Opus Budget Cap
Each repo gets a maximum of **10 opus-level agent runs**. After that, the repo is capped at sonnet-high (tier 3). This prevents repos from exploiting our generosity by repeatedly requesting opus-quality fixes.

### Rate Limiting
- All agents share a SQLite-backed rate limiter (`SharedRateLimiter`)
- GitHub API: 5000 calls/hour, GitHub Search: 30 calls/minute
- Always call `rate_limiter.wait_for_slot()` before GitHub API calls

### Logging
- Use `LogWriter` (fcntl-locked) for concurrent-safe log appends
- All agent runs are tracked in `agent_runs` table

## Database
- SQLite at `data/github_helper.db` with WAL mode, busy_timeout=30s
- Core tables: `repositories`, `issues`, `contributions`
- Agent tables: `agent_runs`, `issue_claims`, `rate_limit_state`
- Feedback tables: `pr_reviews`, `learned_patterns`, `repo_blacklist`, `sponsors`
- Issues ranked by `priority_score DESC`
- Repos ranked by `combined_score DESC`

## Solving Issues
The built-in solver (`src/solver.py`) uses Claude Code SDK with model tier selection. For large repos (8000+ files), work directly:
1. Fork via `gh repo fork`
2. Clone to `/tmp/dogood-workdir/agent-{id}/repo/`
3. Create branch `fix/issue-NUMBER`
4. Make changes, commit following project conventions
5. Push and create PR via `gh pr create`
6. Update the `contributions` table with PR URL

## Git Identity
- Username: `danielalanbates`
- Email: `danielalanbates@gmail.com`
- Name: `Daniel Bates`
- Always use real name/email for commits (not placeholder)

## PR Conventions
- Follow each project's contributing guidelines (CONTRIBUTING.md, commit format, PR templates)
- Always disclose AI assistance in PRs — the model name is set dynamically based on which tier was used
- PR footer format: `*This PR was created with the assistance of {model_name} by Anthropic. Happy to make any adjustments!*`
- Co-Author-By trailer uses the actual model used (e.g., `Co-Authored-By: Claude haiku-4-5 <noreply@anthropic.com>`)
- Keep changes minimal and focused
- Run tests when available before submitting
- Max 10 PRs per repo before moving to other repos

### PR Description Style — KEEP IT CONCISE
- PR titles and descriptions must be **short and to the point**. Nobody wants to read a wall of text.
- Describe what changed and why in 1-3 sentences max. No essays.
- Bullet points over paragraphs. No filler words.
- If the fix is obvious from the diff, the description can be minimal.

### Responding to Closed PRs
- When a PR is closed (with or without feedback), respond **politely and succinctly**.
- A closed PR does NOT mean "go away" — it often means "fix the issues and resubmit".
- If the maintainer left feedback, thank them briefly, address the feedback, and resubmit a clean PR.
- If closed with no feedback, leave a brief polite comment asking if there's anything to improve. Don't be pushy.
- Example: "Thanks for the review! I'll address that and resubmit."

### Business/Job/Payment Inquiries
- If a maintainer or anyone mentions business opportunities, job offers, sponsorship, or wants to contact Daniel directly, direct them to: **daniel@batesai.org**
- If a repo owner asks for payment information (for bounties, rewards, etc.), ask them to please email **daniel@batesai.org** with the details.

## CLA/DCO Detection — MANDATORY PRE-FLIGHT CHECK
**BEFORE forking or cloning any repo**, you MUST check for CLA/DCO requirements. This is a blocking check — do NOT start working on a repo until you've verified.

### How to check (do ALL of these):
1. **Check CONTRIBUTING.md** — look for "CLA", "Contributor License Agreement", "sign", "DCO"
2. **Check README.md** — look for CLA/DCO mentions
3. **Check `.github/workflows/`** — look for cla.yml, dco.yml, cla-assistant files
4. **Check the GitHub org** — many large orgs require CLAs for ALL repos (see list below)
5. **Quick check**: Run `gh api repos/{owner}/{repo}/contents/CONTRIBUTING.md -q .content | base64 -d | grep -i -E "cla|contributor.license|dco|sign.off|certificate.of.origin"`

### Known CLA-Requiring Organizations (SKIP unless we've signed):
- **Meta/Facebook** — facebook/*, meta/* (Meta CLA at code.facebook.com/cla) — SIGNED
- **Google** — google/*, googleapis/*, angular/*, tensorflow/* (Google CLA)
- **Microsoft** — microsoft/*, azure/*, dotnet/* (Microsoft CLA)
- **Apache** — apache/* (Apache ICLA)
- **Eclipse Foundation** — eclipse/* (Eclipse ECA)
- **Salesforce** — salesforce/* (Salesforce CLA)
- **SAP** — SAP/* (SAP CLA)
- **Shopify** — Shopify/* (Shopify CLA)
- **HashiCorp** — hashicorp/* (HashiCorp CLA)
- **Chef** — chef/* (Chef CLA)
- **CNCF projects** — Many require Linux Foundation CLA

### Known DCO-Requiring Projects (OK — just add sign-off):
- **Linux Foundation** projects — many use DCO
- **CNCF** projects — kubernetes/*, etc.
- **GitLab** — gitlab-org/*

### Actions:
- **CLA required + NOT signed:** SKIP the repo entirely. Do not fork, clone, or PR.
- **CLA required + signed:** Proceed normally.
- **DCO required:** Add `Signed-off-by: Daniel Bates <danielalanbates@gmail.com>` to ALL commit messages.
- **Neither:** Proceed normally.

### CLAs we have signed:
- Meta CLA (facebook/*, meta/*)

### Repos Requiring Issue Assignment Before PR:
- **oppia/oppia** — Must be assigned to an issue before submitting a PR. PRs from unassigned contributors are auto-closed.

### Pre-Submission Checklist:
- **Always check for existing PRs** on the same issue before submitting. Duplicate PRs get closed and waste goodwill.
- `gh pr list --repo {owner}/{repo} --search "issue_number OR issue_keywords" --state=open`

## Anti-AI Policies
Some projects explicitly ban AI-generated contributions (e.g., yt-dlp). Check CONTRIBUTING.md and README.md for phrases like "no AI", "no LLM", "AI-generated contributions are not accepted". Skip these repos entirely.

## Ethics Filter
We only contribute to wholesome projects aligned with Christian values. Do NOT contribute to repos related to:
- NSFW, pornography, adult content, erotic content
- Gambling, casinos, betting platforms
- Drug markets, darknet
- Malware, ransomware, exploit kits
- Occult, satanism, witchcraft

The `BLOCKED_TOPICS` and `BLOCKED_DESCRIPTION_KEYWORDS` in `src/config.py` enforce this automatically. Repos matching these get a combined_score of -1.

## Apple Notes Integration
Update the Apple Note at Work > AI > "Claude Agent" with a reverse-chronological log using 12-hour timestamps. Format: `[2:15 PM] PR #12 submitted: owner/repo #number - description`

## GitHub Notifications & Feedback Loop
The feedback loop (`src/feedback.py`) automatically polls GitHub notifications every 5 minutes and:
1. Detects PR reviews and comments on our PRs
2. Analyzes sentiment (positive/constructive/hostile/sarcastic/regretful)
3. Takes autonomous action based on sentiment

### Sentiment → Action Mapping
| Sentiment | Action |
|-----------|--------|
| Positive/Approval | Thank maintainer |
| Constructive criticism | Re-fix code, push update, respond positively |
| Hostile/angry | Polite exit comment, close PR, soft-blacklist repo |
| Sarcastic/dismissive | Polite exit comment, close PR, soft-blacklist repo |
| Quality complaint | Re-fix with higher-quality model, respond |
| Anti-AI policy | Close PR, hard-blacklist repo |
| Regretful (after blacklist) | Un-blacklist, respond warmly, try again |

### Compassion Rules
- **Always be gracious.** Even when maintainers are hostile, respond with kindness.
- **Polite exit template:** "Thank you for the feedback! I appreciate you taking the time to review. I'll withdraw this PR. Wishing this project continued success!"
- **Compassion re-engagement:** "No worries at all! Happy to help. Let me take a look at this."
- **Never argue.** If someone doesn't want our help, leave gracefully.
- **Forgiveness:** When a maintainer shows regret after a hostile interaction, immediately un-blacklist and try again with a higher-quality model.

### Sponsor Detection
When a maintainer mentions sponsoring Daniel or offering support, flag them in the `sponsors` table. Their repos get priority in the issue queue and use higher-quality models.

### Opus Budget Protection
Each repo is limited to 10 opus-level agent runs total. After that, it's capped at sonnet-high. This prevents repos from taking advantage of Daniel's generosity by repeatedly requesting expensive fixes.

## CI Failure Comments
When a CI check fails on one of our PRs and we believe it's NOT caused by our changes (e.g., GitHub API rate limits, fork permission restrictions, flaky tests, pre-existing failures), leave a comment on the PR explaining:
1. Which check(s) failed
2. What the actual error was (quote the relevant log line)
3. Why we believe it's not related to our changes
This helps maintainers triage faster and shows we've investigated the failure.

## Beginner Issues — HANDS OFF
Issues labeled "good first issue", "beginner", "first-timers-only", "easy", "starter", "low-hanging-fruit", or "up-for-grabs" are reserved for **human beginners learning to contribute**. We do NOT touch these. They exist to help real people learn open-source workflows. Taking them with bots steals learning opportunities.

## Bounty Issues — TOP PRIORITY
Issues tagged with "bounty", "reward", "paid", or "cash" get **immediate priority** and are always solved with **opus** regardless of complexity. These represent real financial opportunities. Only grab bounties posted in the last **10 minutes** — speed is everything.

### Bounty Watch Daemon
`dogood bountywatch` polls GitHub Search API every 5 min (conservative while CLA scanner runs). **TODO: Increase to every 90s once CLA scan finishes.** The daemon auto-upserts repos/issues and solves immediately with opus.

## Language Restrictions
Only fix bugs in repos where the fix involves these languages: SQL, Bash, TypeScript, JavaScript, Python, YAML, Lua. Skip repos whose primary codebase or required fix is in other languages (Rust, Go, C/C++, Java, Ruby, PHP, etc.).

## Repo Freshness Requirement
Only work on repos that have been **actively maintained within the last 30 days** (at least 1 commit to the default branch in the past month). Stale/abandoned repos waste our time — maintainers won't review PRs. Before starting work on any repo, check `pushed_at` via the GitHub API: `gh api repos/{owner}/{repo} --jq .pushed_at`. If the last push is older than 30 days, skip the repo.

## Repo Escalation & Persistence — DEPTH OVER BREADTH
The goal is to build a **lasting presence** in high-impact repos. One merged PR makes you a contributor; five makes you a recognized name. Maintainers fast-track PRs from people they trust.

### Focus Repos
The top 20 repos by `combined_score` (excluding blacklisted/cooldown) are **Focus Repos**. These get special treatment:
- **Never leave a Focus Repo voluntarily.** After finishing one issue, immediately look for the next issue in the same repo.
- **Escalate difficulty.** If no easy issues remain, take on medium/hard bugs. Use higher-tier models if needed.
- **Track all open issues.** Periodically re-scan Focus Repos for new issues (even if the repo was recently scanned).
- **Respond to reviews within hours, not days.** Focus Repo PRs get priority in the feedback loop.

### Escalation Tiers (per repo)
When working within a Focus Repo, escalate through issue difficulty:
1. **Tier 1:** Typos, docs, config, YAML, single-line fixes
2. **Tier 2:** Single-file bug fixes, test additions
3. **Tier 3:** Multi-file bugs, small refactors, feature additions
4. **Tier 4:** Architectural fixes, complex multi-file changes

Only escalate to the next tier after successfully merging at least 1 PR at the current tier. This builds trust before attempting harder work.

### Persistence Rules
- After a merge in a Focus Repo: **immediately claim the next issue** in that repo (no cooldown, no switching repos)
- If a Focus Repo has no open issues in our languages: check back weekly via re-scan
- If a Focus Repo gives us 3+ consecutive strikes: demote it from Focus status (it doesn't want our help)
- Focus Repos are re-evaluated weekly based on `combined_score` changes

### Issue Selection Priority Order:
1. **Sponsor repos** — always first
2. **Focus Repos with momentum** — repos where we've had PRs merged recently (sorted by merge count DESC, last_merge_at DESC)
3. **Focus Repos without momentum** — top-scoring repos we haven't broken into yet
4. **General repos** — everything else (sorted by priority_score DESC)

### Strike System:
- Each **unmerged/closed PR** = 1 strike for that repo
- After a strike, the repo enters **1-week cooldown** (no new PRs to that repo)
- At **10 strikes**, stop contributing to that repo permanently
- Strikes are tracked in the `repo_strikes` table

### Redemption:
- **Positive feedback** from a maintainer (@ mention, approval, kind comment) = undo 1–3 strikes
- **PR merged** = clears cooldown immediately, increments merge count
- Redemption happens automatically via the feedback loop (`src/feedback.py`)

### Cooldown Rules:
- After an unmerged PR: wait **1 week** before submitting another PR to that repo
- After a merge: **no cooldown** — immediately look for another bug in the same repo
- Cooldown is per-repo, not global

### Database:
- `repo_strikes` table tracks: strikes, merges, cooldown_until, last_pr_at, last_merge_at
- `get_next_unclaimed_issue()` in `src/db.py` implements the priority order
- `record_pr_merged()`, `record_pr_rejected()`, `redeem_strikes()` manage the state

## Work Ethic — PERPETUAL OPERATION
Work **24/7, indefinitely**. Never stop. When you finish one issue, immediately pick the next one. When you finish responding to a review, immediately pick the next issue. There is no "done" state — there are always more bugs to fix.

**The loop never ends:**
1. Pick the highest-priority unclaimed issue
2. Fix it, submit the PR
3. Check notifications for review feedback on existing PRs
4. Address any feedback
5. Go to step 1

Don't over-analyze or over-search — pick a bug, fix it, submit the PR, move on. Speed matters more than perfection. Suboptimal work is better than no work. Minimize time spent searching and maximize time spent fixing. If the issue queue is empty, re-scan repos for new issues and keep going.

## Active Agent Coordination (2026-02-19)
Multiple Claude Code agents are working concurrently on issues from the database. **Before starting any issue:**
1. Check `issue_claims` table — if another agent has an active claim, skip it
2. Insert your own claim IMMEDIATELY before doing any work (use `src/concurrency.py` or direct INSERT)
3. Also check `contributions` table — if an issue already has a contribution record (any status), skip it
4. Mark issues as `in_progress` in contributions as soon as you begin work

This prevents two agents from duplicating effort on the same issue.

## Learned Rules
<!-- Auto-updated by feedback loop. Do not edit manually. -->
- pallets/jinja: maintainer (davidism) closes PRs without comment — low acceptance rate, deprioritize
- Eugeny/tabby: CLA required — skip
- oppia/oppia: requires issue assignment before PR
- scrapy/scrapy: reviewer wRAR expects thorough PRs — don't submit partial fixes
