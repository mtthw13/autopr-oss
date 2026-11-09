"""Reconciler — batch reconcile open managed PRs against the base branch."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

from autopr.config import AutoPRConfig
from autopr.conflict_resolver import resolve_pr_conflicts
from autopr.models import ConflictResolutionResult, ReconcileResult

console = Console()


def list_managed_prs(
    repo_path: Path,
    prefixes: list[str],
    labels: list[str],
    base_branch: str,
) -> list[dict]:
    """Query open PRs and filter to those managed by autopr."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            base_branch,
            "--json",
            "number,headRefName,title,body,mergeable,mergeStateStatus,labels",
            "--limit",
            "100",
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]Failed to list PRs: {result.stderr.strip()}[/red]")
        return []

    prs = json.loads(result.stdout) if result.stdout.strip() else []
    managed = []

    for pr in prs:
        branch = pr.get("headRefName", "")
        pr_labels = [lbl.get("name", "") for lbl in pr.get("labels", [])]

        # Match by branch prefix
        prefix_match = any(branch.startswith(p) for p in prefixes)
        # Match by label
        label_match = any(lbl in labels for lbl in pr_labels)

        if prefix_match or label_match:
            managed.append(pr)

    return managed


def reconcile_open_prs(
    repo_path: Path,
    config: AutoPRConfig,
    base_branch: str = "main",
    model: str = "claude-opus-4-6",
    dry_run: bool = False,
) -> ReconcileResult:
    """Reconcile all open managed PRs against the base branch.

    - BEHIND PRs get rebased via gh pr update-branch
    - CONFLICTING PRs get conflict resolution via conflict_resolver
    - Clean PRs are skipped
    """
    automation = config.automation

    console.print("\n[bold]autopr reconcile-open-prs[/bold]")
    console.print(f"  Base branch: {base_branch}")

    # Fetch latest base branch
    subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=str(repo_path),
        capture_output=True,
    )

    managed = list_managed_prs(
        repo_path, automation.managed_branch_prefixes, automation.managed_labels, base_branch
    )
    console.print(f"  Managed PRs: {len(managed)}")

    result = ReconcileResult(total_prs=len(managed))

    if dry_run:
        console.print("  [dim]Dry run — no changes[/dim]")
        result.skipped = len(managed)
        return result

    for pr in managed:
        pr_number = pr.get("number", 0)
        pr_branch = pr.get("headRefName", "")
        mergeable = pr.get("mergeable", "")
        merge_state = pr.get("mergeStateStatus", "")

        console.print(f"\n  PR #{pr_number} ({pr_branch}): {mergeable}/{merge_state}")

        # Unknown — GitHub hasn't computed yet, skip
        if mergeable == "UNKNOWN":
            console.print("    [dim]Skipping — mergeable status unknown[/dim]")
            result.skipped += 1
            result.details.append(
                ConflictResolutionResult(
                    pr_number=pr_number,
                    branch_name=pr_branch,
                    status="skipped",
                    error="Mergeable status unknown",
                )
            )
            continue

        # Already clean
        if merge_state in ("CLEAN", "HAS_HOOKS", "UNSTABLE") and mergeable != "CONFLICTING":
            console.print("    [dim]Already up to date[/dim]")
            result.skipped += 1
            result.details.append(
                ConflictResolutionResult(
                    pr_number=pr_number,
                    branch_name=pr_branch,
                    status="skipped",
                )
            )
            continue

        # Behind but not conflicting — simple rebase via GitHub API
        if merge_state == "BEHIND" and mergeable != "CONFLICTING":
            if not automation.reconcile_behind_prs:
                console.print("    [dim]Behind — skipping (reconcile_behind_prs=False)[/dim]")
                result.skipped += 1
                result.details.append(
                    ConflictResolutionResult(
                        pr_number=pr_number,
                        branch_name=pr_branch,
                        status="skipped",
                        error="reconcile_behind_prs disabled",
                    )
                )
                continue

            update = subprocess.run(
                ["gh", "pr", "update-branch", str(pr_number), "--rebase"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
            )
            if update.returncode == 0:
                console.print(f"    [green]Rebased onto {base_branch}[/green]")
                result.behind_updated += 1
                result.details.append(
                    ConflictResolutionResult(
                        pr_number=pr_number,
                        branch_name=pr_branch,
                        status="clean_rebase",
                        strategy_used="rebase",
                    )
                )
            else:
                console.print(f"    [yellow]Update-branch failed: {update.stderr.strip()}[/yellow]")
                result.needs_human += 1
                result.details.append(
                    ConflictResolutionResult(
                        pr_number=pr_number,
                        branch_name=pr_branch,
                        status="failed",
                        error=update.stderr.strip(),
                    )
                )
            continue

        # Conflicting — enter conflict resolver
        if merge_state in ("DIRTY", "CONFLICTING") or mergeable == "CONFLICTING":
            if not automation.auto_resolve_conflicts:
                console.print(
                    "    [dim]Conflicting — skipping (auto_resolve_conflicts=False)[/dim]"
                )
                result.needs_human += 1
                result.details.append(
                    ConflictResolutionResult(
                        pr_number=pr_number,
                        branch_name=pr_branch,
                        status="skipped",
                        error="auto_resolve_conflicts disabled",
                    )
                )
                continue

            from autopr.pr_creator import parse_opportunity_from_pr_body

            opp = parse_opportunity_from_pr_body(
                pr.get("body", ""), pr_number=pr_number, title=pr.get("title", "")
            )
            pr_info = {
                "number": pr_number,
                "headRefName": pr_branch,
                "title": pr.get("title", ""),
                "problem": opp.problem,
                "solution": opp.solution,
            }

            wt_dir = tempfile.mkdtemp(prefix=f"autopr-reconcile-{pr_number}-")
            subprocess.run(
                ["git", "fetch", "origin", pr_branch],
                cwd=str(repo_path),
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "--detach", wt_dir, f"origin/{pr_branch}"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=True,
            )
            wt_path = Path(wt_dir)
            try:
                cr_result = resolve_pr_conflicts(
                    repo_path,
                    wt_path,
                    pr_info,
                    base_branch,
                    model,
                    automation,
                    health_checks=config.repo.health_checks,
                    protected_files=config.repo.protected_files,
                )
                result.details.append(cr_result)
                if cr_result.status in ("clean_rebase", "conflict_resolved"):
                    result.conflicts_resolved += 1
                else:
                    result.needs_human += 1
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=str(repo_path),
                    capture_output=True,
                )
            continue

        # Catch-all
        result.skipped += 1
        result.details.append(
            ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="skipped",
                error=f"Unhandled state: {merge_state}/{mergeable}",
            )
        )

    # Print summary
    console.print("\n[bold]Reconcile summary:[/bold]")
    console.print(f"  Total:     {result.total_prs}")
    console.print(f"  Updated:   {result.behind_updated}")
    console.print(f"  Resolved:  {result.conflicts_resolved}")
    console.print(f"  Human:     {result.needs_human}")
    console.print(f"  Skipped:   {result.skipped}")

    return result
