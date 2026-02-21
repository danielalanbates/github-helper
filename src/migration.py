"""Schema migration runner for new tables and columns."""

import sqlite3
from src.config import DB_PATH, DATA_DIR


MIGRATIONS = [
    # Migration 1: agent_runs table
    {
        "id": 1,
        "description": "Create agent_runs table",
        "sql": """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,
                issue_id INTEGER REFERENCES issues(id),
                repo_id INTEGER REFERENCES repositories(id),
                model TEXT NOT NULL,
                effort TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'starting'
                    CHECK(status IN ('starting','cloning','fixing','pushing',
                                     'pr_created','failed','escalated')),
                escalated_from TEXT,
                escalated_reason TEXT,
                work_dir TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                cost_usd REAL DEFAULT 0,
                pr_url TEXT,
                pr_number INTEGER,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runs_issue
                ON agent_runs(issue_id);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_status
                ON agent_runs(status);
        """,
    },
    # Migration 2: issue_claims table
    {
        "id": 2,
        "description": "Create issue_claims table",
        "sql": """
            CREATE TABLE IF NOT EXISTS issue_claims (
                issue_id INTEGER PRIMARY KEY,
                agent_id TEXT NOT NULL,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','completed','released','expired'))
            );
        """,
    },
    # Migration 3: repo_blacklist table
    {
        "id": 3,
        "description": "Create repo_blacklist table",
        "sql": """
            CREATE TABLE IF NOT EXISTS repo_blacklist (
                repo_id INTEGER REFERENCES repositories(id),
                full_name TEXT UNIQUE NOT NULL,
                reason TEXT NOT NULL,
                details TEXT DEFAULT '{}',
                blacklisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                forgiven_at TIMESTAMP
            );
        """,
    },
    # Migration 4: pr_reviews table
    {
        "id": 4,
        "description": "Create pr_reviews table",
        "sql": """
            CREATE TABLE IF NOT EXISTS pr_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contribution_id INTEGER REFERENCES contributions(id),
                pr_url TEXT,
                reviewer TEXT,
                review_type TEXT,
                body TEXT,
                sentiment TEXT
                    CHECK(sentiment IN ('positive','constructive','hostile',
                                        'sarcastic','regretful',NULL)),
                action_taken TEXT
                    CHECK(action_taken IN ('fix_pushed','pr_closed',
                                           'repo_blacklisted','thanked',
                                           're_engaged',NULL)),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pr_reviews_contribution
                ON pr_reviews(contribution_id);
            CREATE INDEX IF NOT EXISTS idx_pr_reviews_sentiment
                ON pr_reviews(sentiment);
        """,
    },
    # Migration 5: learned_patterns table
    {
        "id": 5,
        "description": "Create learned_patterns table",
        "sql": """
            CREATE TABLE IF NOT EXISTS learned_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL
                    CHECK(pattern_type IN ('commit_format','dco_required',
                                           'anti_ai','review_pattern')),
                repo_full_name TEXT,
                pattern_value TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(pattern_type, repo_full_name, pattern_value)
            );
        """,
    },
    # Migration 6: sponsors table
    {
        "id": 6,
        "description": "Create sponsors table",
        "sql": """
            CREATE TABLE IF NOT EXISTS sponsors (
                github_username TEXT PRIMARY KEY,
                repo_full_name TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                source TEXT CHECK(source IN ('comment','mention','email')),
                details TEXT DEFAULT '{}'
            );
        """,
    },
    # Migration 7: rate_limit_state table
    {
        "id": 7,
        "description": "Create rate_limit_state table",
        "sql": """
            CREATE TABLE IF NOT EXISTS rate_limit_state (
                resource TEXT PRIMARY KEY,
                requests_made INTEGER DEFAULT 0,
                window_start TIMESTAMP,
                limit_per_window INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO rate_limit_state
                (resource, requests_made, window_start, limit_per_window)
                VALUES ('github_api', 0, CURRENT_TIMESTAMP, 5000);
            INSERT OR IGNORE INTO rate_limit_state
                (resource, requests_made, window_start, limit_per_window)
                VALUES ('github_search', 0, CURRENT_TIMESTAMP, 30);
        """,
    },
    # Migration 8: ALTER issues table
    {
        "id": 8,
        "description": "Add complexity_score and estimated_model to issues",
        "sql": """
            ALTER TABLE issues ADD COLUMN complexity_score REAL DEFAULT 0;
            ALTER TABLE issues ADD COLUMN estimated_model TEXT;
        """,
    },
    # Migration 9: ALTER contributions table
    {
        "id": 9,
        "description": "Add agent_id, model_used, cost_usd, feedback_status to contributions",
        "sql": """
            ALTER TABLE contributions ADD COLUMN agent_id TEXT;
            ALTER TABLE contributions ADD COLUMN model_used TEXT;
            ALTER TABLE contributions ADD COLUMN cost_usd REAL DEFAULT 0;
            ALTER TABLE contributions ADD COLUMN feedback_status TEXT;
        """,
    },
    # Migration 10: Anthropic API rate limit (Tier 1 = 50 RPM)
    {
        "id": 10,
        "description": "Add anthropic_api rate limit row",
        "sql": """
            INSERT OR IGNORE INTO rate_limit_state
                (resource, requests_made, window_start, limit_per_window)
                VALUES ('anthropic_api', 0, CURRENT_TIMESTAMP, 40);
        """,
    },
    # Migration 11: repo_strikes table for loyalty algorithm
    {
        "id": 11,
        "description": "Create repo_strikes table for loyalty/strike tracking",
        "sql": """
            CREATE TABLE IF NOT EXISTS repo_strikes (
                repo_id INTEGER PRIMARY KEY REFERENCES repositories(id),
                full_name TEXT UNIQUE NOT NULL,
                strikes INTEGER DEFAULT 0,
                merges INTEGER DEFAULT 0,
                last_pr_at TIMESTAMP,
                last_merge_at TIMESTAMP,
                last_rejection_at TIMESTAMP,
                cooldown_until TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_repo_strikes_cooldown
                ON repo_strikes(cooldown_until);
        """,
    },
    # Migration 12: schema_migrations tracking table
    {
        "id": 0,
        "description": "Create schema_migrations tracking table",
        "sql": """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id INTEGER PRIMARY KEY,
                description TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """,
    },
    # Migration 13: Add opus_attempts tracking per issue
    {
        "id": 13,
        "description": "Add opus_attempts column to contributions for per-issue tracking",
        "sql": """
            ALTER TABLE contributions ADD COLUMN opus_attempts INTEGER DEFAULT 0;
        """,
    },
    # Migration 14: Add pr_closed_at timestamp to track when PRs are closed
    {
        "id": 14,
        "description": "Add pr_closed_at to contributions for follow-up on closed PRs",
        "sql": """
            ALTER TABLE contributions ADD COLUMN pr_closed_at TIMESTAMP;
            ALTER TABLE contributions ADD COLUMN pr_close_reason TEXT;
        """,
    },
    # Migration 15: Feedback revision tracking columns
    {
        "id": 15,
        "description": "Add feedback columns to contributions for revision tracking",
        "sql": """
            ALTER TABLE contributions ADD COLUMN feedback_text TEXT;
            ALTER TABLE contributions ADD COLUMN feedback_pr_url TEXT;
            ALTER TABLE contributions ADD COLUMN feedback_reviewer TEXT;
        """,
    },
    # Migration 16: Mandatory model override for feedback items
    {
        "id": 16,
        "description": "Add mandatory_model to contributions for Daniel tier overrides",
        "sql": """
            ALTER TABLE contributions ADD COLUMN mandatory_model TEXT;
        """,
    },
]


def run_migrations(conn: sqlite3.Connection) -> int:
    """Run all pending migrations. Returns count of migrations applied."""
    # Ensure migrations table exists first
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    applied = {
        row[0]
        for row in conn.execute("SELECT id FROM schema_migrations").fetchall()
    }

    count = 0
    for migration in MIGRATIONS:
        mid = migration["id"]
        if mid == 0 or mid in applied:
            continue
        try:
            conn.executescript(migration["sql"])
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (id, description) VALUES (?, ?)",
                (mid, migration["description"]),
            )
            conn.commit()
            count += 1
        except sqlite3.OperationalError as e:
            # Column already exists or table already exists — skip gracefully
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (id, description) VALUES (?, ?)",
                    (mid, migration["description"]),
                )
                conn.commit()
            else:
                raise
    return count
