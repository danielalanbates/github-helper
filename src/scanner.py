"""GitHub discovery and scanning engine."""

import asyncio
import json
import time
import httpx
from ghapi.all import GhApi
from src.config import (
    GITHUB_TOKEN, GITHUB_GRAPHQL_URL, SOCIAL_TOPICS,
    BEGINNER_LABELS, SEARCH_DELAY_SECONDS, SEED_DATA_PATH,
)
from src.db import get_connection, upsert_repository, upsert_issue
from src.utils import parse_github_url, now_iso

# --- GraphQL Queries ---

SEARCH_REPOS_QUERY = """
query SearchRepos($query: String!, $cursor: String) {
  search(type: REPOSITORY, query: $query, first: 50, after: $cursor) {
    repositoryCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Repository {
        databaseId
        nameWithOwner
        owner { login }
        name
        url
        description
        stargazerCount
        forkCount
        pushedAt
        issues(states: [OPEN]) { totalCount }
        primaryLanguage { name }
        licenseInfo { spdxId }
        repositoryTopics(first: 20) {
          nodes { topic { name } }
        }
      }
    }
  }
  rateLimit { remaining resetAt }
}
"""

SEARCH_ISSUES_QUERY = """
query SearchIssues($query: String!, $cursor: String) {
  search(type: ISSUE, query: $query, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        databaseId
        number
        title
        bodyText
        state
        labels(first: 10) { nodes { name } }
        comments { totalCount }
        createdAt
        updatedAt
        assignees(first: 1) { totalCount }
        repository {
          databaseId
          nameWithOwner
          owner { login }
          name
          url
          stargazerCount
          forkCount
          primaryLanguage { name }
        }
      }
    }
  }
  rateLimit { remaining resetAt }
}
"""

REPO_DETAIL_QUERY = """
query RepoDetail($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    databaseId
    nameWithOwner
    owner { login }
    name
    url
    description
    stargazerCount
    forkCount
    pushedAt
    issues(states: [OPEN]) { totalCount }
    primaryLanguage { name }
    licenseInfo { spdxId }
    repositoryTopics(first: 20) {
      nodes { topic { name } }
    }
    codeOfConduct { name }
    contributingGuidelines { body }
  }
  rateLimit { remaining resetAt }
}
"""


class Scanner:
    def __init__(self, token: str = GITHUB_TOKEN):
        self.token = token
        self.api = GhApi(token=token) if token else None
        self.graphql_headers = {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        }
        self._last_search_time = 0.0

    async def _graphql_query(self, query: str, variables: dict = None) -> dict:
        """Execute a GraphQL query against GitHub API."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                GITHUB_GRAPHQL_URL,
                json={"query": query, "variables": variables or {}},
                headers=self.graphql_headers,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]

    def _rate_limit_wait(self):
        """Enforce minimum delay between search API calls."""
        elapsed = time.time() - self._last_search_time
        if elapsed < SEARCH_DELAY_SECONDS:
            time.sleep(SEARCH_DELAY_SECONDS - elapsed)
        self._last_search_time = time.time()

    def import_seed_data(self) -> int:
        """Import repos from awesome-for-beginners/data.json as seed."""
        with open(SEED_DATA_PATH) as f:
            data = json.load(f)

        conn = get_connection()
        count = 0
        for repo in data.get("repositories", []):
            url = repo["link"]
            if "github.com" not in url and "gitlab.com" not in url:
                continue
            try:
                owner, name = parse_github_url(url)
            except (IndexError, ValueError):
                continue

            upsert_repository(conn, {
                "owner": owner,
                "name": name,
                "full_name": f"{owner}/{name}",
                "url": url,
                "description": repo.get("description", ""),
                "language": repo.get("technologies", [None])[0] if repo.get("technologies") else None,
            })
            count += 1
        return count

    async def scan_by_topic(self, topic: str, min_stars: int = 100, limit: int = 200) -> int:
        """Scan repos by GitHub topic. Returns count of repos found."""
        conn = get_connection()
        query_str = f"topic:{topic} stars:>{min_stars} sort:stars-desc"
        count = 0
        cursor = None

        while count < limit:
            self._rate_limit_wait()
            try:
                data = await self._graphql_query(SEARCH_REPOS_QUERY, {
                    "query": query_str,
                    "cursor": cursor,
                })
            except Exception as e:
                print(f"  Error searching topic '{topic}': {e}")
                break

            search = data["search"]
            nodes = search["nodes"]

            if not nodes:
                break

            for node in nodes:
                if count >= limit:
                    break
                topics = [t["topic"]["name"] for t in node.get("repositoryTopics", {}).get("nodes", [])]
                upsert_repository(conn, {
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
                    "pushed_at": node.get("pushedAt"),
                })
                count += 1

            page_info = search["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

            remaining = data.get("rateLimit", {}).get("remaining", 999)
            if remaining < 50:
                print(f"  Rate limit low ({remaining}), pausing...")
                await asyncio.sleep(60)

        return count

    async def scan_by_labels(self, min_stars: int = 100, limit: int = 200) -> int:
        """Scan for issues across multiple search strategies (beginner + general bugs)."""
        # Run multiple search strategies for broader coverage
        searches = [
            # Beginner-friendly issues
            f'label:"good first issue" state:open stars:>{min_stars} created:>2025-01-01 sort:reactions-+1-desc',
            f'label:"good first issue" state:open stars:>{min_stars} created:>2025-01-01 sort:created-desc',
            f'label:"help wanted" state:open stars:>{min_stars} created:>2025-01-01 sort:created-desc',
            # General bugs — the main pipeline now
            f'label:bug state:open stars:>{min_stars} language:Python created:>2025-06-01 sort:reactions-+1-desc',
            f'label:bug state:open stars:>{min_stars} language:TypeScript created:>2025-06-01 sort:reactions-+1-desc',
            f'label:bug state:open stars:>{min_stars} language:JavaScript created:>2025-06-01 sort:reactions-+1-desc',
            f'label:bug state:open stars:>{min_stars} created:>2025-09-01 sort:created-desc',
            f'label:bug state:open stars:>{min_stars} created:>2025-09-01 sort:reactions-+1-desc',
        ]
        total = 0
        for i, query_str in enumerate(searches):
            remaining_limit = limit - total
            if remaining_limit <= 0:
                break
            print(f"  Search strategy {i+1}/{len(searches)}...")
            found = await self._scan_issues_query(query_str, min_stars, remaining_limit)
            total += found
        return total

    async def _scan_issues_query(self, query_str: str, min_stars: int, limit: int) -> int:
        """Run a single issue search query with filtering. Returns count found."""
        conn = get_connection()
        count = 0
        skipped = 0
        cursor = None

        while count < limit:
            self._rate_limit_wait()
            try:
                data = await self._graphql_query(SEARCH_ISSUES_QUERY, {
                    "query": query_str,
                    "cursor": cursor,
                })
            except Exception as e:
                print(f"  Error searching issues: {e}")
                break

            search = data["search"]
            nodes = search["nodes"]

            if not nodes:
                break

            for node in nodes:
                if count >= limit:
                    break

                repo_node = node.get("repository")
                if not repo_node:
                    continue

                # Filter: skip repos with too few stars (API doesn't always enforce)
                stars = repo_node.get("stargazerCount", 0)
                if stars < min_stars:
                    skipped += 1
                    continue

                # Filter: skip already-assigned issues
                assignee_count = node.get("assignees", {}).get("totalCount", 0)
                if assignee_count > 0:
                    skipped += 1
                    continue

                # Filter: skip issues with no body text
                body = node.get("bodyText", "") or ""
                if len(body.strip()) < 10:
                    skipped += 1
                    continue

                # Upsert the parent repo
                repo_id = upsert_repository(conn, {
                    "github_id": repo_node.get("databaseId"),
                    "owner": repo_node["owner"]["login"],
                    "name": repo_node["name"],
                    "full_name": repo_node["nameWithOwner"],
                    "url": repo_node["url"],
                    "stars": stars,
                    "forks": repo_node.get("forkCount", 0),
                    "language": (repo_node.get("primaryLanguage") or {}).get("name"),
                })

                # Upsert the issue
                labels = [l["name"] for l in node.get("labels", {}).get("nodes", [])]
                upsert_issue(conn, {
                    "github_id": node.get("databaseId"),
                    "repo_id": repo_id,
                    "number": node["number"],
                    "title": node.get("title"),
                    "body": body[:5000],
                    "labels": labels,
                    "state": node.get("state", "OPEN").lower(),
                    "comments_count": node.get("comments", {}).get("totalCount", 0),
                    "created_at": node.get("createdAt"),
                    "updated_at": node.get("updatedAt"),
                    "is_assigned": False,
                })
                count += 1

            page_info = search["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

            remaining = data.get("rateLimit", {}).get("remaining", 999)
            if remaining < 50:
                print(f"  Rate limit low ({remaining}), pausing...")
                await asyncio.sleep(60)

        if skipped:
            print(f"  Filtered out {skipped} low-quality issues")
        return count

    async def scan_social_good_repos(self, min_stars: int = 50, limit_per_topic: int = 100) -> int:
        """Scan all social-good topics. Returns total repos found."""
        total = 0
        for topic in sorted(SOCIAL_TOPICS):
            print(f"  Scanning topic: {topic}...")
            count = await self.scan_by_topic(topic, min_stars, limit_per_topic)
            print(f"    Found {count} repos")
            total += count
            await asyncio.sleep(1)
        return total

    async def enrich_repository(self, full_name: str) -> dict:
        """Fetch detailed info for a single repo via GraphQL."""
        owner, name = full_name.split("/")
        try:
            data = await self._graphql_query(REPO_DETAIL_QUERY, {
                "owner": owner, "name": name
            })
        except Exception as e:
            return {"error": str(e)}

        repo = data["repository"]
        if not repo:
            return {"error": "Repository not found"}

        topics = [t["topic"]["name"] for t in repo.get("repositoryTopics", {}).get("nodes", [])]
        has_coc = repo.get("codeOfConduct") is not None
        has_contributing = repo.get("contributingGuidelines") is not None

        conn = get_connection()
        upsert_repository(conn, {
            "github_id": repo.get("databaseId"),
            "owner": owner,
            "name": name,
            "full_name": repo["nameWithOwner"],
            "url": repo["url"],
            "description": repo.get("description"),
            "stars": repo.get("stargazerCount", 0),
            "forks": repo.get("forkCount", 0),
            "open_issues": repo.get("issues", {}).get("totalCount", 0),
            "language": (repo.get("primaryLanguage") or {}).get("name"),
            "license": (repo.get("licenseInfo") or {}).get("spdxId"),
            "topics": topics,
            "pushed_at": repo.get("pushedAt"),
            "has_coc": has_coc,
            "has_contributing": has_contributing,
        })

        return {"success": True, "full_name": repo["nameWithOwner"]}

    async def enrich_batch(self, full_names: list[str]) -> int:
        """Enrich multiple repos. Returns count enriched."""
        count = 0
        for full_name in full_names:
            self._rate_limit_wait()
            result = await self.enrich_repository(full_name)
            if result.get("success"):
                count += 1
        return count

    # ========================================================================
    # PRODUCTION: Full-index scan of all repos with N+ stars
    # ========================================================================

    async def full_index(self, min_stars: int = 1000, progress_cb=None) -> int:
        """Index ALL GitHub repos with min_stars+ by sharding star ranges.

        GitHub search returns max 1000 results per query, so we shard into
        narrow star ranges (e.g. stars:1000..1050) to stay under the limit.
        Each shard paginates through all results before moving to the next.
        """
        conn = get_connection()

        # Step 1: Determine star range shards
        # Count total repos first
        data = await self._graphql_query(
            'query($q:String!){search(type:REPOSITORY,query:$q,first:1){repositoryCount}}',
            {"q": f"stars:>={min_stars}"}
        )
        total_repos = data["search"]["repositoryCount"]
        if progress_cb:
            progress_cb("init", 0, total_repos)

        # Build star range shards: narrow enough that each has <1000 repos
        shards = await self._build_star_shards(min_stars, total_repos)
        if progress_cb:
            progress_cb("shards", len(shards), total_repos)

        # Step 2: Scan each shard
        total_indexed = 0
        for i, (lo, hi) in enumerate(shards):
            if hi is None:
                query_str = f"stars:>={lo} sort:stars-desc"
            else:
                query_str = f"stars:{lo}..{hi} sort:stars-desc"

            count = await self._scan_repos_shard(conn, query_str)
            total_indexed += count

            if progress_cb:
                progress_cb("shard_done", total_indexed, total_repos, shard_num=i+1, shard_total=len(shards), shard_count=count)

        return total_indexed

    async def _build_star_shards(self, min_stars: int, total_repos: int) -> list[tuple]:
        """Build star range shards so each has <1000 repos.

        Recursively splits ranges until every shard is under the GitHub
        search API's 1000-result cap.
        """
        COUNT_QUERY = 'query($q:String!){search(type:REPOSITORY,query:$q,first:1){repositoryCount}}'

        async def count_range(lo, hi):
            self._rate_limit_wait()
            try:
                if hi is None:
                    q = f"stars:>={lo}"
                else:
                    q = f"stars:{lo}..{hi}"
                data = await self._graphql_query(COUNT_QUERY, {"q": q})
                return data["search"]["repositoryCount"]
            except Exception:
                return 999  # assume near limit

        async def split(lo, hi, depth=0):
            """Recursively split until each shard has <950 repos."""
            c = await count_range(lo, hi)
            if c <= 950 or (hi is not None and hi - lo <= 1) or depth > 15:
                return [(lo, hi)]

            if hi is None:
                # Top shard - find a reasonable split point
                mid = lo * 2
            else:
                mid = (lo + hi) // 2

            # Split into two halves
            left = await split(lo, mid, depth + 1)
            right = await split(mid + 1, hi, depth + 1)
            return left + right

        # Start with coarse boundaries, then recursively refine
        boundaries = [500000, 100000, 50000, 20000, 10000, 5000, 3000, 2000, 1500, 1200, min_stars]
        boundaries = sorted(set(b for b in boundaries if b >= min_stars), reverse=True)

        all_shards = []

        # Top shard: above highest boundary
        top = await split(boundaries[0], None)
        all_shards.extend(top)

        # Each range between consecutive boundaries
        for i in range(len(boundaries) - 1):
            hi = boundaries[i]
            lo = boundaries[i + 1]
            shards = await split(lo, hi)
            all_shards.extend(shards)

        print(f"  Built {len(all_shards)} shards (verified each <1000 repos)", flush=True)
        return all_shards

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
                print(f"    Error: {e}")
                break

            search = data["search"]
            nodes = search["nodes"]
            if not nodes:
                break

            for node in nodes:
                if not node or not node.get("nameWithOwner"):
                    continue
                topics = [t["topic"]["name"] for t in node.get("repositoryTopics", {}).get("nodes", [])]
                upsert_repository(conn, {
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
                    "pushed_at": node.get("pushedAt"),
                })
                count += 1

            page_info = search["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

            # Rate limit safety
            remaining = data.get("rateLimit", {}).get("remaining", 999)
            if remaining < 100:
                print(f"    Rate limit low ({remaining}), waiting 60s...")
                await asyncio.sleep(60)

        return count

    async def scan_issues_for_repos(self, min_stars: int = 1000, limit_per_repo: int = 10, progress_cb=None) -> int:
        """Scan open beginner-friendly issues using batched per-repo GraphQL queries.

        Batches 5 repos per GraphQL query using aliases to fetch issues efficiently.
        Repos are processed in order of combined_score (best repos first).
        """
        conn = get_connection()
        repos = conn.execute("""
            SELECT id, full_name, stars FROM repositories
            WHERE stars >= ?
            ORDER BY combined_score DESC, stars DESC
        """, (min_stars,)).fetchall()

        total = 0
        batch_size = 5  # 5 repos per GraphQL query

        for batch_start in range(0, len(repos), batch_size):
            batch = repos[batch_start:batch_start + batch_size]

            # Build a batched GraphQL query with aliases
            fragments = []
            variables = {}
            for j, repo in enumerate(batch):
                owner, name = repo["full_name"].split("/")
                alias = f"r{j}"
                fragments.append(f"""
                    {alias}: repository(owner: ${alias}o, name: ${alias}n) {{
                        issues(first: {limit_per_repo}, states: [OPEN],
                               orderBy: {{field: CREATED_AT, direction: DESC}}) {{
                            nodes {{
                                databaseId number title bodyText state
                                labels(first: 10) {{ nodes {{ name }} }}
                                comments {{ totalCount }}
                                createdAt updatedAt
                                assignees(first: 1) {{ totalCount }}
                            }}
                        }}
                    }}
                """)
                variables[f"{alias}o"] = owner
                variables[f"{alias}n"] = name

            # Build variable declarations
            var_decls = ", ".join(
                f"${alias}o: String!, ${alias}n: String!"
                for alias in [f"r{j}" for j in range(len(batch))]
            )
            query = f"query({var_decls}) {{\n{''.join(fragments)}\n  rateLimit {{ remaining resetAt }}\n}}"

            self._rate_limit_wait()
            try:
                data = await self._graphql_query(query, variables)
            except Exception as e:
                print(f"    Error at batch {batch_start}: {e}", flush=True)
                continue

            # Process results for each repo in the batch
            for j, repo in enumerate(batch):
                alias = f"r{j}"
                repo_data = data.get(alias)
                if not repo_data:
                    continue

                issues_data = repo_data.get("issues", {})
                nodes = issues_data.get("nodes", [])
                for node in nodes:
                    if not node:
                        continue
                    assignee_count = node.get("assignees", {}).get("totalCount", 0)
                    body = node.get("bodyText", "") or ""
                    labels = [l["name"] for l in node.get("labels", {}).get("nodes", [])]

                    # Skip assigned or empty
                    if assignee_count > 0 or len(body.strip()) < 10:
                        continue

                    upsert_issue(conn, {
                        "github_id": node.get("databaseId"),
                        "repo_id": repo["id"],
                        "number": node["number"],
                        "title": node.get("title"),
                        "body": body[:5000],
                        "labels": labels,
                        "state": "open",
                        "comments_count": node.get("comments", {}).get("totalCount", 0),
                        "created_at": node.get("createdAt"),
                        "updated_at": node.get("updatedAt"),
                        "is_assigned": False,
                    })
                    total += 1

            done = min(batch_start + batch_size, len(repos))
            if progress_cb and (done % 50 < batch_size or done == len(repos)):
                progress_cb("issues", total, len(repos), repos_done=done)

        return total

    async def _fetch_repo_issues(self, conn, repo_id: int, full_name: str, limit: int) -> int:
        """Fetch open beginner-friendly issues for a single repo."""
        owner, name = full_name.split("/")
        query = """
        query($owner: String!, $name: String!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            issues(first: 20, states: [OPEN], labels: ["good first issue", "help wanted", "bug"], after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
              nodes {
                databaseId
                number
                title
                bodyText
                state
                labels(first: 10) { nodes { name } }
                comments { totalCount }
                createdAt
                updatedAt
                assignees(first: 1) { totalCount }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        count = 0
        cursor = None

        while count < limit:
            data = await self._graphql_query(query, {"owner": owner, "name": name, "cursor": cursor})
            repo_data = data.get("repository")
            if not repo_data:
                break
            issues_data = repo_data.get("issues", {})
            nodes = issues_data.get("nodes", [])
            if not nodes:
                break

            for node in nodes:
                if count >= limit:
                    break
                if not node:
                    continue

                assignee_count = node.get("assignees", {}).get("totalCount", 0)
                body = node.get("bodyText", "") or ""
                labels = [l["name"] for l in node.get("labels", {}).get("nodes", [])]

                # Skip assigned or empty
                if assignee_count > 0 or len(body.strip()) < 10:
                    continue

                upsert_issue(conn, {
                    "github_id": node.get("databaseId"),
                    "repo_id": repo_id,
                    "number": node["number"],
                    "title": node.get("title"),
                    "body": body[:5000],
                    "labels": labels,
                    "state": "open",
                    "comments_count": node.get("comments", {}).get("totalCount", 0),
                    "created_at": node.get("createdAt"),
                    "updated_at": node.get("updatedAt"),
                    "is_assigned": False,
                })
                count += 1

            page_info = issues_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info["endCursor"]

        return count

    async def maintain(self, min_stars: int = 1000, progress_cb=None) -> dict:
        """Daily maintenance: find new repos, refresh stale ones, update issues."""
        conn = get_connection()
        results = {"new_repos": 0, "refreshed": 0, "new_issues": 0}

        # 1. Find new repos that recently crossed the star threshold
        # Search for repos created or recently updated with enough stars
        self._rate_limit_wait()
        query_str = f"stars:>={min_stars} pushed:>{_days_ago(7)} sort:updated-desc"
        count = await self._scan_repos_shard(conn, query_str)
        results["new_repos"] = count
        if progress_cb:
            progress_cb("new_repos", count)

        # 2. Refresh stale repos (not scanned in 7+ days)
        stale = conn.execute("""
            SELECT full_name FROM repositories
            WHERE stars >= ? AND (
                last_scanned IS NULL OR
                last_scanned < datetime('now', '-7 days')
            )
            ORDER BY stars DESC
            LIMIT 500
        """, (min_stars,)).fetchall()

        refreshed = 0
        for repo in stale:
            self._rate_limit_wait()
            result = await self.enrich_repository(repo["full_name"])
            if result.get("success"):
                refreshed += 1
        results["refreshed"] = refreshed
        if progress_cb:
            progress_cb("refreshed", refreshed)

        # 3. Scan for new issues on top-ranked repos
        issues = await self.scan_issues_for_repos(min_stars, limit_per_repo=5, progress_cb=progress_cb)
        results["new_issues"] = issues

        return results


def _days_ago(n: int) -> str:
    """Return ISO date string for N days ago."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")
