"""Scoring algorithm and ranking engine."""

import json
import math
from src.config import (
    WEIGHT_POPULARITY, WEIGHT_SOCIAL_IMPACT, WEIGHT_NEED, WEIGHT_KINDNESS,
    SOCIAL_TOPICS, BEGINNER_LABELS,
    BLOCKED_TOPICS, BLOCKED_DESCRIPTION_KEYWORDS,
)
from src.db import get_connection, update_scores


class Ranker:
    def __init__(self):
        self.conn = get_connection()

    def rank_all(self) -> int:
        """Recalculate scores for all repositories. Returns count updated."""
        repos = self.conn.execute("SELECT * FROM repositories").fetchall()
        if not repos:
            return 0

        all_stars = [r["stars"] or 0 for r in repos]
        all_forks = [r["forks"] or 0 for r in repos]
        all_issue_ratios = []
        for r in repos:
            contrib = max(r["contributor_count"] or 1, 1)
            ratio = (r["open_issues"] or 0) / contrib
            all_issue_ratios.append(ratio)

        max_log_stars = max((math.log(s + 1) for s in all_stars), default=1) or 1
        max_log_forks = max((math.log(f + 1) for f in all_forks), default=1) or 1
        max_issue_ratio = max(all_issue_ratios, default=1) or 1

        count = 0
        for repo in repos:
            scores = self._score_repo(repo, max_log_stars, max_log_forks, max_issue_ratio)
            update_scores(self.conn, repo["id"], scores)
            count += 1

        return count

    def _is_blocked(self, repo) -> bool:
        """Check if a repo is blocked by our ethics filter."""
        topics = set(json.loads(repo["topics"] or "[]"))
        topics_lower = {t.lower() for t in topics}
        if topics_lower & BLOCKED_TOPICS:
            return True
        desc = (repo["description"] or "").lower()
        name = (repo["name"] or "").lower()
        for kw in BLOCKED_DESCRIPTION_KEYWORDS:
            if kw in desc or kw in name:
                return True
        return False

    def _score_repo(self, repo, max_log_stars, max_log_forks, max_issue_ratio) -> dict:
        """Calculate all scores for a single repository."""
        if self._is_blocked(repo):
            return {
                "popularity_score": 0, "social_impact_score": 0,
                "need_score": 0, "kindness_score": 0, "combined_score": -1,
            }

        # --- Popularity ---
        log_stars = math.log((repo["stars"] or 0) + 1)
        log_forks = math.log((repo["forks"] or 0) + 1)
        popularity = (
            (log_stars / max_log_stars) * 0.6 +
            (log_forks / max_log_forks) * 0.4
        )

        # --- Social Impact ---
        topics = set(json.loads(repo["topics"] or "[]"))
        topics_lower = {t.lower() for t in topics}
        topic_matches = topics_lower & SOCIAL_TOPICS
        topic_score = min(len(topic_matches) / 3, 1.0)  # 3+ matches = max

        desc = (repo["description"] or "").lower()
        desc_keywords = {
            "nonprofit", "charity", "social good", "accessibility",
            "education", "health", "climate", "sustainability",
            "humanitarian", "open source", "community",
        }
        desc_matches = sum(1 for kw in desc_keywords if kw in desc)
        desc_signal = min(desc_matches / 3, 1.0)

        social_impact = topic_score * 0.6 + desc_signal * 0.4

        # --- Need ---
        contributor_count = max(repo["contributor_count"] or 1, 1)
        issue_ratio = (repo["open_issues"] or 0) / contributor_count
        need = issue_ratio / max_issue_ratio

        # --- Kindness ---
        kindness = 0.0
        issue_labels = self._get_repo_labels(repo["id"])
        if issue_labels & BEGINNER_LABELS:
            kindness += 0.3
        if repo["has_contributing"]:
            kindness += 0.25
        if repo["has_coc"]:
            kindness += 0.20
        if repo["has_issue_templates"]:
            kindness += 0.15
        kindness += 0.05  # Base bonus for being in the system

        # --- Combined ---
        combined = (
            popularity * WEIGHT_POPULARITY +
            social_impact * WEIGHT_SOCIAL_IMPACT +
            need * WEIGHT_NEED +
            kindness * WEIGHT_KINDNESS
        )

        return {
            "popularity_score": round(popularity, 4),
            "social_impact_score": round(social_impact, 4),
            "need_score": round(need, 4),
            "kindness_score": round(kindness, 4),
            "combined_score": round(combined, 4),
        }

    def _get_repo_labels(self, repo_id: int) -> set:
        """Get all unique labels from issues for a repo."""
        cursor = self.conn.execute(
            "SELECT labels FROM issues WHERE repo_id = ?", (repo_id,)
        )
        all_labels = set()
        for row in cursor:
            labels = json.loads(row["labels"] or "[]")
            all_labels.update(l.lower() for l in labels)
        return all_labels

    def rank_issues(self) -> int:
        """Score and rank issues for solvability, including complexity for model selection."""
        from src.model_selector import score_complexity, select_tier

        issues = self.conn.execute("""
            SELECT i.*, r.combined_score as repo_score, r.language, r.stars,
                   r.open_issues as repo_open_issues
            FROM issues i
            JOIN repositories r ON i.repo_id = r.id
            WHERE i.state = 'open' AND i.is_assigned = 0
        """).fetchall()

        count = 0
        for issue in issues:
            priority = self._score_issue(issue)
            repo_dict = {"language": issue["language"], "stars": issue["stars"],
                         "open_issues": issue["repo_open_issues"]}
            complexity = score_complexity(dict(issue), repo_dict)
            tier = select_tier(complexity)
            self.conn.execute(
                """UPDATE issues SET priority_score = ?, complexity_score = ?,
                   estimated_model = ? WHERE id = ?""",
                (priority, complexity, tier["label"], issue["id"])
            )
            count += 1
        self.conn.commit()
        return count

    def _score_issue(self, issue) -> float:
        """Score a single issue for priority."""
        labels = set(json.loads(issue["labels"] or "[]"))
        labels_lower = {l.lower() for l in labels}

        comment_score = max(0, 1 - (issue["comments_count"] or 0) / 20)
        repo_score = issue["repo_score"] or 0
        bug_bonus = 0.3 if "bug" in labels_lower else 0

        priority = (
            repo_score * 0.45 +
            bug_bonus * 0.35 +
            comment_score * 0.20
        )
        return round(min(priority, 1.0), 4)
