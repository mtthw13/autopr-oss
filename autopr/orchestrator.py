"""Orchestrator — runs the full autopr pipeline using isolated git worktrees.

Your main checkout is NEVER modified. Each opportunity gets its own worktree,
so you can keep working on main while autopr creates PRs in parallel directories.

Default flow (no --manifest):
  1. Claude scans the repo for NEW opportunities
  2. Already-handled branches/PRs are skipped automatically
  3. Each new opportunity → worktree → fix → health check → review → PR
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from autopr.analyzer import analyze_from_manifest, analyze_repo
from autopr.codex_reviewer import (
    codex_review_loop,
    codex_review_scope,
    refine_opportunity_from_feedback,
)
from autopr.config import AutoPRConfig
from autopr.fixer import (
    commit_changes,
    prune_worktrees,
    push_branch,
    remove_worktree,
    resolve_worktree_dir,
    run_fix,
)
from autopr.health import run_health_checks
from autopr.models import (
    FinalizedResult,
    Opportunity,
    PreparedCandidate,
    PRResult,
    ReviewVerdict,
    RunReport,
)
from autopr.pr_creator import push_and_create_pr
from autopr.reviewer import structured_review_fix
from autopr.screener import screen_opportunity

console = Console()


def get_open_or_merged_prs(repo_path: Path, prefix: str) -> set[str]:
    """Get opportunity IDs that have an open or merged PR on GitHub.

    Only these should be skipped — a stale local branch with no PR means
    the previous run failed partway through and should be retried.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--search",
            f"head:{prefix}",
            "--json",
            "headRefName,state",
            "--limit",
            "200",
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(
            f"[yellow]Warning: could not query GitHub PRs ({result.stderr.strip()}). "
            "Falling back to branch-name check.[/yellow]"
        )
        return _get_existing_branches(repo_path, prefix)

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()

    handled: set[str] = set()
    for pr in prs:
        branch = pr.get("headRefName", "")
        state = pr.get("state", "").upper()
        if prefix in branch and state in ("OPEN", "MERGED"):
            idx = branch.index(prefix)
            opp_id = branch[idx + len(prefix) :]
            handled.add(opp_id)
    return handled


def _get_existing_branches(repo_path: Path, prefix: str) -> set[str]:
    """Fallback: get branch names that exist locally/remotely for this prefix."""
    result = subprocess.run(
        ["git", "branch", "-a", "--list", f"*{prefix}*"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    branches = set()
    for line in result.stdout.strip().split("\n"):
        line = line.strip().lstrip("* ")
        if prefix in line:
            idx = line.index(prefix)
            opp_id = line[idx + len(prefix) :]
            branches.add(opp_id)
    return branches


def get_open_pr_changed_files(repo_path: Path, prefix: str) -> dict[str, list[str]]:
    """Get files changed by each open autopr PR."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--search",
            f"head:{prefix}",
            "--json",
            "headRefName,files",
            "--limit",
            "200",
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    pr_files: dict[str, list[str]] = {}
    for pr in prs:
        branch = pr.get("headRefName", "")
        if prefix not in branch:
            continue
        idx = branch.index(prefix)
        opp_id = branch[idx + len(prefix) :]
        files = [f.get("path", "") for f in pr.get("files", []) if f.get("path")]
        if files:
            pr_files[opp_id] = files
    return pr_files


def filter_already_handled(
    opportunities: list[Opportunity],
    repo_path: Path,
    branch_prefix: str,
) -> list[Opportunity]:
    """Skip opportunities that already have an open or merged PR on GitHub,
    or that touch files already modified by an open PR (avoids merge conflicts).

    Also skip opportunities whose files overlap with open PRs to reduce
    merge conflicts across concurrent autopr branches.

    Branches left over from failed runs (no PR) are NOT skipped — they will
    be cleaned up and retried by the fixer.
    """
    handled = get_open_or_merged_prs(repo_path, branch_prefix)
    open_pr_files = get_open_pr_changed_files(repo_path, branch_prefix)

    file_to_pr: dict[str, str] = {}
    for opp_id, files in open_pr_files.items():
        for file_path in files:
            file_to_pr[file_path] = opp_id

    new_opps = []
    for opp in opportunities:
        if opp.id in handled:
            console.print(f"[dim]  Skipping '{opp.id}' — open or merged PR exists[/dim]")
            continue

        overlapping_files = [file_path for file_path in opp.files if file_path in file_to_pr]
        if overlapping_files:
            blocking_pr = file_to_pr[overlapping_files[0]]
            console.print(
                f"[dim]  Skipping '{opp.id}' — "
                f"{', '.join(overlapping_files[:3])} already modified by open PR "
                f"'{blocking_pr}'[/dim]"
            )
            continue

        new_opps.append(opp)

    skipped = len(opportunities) - len(new_opps)
    if skipped:
        console.print(
            f"[yellow]Skipped {skipped} opportunities (existing PRs or file overlap)[/yellow]"
        )
    return new_opps


def prepare_candidate(
    opportunity: Opportunity,
    config: AutoPRConfig,
) -> PreparedCandidate:
    """Screen, fix, and health-check an opportunity in an isolated worktree.

    Shared execution spine used by both `autopr run` and `autopr research`.
    Returns a PreparedCandidate with the worktree ready for review/finalization.
    """
    from autopr.fixer import revert_out_of_scope_files

    branch_name = f"{config.repo.branch_prefix}{opportunity.id}"
    prepared = PreparedCandidate(
        opportunity=opportunity,
        branch_name=branch_name,
        worktree_path="",
    )

    # ── Screen ───────────────────────────────────────────────────────
    if config.gating.enable_screener:
        screen_result = screen_opportunity(
            opportunity,
            config.repo.path,
            model=config.gating.screener_model or config.model,
            max_turns=config.gating.screener_max_turns,
        )
        prepared.screen = screen_result
        if not screen_result.proceed:
            prepared.status = "skipped"
            prepared.error = f"Screener: {screen_result.summary}"
            console.print(
                f"[yellow]SKIPPED:[/yellow] {opportunity.title}\n  Reason: {screen_result.summary}"
            )
            return prepared

    # ── Codex scope critique ─────────────────────────────────────────
    if config.gating.enable_material_review:
        codex_scope = codex_review_scope(
            opportunity,
            config.repo.path,
            model=config.gating.review_model,
        )
        prepared.codex_scope_review = codex_scope
        if not codex_scope.approve:
            opportunity = refine_opportunity_from_feedback(opportunity, codex_scope)
            prepared.opportunity = opportunity

    # ── Fix in worktree ──────────────────────────────────────────────
    branch_name, wt_path = run_fix(opportunity, config)
    prepared.branch_name = branch_name
    prepared.worktree_path = str(wt_path)

    # Revert out-of-scope changes
    reverted = revert_out_of_scope_files(wt_path, opportunity.files)
    if reverted:
        console.print(f"[yellow]Reverted {len(reverted)} out-of-scope file(s)[/yellow]")

    # ── Health checks ────────────────────────────────────────────────
    health_results = run_health_checks(
        config.repo.health_checks,
        wt_path,
        model=config.model,
        protected_files=config.repo.protected_files,
    )
    prepared.health_checks = health_results

    if not all(r.passed for r in health_results):
        prepared.status = "failed"
        prepared.error = "Health checks failed after max retries"
        console.print(
            f"[bold red]FAILED:[/bold red] {opportunity.title} — health checks didn't pass"
        )
        return prepared

    # Commit health check auto-fixes
    commit_changes(
        wt_path,
        f"fix: auto-fix health check issues for {opportunity.id}",
        protected_files=config.repo.protected_files,
    )

    prepared.status = "ready"
    return prepared


def finalize_candidate(
    opportunity: Opportunity,
    prepared: PreparedCandidate,
    config: AutoPRConfig,
) -> FinalizedResult:
    """Review, commit, and create a PR for a prepared candidate.

    Shared execution spine used by both `autopr run` and `autopr research`.
    """
    wt_path = Path(prepared.worktree_path)
    result = FinalizedResult()

    # ── Structured review ────────────────────────────────────────────
    structured_review = structured_review_fix(
        opportunity,
        wt_path,
        base_branch=config.repo.base_branch,
        model=config.reviewer_model,
        verdict_rules=config.gating.verdict_rules,
    )
    review = structured_review.to_review_result()
    result.review = review
    result.structured_review = structured_review

    if review.verdict == ReviewVerdict.REJECT:
        result.status = "failed"
        result.error = f"Reviewer rejected: {review.summary}"
        console.print(
            f"[bold red]REJECTED:[/bold red] {opportunity.title}\n  Reason: {review.summary}"
        )
        return result

    if review.verdict == ReviewVerdict.REQUEST_CHANGES:
        console.print(f"[yellow]Review requested changes:[/yellow] {review.summary}")

    # ── Codex diff review loop ───────────────────────────────────────
    codex_diff = None
    if config.gating.enable_material_review:
        codex_diff = codex_review_loop(
            opportunity,
            wt_path,
            base_branch=config.repo.base_branch,
            review_model=config.gating.review_model,
            fix_model=config.model,
            max_iterations=config.gating.codex_max_fix_iterations,
        )
        result.codex_diff_review = codex_diff
        if not codex_diff.approve:
            result.status = "failed"
            result.error = f"Codex review: {codex_diff.summary}"
            console.print(
                f"[bold red]CODEX REJECTED:[/bold red] {opportunity.title}\n  {codex_diff.summary}"
            )
            return result

    # ── Commit + push + PR ───────────────────────────────────────────
    commit_changes(
        wt_path,
        f"fix: apply review feedback for {opportunity.id}",
        protected_files=config.repo.protected_files,
    )

    if config.push_to_remote and config.create_github_pr:
        result.pr_url = push_and_create_pr(
            opportunity,
            prepared.branch_name,
            wt_path,
            base_branch=config.repo.base_branch,
            review=review,
            structured_review=structured_review,
            dry_run=config.dry_run,
            health_checks=prepared.health_checks,
            codex_review=codex_diff,
        )
    elif config.push_to_remote:
        push_branch(wt_path, prepared.branch_name)

    result.status = "success"
    console.print(f"[bold green]SUCCESS:[/bold green] {opportunity.title}")
    return result


def process_single_opportunity(
    opportunity: Opportunity,
    config: AutoPRConfig,
) -> PRResult:
    """Process one opportunity end-to-end in its own worktree.

    Pipeline: prepare_candidate → finalize_candidate
    The worktree is cleaned up after completion.
    """
    console.rule(f"[bold]PR: {opportunity.title}[/bold]")

    pr_result = PRResult(
        opportunity=opportunity,
        branch_name=f"{config.repo.branch_prefix}{opportunity.id}",
        status="in_progress",
    )
    prepared: PreparedCandidate | None = None

    try:
        prepared = prepare_candidate(opportunity, config)
        pr_result.branch_name = prepared.branch_name
        pr_result.screen = prepared.screen
        pr_result.codex_scope_review = prepared.codex_scope_review
        pr_result.health_checks = prepared.health_checks

        if prepared.status != "ready":
            pr_result.status = prepared.status
            pr_result.error = prepared.error
            return pr_result

        finalized = finalize_candidate(prepared.opportunity, prepared, config)
        pr_result.review = finalized.review
        pr_result.codex_diff_review = finalized.codex_diff_review
        pr_result.pr_url = finalized.pr_url
        pr_result.status = finalized.status
        pr_result.error = finalized.error

    except Exception as e:
        pr_result.status = "failed"
        pr_result.error = str(e)
        console.print(f"[bold red]ERROR:[/bold red] {opportunity.title}\n  {e}")

    finally:
        wt_path = Path(prepared.worktree_path) if prepared and prepared.worktree_path else None
        if wt_path and wt_path.exists():
            try:
                remove_worktree(config.repo.path, wt_path)
            except Exception:
                pass

    return pr_result


def run_pipeline(
    config: AutoPRConfig,
    manifest_path: Path | None = None,
    playbook_dir: Path | None = None,
    pattern_ids: list[str] | None = None,
    max_workers: int = 4,
) -> RunReport:
    """Run the full autopr pipeline.

    Each opportunity is processed in its own git worktree so your main
    checkout is never modified and you can keep working normally.

    Args:
        config: The AutoPR configuration
        manifest_path: Optional path to a JSON manifest. If None, the
                       analyzer scans the repo using each playbook pattern.
        playbook_dir: Optional path to a playbook directory. If None, uses
                      the built-in playbook/ shipped with autopr.
        pattern_ids: If set, only run these playbook pattern IDs.
        max_workers: Max parallel scanner processes for analysis.
    """
    report = RunReport(
        repo_path=str(config.repo.path),
        repo_url=config.repo.remote_url,
        base_branch=config.repo.base_branch,
    )

    worktree_dir = resolve_worktree_dir(config)
    console.print(f"[dim]Worktrees will be created in: {worktree_dir}[/dim]")

    # Step 1: Find opportunities (reads the main repo — no modifications)
    if manifest_path:
        opportunities = analyze_from_manifest(manifest_path, config.repo)
    else:
        opportunities = analyze_repo(
            config.repo,
            model=config.model,
            playbook_dir=playbook_dir,
            pattern_ids=pattern_ids,
            max_workers=max_workers,
        )

    if not opportunities:
        console.print("[yellow]No opportunities found. Nothing to do.[/yellow]")
        report.completed_at = datetime.now()
        return report

    # Filter out opportunities that already have branches from prior runs
    opportunities = filter_already_handled(
        opportunities,
        config.repo.path,
        config.repo.branch_prefix,
    )

    if not opportunities:
        console.print(
            "[yellow]All opportunities already have open or merged PRs. Nothing new to do.[/yellow]"
        )
        report.completed_at = datetime.now()
        return report

    # Display opportunities
    table = Table(title="Opportunities Found")
    table.add_column("ID", style="cyan")
    table.add_column("Title", style="white")
    table.add_column("Type", style="yellow")
    table.add_column("Confidence", justify="right", style="green")
    table.add_column("Complexity", style="dim")

    for opp in opportunities:
        table.add_row(
            opp.id,
            opp.title,
            opp.type,
            f"{opp.autonomous_confidence:.0%}",
            opp.complexity,
        )
    console.print(table)

    # Step 2: Process each opportunity
    # Each gets its own worktree so they're fully isolated from each other
    # and from your main checkout.
    for opp in opportunities:
        result = process_single_opportunity(opp, config)
        report.results.append(result)

    # Cleanup
    prune_worktrees(config.repo.path)

    # Final summary
    report.completed_at = datetime.now()
    print_summary(report)

    return report


def print_summary(report: RunReport) -> None:
    """Print a final summary table."""
    console.print("\n")
    console.rule("[bold]AutoPR Run Summary[/bold]")

    table = Table()
    table.add_column("PR", style="cyan")
    table.add_column("Status", style="white")
    table.add_column("PR URL", style="blue")
    table.add_column("Review", style="yellow")
    table.add_column("Health", style="green")

    for r in report.results:
        status_style = "green" if r.status == "success" else "red"
        review_text = r.review.verdict.value if r.review else "n/a"
        health_text = (
            f"{sum(1 for h in r.health_checks if h.passed)}/{len(r.health_checks)}"
            if r.health_checks
            else "n/a"
        )
        table.add_row(
            r.opportunity.title[:50],
            f"[{status_style}]{r.status}[/{status_style}]",
            r.pr_url or "—",
            review_text,
            health_text,
        )

    console.print(table)

    succeeded = len(report.succeeded)
    failed = len(report.failed)
    total = len(report.results)
    console.print(
        f"\n[bold]Total: {total}[/bold] | "
        f"[green]Succeeded: {succeeded}[/green] | "
        f"[red]Failed: {failed}[/red]"
    )
