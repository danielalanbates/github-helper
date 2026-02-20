"""Model tier selection engine based on issue complexity."""

import json
import math
from src.config import MODEL_TIERS, MAX_OPUS_PER_ISSUE


# Keywords that signal simple issues
SIMPLE_KEYWORDS = {
    "typo", "typos", "spelling", "rename", "whitespace", "indent",
    "formatting", "css", "style", "yaml", "yml", "config",
    "readme", "docs", "documentation", "comment", "todo",
    "unused import", "unused variable", "lint", "linting",
}

# Keywords that signal complex issues
COMPLEX_KEYWORDS = {
    "refactor", "race condition", "deadlock", "memory leak",
    "architecture", "redesign", "rewrite", "migration",
    "security", "vulnerability", "authentication", "authorization",
    "performance", "optimization", "concurrent", "async",
    "breaking change", "api change", "backwards compatible",
}

# Languages with higher inherent complexity
COMPLEX_LANGUAGES = {"rust", "c", "c++", "go", "java", "scala", "haskell"}
SIMPLE_LANGUAGES = {"markdown", "yaml", "json", "css", "html", "toml"}


def score_complexity(issue: dict, repo: dict = None) -> float:
    """Score issue complexity from 0.0 (trivial) to 1.0 (very complex).

    Signals and weights:
    - Issue body length (15%)
    - Comment count (10%)
    - Labels (15%)
    - Language complexity (20%)
    - Repo stars/size (15%)
    - Open issues ratio (10%)
    - Title keywords (15%)
    """
    scores = []

    # 1. Issue body length (15%) — longer = more complex
    body = issue.get("body") or ""
    body_len = len(body)
    body_score = min(body_len / 3000, 1.0)
    scores.append(("body_length", body_score, 0.15))

    # 2. Comment count (10%) — more discussion = more complex
    comments = issue.get("comments_count") or 0
    comment_score = min(comments / 15, 1.0)
    scores.append(("comments", comment_score, 0.10))

    # 3. Labels (15%)
    labels_raw = issue.get("labels") or "[]"
    if isinstance(labels_raw, str):
        labels = set(l.lower() for l in json.loads(labels_raw))
    else:
        labels = set(l.lower() for l in labels_raw)

    from src.config import BEGINNER_LABELS
    label_score = 0.7  # default medium
    if labels & BEGINNER_LABELS:
        label_score = 0.2  # beginner = simpler
    if "bug" in labels:
        label_score = max(label_score, 0.5)
    if any(l in labels for l in ("enhancement", "feature", "feature-request")):
        label_score = max(label_score, 0.6)
    if any(l in labels for l in ("critical", "security", "breaking")):
        label_score = 0.9
    scores.append(("labels", label_score, 0.15))

    # 4. Language complexity (20%)
    language = (repo or {}).get("language", "") or ""
    lang_lower = language.lower()
    if lang_lower in SIMPLE_LANGUAGES:
        lang_score = 0.1
    elif lang_lower in COMPLEX_LANGUAGES:
        lang_score = 0.8
    else:
        lang_score = 0.4  # Python, JS, TS, etc.
    scores.append(("language", lang_score, 0.20))

    # 5. Repo stars/size (15%) — bigger repos = harder to navigate
    stars = (repo or {}).get("stars", 0) or 0
    star_score = min(math.log(stars + 1) / math.log(100000), 1.0)
    scores.append(("repo_size", star_score, 0.15))

    # 6. Open issues ratio (10%)
    open_issues = (repo or {}).get("open_issues", 0) or 0
    issues_score = min(open_issues / 500, 1.0)
    scores.append(("open_issues", issues_score, 0.10))

    # 7. Title keywords (15%)
    title = (issue.get("title") or "").lower()
    combined_text = f"{title} {body[:500].lower()}"
    simple_matches = sum(1 for kw in SIMPLE_KEYWORDS if kw in combined_text)
    complex_matches = sum(1 for kw in COMPLEX_KEYWORDS if kw in combined_text)

    if simple_matches > 0 and complex_matches == 0:
        keyword_score = 0.1
    elif complex_matches > simple_matches:
        keyword_score = min(0.5 + complex_matches * 0.15, 1.0)
    elif complex_matches > 0:
        keyword_score = 0.5
    else:
        keyword_score = 0.4
    scores.append(("keywords", keyword_score, 0.15))

    # Weighted sum
    total = sum(score * weight for _, score, weight in scores)
    return round(min(max(total, 0.0), 1.0), 4)


def select_tier(complexity: float, issue_id: int = None, conn = None) -> dict:
    """Select the appropriate model tier based on complexity score.

    Args:
        complexity: 0.0–1.0 complexity score
        issue_id: the issue being fixed (for per-issue opus budget checking)
        conn: database connection (required if issue_id is provided)

    Returns:
        Model tier dict from MODEL_TIERS
    """
    if complexity <= 0.25:
        tier_idx = 0  # sonnet medium (was haiku, but we avoid haiku)
    elif complexity <= 0.50:
        tier_idx = 1  # sonnet medium
    elif complexity <= 0.75:
        tier_idx = 2  # sonnet high
    else:
        tier_idx = 3  # opus

    # Enforce opus budget per issue
    if tier_idx == 3 and issue_id and conn:
        from src.db import get_opus_attempts_for_issue
        opus_attempts = get_opus_attempts_for_issue(conn, issue_id)
        if opus_attempts >= MAX_OPUS_PER_ISSUE:
            tier_idx = 2  # downgrade to sonnet high

    return MODEL_TIERS[tier_idx].copy()


def get_next_tier(current_tier: dict) -> dict | None:
    """Get the next tier up for escalation. Returns None if already at max."""
    current_tier_num = current_tier["tier"]
    for t in MODEL_TIERS:
        if t["tier"] == current_tier_num + 1:
            return t.copy()
    return None


def get_tier_by_number(tier_num: int) -> dict | None:
    """Get a specific tier by number."""
    for t in MODEL_TIERS:
        if t["tier"] == tier_num:
            return t.copy()
    return None
