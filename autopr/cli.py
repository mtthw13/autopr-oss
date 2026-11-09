"""CLI entry point for autopr."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

from autopr.config import AutoPRConfig, HealthCheckConfig, RepoConfig
from autopr.orchestrator import run_pipeline
from autopr.research import run_research

console = Console()


def _find_config() -> Path | None:
    """Auto-detect autopr.yaml next to the autopr package or in cwd."""
    # Check next to the package (project root)
    pkg_dir = Path(__file__).resolve().parent.parent
    candidate = pkg_dir / "autopr.yaml"
    if candidate.exists():
        return candidate
    # Check cwd
    candidate = Path.cwd() / "autopr.yaml"
    if candidate.exists():
        return candidate
    return None


def _revert_protected_to_base(
    wt_path: Path,
    base_branch: str,
    protected: list[str],
) -> None:
    """Revert protected files to their base-branch version.

    Unlike restore_protected_files (which restores to HEAD), this checks out
    from origin/<base_branch> so that already-committed bad changes are undone.
    """
    import fnmatch
    import subprocess as _sp

    # Files changed between base branch and current HEAD
    result = _sp.run(
        ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return

    changed = result.stdout.strip().split("\n")
    to_revert = [f for f in changed if any(fnmatch.fnmatch(f, pat) for pat in protected)]

    if to_revert:
        _sp.run(
            ["git", "checkout", f"origin/{base_branch}", "--"] + to_revert,
            cwd=str(wt_path),
            capture_output=True,
        )
        for f in to_revert:
            console.print(f"[dim]  Protected: reverted {f} to {base_branch}[/dim]")


def _load_dotenv() -> None:
    """Load .env file from the autopr project directory if it exists."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    import os

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def _normalize_tool_repo(remote: str) -> str:
    """Reduce a git remote to the `owner/repo` form `actions/checkout` requires.

    `repository:` rejects clone URLs, so a raw `git remote get-url origin` value
    fails on the target repo's first workflow run.
    """
    remote = remote.strip()
    if not remote:
        return ""

    remote = remote.removesuffix(".git")

    if "://" in remote:
        # scheme://host/owner/repo — drop scheme, then the host segment.
        path = remote.partition("://")[2].partition("/")[2]
    elif ":" in remote:
        # scp-style git@host:owner/repo
        path = remote.rpartition(":")[2]
    else:
        path = remote

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 2:
        return "/".join(segments[-2:])
    return remote


def _detect_tool_repo() -> str:
    """Best-effort detection of the autopr source repository, as `owner/repo`."""
    project_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return _normalize_tool_repo(result.stdout)


def _yaml_inline_list(values: list[str]) -> str:
    """Render a YAML inline list with quoted strings."""
    return ", ".join(f'"{value}"' for value in values)


def _render_workflow_template(template: str, config: AutoPRConfig) -> str:
    """Render a workflow template using config-backed placeholders."""
    branch_prefix = (
        config.automation.managed_branch_prefixes[0]
        if config.automation.managed_branch_prefixes
        else config.repo.branch_prefix
    )
    replacements = {
        "__AUTOPR_TOOL_REPO__": config.automation.tool_repo,
        "__AUTOPR_TOOL_REF__": config.automation.tool_ref,
        "__AUTOPR_CI_WORKFLOW_NAMES__": _yaml_inline_list(config.automation.ci_workflow_names),
        "__AUTOPR_BRANCH_PREFIX__": branch_prefix,
        "__AUTOPR_COMMENT_COMMAND__": config.automation.comment_command,
        "__AUTOPR_MAX_RUNS__": str(config.automation.max_cumulative_workflow_runs),
        "__AUTOPR_BASE_BRANCH__": config.repo.base_branch,
    }
    rendered = template
    for needle, value in replacements.items():
        rendered = rendered.replace(needle, value)
    return rendered


def _install_workflow_templates(repo: Path, config: AutoPRConfig) -> None:
    """Render workflow templates into the target repo's .github/workflows/."""
    if not config.automation.tool_repo:
        raise click.ClickException(
            "Workflow generation requires automation.tool_repo to be configured."
        )

    workflows_dir = repo / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    templates_dir = Path(__file__).resolve().parent / "templates"
    if not templates_dir.exists():
        console.print("[dim]No workflow templates found — skipping[/dim]")
        return

    installed = 0
    for template in templates_dir.glob("*.yml"):
        dest = workflows_dir / template.name
        if dest.exists():
            console.print(f"[dim]  Skipping {template.name} — already exists[/dim]")
            continue
        dest.write_text(_render_workflow_template(template.read_text(), config))
        console.print(f"[green]  Installed {template.name}[/green]")
        installed += 1

    if installed > 0:
        console.print(
            "\n[yellow]Remember to add ANTHROPIC_API_KEY as a repository secret![/yellow]"
        )


@click.group()
@click.version_option(version="0.1.0")
def main():
    """autopr — Autonomous PR generation for low-hanging-fruit fixes."""
    _load_dotenv()


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option(
    "--config", "-c", "config_path", type=click.Path(exists=True), help="YAML config file"
)
@click.option(
    "--manifest",
    "-m",
    "manifest_path",
    type=click.Path(exists=True),
    help="JSON manifest of opportunities (skip analysis)",
)
@click.option(
    "--playbook",
    "-p",
    "playbook_path",
    type=click.Path(exists=True),
    help="Path to playbook directory (default: built-in playbook/)",
)
@click.option(
    "--patterns",
    "pattern_ids",
    default=None,
    help="Comma-separated pattern IDs to run (e.g. unused_file,n_plus_one)",
)
@click.option(
    "--max-workers",
    "max_workers",
    default=4,
    type=int,
    help="Max parallel scanner processes (default: 4)",
)
@click.option("--dry-run", is_flag=True, help="Don't push or create PRs")
@click.option("--no-push", is_flag=True, help="Don't push branches to remote")
@click.option("--no-pr", is_flag=True, help="Don't create GitHub PRs (still pushes)")
@click.option(
    "--model", default="claude-opus-4-6", help="Claude model for fixes (default: claude-opus-4-6)"
)
@click.option(
    "--reviewer-model",
    default="claude-opus-4-6",
    help="Claude model for reviews (default: claude-opus-4-6)",
)
@click.option("--base-branch", default="main", help="Base branch (default: main)")
@click.option("--branch-prefix", default="autopr/", help="Branch prefix (default: autopr/)")
@click.option("--no-screen", is_flag=True, help="Skip the pre-fix screener")
@click.option(
    "--screener-model", default="", help="Claude model for screener (default: same as model)"
)
@click.option("--no-mat-rev", is_flag=True, help="Skip material-risk review step")
@click.option(
    "--review-model",
    default="",
    help="Review model (default: codex; set to claude-opus-4-6 for Claude)",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def run(
    repo_path: str,
    config_path: str | None,
    manifest_path: str | None,
    playbook_path: str | None,
    pattern_ids: str | None,
    max_workers: int,
    dry_run: bool,
    no_push: bool,
    no_pr: bool,
    model: str,
    reviewer_model: str,
    base_branch: str,
    branch_prefix: str,
    no_screen: bool,
    screener_model: str,
    no_mat_rev: bool,
    review_model: str,
    verbose: bool,
):
    """Scan a repo for new opportunities and fix them autonomously.

    By default, Claude scans the repo using every playbook pattern in
    parallel. Opportunities that already have an open/merged PR are
    skipped automatically.

    REPO_PATH is the path to the git repository.

    Examples:

        # Default: scan all patterns, fix, and create PRs
        autopr run /path/to/repo

        # Only scan for unused files and dead code
        autopr run /path/to/repo --patterns unused_file,dead_code

        # Dry run (scan + fix locally, no push, no PRs)
        autopr run /path/to/repo --dry-run

        # Override with a hand-written manifest (skip scanning)
        autopr run /path/to/repo --manifest opportunities.json
    """
    # Build config
    resolved_config = config_path or _find_config()
    if resolved_config:
        config = AutoPRConfig.from_yaml(Path(resolved_config))
        config.repo.path = Path(repo_path)
    else:
        config = AutoPRConfig(
            repo=RepoConfig(
                path=Path(repo_path),
                base_branch=base_branch,
                branch_prefix=branch_prefix,
            ),
        )

    # Apply CLI overrides
    config.dry_run = dry_run
    config.push_to_remote = not no_push and not dry_run
    config.create_github_pr = not no_pr and not dry_run
    config.model = model
    config.reviewer_model = reviewer_model
    config.verbose = verbose
    if no_screen:
        config.gating.enable_screener = False
    if screener_model:
        config.gating.screener_model = screener_model
    if no_mat_rev:
        config.gating.enable_material_review = False
    if review_model:
        config.gating.review_model = review_model

    # Detect remote URL if not set
    if not config.repo.remote_url:
        import subprocess

        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(config.repo.path),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            config.repo.remote_url = result.stdout.strip()

    manifest = Path(manifest_path) if manifest_path else None
    playbook = Path(playbook_path) if playbook_path else None
    patterns_list = (
        [p.strip() for p in pattern_ids.split(",")] if pattern_ids else config.patterns or None
    )
    mode = "manifest" if manifest else "playbook scan"

    console.print("[bold]autopr[/bold] v0.1.0")
    console.print(f"  Repo:     {config.repo.path}")
    console.print(f"  Base:     {config.repo.base_branch}")
    console.print(f"  Mode:     {mode}")
    if playbook:
        console.print(f"  Playbook: {playbook}")
    if patterns_list:
        console.print(f"  Patterns: {', '.join(patterns_list)}")
    console.print(f"  Workers:  {max_workers}")
    console.print(f"  Model:    {config.model} (reviewer: {config.reviewer_model})")
    console.print(f"  Dry run:  {config.dry_run}")
    console.print()

    # Run pipeline
    report = run_pipeline(
        config,
        manifest_path=manifest,
        playbook_dir=playbook,
        pattern_ids=patterns_list,
        max_workers=max_workers,
    )

    # Write report to autopr's own directory, not the target repo
    report_dir = Path(__file__).resolve().parent.parent / ".autopr-reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / "run-report.json"
    with open(report_path, "w") as f:
        json.dump(report.model_dump(mode="json"), f, indent=2, default=str)
    console.print(f"\n[dim]Report saved to {report_path}[/dim]")

    # Exit code
    if report.failed:
        sys.exit(1)


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--model", default="claude-opus-4-6", help="Claude model (default: claude-opus-4-6)")
@click.option("--include", "-i", multiple=True, help="Paths to include in analysis")
@click.option(
    "--playbook",
    "-p",
    "playbook_path",
    type=click.Path(exists=True),
    help="Path to playbook directory",
)
@click.option(
    "--patterns",
    "pattern_ids",
    default=None,
    help="Comma-separated pattern IDs to run (e.g. unused_file,n_plus_one)",
)
@click.option(
    "--max-workers",
    "max_workers",
    default=4,
    type=int,
    help="Max parallel scanner processes (default: 4)",
)
@click.option("--output", "-o", "output_path", default="opportunities.json", help="Output file")
def scan(
    repo_path: str,
    model: str,
    include: tuple[str, ...],
    playbook_path: str | None,
    pattern_ids: str | None,
    max_workers: int,
    output_path: str,
):
    """Scan a repo for opportunities without fixing anything.

    Runs each playbook pattern in parallel against the repo and outputs
    a JSON manifest that can be fed to `autopr run --manifest`.

    Examples:

        # Scan and save opportunities
        autopr scan /path/to/repo -o opportunities.json

        # Only scan for unused files
        autopr scan /path/to/repo --patterns unused_file

        # Scan specific paths with custom playbook
        autopr scan /path/to/repo -i backend/src -p ./my-playbook/
    """
    from autopr.analyzer import analyze_repo

    repo_config = RepoConfig(
        path=Path(repo_path),
        include_paths=list(include),
    )

    playbook = Path(playbook_path) if playbook_path else None
    patterns_list = [p.strip() for p in pattern_ids.split(",")] if pattern_ids else None
    opportunities = analyze_repo(
        repo_config,
        model=model,
        playbook_dir=playbook,
        pattern_ids=patterns_list,
        max_workers=max_workers,
    )

    output = Path(output_path)
    with open(output, "w") as f:
        json.dump([o.model_dump(mode="json") for o in opportunities], f, indent=2)

    console.print(f"\n[green]Saved {len(opportunities)} opportunities to {output}[/green]")


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option(
    "--pr", "pr_number", type=int, default=None, help="PR number to review (fetches from GitHub)"
)
@click.option("--base-branch", default="main", help="Base branch (default: main)")
@click.option("--model", default="claude-opus-4-6", help="Claude model (default: claude-opus-4-6)")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True),
    help="YAML config file (for gating rules)",
)
@click.option("--update-pr", is_flag=True, help="Update PR body with new AI Review section")
@click.option(
    "--max-fix-attempts",
    default=3,
    type=int,
    help="Max fix attempts for failing dimensions (default: 3)",
)
@click.option(
    "--review-model",
    default="",
    help="Review model for material-risk review (default: codex; set to claude-opus-4-6 for Claude)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show raw reviewer output")
def review(
    repo_path: str,
    pr_number: int | None,
    base_branch: str,
    model: str,
    config_path: str | None,
    update_pr: bool,
    max_fix_attempts: int,
    review_model: str,
    verbose: bool,
):
    """Review changes and auto-fix failing dimensions.

    Runs a structured 6-dimension review. If any dimensions fail, automatically
    fixes the issues, commits, pushes, and re-reviews — looping until all pass
    or max attempts are exhausted. If a fix goes out of scope, changes are
    discarded.

    Without --pr, reviews the current branch against the base branch.
    With --pr, fetches the PR from GitHub and reviews its changes.

    Examples:

        autopr review /path/to/repo --pr 42
        autopr review /path/to/repo --pr 42 --update-pr
    """
    from autopr.config import AutoPRConfig
    from autopr.fixer import (
        commit_changes,
        ensure_worktree_clean,
    )
    from autopr.models import DimensionVerdict, Opportunity, Severity
    from autopr.pr_creator import (
        create_pr_description,
        parse_opportunity_from_pr_body,
    )
    from autopr.research import list_changed_files
    from autopr.reviewer import (
        build_review_fix_prompt,
        check_fix_in_scope,
        run_fixer_agent,
        structured_review_fix,
    )

    # Load gating rules from config
    resolved_config = config_path or _find_config()
    verdict_rules = None
    if resolved_config:
        cfg = AutoPRConfig.from_yaml(Path(resolved_config))
        verdict_rules = cfg.gating.verdict_rules
        if not review_model and cfg.gating.review_model:
            review_model = cfg.gating.review_model

    repo = Path(repo_path)

    if pr_number is not None:
        # Fetch PR data via gh CLI
        gh_result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "body,headRefName,title"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        if gh_result.returncode != 0:
            console.print(
                f"[bold red]Error:[/bold red] Failed to fetch PR #{pr_number}: {gh_result.stderr.strip()}"
            )
            raise SystemExit(1)

        pr_data = json.loads(gh_result.stdout)
        pr_body = pr_data.get("body", "")
        pr_branch = pr_data.get("headRefName", "")
        pr_title = pr_data.get("title", f"PR #{pr_number}")

        opp = parse_opportunity_from_pr_body(pr_body, pr_number=pr_number, title=pr_title)

        console.print(f"\n[bold]autopr review[/bold] — PR #{pr_number}")
        console.print(f"  Title:    {pr_title}")
        console.print(f"  Branch:   [cyan]{pr_branch}[/cyan]")
        console.print(f"  Base:     {base_branch}")
        console.print(f"  Model:    {model}")
        console.print(f"  Files:    {', '.join(opp.files) or '(parsed from PR body)'}")
        console.print(f"  Problem:  {opp.problem[:120]}{'...' if len(opp.problem) > 120 else ''}")
        console.print(f"  Solution: {opp.solution[:120]}{'...' if len(opp.solution) > 120 else ''}")

        # Fetch the branch and create a temporary worktree (avoids touching main checkout)
        console.print(f"\n  Fetching branch [cyan]{pr_branch}[/cyan]...")
        subprocess.run(
            ["git", "fetch", "origin", pr_branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        import tempfile

        worktree_dir = tempfile.mkdtemp(prefix=f"autopr-review-{pr_number}-")
        subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_dir, f"origin/{pr_branch}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        review_path = Path(worktree_dir)

        # Show diff stats
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", f"origin/{base_branch}...HEAD"],
            cwd=str(review_path),
            capture_output=True,
            text=True,
        )
        if diff_stat.stdout.strip():
            console.print("\n[bold]Diff summary:[/bold]")
            for line in diff_stat.stdout.strip().splitlines():
                console.print(f"  {line.strip()}")
    else:
        opp = Opportunity(
            id="manual-review",
            title="Manual review",
            type="manual",
            severity=Severity.MEDIUM,
            files=["(current branch changes)"],
            problem="Review requested",
            solution="See diff",
        )
        review_path = repo
        worktree_dir = None

    try:
        pr_branch = pr_branch if pr_number else None

        # Mechanically revert protected files before the reviewer ever sees
        # the diff — don't rely on the LLM to catch build-infra changes.
        if pr_branch:
            console.print("\n[bold]Protected files check:[/bold]")
            from autopr.config import DEFAULT_PROTECTED_FILES

            _revert_protected_to_base(review_path, base_branch, DEFAULT_PROTECTED_FILES)
            if commit_changes(
                review_path,
                "Revert protected file changes",
                protected_files=DEFAULT_PROTECTED_FILES,
            ):
                console.print("  [green]Reverted protected files and committed[/green]")
                ensure_worktree_clean(review_path, context="review protected-file revert")
                _push = subprocess.run(
                    ["git", "push", "origin", f"HEAD:refs/heads/{pr_branch}"],
                    cwd=str(review_path),
                    capture_output=True,
                    text=True,
                )
                if _push.returncode == 0:
                    console.print(f"  [green]Pushed revert to {pr_branch}[/green]")
                else:
                    console.print(
                        f"  [yellow]Could not push protected file revert: {_push.stderr.strip()}[/yellow]"
                    )
            else:
                console.print("  [dim]No protected files were modified — clean[/dim]")

        for attempt in range(max_fix_attempts + 1):
            console.rule(f"[bold]Review{f' (attempt {attempt + 1})' if attempt > 0 else ''}[/bold]")

            structured_result = structured_review_fix(
                opp,
                review_path,
                base_branch=base_branch,
                model=model,
                verdict_rules=verdict_rules,
            )

            # Print dimensions
            if structured_result.dimensions:
                console.print()
                for dim in structured_result.dimensions:
                    icon = (
                        "[green]PASS[/green]"
                        if dim.verdict.value == "pass"
                        else "[red]FAIL[/red]"
                        if dim.verdict.value == "fail"
                        else "[dim]SKIP[/dim]"
                    )
                    console.print(f"  {icon}  {dim.dimension}: {dim.finding}")

            failed = [d for d in structured_result.dimensions if d.verdict == DimensionVerdict.FAIL]
            if not structured_result.dimensions:
                # Reviewer returned no dimensions — treat as failure, not success
                console.print(f"\n[bold red]Review failed:[/bold red] {structured_result.summary}")
                if attempt == max_fix_attempts:
                    break
                console.print("[yellow]Retrying review...[/yellow]")
                continue
            if not failed:
                console.print("\n[bold green]All dimensions pass![/bold green]")
                console.print(f"[bold]Summary:[/bold] {structured_result.summary}")
                break

            console.print(f"\n[yellow]{len(failed)} dimension(s) failed[/yellow]")
            console.print(f"[bold]Summary:[/bold] {structured_result.summary}")

            if attempt == max_fix_attempts:
                console.print(f"[red]Exhausted {max_fix_attempts} fix attempts[/red]")
                break

            # Fix the failures
            console.print(
                f"\n[bold cyan]Fixing {len(failed)} issue(s) (attempt {attempt + 1}/{max_fix_attempts})...[/bold cyan]"
            )
            fix_prompt = build_review_fix_prompt(opp, failed, base_branch)
            run_fixer_agent(review_path, fix_prompt, model)

            # Revert protected files to the base branch version — not HEAD,
            # because the bad change may already be committed on this branch.
            from autopr.config import DEFAULT_PROTECTED_FILES

            _revert_protected_to_base(review_path, base_branch, DEFAULT_PROTECTED_FILES)

            # Check scope
            changed = list_changed_files(review_path)
            in_scope, scope_reason = check_fix_in_scope(changed, opp.files)
            if not in_scope:
                console.print(f"[bold red]Out of scope:[/bold red] {scope_reason}")
                console.print("[yellow]Discarding fix — retrying[/yellow]")
                subprocess.run(
                    ["git", "checkout", "--", "."],
                    cwd=str(review_path),
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=str(review_path),
                    capture_output=True,
                    text=True,
                )
                continue

            console.print(f"  [dim]Scope check:[/dim] {scope_reason}")

            # Commit and push
            commit_changes(
                review_path,
                f"Fix review findings (attempt {attempt + 1})",
                protected_files=DEFAULT_PROTECTED_FILES,
            )
            if pr_branch:
                ensure_worktree_clean(review_path, context=f"review fix attempt {attempt + 1}")
                console.print(f"  [dim]Pushing to {pr_branch}...[/dim]")
                subprocess.run(
                    ["git", "push", "origin", f"HEAD:refs/heads/{pr_branch}"],
                    cwd=str(review_path),
                    capture_output=True,
                    text=True,
                )

        # Update opportunity to reflect actual changes — use gh pr diff
        # which shows exactly what GitHub's "Files changed" tab shows,
        # not git diff which includes files from merged commits
        if pr_number is not None:
            actual_files_result = subprocess.run(
                ["gh", "pr", "diff", str(pr_number), "--name-only"],
                cwd=str(repo),
                capture_output=True,
                text=True,
            )
            if actual_files_result.returncode == 0:
                actual_files = [f for f in actual_files_result.stdout.strip().split("\n") if f]
                if actual_files:
                    opp.files = actual_files

        # Update PR description with final review
        if update_pr and pr_number is not None:
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(pr_number),
                    "--body",
                    create_pr_description(opp, structured_review=structured_result),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
            )
            console.print(f"\n[green]Updated PR #{pr_number}[/green]")
    finally:
        if worktree_dir:
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_dir],
                cwd=str(repo),
                capture_output=True,
                text=True,
            )


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--config", "-c", "config_path", type=click.Path(), help="Output config path")
@click.option(
    "--workflows/--no-workflows",
    default=True,
    help="Generate GitHub Actions workflow files (default: yes)",
)
def init(repo_path: str, config_path: str | None, workflows: bool):
    """Generate a default autopr.yaml config for a repository.

    Examples:

        autopr init /path/to/repo
        autopr init /path/to/repo -c my-config.yaml
        autopr init /path/to/repo --no-workflows
    """
    import subprocess

    repo = Path(repo_path)
    out = Path(config_path) if config_path else repo / "autopr.yaml"

    # Detect remote URL
    remote_url = ""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        remote_url = result.stdout.strip()

    # Detect health check commands from Makefile targets
    makefile = repo / "Makefile"
    commands = []
    if makefile.exists():
        import re as _re

        content = makefile.read_text()
        # Extract actual target names defined in the Makefile
        targets = set(_re.findall(r"^([A-Za-z_][\w-]*):", content, _re.MULTILINE))
        # Prefer generic check/test targets
        if "check" in targets:
            commands.append("make check")
        if "test" in targets:
            commands.append("make test")
        # Fall back to backend-* targets
        if not commands:
            if "backend-check-fix" in targets:
                commands.append("make backend-check-fix")
            if "backend-test" in targets:
                commands.append("make backend-test")

    if not commands:
        # Fallback: detect common patterns
        if (repo / "package.json").exists():
            commands.append("npm test")
        elif (repo / "pyproject.toml").exists():
            commands.append("pytest")

    config = AutoPRConfig(
        repo=RepoConfig(
            path=repo,
            remote_url=remote_url,
            health_checks=HealthCheckConfig(
                commands=commands or ["echo 'No health checks configured'"]
            ),
        ),
    )
    if not config.automation.tool_repo:
        config.automation.tool_repo = _detect_tool_repo()

    if workflows and not config.automation.tool_repo:
        raise click.ClickException(
            "Cannot generate workflows because automation.tool_repo could not be detected. "
            "Set it in autopr.yaml and rerun init."
        )

    config.to_yaml(out)
    console.print(f"[green]Config written to {out}[/green]")
    console.print("[dim]Edit the file to customize paths, health checks, and models.[/dim]")

    if workflows:
        _install_workflow_templates(repo, config)


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option(
    "--config", "-c", "config_path", type=click.Path(exists=True), help="YAML config file"
)
@click.option(
    "--program",
    "program_path",
    type=click.Path(exists=True),
    help="Override the research program markdown file",
)
@click.option(
    "--metrics",
    default=None,
    help="Comma-separated Lighthouse metrics to focus on (e.g. performance_score,lcp_ms)",
)
@click.option(
    "--pages", default=None, help="Comma-separated URL paths to test (e.g. /,/about,/dashboard)"
)
@click.option("--max-iterations", type=int, help="Override the research iteration limit")
@click.option("--branch-name", default="", help="Override the research branch name")
@click.option("--dry-run", is_flag=True, help="Do not push or create a GitHub PR")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def research(
    repo_path: str,
    config_path: str | None,
    program_path: str | None,
    metrics: str | None,
    pages: str | None,
    max_iterations: int | None,
    branch_name: str,
    dry_run: bool,
    verbose: bool,
):
    """Run iterative performance research against preview deployments.

    By default, optimizes the Lighthouse performance score on `/`. No
    program.md file is required — just point it at a repo with a deploy
    command configured.

    Examples:

        # Basic: optimize performance score
        autopr research /path/to/repo

        # Focus on specific metrics and pages
        autopr research /path/to/repo --metrics lcp_ms,cls --pages /,/dashboard

        # Use a custom research program
        autopr research /path/to/repo --program research-plan.md
    """
    resolved_config = config_path or _find_config()
    if resolved_config:
        config = AutoPRConfig.from_yaml(Path(resolved_config))
        config.repo.path = Path(repo_path)
    else:
        config = AutoPRConfig(
            repo=RepoConfig(path=Path(repo_path)),
        )

    config.dry_run = dry_run
    config.push_to_remote = not dry_run
    config.create_github_pr = not dry_run
    config.verbose = verbose

    if not config.repo.remote_url:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(config.repo.path),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            config.repo.remote_url = result.stdout.strip()

    # Apply --pages override
    if pages:
        config.research.target_pages = [p.strip() for p in pages.split(",")]

    # Apply --metrics override: first metric becomes primary, rest become guardrails
    if metrics:
        from autopr.config import ResearchGuardrailMetricConfig, ResearchPrimaryMetricConfig

        metric_list = [m.strip() for m in metrics.split(",")]
        config.research.scoring.primary_metric = ResearchPrimaryMetricConfig(
            name=metric_list[0],
            direction="minimize" if metric_list[0] != "performance_score" else "maximize",
            min_improvement_abs=1.0,
        )
        if len(metric_list) > 1:
            config.research.scoring.guardrails = [
                ResearchGuardrailMetricConfig(
                    name=m,
                    direction="minimize",
                    max_regression_pct=5.0,
                )
                for m in metric_list[1:]
            ]

    report = run_research(
        config,
        program_file=Path(program_path) if program_path else None,
        max_iterations=max_iterations,
        branch_name=branch_name or None,
    )

    report_dir = Path(__file__).resolve().parent.parent / ".autopr-reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / "research-report.json"
    console.print("[bold]autopr research[/bold]")
    console.print(f"  Repo:     {config.repo.path}")
    console.print(f"  Branch:   {report.branch_name}")
    console.print(f"  Status:   {report.status}")
    console.print(f"  Dry run:  {config.dry_run}")
    console.print(f"\n[dim]Report saved to {report_path}[/dim]")

    if report.status == "failed":
        raise SystemExit(1)


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option(
    "--event",
    required=True,
    type=click.Choice(
        ["ci_failed", "review_submitted", "review_comment", "pr_comment", "base_branch_updated"]
    ),
    help="Event type that triggered this reaction",
)
@click.option("--pr", "pr_number", type=int, required=True, help="PR number")
@click.option("--head-branch", required=True, help="PR head branch name")
@click.option("--head-sha", required=True, help="Current HEAD SHA of the PR branch")
@click.option("--base-branch", default="main", help="Base branch (default: main)")
@click.option("--created-at", default="", help="ISO timestamp of the triggering comment")
@click.option("--check-name", default="", help="Name of the failed CI check")
@click.option("--check-conclusion", default="", help="Conclusion of the CI check")
@click.option("--comment-id", type=int, default=None, help="ID of the triggering comment")
@click.option("--review-id", type=int, default=None, help="ID of the triggering review")
@click.option("--comment-body", default="", help="Body of the triggering comment")
@click.option("--comment-author", default="", help="Author of the triggering comment")
@click.option("--review-state", default="", help="State of the review (e.g. changes_requested)")
@click.option("--model", default="claude-opus-4-6", help="Claude model (default: claude-opus-4-6)")
@click.option(
    "--config", "-c", "config_path", type=click.Path(exists=True), help="YAML config file"
)
@click.option("--dry-run", is_flag=True, help="Don't push or modify anything")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def react(
    repo_path: str,
    event: str,
    pr_number: int,
    head_branch: str,
    head_sha: str,
    base_branch: str,
    created_at: str,
    check_name: str,
    check_conclusion: str,
    comment_id: int | None,
    review_id: int | None,
    comment_body: str,
    comment_author: str,
    review_state: str,
    model: str,
    config_path: str | None,
    dry_run: bool,
    verbose: bool,
):
    """React to a PR event (CI failure, review comment, etc.).

    Dispatches to the appropriate handler based on --event. Designed to be
    called from GitHub Actions workflows.

    Examples:

        autopr react /path/to/repo --event ci_failed --pr 42 \\
            --head-branch autopr/fix-batch --head-sha abc123 \\
            --check-name "make test"

        autopr react /path/to/repo --event review_submitted --pr 42 \\
            --head-branch autopr/fix-batch --head-sha abc123 \\
            --comment-body "Please add error handling"
    """
    from autopr.reactor import dispatch_event

    resolved_config = config_path or _find_config()
    if resolved_config:
        config = AutoPRConfig.from_yaml(Path(resolved_config))
        config.repo.path = Path(repo_path)
    else:
        config = AutoPRConfig(
            repo=RepoConfig(path=Path(repo_path), base_branch=base_branch),
        )

    config.model = model
    config.verbose = verbose

    result = dispatch_event(
        event=event,
        repo_path=Path(repo_path),
        pr_number=pr_number,
        pr_branch=head_branch,
        model=model,
        config=config,
        head_sha=head_sha,
        check_name=check_name,
        comment_body=comment_body,
        comment_id=comment_id,
        review_id=review_id,
        comment_author=comment_author,
        created_at=created_at,
        base_branch=base_branch,
        dry_run=dry_run,
    )

    console.print(f"\n[bold]Result:[/bold] {result.action}")
    if result.summary:
        console.print(f"  {result.summary}")

    if result.action in ("needs_human", "needs_input"):
        sys.exit(1)


@main.command(name="reconcile-open-prs")
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--base-branch", default="main", help="Base branch (default: main)")
@click.option("--model", default="claude-opus-4-6", help="Claude model (default: claude-opus-4-6)")
@click.option(
    "--config", "-c", "config_path", type=click.Path(exists=True), help="YAML config file"
)
@click.option("--dry-run", is_flag=True, help="Don't push or modify anything")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def reconcile_open_prs(
    repo_path: str,
    base_branch: str,
    model: str,
    config_path: str | None,
    dry_run: bool,
    verbose: bool,
):
    """Reconcile all open managed PRs against the base branch.

    Finds all open PRs with autopr/ branch prefixes or autopr-managed labels,
    rebases those that are behind, and resolves merge conflicts.

    Examples:

        autopr reconcile-open-prs /path/to/repo
        autopr reconcile-open-prs /path/to/repo --base-branch develop --dry-run
    """
    from autopr.reconciler import reconcile_open_prs as _reconcile

    resolved_config = config_path or _find_config()
    if resolved_config:
        config = AutoPRConfig.from_yaml(Path(resolved_config))
        config.repo.path = Path(repo_path)
    else:
        config = AutoPRConfig(
            repo=RepoConfig(path=Path(repo_path), base_branch=base_branch),
        )

    config.model = model
    config.verbose = verbose

    result = _reconcile(
        repo_path=Path(repo_path),
        config=config,
        base_branch=base_branch,
        model=model,
        dry_run=dry_run,
    )

    if result.needs_human > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
