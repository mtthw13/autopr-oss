"""Fix generator — uses git worktrees so your main checkout is never touched."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

from rich.console import Console

from autopr.config import AutoPRConfig
from autopr.models import Opportunity

console = Console()

FIX_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are an expert software engineer making a precise, minimal fix.

    ## Opportunity
    **Title:** {title}
    **Type:** {type}
    **Problem:** {problem}
    **Solution:** {solution}
    **Files:** {files}
    {line_hint}

    ## Instructions
    1. Read the relevant files listed above to understand the current code.
    2. Implement the fix described in "Solution" — make the MINIMUM changes needed.
    3. Do NOT change any public API signatures unless the solution explicitly requires it.
    4. Do NOT add comments explaining what you changed (the PR description handles that).
    5. Do NOT add type: ignore, noqa, or eslint-disable comments.
    6. Do NOT add unused imports or variables.
    7. If removing dead code, verify with grep/search that it's truly unused first.
    8. If batching queries, maintain the exact same return shape and behavior.
    9. After making changes, review your diff to ensure correctness.

    Make the fix now. Only modify the files listed above unless absolutely necessary.
""")


# ── Worktree helpers ──────────────────────────────────────────────────────────


def resolve_worktree_dir(config: AutoPRConfig) -> Path:
    """Return the base directory where worktrees are created.

    Defaults to <repo>-autopr/ sitting next to the repo, e.g.
      ~/code/myapp           ← your checkout
      ~/code/myapp-autopr/   ← worktrees live here
    """
    if config.repo.worktree_dir:
        return config.repo.worktree_dir
    return config.repo.path.parent / f"{config.repo.path.name}-autopr"


def create_worktree(
    repo_path: Path,
    worktree_dir: Path,
    branch_name: str,
    base_branch: str = "main",
) -> Path:
    """Create a git worktree for *branch_name* under *worktree_dir*.

    Returns the absolute path to the new worktree.
    """
    wt_path = worktree_dir / branch_name.replace("/", "-")

    # Clean up stale worktree if it exists
    if wt_path.exists():
        remove_worktree(repo_path, wt_path)

    worktree_dir.mkdir(parents=True, exist_ok=True)

    # Fetch latest from origin so worktrees are never based on stale local state
    subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=str(repo_path),
        capture_output=True,
    )

    # Create the branch from origin's version, not local
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=str(repo_path),
        capture_output=True,  # ignore errors if branch doesn't exist
    )
    subprocess.run(
        ["git", "branch", branch_name, f"origin/{base_branch}"],
        cwd=str(repo_path),
        capture_output=True,
        check=True,
    )

    # Create the worktree
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), branch_name],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )

    console.print(f"[dim]Created worktree: {wt_path}[/dim]")
    return wt_path


def remove_worktree(repo_path: Path, wt_path: Path) -> None:
    """Remove a worktree and its directory."""
    subprocess.run(
        ["git", "worktree", "remove", str(wt_path), "--force"],
        cwd=str(repo_path),
        capture_output=True,
    )
    # Belt-and-suspenders: if the directory remains, delete it
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


def prune_worktrees(repo_path: Path) -> None:
    """Prune stale worktree bookkeeping."""
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(repo_path),
        capture_output=True,
    )


# ── Git helpers (operate inside a worktree) ───────────────────────────────────


def restore_protected_files(wt_path: Path, protected: list[str]) -> None:
    """Revert any modifications to protected files before committing.

    Uses git checkout to silently restore each protected pattern.
    Failures are ignored — the file may not exist in this repo.
    """
    if not protected:
        return

    # Get list of modified/added files that match protected patterns
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return

    changed = set(result.stdout.strip().split("\n"))

    # Also include untracked files
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    untracked_files = (
        set(untracked.stdout.strip().split("\n")) if untracked.stdout.strip() else set()
    )

    import fnmatch

    to_restore: list[str] = []
    to_remove: list[str] = []
    for pattern in protected:
        for f in changed:
            if fnmatch.fnmatch(f, pattern):
                to_restore.append(f)
        for f in untracked_files:
            if fnmatch.fnmatch(f, pattern):
                to_remove.append(f)

    if to_restore:
        subprocess.run(
            ["git", "checkout", "HEAD", "--"] + to_restore,
            cwd=str(wt_path),
            capture_output=True,
        )
        for f in to_restore:
            console.print(f"[dim]  Protected: reverted {f}[/dim]")

    for f in to_remove:
        path = wt_path / f
        if path.exists():
            path.unlink()
            console.print(f"[dim]  Protected: removed untracked {f}[/dim]")


FORBIDDEN_DIFF_PATTERNS = [
    r"#\s*type:\s*ignore",
    r"#\s*noqa",
    r"//\s*eslint-disable",
    r"//\s*@ts-ignore",
    r"//\s*@ts-expect-error",
]


def strip_lint_suppressions(wt_path: Path) -> list[str]:
    """Detect newly-added lint suppressions in staged diff and revert those files.

    Returns list of files that were reverted.
    """
    import re

    diff = subprocess.run(
        ["git", "diff", "--cached", "-U0"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if not diff.stdout:
        return []

    # Track which files have violations
    current_file = None
    violating_files: set[str] = set()

    for line in diff.stdout.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and current_file:
            for pattern in FORBIDDEN_DIFF_PATTERNS:
                if re.search(pattern, line):
                    violating_files.add(current_file)
                    break

    if not violating_files:
        return []

    # Revert the violating files
    subprocess.run(
        ["git", "checkout", "HEAD", "--"] + list(violating_files),
        cwd=str(wt_path),
        capture_output=True,
    )
    # Re-stage after revert
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(wt_path),
        capture_output=True,
    )
    for f in sorted(violating_files):
        console.print(f"[yellow]  Reverted {f} (contained lint suppression)[/yellow]")

    return sorted(violating_files)


def revert_out_of_scope_files(wt_path: Path, allowed_files: list[str]) -> list[str]:
    """Revert modified files not in the allowed list.

    Doc files (.md, .txt, .rst) are exempt — updating stale docs is in scope.
    Returns list of reverted files.
    """
    from pathlib import Path as _Path

    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return []

    changed = result.stdout.strip().split("\n")
    allowed_set = set(allowed_files)
    doc_extensions = {".md", ".txt", ".rst"}

    out_of_scope = [
        f for f in changed if f not in allowed_set and _Path(f).suffix not in doc_extensions
    ]

    if not out_of_scope:
        return []

    subprocess.run(
        ["git", "checkout", "HEAD", "--"] + out_of_scope,
        cwd=str(wt_path),
        capture_output=True,
    )
    for f in out_of_scope:
        console.print(f"[dim]  Scope: reverted out-of-scope {f}[/dim]")

    return out_of_scope


def commit_changes(wt_path: Path, message: str, protected_files: list[str] | None = None) -> bool:
    """Stage all changes and commit inside a worktree.

    Protected files are reverted before staging. Lint suppressions are stripped.
    Returns True if there was something to commit.
    """
    if protected_files:
        restore_protected_files(wt_path, protected_files)

    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(wt_path),
        capture_output=True,
        check=True,
    )

    # Strip any lint suppressions the fixer snuck in
    strip_lint_suppressions(wt_path)

    diff = subprocess.run(
        ["git", "diff", "--cached", "--stat"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if not diff.stdout.strip():
        console.print("[yellow]No changes to commit[/yellow]")
        return False

    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
        check=True,
    )
    console.print(f"[dim]Committed: {message[:60]}...[/dim]")
    return True


def ensure_worktree_clean(wt_path: Path, context: str = "") -> bool:
    """Safety net: commit any uncommitted changes before a push.

    Returns True if a safety-net commit was made, False if already clean.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return False

    label = f" ({context})" if context else ""
    console.print(
        f"[yellow]Warning: uncommitted changes found before push{label} — "
        f"committing as safety net[/yellow]"
    )

    subprocess.run(["git", "add", "-A"], cwd=str(wt_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"fix: commit remaining uncommitted changes{label}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
        check=True,
    )
    return True


def push_branch(wt_path: Path, branch_name: str) -> None:
    """Push branch to origin (run from inside the worktree)."""
    ensure_worktree_clean(wt_path, context=f"push {branch_name}")
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
        check=True,
    )
    console.print(f"[dim]Pushed: {branch_name}[/dim]")


# ── Main entry point ──────────────────────────────────────────────────────────


def run_fix(
    opportunity: Opportunity,
    config: AutoPRConfig,
) -> tuple[str, Path]:
    """Generate and apply a fix in an isolated worktree.

    Returns (branch_name, worktree_path) on success.
    Raises on failure.
    """
    repo_path = config.repo.path
    branch_name = f"{config.repo.branch_prefix}{opportunity.id}"
    worktree_dir = resolve_worktree_dir(config)

    console.print(
        f"\n[bold cyan]Fixing:[/bold cyan] {opportunity.title} [dim]({opportunity.type})[/dim]"
    )

    # 1. Create an isolated worktree
    wt_path = create_worktree(
        repo_path,
        worktree_dir,
        branch_name,
        config.repo.base_branch,
    )

    # 2. Build prompt
    line_hint = ""
    if opportunity.line_ranges:
        hints = "\n".join(f"  - {f}: lines {r}" for f, r in opportunity.line_ranges.items())
        line_hint = f"**Line ranges (approximate):**\n{hints}"

    prompt = FIX_PROMPT_TEMPLATE.format(
        title=opportunity.title,
        type=opportunity.type,
        problem=opportunity.problem,
        solution=opportunity.solution,
        files=", ".join(opportunity.files),
        line_hint=line_hint,
    )

    # 3. Run Claude Code inside the worktree (never touches your main checkout)
    #    Uses -p (print mode) so it runs non-interactively.
    #    --dangerously-skip-permissions lets it edit files without prompts.
    #    Prompt is piped via stdin to avoid shell argument length issues.
    cmd = [
        "claude",
        "-p",
        "--model",
        config.model,
        "--effort",
        "high",
        "--max-turns",
        "10",
        "--allowedTools",
        "Read,Edit,Write,Bash,Grep,Glob",
        "--permission-mode",
        "bypassPermissions",
    ]

    console.print(f"[dim]Running Claude Code in worktree {wt_path}...[/dim]")
    result = subprocess.run(
        cmd,
        cwd=str(wt_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Claude Code fixer failed:\n{result.stderr}")

    # 4. Commit inside the worktree
    commit_msg = (
        f"{opportunity.title}\n\n"
        f"Problem: {opportunity.problem}\n\n"
        f"Solution: {opportunity.solution}\n\n"
        f"Generated by autopr"
    )
    committed = commit_changes(wt_path, commit_msg, protected_files=config.repo.protected_files)
    if not committed:
        raise RuntimeError("Fix produced no changes — Claude may have failed silently")

    return branch_name, wt_path
