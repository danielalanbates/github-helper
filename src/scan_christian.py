"""One-off script to scan issues for Christian-tagged repos."""
import sqlite3
import subprocess
import json
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "github_helper.db"

db = sqlite3.connect(str(DB_PATH))
db.row_factory = sqlite3.Row

repos = db.execute("""
    SELECT id, full_name, owner, name FROM repositories
    WHERE tags LIKE '%"christian"%'
    ORDER BY stars DESC
""").fetchall()

total_issues = 0
for repo in repos:
    full_name = repo["full_name"]
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{full_name}/issues",
             "--paginate", "-q",
             '.[] | select(.pull_request == null) | '
             '{number: .number, title: .title, '
             'body: (.body // "" | .[0:500]), state: .state, '
             'labels: [.labels[].name], created_at: .created_at, '
             'updated_at: .updated_at, assignee: .assignee.login, '
             'url: .html_url}'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"  SKIP {full_name}: API error")
            continue

        issues = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    issues.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        imported = 0
        for iss in issues[:30]:
            is_assigned = 1 if iss.get("assignee") else 0
            labels_json = json.dumps(iss.get("labels", []))
            db.execute("""INSERT OR IGNORE INTO issues
                (repo_id, number, title, body, state, labels, is_assigned,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (repo["id"], iss["number"], iss.get("title", ""),
                 iss.get("body", ""), iss.get("state", "open"),
                 labels_json, is_assigned,
                 iss.get("created_at"), iss.get("updated_at")))
            imported += 1

        if imported > 0:
            print(f"  {full_name}: {imported} issues")
            total_issues += imported

    except Exception as e:
        print(f"  SKIP {full_name}: {e}")

db.commit()
print(f"\nTotal: {total_issues} issues imported")
db.close()
