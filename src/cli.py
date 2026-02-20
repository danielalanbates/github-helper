"""Main CLI entry point for the Do-Good GitHub Agent."""

import asyncio
import json
import sys
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="dogood",
    help="Do-Good GitHub Agent: Find and fix bugs on social-good open-source projects",
)
console = Console(force_terminal=False)


@app.command()
def scan(
    topic: str = typer.Option(None, help="Specific topic to scan (e.g. 'nonprofit')"),
    min_stars: int = typer.Option(100, help="Minimum star count"),
    limit: int = typer.Option(200, help="Max repos/issues to fetch"),
    seed: bool = typer.Option(False, help="Import from awesome-for-beginners seed data"),
    labels: bool = typer.Option(False, help="Scan by beginner-friendly issue labels"),
):
    """Scan GitHub for repos and issues matching do-good criteria."""
    from src.scanner import Scanner
    scanner = Scanner()

    if seed:
        count = scanner.import_seed_data()
        console.print(f"[green]Imported {count} repos from seed data[/green]")
        return

    if not scanner.token:
        console.print("[red]Error: GITHUB_TOKEN not set. Copy .env.example to .env and add your token.[/red]")
        raise typer.Exit(1)

    if topic:
        console.print(f"[blue]Scanning topic: {topic}...[/blue]")
        count = asyncio.run(scanner.scan_by_topic(topic, min_stars, limit))
        console.print(f"[green]Found {count} repos for topic '{topic}'[/green]")
    elif labels:
        console.print("[blue]Scanning for issues with beginner-friendly labels...[/blue]")
        count = asyncio.run(scanner.scan_by_labels(min_stars, limit))
        console.print(f"[green]Found {count} issues with beginner labels[/green]")
    else:
        console.print("[blue]Scanning all social-good topics...[/blue]")
        count = asyncio.run(scanner.scan_social_good_repos(min_stars, limit))
        console.print(f"[green]Scanned {count} social-good repos total[/green]")


@app.command()
def enrich(
    limit: int = typer.Option(50, help="Max repos to enrich"),
):
    """Enrich repos with detailed data (community health, topics, etc.)."""
    from src.scanner import Scanner
    from src.db import get_connection

    scanner = Scanner()
    if not scanner.token:
        console.print("[red]Error: GITHUB_TOKEN not set.[/red]")
        raise typer.Exit(1)

    conn = get_connection()
    repos = conn.execute("""
        SELECT full_name FROM repositories
        WHERE has_contributing = 0 AND has_coc = 0
        ORDER BY stars DESC
        LIMIT ?
    """, (limit,)).fetchall()

    full_names = [r["full_name"] for r in repos if r["full_name"]]
    console.print(f"[blue]Enriching {len(full_names)} repositories...[/blue]")
    count = asyncio.run(scanner.enrich_batch(full_names))
    console.print(f"[green]Enriched {count} repositories[/green]")


@app.command()
def rank(
    top: int = typer.Option(50, help="Number of top repos to display"),
):
    """Rank repositories by do-good score and display results."""
    from src.ranker import Ranker

    ranker = Ranker()
    updated = ranker.rank_all()
    console.print(f"[blue]Ranked {updated} repositories[/blue]")

    from src.db import get_connection
    conn = get_connection()
    repos = conn.execute(
        "SELECT * FROM repositories ORDER BY combined_score DESC LIMIT ?",
        (top,)
    ).fetchall()

    if not repos:
        console.print("[yellow]No repos in database. Run 'scan' first.[/yellow]")
        return

    table = Table(title=f"Top {min(top, len(repos))} Do-Good Repositories")
    table.add_column("Rank", style="dim", width=4)
    table.add_column("Repository", style="bold", max_width=40)
    table.add_column("Stars", justify="right", width=7)
    table.add_column("Lang", width=10)
    table.add_column("Pop", justify="right", width=6)
    table.add_column("Social", justify="right", width=6)
    table.add_column("Need", justify="right", width=6)
    table.add_column("Kind", justify="right", width=6)
    table.add_column("Score", justify="right", style="green bold", width=6)

    for i, repo in enumerate(repos, 1):
        table.add_row(
            str(i),
            repo["full_name"] or f"{repo['owner']}/{repo['name']}",
            str(repo["stars"] or 0),
            (repo["language"] or "?")[:10],
            f"{repo['popularity_score']:.2f}",
            f"{repo['social_impact_score']:.2f}",
            f"{repo['need_score']:.2f}",
            f"{repo['kindness_score']:.2f}",
            f"{repo['combined_score']:.2f}",
        )
    console.print(table)


@app.command()
def issues(
    top: int = typer.Option(20, help="Number of top issues to display"),
):
    """Find and display the best issues to work on."""
    from src.ranker import Ranker
    from src.db import get_connection

    ranker = Ranker()
    ranked = ranker.rank_issues()
    console.print(f"[blue]Ranked {ranked} open issues[/blue]")

    conn = get_connection()
    rows = conn.execute("""
        SELECT i.*, r.full_name, r.language, r.stars
        FROM issues i
        JOIN repositories r ON i.repo_id = r.id
        WHERE i.state = 'open' AND i.is_assigned = 0
        ORDER BY i.priority_score DESC
        LIMIT ?
    """, (top,)).fetchall()

    if not rows:
        console.print("[yellow]No open issues found. Run 'scan --labels' first.[/yellow]")
        return

    table = Table(title=f"Top {min(top, len(rows))} Issues to Solve")
    table.add_column("ID", style="dim", width=4)
    table.add_column("Repository", max_width=30)
    table.add_column("#", justify="right", width=6)
    table.add_column("Title", max_width=50)
    table.add_column("Labels", max_width=25)
    table.add_column("Priority", justify="right", style="green bold", width=8)

    for row in rows:
        labels = json.loads(row["labels"] or "[]")
        table.add_row(
            str(row["id"]),
            row["full_name"],
            str(row["number"]),
            (row["title"] or "")[:50],
            ", ".join(labels[:3]),
            f"{row['priority_score']:.3f}",
        )
    console.print(table)


@app.command()
def solve(
    issue_id: int = typer.Option(..., help="Database issue ID to solve"),
    dry_run: bool = typer.Option(False, help="Analyze only, don't create PR"),
    agent_id: str = typer.Option("", help="Agent ID (for factory mode)"),
    model_tier: str = typer.Option("", help="Model tier JSON (for factory mode)"),
):
    """Solve a specific issue using Claude and submit a PR."""
    from src.solver import Solver

    # Parse model tier if provided (from factory subprocess)
    tier_dict = None
    if model_tier:
        tier_dict = json.loads(model_tier)

    console.print(f"[blue]Solving issue ID {issue_id}...[/blue]")
    solver = Solver(
        agent_id=agent_id or "main",
        model_tier=tier_dict,
    )
    result = asyncio.run(solver.solve_issue(issue_id))

    if result["success"]:
        console.print(f"[green bold]PR created: {result['pr_url']}[/green bold]")
    else:
        console.print(f"[red]Failed: {result.get('error', 'Unknown error')}[/red]")


@app.command()
def run(
    max_issues: int = typer.Option(1, help="Max issues to solve per run"),
    min_stars: int = typer.Option(100, help="Minimum stars for scanning"),
    skip_scan: bool = typer.Option(False, help="Skip scanning, use existing data"),
):
    """Run the full pipeline: scan -> rank -> pick best issue -> solve -> PR."""
    from src.scanner import Scanner
    from src.ranker import Ranker
    from src.solver import Solver
    from src.db import get_connection

    console.print("[bold]Starting Do-Good Agent pipeline...[/bold]\n")

    if not skip_scan:
        # Step 1: Scan
        console.print("[blue]Step 1: Scanning GitHub for issues...[/blue]")
        scanner = Scanner()
        if not scanner.token:
            console.print("[red]Error: GITHUB_TOKEN not set.[/red]")
            raise typer.Exit(1)
        count = asyncio.run(scanner.scan_by_labels(min_stars, limit=200))
        console.print(f"  Found {count} issues\n")
    else:
        console.print("[dim]Skipping scan, using existing data[/dim]\n")

    # Step 2: Rank
    console.print("[blue]Step 2: Ranking repositories and issues...[/blue]")
    ranker = Ranker()
    ranker.rank_all()
    issue_count = ranker.rank_issues()
    console.print(f"  Ranked {issue_count} issues\n")

    # Step 3: Pick best issue(s)
    console.print("[blue]Step 3: Selecting best issues...[/blue]")
    conn = get_connection()
    top_issues = conn.execute("""
        SELECT i.id, i.number, i.title, r.full_name
        FROM issues i
        JOIN repositories r ON i.repo_id = r.id
        WHERE i.state = 'open' AND i.is_assigned = 0
        ORDER BY i.priority_score DESC
        LIMIT ?
    """, (max_issues,)).fetchall()

    if not top_issues:
        console.print("[yellow]No suitable issues found. Try scanning first.[/yellow]")
        return

    for issue in top_issues:
        console.print(f"  Selected: {issue['full_name']}#{issue['number']} - {issue['title'][:60]}")

    # Step 4: Solve
    console.print(f"\n[blue]Step 4: Solving {len(top_issues)} issue(s)...[/blue]")
    solver = Solver()
    for issue in top_issues:
        console.print(f"\n  Working on {issue['full_name']}#{issue['number']}...")
        try:
            result = asyncio.run(solver.solve_issue(issue["id"]))
            if result["success"]:
                console.print(f"  [green bold]PR: {result['pr_url']}[/green bold]")
            else:
                console.print(f"  [red]Failed: {result.get('error')}[/red]")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

    console.print("\n[bold green]Pipeline complete![/bold green]")


@app.command()
def history():
    """Show contribution history."""
    from src.db import get_connection

    conn = get_connection()
    rows = conn.execute("""
        SELECT c.*, r.full_name, i.number as issue_number, i.title as issue_title
        FROM contributions c
        LEFT JOIN repositories r ON c.repo_id = r.id
        LEFT JOIN issues i ON c.issue_id = i.id
        ORDER BY c.created_at DESC
    """).fetchall()

    if not rows:
        console.print("[dim]No contributions yet. Run 'solve' or 'run' to get started.[/dim]")
        return

    table = Table(title="Contribution History")
    table.add_column("Date", width=19)
    table.add_column("Repository", max_width=30)
    table.add_column("Issue", width=8)
    table.add_column("Action", width=12)
    table.add_column("Status", width=12)
    table.add_column("PR URL", max_width=50)

    for row in rows:
        table.add_row(
            str(row["created_at"])[:19],
            row["full_name"] or "?",
            f"#{row['issue_number']}" if row["issue_number"] else "?",
            row["action"] or "?",
            row["status"] or "?",
            row["pr_url"] or "-",
        )
    console.print(table)


@app.command()
def fullindex(
    min_stars: int = typer.Option(1000, help="Minimum star count"),
):
    """Index ALL GitHub repos with min_stars+. Takes ~2 hours for 1000+ stars."""
    from src.scanner import Scanner
    import time

    scanner = Scanner()
    if not scanner.token:
        console.print("[red]Error: GITHUB_TOKEN not set.[/red]")
        raise typer.Exit(1)

    start = time.time()
    print(f"Full Index: All repos with {min_stars}+ stars\n", flush=True)

    def progress(event, *args, **kwargs):
        if event == "init":
            print(f"  Target: {args[1]:,} repositories", flush=True)
        elif event == "shards":
            print(f"  Shards: {args[0]} star-range buckets\n", flush=True)
        elif event == "shard_done":
            indexed, total = args[0], args[1]
            shard_num = kwargs.get("shard_num", "?")
            shard_total = kwargs.get("shard_total", "?")
            shard_count = kwargs.get("shard_count", 0)
            elapsed = time.time() - start
            rate = indexed / elapsed * 3600 if elapsed > 0 else 0
            pct = indexed / total * 100 if total else 0
            print(
                f"  Shard {shard_num}/{shard_total}: +{shard_count} repos "
                f"| Total: {indexed:,}/{total:,} ({pct:.1f}%) "
                f"| Rate: {rate:,.0f}/hr "
                f"| Elapsed: {elapsed/60:.1f}m",
                flush=True,
            )

    total = asyncio.run(scanner.full_index(min_stars, progress_cb=progress))
    elapsed = time.time() - start
    print(f"\nDone! Indexed {total:,} repos in {elapsed/60:.1f} minutes", flush=True)


@app.command()
def scanissues(
    min_stars: int = typer.Option(1000, help="Minimum repo stars"),
    limit: int = typer.Option(10, help="Max issues per repo"),
):
    """Scan all indexed repos for open beginner-friendly issues."""
    from src.scanner import Scanner
    import time

    scanner = Scanner()
    if not scanner.token:
        console.print("[red]Error: GITHUB_TOKEN not set.[/red]")
        raise typer.Exit(1)

    start = time.time()
    console.print(f"[bold]Scanning issues for repos with {min_stars}+ stars...[/bold]\n")

    def progress(event, *args, **kwargs):
        if event == "issues":
            repos_done = kwargs.get("repos_done", 0)
            elapsed = time.time() - start
            rate = repos_done / elapsed * 3600 if elapsed > 0 else 0
            print(
                f"  Repos: {repos_done:,}/{args[1]:,} "
                f"| Issues found: {args[0]:,} "
                f"| Rate: {rate:,.0f} repos/hr "
                f"| Elapsed: {elapsed/60:.1f}m",
                flush=True,
            )

    total = asyncio.run(scanner.scan_issues_for_repos(min_stars, limit, progress_cb=progress))
    elapsed = time.time() - start
    print(f"\nDone! Found {total:,} issues in {elapsed/60:.1f} minutes", flush=True)


@app.command()
def maintain(
    min_stars: int = typer.Option(1000, help="Minimum star count"),
):
    """Daily maintenance: find new repos, refresh stale data, scan issues."""
    from src.scanner import Scanner
    from src.ranker import Ranker
    import time

    scanner = Scanner()
    if not scanner.token:
        console.print("[red]Error: GITHUB_TOKEN not set.[/red]")
        raise typer.Exit(1)

    start = time.time()
    console.print("[bold]Running daily maintenance...[/bold]\n")

    def progress(event, *args, **kwargs):
        if event == "new_repos":
            console.print(f"  New/updated repos found: [cyan]{args[0]}[/cyan]")
        elif event == "refreshed":
            console.print(f"  Stale repos refreshed:   [cyan]{args[0]}[/cyan]")
        elif event == "issues":
            console.print(f"  Issues scanned:          [cyan]{args[0]}[/cyan]")

    results = asyncio.run(scanner.maintain(min_stars, progress_cb=progress))

    # Re-rank after maintenance
    console.print("\n  Re-ranking...")
    ranker = Ranker()
    ranked = ranker.rank_all()
    ranker.rank_issues()
    console.print(f"  Ranked [cyan]{ranked}[/cyan] repos")

    elapsed = time.time() - start
    console.print(f"\n[green bold]Maintenance complete in {elapsed/60:.1f} minutes[/green bold]")
    console.print(f"  New repos:     {results['new_repos']}")
    console.print(f"  Refreshed:     {results['refreshed']}")
    console.print(f"  New issues:    {results['new_issues']}")


@app.command()
def stats():
    """Show database statistics including agent metrics."""
    from src.db import get_connection

    conn = get_connection()
    repo_count = conn.execute("SELECT COUNT(*) as c FROM repositories").fetchone()["c"]
    issue_count = conn.execute("SELECT COUNT(*) as c FROM issues").fetchone()["c"]
    open_issues = conn.execute("SELECT COUNT(*) as c FROM issues WHERE state = 'open'").fetchone()["c"]
    contrib_count = conn.execute("SELECT COUNT(*) as c FROM contributions").fetchone()["c"]
    prs_created = conn.execute("SELECT COUNT(*) as c FROM contributions WHERE status = 'pr_created'").fetchone()["c"]

    top_lang = conn.execute("""
        SELECT language, COUNT(*) as c FROM repositories
        WHERE language IS NOT NULL
        GROUP BY language ORDER BY c DESC LIMIT 5
    """).fetchall()

    console.print("\n[bold]Do-Good Agent Statistics[/bold]\n")
    console.print(f"  Repositories tracked:  [cyan]{repo_count}[/cyan]")
    console.print(f"  Issues tracked:        [cyan]{issue_count}[/cyan]")
    console.print(f"  Open issues:           [cyan]{open_issues}[/cyan]")
    console.print(f"  Contributions made:    [cyan]{contrib_count}[/cyan]")
    console.print(f"  PRs created:           [green]{prs_created}[/green]")

    # Agent metrics
    try:
        agent_runs_total = conn.execute("SELECT COUNT(*) as c FROM agent_runs").fetchone()["c"]
        agent_runs_success = conn.execute(
            "SELECT COUNT(*) as c FROM agent_runs WHERE status = 'pr_created'"
        ).fetchone()["c"]
        agent_runs_failed = conn.execute(
            "SELECT COUNT(*) as c FROM agent_runs WHERE status = 'failed'"
        ).fetchone()["c"]
        agent_runs_escalated = conn.execute(
            "SELECT COUNT(*) as c FROM agent_runs WHERE status = 'escalated'"
        ).fetchone()["c"]
        total_cost = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as c FROM agent_runs"
        ).fetchone()["c"]
        blacklisted = conn.execute(
            "SELECT COUNT(*) as c FROM repo_blacklist WHERE forgiven_at IS NULL"
        ).fetchone()["c"]

        model_breakdown = conn.execute("""
            SELECT model, COUNT(*) as c FROM agent_runs
            GROUP BY model ORDER BY c DESC
        """).fetchall()

        console.print(f"\n  [bold]Agent Metrics[/bold]")
        console.print(f"  Agent runs:            [cyan]{agent_runs_total}[/cyan]")
        console.print(f"  Successful:            [green]{agent_runs_success}[/green]")
        console.print(f"  Failed:                [red]{agent_runs_failed}[/red]")
        console.print(f"  Escalated:             [yellow]{agent_runs_escalated}[/yellow]")
        console.print(f"  Total cost:            [cyan]${total_cost:.2f}[/cyan]")
        console.print(f"  Blacklisted repos:     [red]{blacklisted}[/red]")

        if model_breakdown:
            console.print(f"\n  Model usage:")
            for m in model_breakdown:
                console.print(f"    {m['model']}: {m['c']} runs")
    except Exception:
        pass  # Tables may not exist yet

    if top_lang:
        console.print(f"\n  Top languages:")
        for lang in top_lang:
            console.print(f"    {lang['language']}: {lang['c']} repos")
    console.print()


@app.command()
def factory(
    max_issues: int = typer.Option(100, help="Max issues to solve"),
    max_concurrent: int = typer.Option(5, help="Max parallel agents"),
    min_stars: int = typer.Option(1000, help="Minimum repo stars"),
    max_cost_usd: float = typer.Option(0.0, help="Max total cost in USD (0=unlimited)"),
):
    """Run the multi-agent factory: spawns parallel agents to solve issues."""
    from src.orchestrator import AgentFactory

    console.print(f"[bold]Starting Agent Factory[/bold]")
    console.print(f"  Max issues:     {max_issues}")
    console.print(f"  Max concurrent: {max_concurrent}")
    console.print(f"  Min stars:      {min_stars}")
    if max_cost_usd:
        console.print(f"  Budget:         ${max_cost_usd:.2f}")
    console.print()

    agent_factory = AgentFactory(max_concurrent=max_concurrent, min_stars=min_stars,
                                 max_cost_usd=max_cost_usd)
    stats = asyncio.run(agent_factory.run(max_issues=max_issues))

    console.print(f"\n[bold green]Factory complete![/bold green]")
    console.print(f"  Started:   {stats['started']}")
    console.print(f"  Succeeded: [green]{stats['succeeded']}[/green]")
    console.print(f"  Failed:    [red]{stats['failed']}[/red]")
    console.print(f"  Escalated: [yellow]{stats['escalated']}[/yellow]")


@app.command()
def feedback():
    """Run one feedback cycle: check notifications, analyze reviews, respond."""
    from src.feedback import FeedbackLoop

    console.print("[blue]Running feedback cycle...[/blue]")
    loop = FeedbackLoop()
    stats = asyncio.run(loop.run_once())

    console.print(f"\n[bold]Feedback Results:[/bold]")
    console.print(f"  Processed:    {stats['processed']}")
    console.print(f"  Positive:     [green]{stats['positive']}[/green]")
    console.print(f"  Constructive: [blue]{stats['constructive']}[/blue]")
    console.print(f"  Hostile:      [red]{stats['hostile']}[/red]")
    console.print(f"  Anti-AI:      [red]{stats['anti_ai']}[/red]")
    console.print(f"  Sponsors:     [yellow]{stats['sponsor']}[/yellow]")
    console.print(f"  Regretful:    [cyan]{stats['regretful']}[/cyan]")
    console.print(f"  Payment:      [yellow]{stats.get('payment_request', 0)}[/yellow]")
    console.print(f"  Job inquiry:  [yellow]{stats.get('job_inquiry', 0)}[/yellow]")
    console.print(f"  Contact:      [yellow]{stats.get('contact_request', 0)}[/yellow]")


@app.command()
def agents(
    show_all: bool = typer.Option(False, help="Show all runs, not just active"),
):
    """Show agent runs — active or all."""
    from src.db import get_connection, get_agent_runs

    conn = get_connection()
    rows = get_agent_runs(conn, active_only=not show_all)

    if not rows:
        console.print("[dim]No agent runs found.[/dim]")
        return

    table = Table(title="Agent Runs" if show_all else "Active Agent Runs")
    table.add_column("ID", style="dim", width=12)
    table.add_column("Repository", max_width=30)
    table.add_column("Issue", width=8)
    table.add_column("Model", width=15)
    table.add_column("Status", width=12)
    table.add_column("Cost", justify="right", width=7)
    table.add_column("Started", width=19)
    table.add_column("PR URL", max_width=40)

    for row in rows:
        status_style = {
            "pr_created": "green",
            "failed": "red",
            "escalated": "yellow",
            "fixing": "blue",
            "starting": "dim",
        }.get(row["status"], "white")
        table.add_row(
            row["id"][:12],
            row["full_name"] or "?",
            f"#{row['issue_number']}" if row["issue_number"] else "?",
            row["model"] or "?",
            f"[{status_style}]{row['status']}[/{status_style}]",
            f"${row['cost_usd']:.2f}" if row["cost_usd"] else "-",
            str(row["started_at"])[:19] if row["started_at"] else "-",
            row["pr_url"] or "-",
        )
    console.print(table)


@app.command()
def blacklist(
    add: str = typer.Option("", help="Add owner/repo to blacklist"),
    forgive: str = typer.Option("", help="Forgive owner/repo from blacklist"),
    reason: str = typer.Option("manual", help="Reason for blacklisting"),
):
    """View or manage the repo blacklist."""
    from src.db import get_connection, add_to_blacklist, remove_from_blacklist, get_blacklist

    conn = get_connection()

    if add:
        add_to_blacklist(conn, add, reason)
        console.print(f"[red]Blacklisted {add}: {reason}[/red]")
        return

    if forgive:
        remove_from_blacklist(conn, forgive)
        console.print(f"[green]Forgave {forgive}[/green]")
        return

    # Show blacklist
    rows = get_blacklist(conn)
    if not rows:
        console.print("[dim]Blacklist is empty.[/dim]")
        return

    table = Table(title="Repo Blacklist")
    table.add_column("Repository", max_width=40)
    table.add_column("Reason", max_width=30)
    table.add_column("Blacklisted", width=19)
    table.add_column("Forgiven", width=19)

    for row in rows:
        forgiven = str(row["forgiven_at"])[:19] if row["forgiven_at"] else "-"
        style = "dim" if row["forgiven_at"] else "red"
        table.add_row(
            f"[{style}]{row['full_name']}[/{style}]",
            row["reason"] or "?",
            str(row["blacklisted_at"])[:19],
            forgiven,
        )
    console.print(table)


@app.command()
def bountywatch(
    interval: int = typer.Option(120, help="Poll interval in seconds"),
):
    """Background daemon: scan GitHub for bounty issues every 2 min, solve with opus."""
    import time
    import subprocess as sp
    from datetime import datetime, timezone, timedelta
    from src.db import get_connection, add_to_blacklist
    from src.config import SUPPORTED_LANGUAGES, BOUNTY_LABELS, BEGINNER_LABELS

    conn = get_connection()
    lang_list = ["Python", "JavaScript", "TypeScript", "Shell", "YAML", "HTML", "CSS", "Vue", "Svelte", "Lua"]
    seen_urls = set()

    console.print(f"[bold]Bounty Watch started[/bold] — polling every {interval}s")
    console.print(f"  Freshness window: 10 minutes")
    console.print(f"  Languages: {', '.join(lang_list)}")
    console.print()

    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            bounty_labels = ["bounty", "has-bounty", "bounty-posted", "💰"]
            found = []

            for label in bounty_labels:
                for lang in lang_list:
                    query = f'label:"{label}" state:open language:{lang} created:>{cutoff}'
                    r = sp.run(
                        ["gh", "api", "search/issues",
                         "-X", "GET",
                         "-f", f"q={query}",
                         "-f", "sort=created",
                         "-f", "order=desc",
                         "-f", "per_page=10",
                         "--jq", '.items[] | {url: .html_url, title: .title, number: .number, repo: .repository_url, labels: [.labels[].name], created: .created_at}'],
                        capture_output=True, text=True, timeout=15
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        import json as _json
                        for line in r.stdout.strip().split("\n"):
                            if line.strip():
                                try:
                                    item = _json.loads(line)
                                    if item["url"] not in seen_urls:
                                        found.append(item)
                                        seen_urls.add(item["url"])
                                except Exception:
                                    pass
                    time.sleep(2)  # Rate limit: 30 search/min

            if found:
                from src.telegram import notify_github_attention
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Found {len(found)} new bounties!", flush=True)
                for item in found:
                    print(f"  💰 {item['url']} — {item['title'][:60]}", flush=True)
                    # Notify Daniel via Telegram
                    repo_name = item["repo"].replace("https://api.github.com/repos/", "")
                    notify_github_attention(
                        "bounty_found", repo_name, item["url"],
                        f"#{item['number']}: {item['title'][:100]}\nLabels: {', '.join(item.get('labels', []))}"
                    )

                    # Extract owner/repo from repository_url
                    repo_parts = item["repo"].replace("https://api.github.com/repos/", "").split("/")
                    if len(repo_parts) == 2:
                        owner, repo_name = repo_parts
                        full_name = f"{owner}/{repo_name}"

                        # Check blacklist
                        bl = conn.execute(
                            "SELECT 1 FROM repo_blacklist WHERE full_name = ? AND forgiven_at IS NULL",
                            (full_name,)
                        ).fetchone()
                        if bl:
                            print(f"    Skipped (blacklisted)", flush=True)
                            continue

                        # Upsert repo and issue into DB, then solve immediately
                        from src.scanner import Scanner
                        scanner = Scanner()

                        # Quick repo upsert
                        repo_data = sp.run(
                            ["gh", "api", f"repos/{full_name}",
                             "--jq", '{github_id: .id, owner: .owner.login, name: .name, full_name: .full_name, url: .html_url, stars: .stargazers_count, forks: .forks_count, open_issues: .open_issues_count, description: .description, language: .language, license: .license.spdx_id}'],
                            capture_output=True, text=True, timeout=15
                        )
                        if repo_data.returncode == 0 and repo_data.stdout.strip():
                            import json as _json
                            rd = _json.loads(repo_data.stdout)
                            rd["topics"] = "[]"
                            rd["pushed_at"] = None
                            from src.db import upsert_repository
                            upsert_repository(conn, rd)

                        # Get repo_id
                        repo_row = conn.execute(
                            "SELECT id FROM repositories WHERE full_name = ?", (full_name,)
                        ).fetchone()
                        if not repo_row:
                            print(f"    Skipped (repo not in DB)", flush=True)
                            continue

                        # Upsert issue
                        from src.db import upsert_issue
                        issue_data = {
                            "github_id": item["number"] * 1000000 + repo_row["id"],
                            "repo_id": repo_row["id"],
                            "number": item["number"],
                            "title": item["title"],
                            "body": "",
                            "state": "open",
                            "labels": _json.dumps(item.get("labels", [])),
                            "comments_count": 0,
                            "is_assigned": 0,
                            "is_pull_request": 0,
                            "created_at": item.get("created"),
                            "updated_at": item.get("created"),
                        }
                        upsert_issue(conn, issue_data)

                        # Get issue_id
                        issue_row = conn.execute(
                            "SELECT id FROM issues WHERE repo_id = ? AND number = ?",
                            (repo_row["id"], item["number"])
                        ).fetchone()
                        if not issue_row:
                            print(f"    Skipped (issue not in DB)", flush=True)
                            continue

                        # Solve with opus immediately
                        print(f"    Solving with opus...", flush=True)
                        from src.solver import Solver
                        from src.config import MODEL_TIERS
                        opus_tier = MODEL_TIERS[-1]  # tier 4 = opus
                        solver = Solver(model_tier=opus_tier)
                        try:
                            import asyncio
                            result = asyncio.run(solver.solve_issue(issue_row["id"]))
                            if result["success"]:
                                print(f"    ✅ PR created: {result['pr_url']}", flush=True)
                                notify_github_attention(
                                    "pr_merged", full_name, result["pr_url"],
                                    f"Bounty PR submitted for #{item['number']}: {item['title'][:80]}"
                                )
                            else:
                                print(f"    ❌ Failed: {result.get('error', '')[:80]}", flush=True)
                        except Exception as e:
                            print(f"    ❌ Error: {e}", flush=True)
            else:
                ts = datetime.now().strftime('%H:%M:%S')
                print(f"[{ts}] No new bounties", flush=True)

        except Exception as e:
            print(f"Bounty watch error: {e}", flush=True)

        time.sleep(interval)


@app.command()
def clascan(
    batch_size: int = typer.Option(100, help="Repos per batch"),
):
    """Pre-scan all repos for CLA/DCO requirements and blacklist CLA repos."""
    import time
    import base64
    import subprocess as sp
    from src.db import get_connection, add_to_blacklist
    from src.config import (
        CLA_ORGS, SIGNED_CLA_ORGS, CLA_KEYWORDS, ANTI_AI_KEYWORDS,
        SUPPORTED_LANGUAGES,
    )

    conn = get_connection()
    lang_list = list(SUPPORTED_LANGUAGES)
    lang_ph = ",".join("?" for _ in lang_list)
    signed = {o.lower() for o in SIGNED_CLA_ORGS}
    cla_orgs = {o.lower() for o in CLA_ORGS}

    # Phase 1: Org-level blacklist (instant, no API)
    console.print("[bold]Phase 1: Org-level CLA blacklist[/bold]")
    org_count = 0
    for org in cla_orgs - signed:
        rows = conn.execute(
            "SELECT full_name FROM repositories WHERE LOWER(owner) = ? "
            "AND full_name NOT IN (SELECT full_name FROM repo_blacklist)",
            (org,)
        ).fetchall()
        for r in rows:
            add_to_blacklist(conn, r["full_name"], "cla_required",
                             details=f'{{"source":"org_scan","org":"{org}"}}')
            org_count += 1
    console.print(f"  Blacklisted [red]{org_count}[/red] repos from CLA orgs")

    # Phase 2: API scan of CONTRIBUTING.md for remaining repos
    console.print("\n[bold]Phase 2: Scanning CONTRIBUTING.md for CLA keywords[/bold]")
    repos = conn.execute(f"""
        SELECT id, full_name, owner, name FROM repositories
        WHERE combined_score > 0
          AND language IN ({lang_ph})
          AND full_name NOT IN (SELECT full_name FROM repo_blacklist)
        ORDER BY combined_score DESC
    """, lang_list).fetchall()

    console.print(f"  {len(repos)} repos to scan")
    scanned = 0
    cla_found = 0
    anti_ai_found = 0
    errors = 0
    start = time.time()

    for repo in repos:
        fn = repo["full_name"]
        try:
            r = sp.run(
                ["gh", "api", f"repos/{fn}/contents/CONTRIBUTING.md",
                 "-q", ".content"],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0 and r.stdout.strip():
                text = base64.b64decode(r.stdout.strip()).decode("utf-8", errors="replace").lower()

                # Check CLA
                for kw in CLA_KEYWORDS:
                    if kw in text:
                        add_to_blacklist(conn, fn, "cla_required",
                                         details=f'{{"source":"contributing_scan","keyword":"{kw}"}}')
                        cla_found += 1
                        break

                # Check anti-AI
                for kw in ANTI_AI_KEYWORDS:
                    if kw in text:
                        add_to_blacklist(conn, fn, "anti_ai_policy",
                                         details=f'{{"source":"contributing_scan","keyword":"{kw}"}}')
                        anti_ai_found += 1
                        break
        except Exception:
            errors += 1

        scanned += 1
        if scanned % 50 == 0:
            elapsed = time.time() - start
            rate = scanned / elapsed * 3600 if elapsed > 0 else 0
            print(f"  Scanned {scanned}/{len(repos)} | CLA: {cla_found} | "
                  f"Anti-AI: {anti_ai_found} | Rate: {rate:.0f}/hr | "
                  f"Elapsed: {elapsed/60:.1f}m", flush=True)

        # Rate limit: ~4500/hr to leave headroom
        time.sleep(0.8)

    console.print(f"\n[bold green]CLA scan complete![/bold green]")
    console.print(f"  Scanned:     {scanned}")
    console.print(f"  CLA found:   [red]{cla_found}[/red]")
    console.print(f"  Anti-AI:     [red]{anti_ai_found}[/red]")
    console.print(f"  Org-blocked: [red]{org_count}[/red]")
    console.print(f"  Errors:      {errors}")


@app.command()
def telegramd():
    """Telegram daemon: listens for Daniel's replies and posts them on GitHub."""
    from src.telegram import TelegramDaemon

    daemon = TelegramDaemon()
    daemon.run()


if __name__ == "__main__":
    app()
