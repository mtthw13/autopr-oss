"""Conflict resolver — rebase-first with merge fallback, Claude-driven marker resolution."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from rich.console import Console

from autopr.config import AutomationConfig, HealthCheckConfig
from autopr.health import run_health_checks
from autopr.models import ConflictResolutionResult

console = Console()

CONFLICT_RESOLUTION_PROMPT = textwrap.dedent("""\
    You are resolving merge conflicts during a {strategy} of a PR branch onto {base_branch}.

    ## PR Context
    **Title:** {title}
    **Problem:** {problem}
    **Solution:** {solution}

    ## Conflicting Files
    {file_list}

    ## Instructions
    1. Read each conflicting file — look for <<<<<<< / ======= / >>>>>>> markers.
    2. The PR's intent is described above. Preserve the PR's changes while \
incorporating upstream changes.
    3. Remove ALL conflict markers completely.
    4. Do NOT make any changes beyond resolving the conflicts.
    5. After resolving, verify: grep -rn '<<<<<<' . (must return nothing)
""")


def attempt_rebase(wt_path: Path, base_branch: str) -> bool:
    """Run git rebase origin/{base_branch}. Return True if clean, False if conflicts."""
    result = subprocess.run(
        ["git", "rebase", f"origin/{base_branch}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def abort_rebase(wt_path: Path) -> None:
    """Abort an in-progress rebase."""
    subprocess.run(
        ["git", "rebase", "--abort"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )


def attempt_merge(wt_path: Path, base_branch: str) -> bool:
    """Run git merge --no-commit origin/{base_branch}. Return True if clean."""
    result = subprocess.run(
        ["git", "merge", "--no-commit", f"origin/{base_branch}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def get_conflicting_files(wt_path: Path) -> list[str]:
    """Return list of files with unresolved conflicts."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    return [f for f in result.stdout.strip().split("\n") if f]


def resolve_conflicts_with_claude(
    wt_path: Path,
    conflicting_files: list[str],
    pr_context: dict[str, str],
    model: str,
    strategy: str,
    base_branch: str,
) -> bool:
    """Use Claude to resolve conflict markers. Return True if all markers removed."""
    file_list = "\n".join(f"- `{f}`" for f in conflicting_files)
    prompt = CONFLICT_RESOLUTION_PROMPT.format(
        strategy=strategy,
        base_branch=base_branch,
        title=pr_context.get("title", ""),
        problem=pr_context.get("problem", ""),
        solution=pr_context.get("solution", ""),
        file_list=file_list,
    )

    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        "high",
        "--max-turns",
        "10",
        "--allowedTools",
        "Read,Edit,Write,Bash,Grep,Glob",
        "--permission-mode",
        "bypassPermissions",
    ]

    console.print("[dim]  Running Claude to resolve merge conflicts...[/dim]")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(wt_path),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        console.print("[yellow]  Conflict resolution agent timed out[/yellow]")
        return False

    if result.returncode != 0:
        console.print(
            f"[yellow]  Conflict resolution agent failed (exit {result.returncode})[/yellow]"
        )
        return False

    # Verify no conflict markers remain
    check = subprocess.run(
        ["grep", "-rn", "<<<<<<<", "."],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if check.stdout.strip():
        console.print("[red]  Conflict markers still present after resolution[/red]")
        return False

    return True


def continue_rebase(wt_path: Path, max_retries: int = 2) -> bool:
    """Stage resolved files and continue rebase. Loop for multi-step rebases."""
    for _ in range(max_retries + 1):
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(wt_path),
            capture_output=True,
        )
        result = subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        if result.returncode == 0:
            return True
        # Check if there are more conflicts to resolve
        conflicts = get_conflicting_files(wt_path)
        if not conflicts:
            # No conflicts but rebase not done — might need another continue
            continue
        # More conflicts appeared — caller needs to resolve again
        return False
    return False


def _git_env() -> dict[str, str]:
    """Return env dict that suppresses the rebase editor."""
    import os

    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"
    return env


def _revert_protected_files(wt_path: Path, base_branch: str, protected: list[str]) -> None:
    """Revert protected files to base branch version after conflict resolution."""
    import fnmatch

    branch_diff = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    worktree_diff = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    staged_diff = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    changed = set()
    for output in (branch_diff.stdout, worktree_diff.stdout, staged_diff.stdout):
        if output.strip():
            changed.update(output.strip().split("\n"))
    if not changed:
        return

    to_revert = [f for f in changed if any(fnmatch.fnmatch(f, pat) for pat in protected)]
    if to_revert:
        subprocess.run(
            ["git", "checkout", f"origin/{base_branch}", "--"] + to_revert,
            cwd=str(wt_path),
            capture_output=True,
        )


def _has_conflict_markers(wt_path: Path) -> bool:
    """Return True when conflict markers remain in the worktree."""
    check = subprocess.run(
        ["grep", "-rn", "<<<<<<<", "."],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    return bool(check.stdout.strip())


def _has_staged_changes(wt_path: Path) -> bool:
    """Return True when there is anything staged for commit."""
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(wt_path),
        capture_output=True,
    )
    return diff.returncode != 0


def _commit_if_needed(wt_path: Path, message: str) -> bool:
    """Commit staged changes if present."""
    subprocess.run(["git", "add", "-A"], cwd=str(wt_path), capture_output=True)
    if not _has_staged_changes(wt_path):
        return False
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    return True


def _run_post_resolution_checks(
    wt_path: Path,
    base_branch: str,
    model: str,
    protected_files: list[str],
    health_checks: HealthCheckConfig | None,
) -> str:
    """Apply post-resolution guardrails and return an error message on failure."""
    if _has_conflict_markers(wt_path):
        return "Conflict markers remain after resolution"

    _revert_protected_files(wt_path, base_branch, protected_files)

    if health_checks is not None:
        results = run_health_checks(
            health_checks,
            wt_path,
            model=model,
            protected_files=protected_files,
        )
        if not results or not all(result.passed for result in results):
            return "Health checks failed after conflict resolution"

    if _has_conflict_markers(wt_path):
        return "Conflict markers remain after health checks"

    return ""


def resolve_pr_conflicts(
    repo_path: Path,
    wt_path: Path,
    pr_info: dict,
    base_branch: str,
    model: str,
    config: AutomationConfig,
    health_checks: HealthCheckConfig | None = None,
    protected_files: list[str] | None = None,
) -> ConflictResolutionResult:
    """Orchestrate conflict resolution for a single PR.

    Tries rebase first, falls back to merge. Uses Claude to resolve markers.
    """
    del repo_path
    pr_number = pr_info.get("number", 0)
    pr_branch = pr_info.get("headRefName", "")
    protected = list(protected_files or [])
    if not protected:
        from autopr.config import DEFAULT_PROTECTED_FILES

        protected = list(DEFAULT_PROTECTED_FILES)
    pr_context = {
        "title": pr_info.get("title", ""),
        "problem": pr_info.get("problem", ""),
        "solution": pr_info.get("solution", ""),
    }

    # Fetch latest
    subprocess.run(
        ["git", "fetch", "origin", base_branch, pr_branch],
        cwd=str(wt_path),
        capture_output=True,
    )

    # --- Try rebase ---
    console.print(f"  [dim]Attempting rebase of {pr_branch} onto {base_branch}...[/dim]")
    if attempt_rebase(wt_path, base_branch):
        console.print(f"  [green]Clean rebase for PR #{pr_number}[/green]")
        error = _run_post_resolution_checks(
            wt_path,
            base_branch,
            model,
            protected,
            health_checks,
        )
        if error:
            return ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="failed",
                strategy_used="rebase",
                error=error,
            )
        _commit_if_needed(
            wt_path,
            f"fix: stabilize branch after rebasing {pr_branch} onto {base_branch}",
        )
        # Push with force-with-lease (rebase rewrites history)
        push = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", f"HEAD:refs/heads/{pr_branch}"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        return (
            ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="clean_rebase",
                strategy_used="rebase",
            )
            if push.returncode == 0
            else ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="failed",
                strategy_used="rebase",
                error=f"Push failed: {push.stderr.strip()}",
            )
        )

    # Rebase has conflicts — try Claude resolution
    conflicts = get_conflicting_files(wt_path)
    console.print(f"  [yellow]Rebase conflicts in {len(conflicts)} file(s)[/yellow]")

    if conflicts and resolve_conflicts_with_claude(
        wt_path, conflicts, pr_context, model, "rebase", base_branch
    ):
        if continue_rebase(wt_path, max_retries=config.max_conflict_retries):
            error = _run_post_resolution_checks(
                wt_path,
                base_branch,
                model,
                protected,
                health_checks,
            )
            if error:
                return ConflictResolutionResult(
                    pr_number=pr_number,
                    branch_name=pr_branch,
                    status="failed",
                    strategy_used="rebase",
                    conflicting_files=conflicts,
                    error=error,
                )
            _commit_if_needed(
                wt_path,
                f"fix: stabilize branch after resolving conflicts against {base_branch}",
            )
            push = subprocess.run(
                ["git", "push", "--force-with-lease", "origin", f"HEAD:refs/heads/{pr_branch}"],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
            )
            if push.returncode == 0:
                console.print(f"  [green]Resolved rebase conflicts for PR #{pr_number}[/green]")
                return ConflictResolutionResult(
                    pr_number=pr_number,
                    branch_name=pr_branch,
                    status="conflict_resolved",
                    strategy_used="rebase",
                    conflicting_files=conflicts,
                )

    # Rebase resolution failed — abort and try merge
    abort_rebase(wt_path)
    console.print(f"  [dim]Rebase failed, trying merge for PR #{pr_number}...[/dim]")

    # Reset to the PR branch state
    subprocess.run(
        ["git", "reset", "--hard", f"origin/{pr_branch}"],
        cwd=str(wt_path),
        capture_output=True,
    )

    if attempt_merge(wt_path, base_branch):
        error = _run_post_resolution_checks(
            wt_path,
            base_branch,
            model,
            protected,
            health_checks,
        )
        if error:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(wt_path),
                capture_output=True,
            )
            return ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="failed",
                strategy_used="merge",
                error=error,
            )
        subprocess.run(["git", "add", "-A"], cwd=str(wt_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "--no-edit", "-m", f"Merge {base_branch} into {pr_branch}"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            env=_git_env(),
            check=True,
        )
        push = subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{pr_branch}"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        if push.returncode == 0:
            console.print(f"  [green]Clean merge for PR #{pr_number}[/green]")
            return ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="conflict_resolved",
                strategy_used="merge",
            )

    # Merge has conflicts — try Claude resolution
    merge_conflicts = get_conflicting_files(wt_path)
    if merge_conflicts and resolve_conflicts_with_claude(
        wt_path, merge_conflicts, pr_context, model, "merge", base_branch
    ):
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(wt_path),
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "commit",
                "--no-edit",
                "-m",
                f"Merge {base_branch} into {pr_branch} (resolved)",
            ],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        error = _run_post_resolution_checks(
            wt_path,
            base_branch,
            model,
            protected,
            health_checks,
        )
        if error:
            return ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="failed",
                strategy_used="merge",
                conflicting_files=merge_conflicts,
                error=error,
            )
        _commit_if_needed(
            wt_path,
            f"fix: stabilize branch after resolving merge conflicts against {base_branch}",
        )
        push = subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{pr_branch}"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        if push.returncode == 0:
            console.print(f"  [green]Resolved merge conflicts for PR #{pr_number}[/green]")
            return ConflictResolutionResult(
                pr_number=pr_number,
                branch_name=pr_branch,
                status="conflict_resolved",
                strategy_used="merge",
                conflicting_files=merge_conflicts,
            )

    # All strategies failed
    # Abort any pending merge
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=str(wt_path),
        capture_output=True,
    )
    console.print(f"  [red]Could not resolve conflicts for PR #{pr_number} — needs human[/red]")
    return ConflictResolutionResult(
        pr_number=pr_number,
        branch_name=pr_branch,
        status="failed",
        strategy_used="none",
        conflicting_files=merge_conflicts if merge_conflicts else conflicts,
        error="All resolution strategies exhausted",
    )
