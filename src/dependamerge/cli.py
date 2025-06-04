# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

import typer
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from .github_client import GitHubClient
from .pr_comparator import PRComparator
from .models import PullRequestInfo

app = typer.Typer(
    help="Automatically merge pull requests created by automation tools "
    "across GitHub organizations"
)
console = Console()


@app.command()
def merge(
    pr_url: str = typer.Argument(..., help="GitHub pull request URL"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be done without making changes",
    ),
    similarity_threshold: float = typer.Option(
        0.8,
        "--threshold",
        help="Similarity threshold for matching PRs (0.0-1.0)",
    ),
    merge_method: str = typer.Option(
        "merge",
        "--merge-method",
        help="Merge method: merge, squash, or rebase",
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="GitHub token (or set GITHUB_TOKEN env var)",
    ),
):
    """
    Merge automation pull requests across an organization.

    This command will:
    1. Analyze the provided PR
    2. Find similar PRs in the organization
    3. Approve and merge matching PRs
    """
    try:
        # Initialize clients
        github_client = GitHubClient(token)
        comparator = PRComparator(similarity_threshold)

        console.print(f"[bold blue]Analyzing PR: {pr_url}[/bold blue]")

        # Parse PR URL and get info
        owner, repo_name, pr_number = github_client.parse_pr_url(pr_url)
        source_pr = github_client.get_pull_request_info(owner, repo_name, pr_number)

        # Display source PR info
        _display_pr_info(source_pr, "Source PR")

        # Check if source PR is from automation
        if not github_client.is_automation_author(source_pr.author):
            console.print(
                "[red]Error: Source PR is not from a recognized automation tool[/red]"
            )
            raise typer.Exit(1)

        # Get organization repositories
        console.print(f"\n[bold blue]Scanning organization: {owner}[/bold blue]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Fetching repositories...", total=None)
            repositories = github_client.get_organization_repositories(owner)
            progress.update(task, description=f"Found {len(repositories)} repositories")

        # Find similar PRs
        similar_prs = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Analyzing PRs...", total=len(repositories))

            for repo in repositories:
                if repo.full_name == source_pr.repository_full_name:
                    progress.advance(task)
                    continue

                open_prs = github_client.get_open_pull_requests(repo)

                for pr in open_prs:
                    if not github_client.is_automation_author(pr.user.login):
                        continue

                    try:
                        target_pr = github_client.get_pull_request_info(
                            repo.owner.login, repo.name, pr.number
                        )

                        comparison = comparator.compare_pull_requests(
                            source_pr, target_pr
                        )

                        if comparison.is_similar:
                            similar_prs.append((target_pr, comparison))

                    except Exception as e:
                        console.print(
                            f"[yellow]Warning: Failed to analyze PR {pr.number} "
                            f"in {repo.full_name}: {e}[/yellow]"
                        )

                progress.advance(task)

        # Display results
        if not similar_prs:
            console.print("\n[yellow]No similar PRs found in the organization[/yellow]")
            return

        console.print(
            f"\n[bold green]Found {len(similar_prs)} similar PR(s)[/bold green]"
        )

        # Display similar PRs table
        table = Table(title="Similar Pull Requests")
        table.add_column("Repository", style="cyan")
        table.add_column("PR #", style="magenta")
        table.add_column("Title", style="green")
        table.add_column("Confidence", style="yellow")
        table.add_column("Status", style="blue")

        for target_pr, comparison in similar_prs:
            repo_name = target_pr.repository_full_name
            status = "Ready to merge" if target_pr.mergeable else "Not mergeable"
            table.add_row(
                repo_name,
                str(target_pr.number),
                (
                    target_pr.title[:50] + "..."
                    if len(target_pr.title) > 50
                    else target_pr.title
                ),
                f"{comparison.confidence_score:.2f}",
                status,
            )

        console.print(table)

        # Merge PRs
        if dry_run:
            console.print("\n[yellow]Dry run mode - no changes will be made[/yellow]")
            return

        success_count = 0
        for target_pr, comparison in similar_prs:
            if not target_pr.mergeable:
                console.print(
                    f"[yellow]Skipping unmergeable PR {target_pr.number} "
                    f"in {target_pr.repository_full_name}[/yellow]"
                )
                continue

            repo_owner, repo_name = target_pr.repository_full_name.split("/")

            # Approve PR
            console.print(
                f"[blue]Approving PR {target_pr.number} "
                f"in {target_pr.repository_full_name}[/blue]"
            )
            if github_client.approve_pull_request(
                repo_owner, repo_name, target_pr.number
            ):
                # Merge PR
                console.print(
                    f"[blue]Merging PR {target_pr.number} in "
                    f"{target_pr.repository_full_name}[/blue]"
                )
                if github_client.merge_pull_request(
                    repo_owner, repo_name, target_pr.number, merge_method
                ):
                    console.print(
                        f"[green]✓ Successfully merged PR {target_pr.number}[/green]"
                    )
                    success_count += 1
                else:
                    console.print(f"[red]✗ Failed to merge PR {target_pr.number}[/red]")
            else:
                console.print(f"[red]✗ Failed to approve PR {target_pr.number}[/red]")

        console.print(
            f"\n[bold green]Successfully merged {success_count}/"
            f"{len(similar_prs)} PRs[/bold green]"
        )

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


def _display_pr_info(pr: PullRequestInfo, title: str):
    """Display pull request information in a formatted table."""
    table = Table(title=title)
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Repository", pr.repository_full_name)
    table.add_row("PR Number", str(pr.number))
    table.add_row("Title", pr.title)
    table.add_row("Author", pr.author)
    table.add_row("State", pr.state)
    table.add_row("Mergeable", str(pr.mergeable))
    table.add_row("Files Changed", str(len(pr.files_changed)))
    table.add_row("URL", pr.html_url)

    console.print(table)


if __name__ == "__main__":
    app()
