"""Auto-tagger for the repository database.

Scans all repositories and assigns tags based on keywords found in
name, description, and topics. Tags are stored as a JSON array in
the `tags` column.

Usage:
    python -m src.tagger [--min-stars 10] [--dry-run]
"""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "github_helper.db"

# ═══════════════════════════════════════════════════════════
# TAG RULES — each tag has keyword sets to match against
# name, description, and topics. A repo gets a tag if ANY
# keyword in the set matches.
# ═══════════════════════════════════════════════════════════

TAG_RULES = {
    # ── Christian / Religious ──
    # IMPORTANT: Only match on description + topics, NOT owner/repo name
    # to avoid false positives like "ChristianLempa" or "JesusFreke"
    "christian": {
        "keywords": [
            "bible app", "bible study", "bible verse", "bible api",
            "biblical", "scripture", "church management", "church software",
            "gospel", "sermon", "worship", "prayer app", "ministry",
            "hymnal", "hymn", "liturgy", "catholic", "adventist",
            "devotional", "verse of the day", "openlp",
            "freeshow", "quelea", "churchcrm", "rock rms",
            "bible reader", "bible database", "bible translation",
        ],
        "exclude": [
            "pure-bash-bible", "pentesting-bible", "java-bible",
            "qa_bible", "obfuscation-bible", "interview-bible",
            "performance-tuning-bible", "trevorspray", "legba",
            "psalm php", "static analysis",
        ],
        "description_only": True,  # only match description + topics, not name/owner
    },

    # ── BSV / Bitcoin SV ──
    "bsv": {
        "keywords": [
            "bitcoin sv", "bitcoin-sv", "bitcoinsv",
            "satoshi vision", "teranode",
        ],
        "exclude": [
            "bitcoin core", "ethereum",
        ],
        # Must appear in description or topics, not just name
        "description_only": True,
    },

    # ── MMORPG / Video Games ──
    "mmorpg": {
        "keywords": [
            "mmorpg", "mmo game", "open world rpg",
            "multiplayer rpg", "medieval rpg", "fantasy rpg",
            "veloren", "ryzom", "stendhal", "landsandboat",
            "the mana world", "manaplus", "planeshift",
            "flare-game", "worldforge", "eternal-lands",
            "open source mmo", "open-source mmo",
        ],
        "exclude": [
            "emulator", "private server", "trinitycore",
            "azerothcore", "mangos", "cmangos",
        ],
    },

    # ── Game Development (broader) ──
    "gamedev": {
        "keywords": [
            "game engine", "game framework", "game development",
            "godot", "unity plugin", "unreal plugin", "pygame",
            "phaser", "pixijs", "bevy", "amethyst engine",
            "love2d", "libgdx", "monogame",
        ],
        "exclude": [],
    },

    # ── DevOps / Infrastructure ──
    "devops": {
        "keywords": [
            "kubernetes", "docker", "terraform", "ansible",
            "ci/cd", "infrastructure as code", "helm chart",
            "monitoring", "observability", "grafana", "prometheus",
        ],
        "exclude": [],
    },

    # ── Education / Learning ──
    "education": {
        "keywords": [
            "learn", "tutorial", "course", "educational",
            "teaching", "student", "classroom", "e-learning",
            "coding bootcamp", "beginner friendly",
        ],
        "exclude": [],
    },

    # ── Accessibility ──
    "accessibility": {
        "keywords": [
            "accessibility", "a11y", "screen reader", "wcag",
            "aria", "assistive", "blind", "deaf", "disability",
            "inclusive design",
        ],
        "exclude": [],
    },

    # ── Environment / Climate ──
    "environment": {
        "keywords": [
            "climate", "environment", "sustainability", "carbon",
            "renewable", "solar", "wind energy", "green energy",
            "ecology", "conservation", "recycling",
        ],
        "exclude": [],
    },

    # ── Health / Medical ──
    "health": {
        "keywords": [
            "health", "medical", "hospital", "patient",
            "clinical", "telemedicine", "mental health",
            "fitness", "wellness", "healthcare",
        ],
        "exclude": ["healthcheck", "health check", "health-check"],
    },

    # ── Privacy / Security (defensive) ──
    "privacy": {
        "keywords": [
            "privacy", "encryption", "end-to-end", "e2ee",
            "anonymity", "tor", "vpn", "self-hosted",
            "decentralized", "zero knowledge",
        ],
        "exclude": [],
    },

    # ── Nonprofit / Social Good ──
    "social-good": {
        "keywords": [
            "nonprofit", "non-profit", "charity", "volunteer",
            "humanitarian", "social impact", "civic tech",
            "open data", "public benefit", "democracy",
            "crisis", "disaster relief",
        ],
        "exclude": [],
    },
}


def text_matches(text: str, keywords: list[str], excludes: list[str]) -> bool:
    """Check if text contains any keyword but none of the excludes."""
    text_lower = text.lower()
    # Check excludes first
    for exc in excludes:
        if exc.lower() in text_lower:
            return False
    # Check keywords
    for kw in keywords:
        if kw.lower() in text_lower:
            return True
    return False


def tag_repo(name: str, description: str, topics: str) -> list[str]:
    """Determine which tags apply to a repository."""
    tags = []
    combined = f"{name} {description} {topics}".lower()
    desc_topics = f"{description} {topics}".lower()

    for tag, rule in TAG_RULES.items():
        keywords = rule["keywords"]
        excludes = rule.get("exclude", [])
        desc_only = rule.get("description_only", False)

        # Choose which text to search
        search_text = desc_topics if desc_only else combined

        # Check excludes on the full combined text
        excluded = False
        for exc in excludes:
            if exc.lower() in combined:
                excluded = True
                break
        if excluded:
            continue

        # Check keywords
        if any(kw.lower() in search_text for kw in keywords):
            tags.append(tag)

    return tags


def run(min_stars: int = 10, dry_run: bool = False):
    """Scan all repos and assign tags."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, full_name, name, description, topics FROM repositories "
        "WHERE stars >= ? ORDER BY stars DESC",
        (min_stars,)
    ).fetchall()

    print(f"Scanning {len(rows)} repos (min {min_stars} stars)...")

    tag_counts: dict[str, int] = {}
    updated = 0

    for row in rows:
        desc = row["description"] or ""
        topics = row["topics"] or "[]"
        name = row["full_name"] or row["name"] or ""

        tags = tag_repo(name, desc, topics)

        if tags:
            tags_json = json.dumps(sorted(tags))
            for t in tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1

            if not dry_run:
                conn.execute(
                    "UPDATE repositories SET tags = ? WHERE id = ?",
                    (tags_json, row["id"])
                )
            updated += 1

    if not dry_run:
        conn.commit()

    print(f"\nTagged {updated} repos out of {len(rows)}")
    print("\nTag distribution:")
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {tag:20s} {count:>5}")

    conn.close()


if __name__ == "__main__":
    min_stars = 10
    dry_run = False
    for arg in sys.argv[1:]:
        if arg == "--dry-run":
            dry_run = True
        elif arg.startswith("--min-stars"):
            min_stars = int(arg.split("=")[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1])

    run(min_stars=min_stars, dry_run=dry_run)
