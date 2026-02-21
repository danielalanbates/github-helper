<div align="center">

# dogood

**Autonomous AI agent that finds and fixes bugs across open source — 100+ PRs/day**

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![PRs Submitted](https://img.shields.io/badge/PRs%20submitted-167+-green.svg)](#stats)
[![Buy Me a Coffee](https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg)](https://buymeacoffee.com/danielbates)

<table>
<tr>
<td align="center"><b>167+</b><br>PRs Submitted</td>
<td align="center"><b>127</b><br>PRs in One Day</td>
<td align="center"><b>58+</b><br>Repos Contributed To</td>
<td align="center"><b>1.3M+</b><br>Combined Repo Stars</td>
</tr>
</table>

</div>

## What is dogood?

dogood is an autonomous AI agent system that discovers high-impact open-source projects on GitHub, analyzes their bugs using Claude, and submits pull requests to fix them — running 4 concurrent agents, 24 hours a day, 7 days a week.

It has contributed to **Vue.js** (210k stars), **oh-my-zsh** (185k stars), **AutoGPT** (182k stars), **Hugging Face Transformers** (157k stars), **Langflow** (145k stars), **Dify** (130k stars), and dozens more. Every PR includes full AI disclosure. Every interaction is respectful.

## See it in action

```
$ dogood factory --max-concurrent 4 --min-stars 1000

Agent Factory starting: max_concurrent=4, min_stars=1000
  Agent a3f2: vuejs/vue#13319 [sonnet] — Fix reactivity edge case in v-model
  Agent a3f2: PR created — https://github.com/vuejs/vue/pull/13319
  Agent b7d1: huggingface/transformers#44191 [sonnet] — Fix tokenizer cache miss
  Agent b7d1: PR created — https://github.com/huggingface/transformers/pull/44191
  Agent c9e4: langflow-ai/langflow#11812 [sonnet] — Fix MCP tool base64 handling
  Agent c9e4: PR created — https://github.com/langflow-ai/langflow/pull/11851
  Agent d2a8: FlowiseAI/Flowise#5785 [sonnet] — Fix denied host policy error
  Agent d2a8: PR created — https://github.com/FlowiseAI/Flowise/pull/5812
```

## Features

- **Multi-Agent Factory** — 4 concurrent AI agents running in parallel, 24/7
- **Intelligent Ranking** — scores repos by social impact (30%), maintenance need (25%), community kindness (25%), and popularity (20%)
- **Multi-Tier Models** — starts with Sonnet for straightforward bugs, escalates to Opus for complex ones
- **Ethics-First** — blocks NSFW/gambling/malware, respects anti-AI policies, detects CLAs
- **Feedback Loop** — reads PR reviews, responds to comments, revises code automatically
- **Depth Over Breadth** — builds contributor reputation by focusing on key repos
- **Beginner-Friendly** — never touches "good first issue" labels (those are for humans)
- **Full Audit Trail** — SQLite database, structured logs, Telegram notifications

## Quick Start

```bash
git clone https://github.com/danielalanbates/dogood.git
cd dogood
pip install -e .
cp .env.example .env   # add GITHUB_TOKEN and ANTHROPIC_API_KEY
dogood factory --max-concurrent 4
```

## Architecture

```
src/
├── cli.py             # Typer CLI — scan, rank, solve, factory, stats
├── orchestrator.py    # Multi-agent factory with rate limit coordination
├── solver.py          # Clone, branch, fix, commit, PR — the core loop
├── scanner.py         # GitHub API + GraphQL repo/issue discovery
├── ranker.py          # 4-factor weighted scoring system
├── feedback.py        # PR review monitoring + sentiment analysis
├── model_selector.py  # Complexity scoring → model tier selection
├── config.py          # Ethics filters, blocked topics, CLA detection
├── db.py              # SQLite schema, migrations, queries
├── concurrency.py     # Connection pool, rate limiter, file locking
├── telegram.py        # Real-time notifications + two-way chat
└── rate_coordinator.py # Cross-agent rate limit state
```

## How the Ranking Works

Every discovered repo gets a composite score from four factors:

| Factor | Weight | What it measures |
|--------|--------|-----------------|
| **Social Impact** | 30% | Nonprofit, climate, healthcare, accessibility, education topics |
| **Need** | 25% | Open issues / contributor ratio — who needs help most |
| **Kindness** | 25% | Has CONTRIBUTING.md, code of conduct, issue templates |
| **Popularity** | 20% | Stars + forks (log-normalized) |

Repos are filtered through an ethics layer that blocks NSFW, gambling, malware, and occult content.

## Ethics & Safety

- Detects and respects CLA/DCO requirements — skips repos that require agreement signing
- Detects anti-AI contribution policies — never contributes where AI isn't welcome
- Full AI disclosure in every PR footer
- Never claims "good first issue" bugs — those exist for human newcomers
- Gracefully exits repos where maintainers ask the agent to stop
- Blocks contributions to harmful content categories
- Sentiment analysis on review comments — detects hostile responses

## Configuration

Core settings in `src/config.py`. Model tiers in `tiers.json` (hot-reloadable — no restart needed). API keys in `.env`.

```bash
dogood scan --min-stars 1000        # discover repos
dogood rank                          # score and sort them
dogood solve --issue-id 42           # fix a single issue
dogood factory --max-concurrent 4    # run the full agent factory
dogood stats                         # view contribution metrics
```

## Stats

| Metric | Value |
|--------|-------|
| Total PRs submitted | 167+ |
| Single-day record | 127 PRs |
| Repos contributed to | 58+ |
| Combined repo stars | 1.3M+ |
| Languages supported | Python, TypeScript, JavaScript, SQL, Bash, YAML, Lua |
| Highest-starred repo | Vue.js (210k stars) |

## Contributing

Fork it, make it your own. The ethics filters and AI disclosure are intentional — please don't strip them.

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

Built by [Daniel Bates](https://github.com/danielalanbates) | [Buy Me a Coffee](https://buymeacoffee.com/danielbates) | [batesai.org](https://batesai.org)

</div>
