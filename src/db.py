"""SQLite database schema and query functions."""

import sqlite3
import json
from src.config import DB_PATH, DATA_DIR
from src.utils import now_iso


def get_connection() -> sqlite3.Connection:
    """Get a database connection, creating DB and tables if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)
    # Run migrations for new tables/columns
    from src.migration import run_migrations
    run_migrations(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            github_id INTEGER UNIQUE,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            full_name TEXT UNIQUE,
            url TEXT,
            stars INTEGER DEFAULT 0,
            forks INTEGER DEFAULT 0,
            open_issues INTEGER DEFAULT 0,
            contributor_count INTEGER DEFAULT 0,
            description TEXT,
            topics TEXT DEFAULT '[]',
            language TEXT,
            license TEXT,
            has_contributing BOOLEAN DEFAULT 0,
            has_coc BOOLEAN DEFAULT 0,
            has_issue_templates BOOLEAN DEFAULT 0,
            popularity_score REAL DEFAULT 0,
            social_impact_score REAL DEFAULT 0,
            need_score REAL DEFAULT 0,
            kindness_score REAL DEFAULT 0,
            combined_score REAL DEFAULT 0,
            last_scanned TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            github_id INTEGER UNIQUE,
            repo_id INTEGER REFERENCES repositories(id),
            number INTEGER,
            title TEXT,
            body TEXT,
            labels TEXT DEFAULT '[]',
            state TEXT DEFAULT 'open',
            comments_count INTEGER DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            difficulty_score REAL DEFAULT 0,
            priority_score REAL DEFAULT 0,
            is_assigned BOOLEAN DEFAULT 0,
            UNIQUE(repo_id, number)
        );

        CREATE TABLE IF NOT EXISTS contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id INTEGER REFERENCES issues(id),
            repo_id INTEGER REFERENCES repositories(id),
            action TEXT,
            pr_url TEXT,
            pr_number INTEGER,
            status TEXT DEFAULT 'pending',
            details TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_repos_combined_score
            ON repositories(combined_score DESC);
        CREATE INDEX IF NOT EXISTS idx_repos_full_name
            ON repositories(full_name);
        CREATE INDEX IF NOT EXISTS idx_issues_repo_id
            ON issues(repo_id);
        CREATE INDEX IF NOT EXISTS idx_issues_priority
            ON issues(priority_score DESC);
        CREATE INDEX IF NOT EXISTS idx_issues_state
            ON issues(state);
        CREATE INDEX IF NOT EXISTS idx_contributions_status
            ON contributions(status);
    """)
    conn.commit()


def upsert_repository(conn: sqlite3.Connection, repo: dict) -> int:
    """Insert or update a repository. Returns the row id."""
    full_name = repo.get("full_name") or f"{repo['owner']}/{repo['name']}"
    topics = repo.get("topics", [])
    if isinstance(topics, list):
        topics = json.dumps(topics)

    cursor = conn.execute("""
        INSERT INTO repositories (
            github_id, owner, name, full_name, url, stars, forks,
            open_issues, contributor_count, description, topics,
            language, license, has_contributing, has_coc,
            has_issue_templates, pushed_at, last_scanned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(full_name) DO UPDATE SET
            github_id = COALESCE(excluded.github_id, repositories.github_id),
            stars = COALESCE(excluded.stars, repositories.stars),
            forks = COALESCE(excluded.forks, repositories.forks),
            open_issues = COALESCE(excluded.open_issues, repositories.open_issues),
            contributor_count = COALESCE(excluded.contributor_count, repositories.contributor_count),
            description = COALESCE(excluded.description, repositories.description),
            topics = CASE WHEN excluded.topics != '[]' THEN excluded.topics ELSE repositories.topics END,
            language = COALESCE(excluded.language, repositories.language),
            license = COALESCE(excluded.license, repositories.license),
            has_contributing = MAX(excluded.has_contributing, repositories.has_contributing),
            has_coc = MAX(excluded.has_coc, repositories.has_coc),
            has_issue_templates = MAX(excluded.has_issue_templates, repositories.has_issue_templates),
            pushed_at = COALESCE(excluded.pushed_at, repositories.pushed_at),
            last_scanned = excluded.last_scanned
    """, (
        repo.get("github_id"),
        repo.get("owner", full_name.split("/")[0]),
        repo.get("name", full_name.split("/")[1]),
        full_name,
        repo.get("url"),
        repo.get("stars", 0),
        repo.get("forks", 0),
        repo.get("open_issues", 0),
        repo.get("contributor_count", 0),
        repo.get("description"),
        topics,
        repo.get("language"),
        repo.get("license"),
        repo.get("has_contributing", False),
        repo.get("has_coc", False),
        repo.get("has_issue_templates", False),
        repo.get("pushed_at"),
        now_iso(),
    ))
    conn.commit()

    row = conn.execute(
        "SELECT id FROM repositories WHERE full_name = ?", (full_name,)
    ).fetchone()
    return row["id"]


def upsert_issue(conn: sqlite3.Connection, issue: dict) -> int:
    """Insert or update an issue. Returns the row id."""
    labels = issue.get("labels", [])
    if isinstance(labels, list):
        labels = json.dumps(labels)

    cursor = conn.execute("""
        INSERT INTO issues (
            github_id, repo_id, number, title, body, labels,
            state, comments_count, created_at, updated_at,
            is_assigned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id, number) DO UPDATE SET
            title = excluded.title,
            body = excluded.body,
            labels = excluded.labels,
            state = excluded.state,
            comments_count = excluded.comments_count,
            updated_at = excluded.updated_at,
            is_assigned = excluded.is_assigned
    """, (
        issue.get("github_id"),
        issue["repo_id"],
        issue["number"],
        issue.get("title"),
        issue.get("body"),
        labels,
        issue.get("state", "open"),
        issue.get("comments_count", 0),
        issue.get("created_at"),
        issue.get("updated_at"),
        issue.get("is_assigned", False),
    ))
    conn.commit()

    row = conn.execute(
        "SELECT id FROM issues WHERE repo_id = ? AND number = ?",
        (issue["repo_id"], issue["number"])
    ).fetchone()
    return row["id"]


def get_top_repos(conn: sqlite3.Connection, limit: int = 50) -> list:
    """Get top repos by combined score."""
    return conn.execute(
        "SELECT * FROM repositories ORDER BY combined_score DESC LIMIT ?",
        (limit,)
    ).fetchall()


def get_top_issues(conn: sqlite3.Connection, limit: int = 20) -> list:
    """Get top issues by priority score, joined with repo info."""
    return conn.execute("""
        SELECT i.*, r.full_name, r.language, r.stars
        FROM issues i
        JOIN repositories r ON i.repo_id = r.id
        WHERE i.state = 'open' AND i.is_assigned = 0
        ORDER BY i.priority_score DESC
        LIMIT ?
    """, (limit,)).fetchall()


def get_unscanned_repos(conn: sqlite3.Connection, limit: int = 100) -> list:
    """Get repos that haven't been enriched with detail data."""
    return conn.execute("""
        SELECT * FROM repositories
        WHERE stars = 0 AND last_scanned IS NOT NULL
        ORDER BY created_at ASC
        LIMIT ?
    """, (limit,)).fetchall()


def record_contribution(conn: sqlite3.Connection, contrib: dict) -> int:
    """Log a contribution action. Returns the row id."""
    cursor = conn.execute("""
        INSERT INTO contributions (issue_id, repo_id, action, status, details)
        VALUES (?, ?, ?, ?, ?)
    """, (
        contrib.get("issue_id"),
        contrib.get("repo_id"),
        contrib.get("action"),
        contrib.get("status", "pending"),
        json.dumps(contrib.get("details", {})),
    ))
    conn.commit()
    return cursor.lastrowid


def update_contribution_status(conn: sqlite3.Connection, contrib_id: int, status: str,
                               pr_url: str = None, model_used: str = None,
                               opus_attempts: int = None) -> None:
    """Update a contribution's status and optional fields."""
    updates = ["status = ?", "updated_at = ?"]
    params = [status, now_iso()]

    if pr_url:
        updates.append("pr_url = ?")
        params.append(pr_url)
    if model_used:
        updates.append("model_used = ?")
        params.append(model_used)
    if opus_attempts is not None:
        updates.append("opus_attempts = ?")
        params.append(opus_attempts)

    params.append(contrib_id)

    query = f"UPDATE contributions SET {', '.join(updates)} WHERE id = ?"
    conn.execute(query, params)
    conn.commit()


def update_scores(conn: sqlite3.Connection, repo_id: int, scores: dict) -> None:
    """Update all score columns for a repository."""
    conn.execute("""
        UPDATE repositories SET
            popularity_score = ?,
            social_impact_score = ?,
            need_score = ?,
            kindness_score = ?,
            combined_score = ?
        WHERE id = ?
    """, (
        scores["popularity_score"],
        scores["social_impact_score"],
        scores["need_score"],
        scores["kindness_score"],
        scores["combined_score"],
        repo_id,
    ))
    conn.commit()


def get_contribution_history(conn: sqlite3.Connection) -> list:
    """Get all contributions with repo/issue info."""
    return conn.execute("""
        SELECT c.*, r.full_name, i.number as issue_number, i.title as issue_title
        FROM contributions c
        LEFT JOIN repositories r ON c.repo_id = r.id
        LEFT JOIN issues i ON c.issue_id = i.id
        ORDER BY c.created_at DESC
    """).fetchall()


# --- Blacklist queries ---

def add_to_blacklist(conn: sqlite3.Connection, full_name: str, reason: str,
                     repo_id: int = None, details: str = "{}"):
    """Add a repo to the blacklist."""
    conn.execute(
        """INSERT OR REPLACE INTO repo_blacklist (repo_id, full_name, reason, details)
           VALUES (?, ?, ?, ?)""",
        (repo_id, full_name, reason, details),
    )
    conn.commit()


def remove_from_blacklist(conn: sqlite3.Connection, full_name: str):
    """Forgive a repo — set forgiven_at timestamp."""
    conn.execute(
        "UPDATE repo_blacklist SET forgiven_at = ? WHERE full_name = ?",
        (now_iso(), full_name),
    )
    conn.commit()


def is_blacklisted(conn: sqlite3.Connection, full_name: str) -> bool:
    """Check if a repo is actively blacklisted (not forgiven)."""
    row = conn.execute(
        "SELECT 1 FROM repo_blacklist WHERE full_name = ? AND forgiven_at IS NULL",
        (full_name,),
    ).fetchone()
    return row is not None


def get_blacklist(conn: sqlite3.Connection) -> list:
    """Get all blacklisted repos."""
    return conn.execute(
        "SELECT * FROM repo_blacklist ORDER BY blacklisted_at DESC"
    ).fetchall()


# --- Agent run queries ---

def record_agent_run(conn: sqlite3.Connection, run: dict) -> str:
    """Record an agent run. Returns the run id."""
    conn.execute("""
        INSERT INTO agent_runs (id, issue_id, repo_id, model, effort, status, work_dir,
                                escalated_from, escalated_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run["id"], run.get("issue_id"), run.get("repo_id"),
        run["model"], run["effort"], run.get("status", "starting"),
        run.get("work_dir"), run.get("escalated_from"), run.get("escalated_reason"),
    ))
    conn.commit()
    return run["id"]


def update_agent_run(conn: sqlite3.Connection, run_id: str, **kwargs):
    """Update fields on an agent run."""
    sets = []
    vals = []
    for key, val in kwargs.items():
        sets.append(f"{key} = ?")
        vals.append(val)
    if not sets:
        return
    vals.append(run_id)
    conn.execute(
        f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?",
        vals,
    )
    conn.commit()


def get_agent_runs(conn: sqlite3.Connection, active_only: bool = False,
                   limit: int = 50) -> list:
    """Get agent runs, optionally only active ones."""
    if active_only:
        return conn.execute(
            """SELECT a.*, r.full_name, i.number as issue_number, i.title as issue_title
               FROM agent_runs a
               LEFT JOIN repositories r ON a.repo_id = r.id
               LEFT JOIN issues i ON a.issue_id = i.id
               WHERE a.status NOT IN ('pr_created', 'failed', 'escalated')
               ORDER BY a.started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return conn.execute(
        """SELECT a.*, r.full_name, i.number as issue_number, i.title as issue_title
           FROM agent_runs a
           LEFT JOIN repositories r ON a.repo_id = r.id
           LEFT JOIN issues i ON a.issue_id = i.id
           ORDER BY a.started_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def get_opus_usage_for_repo(conn: sqlite3.Connection, full_name: str) -> int:
    """Count how many opus-tier agent runs have been used for a repo."""
    row = conn.execute(
        """SELECT COUNT(*) as c FROM agent_runs a
           JOIN repositories r ON a.repo_id = r.id
           WHERE r.full_name = ? AND a.model LIKE '%opus%'
           AND a.status IN ('pr_created', 'fixing', 'pushing')""",
        (full_name,),
    ).fetchone()
    return row["c"] if row else 0


def get_opus_attempts_for_issue(conn: sqlite3.Connection, issue_id: int) -> int:
    """Count how many opus attempts have been made for a specific issue."""
    row = conn.execute(
        """SELECT COALESCE(SUM(opus_attempts), 0) as total
           FROM contributions
           WHERE issue_id = ?""",
        (issue_id,),
    ).fetchone()
    return row["total"] if row else 0


# --- Sponsor queries ---

def add_sponsor(conn: sqlite3.Connection, username: str, repo_full_name: str = None,
                source: str = "comment", details: str = "{}"):
    """Record a sponsor."""
    conn.execute(
        """INSERT OR REPLACE INTO sponsors (github_username, repo_full_name, source, details)
           VALUES (?, ?, ?, ?)""",
        (username, repo_full_name, source, details),
    )
    conn.commit()


def get_sponsor_repos(conn: sqlite3.Connection) -> set:
    """Get full_names of repos owned by sponsors."""
    rows = conn.execute(
        "SELECT repo_full_name FROM sponsors WHERE repo_full_name IS NOT NULL"
    ).fetchall()
    return {r["repo_full_name"] for r in rows}


# --- Next issue for factory ---

def get_next_unclaimed_issue(conn: sqlite3.Connection, min_stars: int = 1000) -> dict | None:
    """Get the next best issue to solve using the escalation & persistence algorithm.

    Priority order:
    1. Sponsor repos
    2. Focus Repos with momentum (merged PRs recently, sorted by merge recency)
    3. Focus Repos without momentum (top combined_score, no merges yet)
    4. General repos (sorted by priority_score)

    Focus Repos = top 20 non-blacklisted repos by combined_score.
    Persistence: after a merge, we stay in the same repo for the next issue.

    Exclusions:
    - Repos with >= 10 strikes (unmerged PRs)
    - Repos in cooldown (1 week after unmerged PR)
    - Blacklisted repos
    - Already-contributed issues
    - Stale issues (>2yr old)
    - Repos not maintained in last 30 days
    """
    from src.config import SUPPORTED_LANGUAGES, BEGINNER_LABELS, BOUNTY_LABELS
    lang_placeholders = ",".join("?" for _ in SUPPORTED_LANGUAGES)
    # Build LIKE clauses to exclude beginner-labeled issues
    beginner_excludes = " AND ".join(
        "LOWER(i.labels) NOT LIKE ?" for _ in BEGINNER_LABELS
    )
    beginner_params = [f'%{label}%' for label in BEGINNER_LABELS]
    # Build bounty detection as OR of LIKE clauses
    bounty_checks = " OR ".join(
        "LOWER(i.labels) LIKE ?" for _ in BOUNTY_LABELS
    )
    bounty_params = [f'%{label}%' for label in BOUNTY_LABELS]
    params = list(SUPPORTED_LANGUAGES) + [min_stars] + beginner_params + bounty_params

    row = conn.execute(f"""
        SELECT i.*, r.full_name, r.language, r.stars, r.owner, r.name as repo_name,
               r.url as repo_url, r.id as rid, r.combined_score,
               CASE WHEN s.github_username IS NOT NULL THEN 1 ELSE 0 END as is_sponsor,
               COALESCE(rs.merges, 0) as repo_merges,
               COALESCE(rs.strikes, 0) as repo_strikes,
               rs.last_merge_at,
               CASE WHEN ({bounty_checks}) THEN 1 ELSE 0 END as is_bounty,
               CASE WHEN r.id IN (
                   SELECT id FROM repositories
                   WHERE combined_score > 0
                     AND full_name NOT IN (SELECT full_name FROM repo_blacklist WHERE forgiven_at IS NULL)
                   ORDER BY combined_score DESC
                   LIMIT 20
               ) THEN 1 ELSE 0 END as is_focus_repo
        FROM issues i
        JOIN repositories r ON i.repo_id = r.id
        LEFT JOIN sponsors s ON r.owner = s.github_username
        LEFT JOIN repo_strikes rs ON r.id = rs.repo_id
        WHERE i.state = 'open'
          AND i.is_assigned = 0
          AND r.language IN ({lang_placeholders})
          AND r.stars >= ?
          AND {beginner_excludes}
          AND r.full_name NOT IN (SELECT full_name FROM repo_blacklist WHERE forgiven_at IS NULL)
          AND i.id NOT IN (SELECT issue_id FROM issue_claims WHERE status = 'active')
          AND i.id NOT IN (SELECT issue_id FROM contributions WHERE issue_id IS NOT NULL)
          AND i.updated_at > datetime('now', '-2 years')
          AND (r.pushed_at IS NULL OR r.pushed_at > datetime('now', '-30 days'))
          AND COALESCE(rs.strikes, 0) < 10
          AND (rs.cooldown_until IS NULL OR rs.cooldown_until < datetime('now'))
        ORDER BY
          is_bounty DESC,
          is_sponsor DESC,
          is_focus_repo DESC,
          repo_merges DESC,
          CASE WHEN rs.last_merge_at IS NOT NULL
               THEN julianday('now') - julianday(rs.last_merge_at)
               ELSE 9999 END ASC,
          r.combined_score DESC,
          i.priority_score DESC
        LIMIT 1
    """, params).fetchone()
    return dict(row) if row else None


# --- Learned patterns ---

def add_learned_pattern(conn: sqlite3.Connection, pattern_type: str,
                        pattern_value: str, repo_full_name: str = None,
                        confidence: float = 0.5, source: str = None):
    """Record a learned pattern."""
    conn.execute(
        """INSERT OR REPLACE INTO learned_patterns
           (pattern_type, repo_full_name, pattern_value, confidence, source)
           VALUES (?, ?, ?, ?, ?)""",
        (pattern_type, repo_full_name, pattern_value, confidence, source),
    )
    conn.commit()


# --- Repo strike / loyalty tracking ---

def record_pr_submitted(conn: sqlite3.Connection, repo_id: int, full_name: str):
    """Record that we submitted a PR to a repo. Updates last_pr_at."""
    conn.execute("""
        INSERT INTO repo_strikes (repo_id, full_name, last_pr_at, updated_at)
        VALUES (?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(repo_id) DO UPDATE SET
            last_pr_at = datetime('now'),
            updated_at = datetime('now')
    """, (repo_id, full_name))
    conn.commit()


def record_pr_merged(conn: sqlite3.Connection, repo_id: int, full_name: str):
    """Record a merged PR. Increments merges count, clears cooldown."""
    conn.execute("""
        INSERT INTO repo_strikes (repo_id, full_name, merges, last_merge_at, updated_at)
        VALUES (?, ?, 1, datetime('now'), datetime('now'))
        ON CONFLICT(repo_id) DO UPDATE SET
            merges = repo_strikes.merges + 1,
            last_merge_at = datetime('now'),
            cooldown_until = NULL,
            updated_at = datetime('now')
    """, (repo_id, full_name))
    conn.commit()


def record_pr_rejected(conn: sqlite3.Connection, repo_id: int, full_name: str):
    """Record an unmerged/closed PR. Adds a strike, sets 1-week cooldown."""
    conn.execute("""
        INSERT INTO repo_strikes (repo_id, full_name, strikes, last_rejection_at,
                                  cooldown_until, updated_at)
        VALUES (?, ?, 1, datetime('now'), datetime('now', '+7 days'), datetime('now'))
        ON CONFLICT(repo_id) DO UPDATE SET
            strikes = repo_strikes.strikes + 1,
            last_rejection_at = datetime('now'),
            cooldown_until = datetime('now', '+7 days'),
            updated_at = datetime('now')
    """, (repo_id, full_name))
    conn.commit()


def redeem_strikes(conn: sqlite3.Connection, repo_id: int, count: int = 1):
    """Redeem strikes from positive feedback. Reduces strikes, clears cooldown."""
    conn.execute("""
        UPDATE repo_strikes SET
            strikes = MAX(0, strikes - ?),
            cooldown_until = NULL,
            updated_at = datetime('now')
        WHERE repo_id = ?
    """, (count, repo_id))
    conn.commit()


def get_repo_strikes(conn: sqlite3.Connection, full_name: str) -> dict | None:
    """Get strike/loyalty info for a repo."""
    row = conn.execute(
        "SELECT * FROM repo_strikes WHERE full_name = ?", (full_name,)
    ).fetchone()
    return dict(row) if row else None


def get_loyalty_repos(conn: sqlite3.Connection, limit: int = 20) -> list:
    """Get repos where we have merged PRs, ordered by merge count."""
    return conn.execute("""
        SELECT rs.*, r.combined_score, r.stars, r.language
        FROM repo_strikes rs
        JOIN repositories r ON rs.repo_id = r.id
        WHERE rs.merges > 0
          AND rs.strikes < 10
          AND (rs.cooldown_until IS NULL OR rs.cooldown_until < datetime('now'))
          AND r.full_name NOT IN (SELECT full_name FROM repo_blacklist WHERE forgiven_at IS NULL)
        ORDER BY rs.merges DESC, r.combined_score DESC
        LIMIT ?
    """, (limit,)).fetchall()


def get_learned_patterns(conn: sqlite3.Connection, repo_full_name: str = None) -> list:
    """Get learned patterns for a repo (or global patterns if repo is None)."""
    if repo_full_name:
        return conn.execute(
            """SELECT * FROM learned_patterns
               WHERE repo_full_name = ? OR repo_full_name IS NULL
               ORDER BY confidence DESC""",
            (repo_full_name,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM learned_patterns WHERE repo_full_name IS NULL ORDER BY confidence DESC"
    ).fetchall()
