import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# Add the current directory to sys.path so we can import src
sys.path.append(os.getcwd())

# Mock or import Scanner
try:
    from src.scanner import Scanner, SEARCH_REPOS_QUERY
    from src.db import get_connection
except ImportError:
    # If running from src directly
    sys.path.append(os.path.dirname(os.getcwd()))
    from src.scanner import Scanner, SEARCH_REPOS_QUERY
    from src.db import get_connection

# Configuration
OUTPUT_FILE = "scanned repos/all_repos.sql"
MIN_STARS = 1000
RATE_LIMIT_BUFFER = 0.5  # Stop if remaining < 50% of limit

class DumpScanner(Scanner):
    def __init__(self, token=None, output_file=OUTPUT_FILE):
        super().__init__(token)
        self.output_file = output_file
        self.rate_limit_max = 5000 # Default, will update
        self.sql_file = open(self.output_file, "a", encoding="utf-8")
        print(f"Logging to {self.output_file}", flush=True)

    def __del__(self):
        if hasattr(self, 'sql_file') and self.sql_file:
            self.sql_file.close()

    async def get_rate_limit(self):
        """Fetch current rate limit status."""
        query = """
        query {
          rateLimit {
            limit
            remaining
            resetAt
          }
        }
        """
        data = await self._graphql_query(query)
        rl = data["rateLimit"]
        self.rate_limit_max = rl["limit"]
        return rl

    def dump_repository(self, repo: dict):
        """Write repository data as SQL INSERT statement."""
        # Sanitize strings
        def sql_safe(val):
            if val is None:
                return "NULL"
            if isinstance(val, bool):
                return "1" if val else "0"
            if isinstance(val, (int, float)):
                return str(val)
            # Escape single quotes
            val_str = str(val).replace("'", "''")
            return f"'{val_str}'"

        full_name = repo.get("full_name") or f"{repo['owner']}/{repo['name']}"
        topics = repo.get("topics", [])
        if isinstance(topics, list):
            topics = json.dumps(topics)

        columns = [
            "github_id", "owner", "name", "full_name", "url", 
            "stars", "forks", "open_issues", "description", 
            "topics", "language", "license", "last_scanned"
        ]
        
        values = [
            repo.get("github_id"),
            repo.get("owner"),
            repo.get("name"),
            full_name,
            repo.get("url"),
            repo.get("stars", 0),
            repo.get("forks", 0),
            repo.get("open_issues", 0),
            repo.get("description"),
            topics,
            repo.get("language"),
            repo.get("license"),
            datetime.now(timezone.utc).isoformat()
        ]

        sql_values = [sql_safe(v) for v in values]
        
        sql = f"INSERT OR IGNORE INTO repositories ({', '.join(columns)}) VALUES ({', '.join(sql_values)});"
        self.sql_file.write(sql + "\n")
        self.sql_file.flush()

    async def _scan_repos_shard(self, conn, query_str: str) -> int:
        """Scan all repos matching a search query, paginating through all results."""
        count = 0
        cursor = None

        while True:
            self._rate_limit_wait()
            try:
                data = await self._graphql_query(SEARCH_REPOS_QUERY, {
                    "query": query_str,
                    "cursor": cursor,
                })
            except Exception as e:
                print(f"    Error: {e}", flush=True)
                await asyncio.sleep(60)
                continue

            search = data.get("search")
            if not search:
                break
                
            nodes = search.get("nodes")
            if not nodes:
                break

            for node in nodes:
                if not node or not node.get("nameWithOwner"):
                    continue
                topics = [t["topic"]["name"] for t in node.get("repositoryTopics", {}).get("nodes", [])]
                
                repo_data = {
                    "github_id": node.get("databaseId"),
                    "owner": node["owner"]["login"],
                    "name": node["name"],
                    "full_name": node["nameWithOwner"],
                    "url": node["url"],
                    "description": node.get("description"),
                    "stars": node.get("stargazerCount", 0),
                    "forks": node.get("forkCount", 0),
                    "open_issues": node.get("issues", {}).get("totalCount", 0),
                    "language": (node.get("primaryLanguage") or {}).get("name"),
                    "license": (node.get("licenseInfo") or {}).get("spdxId"),
                    "topics": topics,
                }
                
                self.dump_repository(repo_data)
                count += 1

            page_info = search["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

            # Rate limit safety - STRICTER
            rl = data.get("rateLimit", {})
            remaining = rl.get("remaining", 999)
            limit = self.rate_limit_max 
            
            threshold = limit * RATE_LIMIT_BUFFER
            
            if remaining < threshold:
                reset_at_str = rl.get("resetAt")
                wait_time = 60
                if reset_at_str:
                    try:
                        if reset_at_str.endswith("Z"):
                            reset_at_str = reset_at_str[:-1] + "+00:00"
                        reset_dt = datetime.fromisoformat(reset_at_str)
                        now_dt = datetime.now(timezone.utc)
                        wait_time = (reset_dt - now_dt).total_seconds() + 10 
                        if wait_time < 0: wait_time = 60
                    except Exception as e:
                        print(f"    Error parsing reset time {reset_at_str}: {e}", flush=True)
                        pass
                
                print(f"    Rate limit low ({remaining}/{limit}), waiting {wait_time:.1f}s until reset...", flush=True)
                await asyncio.sleep(wait_time)
                
                new_rl = await self.get_rate_limit()
                self.rate_limit_max = new_rl["limit"]

        return count

async def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN environment variable not set.", flush=True)
        return

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    scanner = DumpScanner(token)
    
    print("Checking GitHub API rate limit...", flush=True)
    try:
        rl = await scanner.get_rate_limit()
        print(f"  Limit: {rl['limit']}, Remaining: {rl['remaining']}, Reset At: {rl['resetAt']}", flush=True)
        
        scanner.rate_limit_max = rl["limit"]
        threshold = rl["limit"] * RATE_LIMIT_BUFFER
        
        if rl["remaining"] < threshold:
            print(f"  Starting below {RATE_LIMIT_BUFFER*100}% capacity. Waiting...", flush=True)
            reset_at_str = rl.get("resetAt")
            if reset_at_str.endswith("Z"):
                reset_at_str = reset_at_str[:-1] + "+00:00"
            reset_dt = datetime.fromisoformat(reset_at_str)
            now_dt = datetime.now(timezone.utc)
            wait_time = (reset_dt - now_dt).total_seconds() + 10
            if wait_time > 0:
                 print(f"  Waiting {wait_time:.1f}s until reset...", flush=True)
                 await asyncio.sleep(wait_time)

    except Exception as e:
        print(f"Error checking rate limit: {e}", flush=True)
        return

    print(f"Starting full index scan for stars >= {MIN_STARS}...", flush=True)
    
    def progress(stage, current, total=None, **kwargs):
        if stage == "shards":
            print(f"  Created {current} shards to scan.", flush=True)
        elif stage == "shard_done":
            shard_num = kwargs.get("shard_num", 0)
            shard_total = kwargs.get("shard_total", 0)
            shard_count = kwargs.get("shard_count", 0)
            print(f"  Shard {shard_num}/{shard_total} done. Found {shard_count} repos. Total indexed: {current}", flush=True)

    await scanner.full_index(min_stars=MIN_STARS, progress_cb=progress)
    print("Scan complete.", flush=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Scan interrupted.", flush=True)
