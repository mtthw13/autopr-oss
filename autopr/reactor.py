"""Reactor — event dispatch and per-event handlers for PR automation."""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from rich.console import Console

from autopr.config import AutoPRConfig
from autopr.models import ReactResult

console = Console()


def get_latest_bot_commit_timestamp(
    wt_path: Path,
    bot_name: str,
    bot_email: str,
) -> datetime | None:
    """Return the timestamp of the most recent commit by the bot, or None."""
    result = subprocess.run(
        ["git", "log", f"--author={bot_email}", "--format=%aI", "-1"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    ts = result.stdout.strip()
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def filter_actionable_comments(
    comments: list[dict],
    cutoff_timestamp: datetime | None,
    bot_name: str = "autopr[bot]",
) -> list[dict]:
    """Keep only comments that are new, non-empty, and not bot-authored."""
    actionable = []
    for c in comments:
        body = (c.get("body") or "").strip()
        if not body:
            continue
        author = c.get("author", {}).get("login", "")
        if author == bot_name:
            continue
        if cutoff_timestamp:
            created = c.get("createdAt", "")
            if created:
                created_dt = datetime.fromisoformat(created)
                if created_dt <= cutoff_timestamp:
                    continue
        actionable.append(c)
    return actionable


def _fetch_review_comments(repo_path: Path, pr_number: int, review_id: int) -> list[dict]:
    """Fetch inline comments for a submitted review."""
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/:owner/:repo/pulls/{pr_number}/reviews/{review_id}/comments",
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    raw_comments = json.loads(result.stdout)
    comments: list[dict] = []
    for comment in raw_comments:
        comments.append(
            {
                "body": comment.get("body", ""),
                "author": {"login": comment.get("user", {}).get("login", "")},
                "createdAt": comment.get("created_at", ""),
            }
        )
    return comments


def post_result_comment(repo_path: Path, pr_number: int, result: ReactResult) -> None:
    """Post a terse result comment on the PR."""
    if result.action == "fixed":
        body = f"Fixed. Pushed `{result.pushed_sha[:8]}`. {result.summary}"
    elif result.action == "no_action":
        body = f"No action needed. {result.summary}"
    elif result.action == "needs_input":
        body = f"Needs input: {result.summary}"
    elif result.action == "needs_human":
        body = f"Needs human intervention: {result.summary}"
    elif result.action == "skipped":
        body = f"Skipped. {result.summary}"
    else:
        body = result.summary or "Processed event."

    subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--body", body],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )


def _create_temp_worktree(repo_path: Path, pr_branch: str, pr_number: int) -> Path:
    """Fetch branch and create a temporary worktree. Returns worktree path."""
    subprocess.run(
        ["git", "fetch", "origin", pr_branch],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    worktree_dir = tempfile.mkdtemp(prefix=f"autopr-react-{pr_number}-")
    subprocess.run(
        ["git", "worktree", "add", "--detach", worktree_dir, f"origin/{pr_branch}"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(worktree_dir)


def _cleanup_worktree(repo_path: Path, wt_path: Path) -> None:
    """Remove a temporary worktree."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )


def _get_head_sha(wt_path: Path) -> str:
    """Get current HEAD sha."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_and_push(
    wt_path: Path, pr_branch: str, message: str, protected_files: list[str]
) -> str:
    """Stage, restore protected files, commit, push. Returns new SHA or empty string."""
    from autopr.fixer import restore_protected_files

    subprocess.run(["git", "add", "-A"], cwd=str(wt_path), capture_output=True)
    restore_protected_files(wt_path, protected_files)

    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(wt_path),
        capture_output=True,
    )
    if result.returncode == 0:
        return ""  # Nothing to commit

    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )

    push = subprocess.run(
        ["git", "push", "origin", f"HEAD:refs/heads/{pr_branch}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        console.print(f"[red]  Push failed: {push.stderr.strip()}[/red]")
        return ""

    return _get_head_sha(wt_path)


def handle_ci_failed(
    repo_path: Path,
    pr_number: int,
    pr_branch: str,
    check_name: str,
    model: str,
    config: AutoPRConfig,
) -> ReactResult:
    """React to a CI check failure by reproducing and auto-fixing."""
    from autopr.config import DEFAULT_PROTECTED_FILES
    from autopr.health import run_health_checks

    wt_path = _create_temp_worktree(repo_path, pr_branch, pr_number)
    try:
        console.print(f"  [dim]Reproducing CI failure for PR #{pr_number}...[/dim]")
        results = run_health_checks(
            config.repo.health_checks,
            wt_path,
            model=model,
            protected_files=DEFAULT_PROTECTED_FILES,
        )

        all_passed = all(r.passed for r in results)
        if not all_passed:
            return ReactResult(
                pr_number=pr_number,
                event="ci_failed",
                action="needs_human",
                summary=f"Health checks still failing after auto-fix attempts ({check_name}).",
            )

        sha = _commit_and_push(
            wt_path, pr_branch, f"Fix CI failure ({check_name})", DEFAULT_PROTECTED_FILES
        )
        if not sha:
            return ReactResult(
                pr_number=pr_number,
                event="ci_failed",
                action="no_action",
                summary="Health checks pass — no changes needed.",
            )

        return ReactResult(
            pr_number=pr_number,
            event="ci_failed",
            action="fixed",
            summary=f"Fixed CI failure ({check_name}).",
            pushed_sha=sha,
        )
    finally:
        _cleanup_worktree(repo_path, wt_path)


def handle_review_feedback(
    repo_path: Path,
    pr_number: int,
    pr_branch: str,
    comment_body: str,
    comment_id: int | None,
    review_id: int | None,
    comment_author: str,
    created_at: str,
    model: str,
    config: AutoPRConfig,
) -> ReactResult:
    """React to a review comment by fixing the issue."""
    from autopr.config import DEFAULT_PROTECTED_FILES
    from autopr.pr_creator import parse_opportunity_from_pr_body
    from autopr.reviewer import run_fixer_agent

    wt_path = _create_temp_worktree(repo_path, pr_branch, pr_number)
    try:
        event_name = "review_submitted" if review_id is not None else "review_comment"
        cutoff = get_latest_bot_commit_timestamp(
            wt_path, config.automation.bot_name, config.automation.bot_email
        )

        comments: list[dict] = []
        if review_id is not None:
            comments.extend(_fetch_review_comments(repo_path, pr_number, review_id))
            if comment_body.strip():
                comments.append(
                    {
                        "body": comment_body,
                        "author": {"login": comment_author},
                        "createdAt": created_at,
                    }
                )
        else:
            comments.append(
                {
                    "body": comment_body,
                    "author": {"login": comment_author},
                    "createdAt": created_at,
                }
            )

        actionable = filter_actionable_comments(
            comments,
            cutoff_timestamp=cutoff,
            bot_name=config.automation.bot_name,
        )
        if not actionable:
            return ReactResult(
                pr_number=pr_number,
                event=event_name,
                action="no_action",
                summary="No new actionable review feedback.",
            )

        # Get PR context
        gh_result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "body,title"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        pr_data = json.loads(gh_result.stdout) if gh_result.returncode == 0 else {}
        opp = parse_opportunity_from_pr_body(
            pr_data.get("body", ""), pr_number=pr_number, title=pr_data.get("title", "")
        )

        # Build fix prompt from comment
        actionable_text = "\n".join(
            f"- {item.get('body', '').strip()}"
            for item in actionable
            if item.get("body", "").strip()
        )
        prompt = (
            f"A reviewer left feedback on PR #{pr_number} ({opp.title}).\n\n"
            f"## Review Feedback\n{actionable_text}\n\n"
            f"## PR Context\n**Problem:** {opp.problem}\n**Solution:** {opp.solution}\n\n"
            "Fix the issue described in the review comment. Make minimal changes."
        )

        run_fixer_agent(wt_path, prompt, model)

        sha = _commit_and_push(
            wt_path,
            pr_branch,
            f"Address review feedback (comment {comment_id or 'N/A'})",
            DEFAULT_PROTECTED_FILES,
        )
        if not sha:
            return ReactResult(
                pr_number=pr_number,
                event=event_name,
                action="no_action",
                summary="No changes needed for the new review feedback.",
            )

        return ReactResult(
            pr_number=pr_number,
            event=event_name,
            action="fixed",
            summary="Addressed review feedback.",
            pushed_sha=sha,
        )
    finally:
        _cleanup_worktree(repo_path, wt_path)


def handle_pr_comment(
    repo_path: Path,
    pr_number: int,
    pr_branch: str,
    comment_body: str,
    comment_command: str,
    model: str,
    config: AutoPRConfig,
) -> ReactResult:
    """React to a PR comment containing the command trigger (e.g. /autopr fix)."""
    if comment_command not in comment_body:
        return ReactResult(
            pr_number=pr_number,
            event="pr_comment",
            action="no_action",
            summary=f"Comment does not contain '{comment_command}'.",
        )

    # Extract instructions after the command
    idx = comment_body.index(comment_command) + len(comment_command)
    instructions = comment_body[idx:].strip()

    return handle_review_feedback(
        repo_path=repo_path,
        pr_number=pr_number,
        pr_branch=pr_branch,
        comment_body=instructions or comment_body,
        comment_id=None,
        review_id=None,
        comment_author="",
        created_at="",
        model=model,
        config=config,
    )


def handle_base_branch_updated(
    repo_path: Path,
    pr_number: int,
    pr_branch: str,
    base_branch: str,
    model: str,
    config: AutoPRConfig,
) -> ReactResult:
    """React to the base branch being updated (merge conflicts or behind)."""
    from autopr.conflict_resolver import resolve_pr_conflicts

    # Check PR mergeability
    gh_result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "mergeStateStatus,mergeable,headRefName,title,body",
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if gh_result.returncode != 0:
        return ReactResult(
            pr_number=pr_number,
            event="base_branch_updated",
            action="skipped",
            summary=f"Could not fetch PR info: {gh_result.stderr.strip()}",
        )

    pr_data = json.loads(gh_result.stdout)
    merge_state = pr_data.get("mergeStateStatus", "")
    mergeable = pr_data.get("mergeable", "")

    # Already clean
    if merge_state in ("CLEAN", "HAS_HOOKS", "UNSTABLE") and mergeable != "CONFLICTING":
        return ReactResult(
            pr_number=pr_number,
            event="base_branch_updated",
            action="no_action",
            summary="PR is already up to date.",
        )

    # Behind but not conflicting — use GitHub's update-branch
    if merge_state == "BEHIND" and mergeable != "CONFLICTING":
        update_result = subprocess.run(
            ["gh", "pr", "update-branch", str(pr_number), "--rebase"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if update_result.returncode == 0:
            return ReactResult(
                pr_number=pr_number,
                event="base_branch_updated",
                action="fixed",
                summary=f"Rebased onto {base_branch} via GitHub API.",
            )
        return ReactResult(
            pr_number=pr_number,
            event="base_branch_updated",
            action="needs_human",
            summary=f"Could not update branch: {update_result.stderr.strip()}",
        )

    # Conflicting — enter conflict resolver
    if merge_state in ("DIRTY", "CONFLICTING") or mergeable == "CONFLICTING":
        from autopr.pr_creator import parse_opportunity_from_pr_body

        opp = parse_opportunity_from_pr_body(
            pr_data.get("body", ""), pr_number=pr_number, title=pr_data.get("title", "")
        )
        pr_info = {
            "number": pr_number,
            "headRefName": pr_branch,
            "title": pr_data.get("title", ""),
            "problem": opp.problem,
            "solution": opp.solution,
        }

        wt_path = _create_temp_worktree(repo_path, pr_branch, pr_number)
        try:
            cr_result = resolve_pr_conflicts(
                repo_path,
                wt_path,
                pr_info,
                base_branch,
                model,
                config.automation,
                health_checks=config.repo.health_checks,
                protected_files=config.repo.protected_files,
            )
            if cr_result.status in ("clean_rebase", "conflict_resolved"):
                return ReactResult(
                    pr_number=pr_number,
                    event="base_branch_updated",
                    action="fixed",
                    summary=f"Resolved conflicts via {cr_result.strategy_used}.",
                )
            else:
                return ReactResult(
                    pr_number=pr_number,
                    event="base_branch_updated",
                    action="needs_human",
                    summary=f"Could not resolve conflicts: {cr_result.error}",
                )
        finally:
            _cleanup_worktree(repo_path, wt_path)

    return ReactResult(
        pr_number=pr_number,
        event="base_branch_updated",
        action="skipped",
        summary=f"Unhandled merge state: {merge_state}/{mergeable}.",
    )


def dispatch_event(
    event: str,
    repo_path: Path,
    pr_number: int,
    pr_branch: str,
    model: str,
    config: AutoPRConfig,
    *,
    head_sha: str = "",
    check_name: str = "",
    comment_body: str = "",
    comment_id: int | None = None,
    review_id: int | None = None,
    comment_author: str = "",
    created_at: str = "",
    base_branch: str = "main",
    dry_run: bool = False,
) -> ReactResult:
    """Route an event to the appropriate handler."""
    console.print(f"\n[bold]autopr react[/bold] — PR #{pr_number}, event={event}")
    console.print(f"  Branch: {pr_branch}")
    console.print(f"  SHA:    {head_sha}")

    if dry_run:
        return ReactResult(
            pr_number=pr_number,
            event=event,
            action="skipped",
            summary="Dry run — no action taken.",
        )

    if event == "ci_failed":
        result = handle_ci_failed(repo_path, pr_number, pr_branch, check_name, model, config)
    elif event in ("review_submitted", "review_comment"):
        result = handle_review_feedback(
            repo_path,
            pr_number,
            pr_branch,
            comment_body,
            comment_id,
            review_id,
            comment_author,
            created_at,
            model,
            config,
        )
    elif event == "pr_comment":
        result = handle_pr_comment(
            repo_path,
            pr_number,
            pr_branch,
            comment_body,
            config.automation.comment_command,
            model,
            config,
        )
    elif event == "base_branch_updated":
        result = handle_base_branch_updated(
            repo_path, pr_number, pr_branch, base_branch, model, config
        )
    else:
        result = ReactResult(
            pr_number=pr_number,
            event=event,
            action="skipped",
            summary=f"Unknown event type: {event}",
        )

    console.print(f"  Result: {result.action} — {result.summary}")

    # Post result comment (unless no_action or skipped)
    if result.action not in ("no_action", "skipped"):
        post_result_comment(repo_path, pr_number, result)

    return result
