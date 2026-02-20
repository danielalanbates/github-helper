# Do-Good GitHub Helper

An autonomous agent that finds and fixes bugs on social-good open-source projects.

## Philosophy

This agent ranks projects by **impact, need, and kindness**. It prioritizes issues systematically, escalates intelligently between Claude models (sonnet → opus), and respects community values and project guidelines.

**Core principles:**
- **Social good first** — Only contribute to wholesome, impactful projects
- **Deep relationships > quantity** — Build lasting presence in key repos instead of spray-and-pray
- **Graceful, respectful interactions** — Always disclose AI assistance, respond to feedback kindly, exit gracefully when not wanted
- **Efficiency with oversight** — Run 24/7, but leave an audit trail for human review
- **Ethical by design** — Blocks NSFW, gambling, malware, and other harmful content automatically

## How It Works

1. **Repository Scanning** — Discovers social-good projects on GitHub
2. **Intelligent Ranking** — Scores repos by impact, maintenance status, and alignment with values
3. **Issue Prioritization** — Analyzes open issues, prioritizes by difficulty and impact
4. **Model Selection** — Starts with Claude Sonnet, escalates to Opus for complex bugs
5. **Fix & PR Submission** — Solves bugs, creates pull requests with full disclosure
6. **Feedback Loop** — Monitors reviews, responds to feedback, learns from interactions
7. **Persistence** — Focuses on key repos, builds contributor reputation over time

## Architecture

```
data/
  github_helper.db          # SQLite DB (repos, issues, contributions, feedback)

src/
  main.py                   # Entry point
  scanner.py                # Repository discovery & ranking
  solver.py                 # Issue solving with Claude
  feedback.py               # GitHub notification monitoring & response
  concurrency.py            # Multi-agent coordination
  db.py                     # Database queries
  config.py                 # Configuration & blocked topics

CLAUDE.md                    # Detailed agent instructions
```

## Database Schema

**Core tables:**
- `repositories` — Ranked repos with impact scores
- `issues` — Parsed GitHub issues
- `contributions` — PR submissions and outcomes
- `agent_runs` — Activity log for concurrent agents
- `issue_claims` — Multi-agent coordination (2-hour claims)
- `pr_reviews` — Feedback from maintainers
- `learned_patterns` — Lessons learned from interactions
- `repo_blacklist` — Repos that don't want our help

**Index:** Issues ranked by `priority_score DESC`

## Language Support

Only fixes bugs involving: **Python, TypeScript, JavaScript, SQL, Bash, YAML, Lua**

Other languages (Rust, Go, C/C++, Java, Ruby, PHP, etc.) are skipped to focus depth.

## Limitations & Philosophy

This is **Daniel Bates' specific implementation** of an AI contribution agent. The core architecture, project-weighting philosophy, and interaction strategy are intentional design decisions.

**You are welcome to:**
- Fork this repo and run your own version
- Modify it for your own use case
- Learn from the approach

**Please don't:**
- Modify the core ranking algorithm or weights without clear justification
- Strip out the AI disclosure in PR footers
- Remove the feedback loop or ethical filters
- Change the philosophy without discussion

If you want to improve this project or have ideas, please open an issue or PR with your suggestions, and we'll discuss!

## Setup

1. **Clone the repo:**
   ```bash
   git clone https://github.com/danielalanbates/github-helper.git
   cd github-helper
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables** in `.env`:
   ```
   GITHUB_TOKEN=your_github_token
   ANTHROPIC_API_KEY=your_anthropic_key
   ```

4. **Initialize the database:**
   ```bash
   python src/main.py --init-db
   ```

5. **Run the agent:**
   ```bash
   python src/main.py --continuous
   ```

## Configuration

See `src/config.py` for:
- Opus budget caps per repo
- Blocked topics (NSFW, malware, occult, gambling, etc.)
- Language restrictions
- Repository freshness requirements (30-day activity threshold)

## Audit Trail

All agent activity is logged to:
- **SQLite** (`data/github_helper.db`) — structured logs
- **Log file** (`Claude Agent - Do-Good GitHub Helper.md`) — human-readable event log
- **Git commits** — all PRs are traceable

## License

MIT License. See `LICENSE` file for details.

---

**Questions?** Open an issue or reach out to **daniel@batesai.org**.

**Want to sponsor this work?** Contact daniel@batesai.org.