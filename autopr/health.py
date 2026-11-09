"""Health check runner — runs configured health checks and attempts to fix failures."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from rich.console import Console

from autopr.config import HealthCheckConfig
from autopr.fixer import restore_protected_files
from autopr.models import HealthCheckResult

console = Console()

FIX_PROMPT_TEMPLATE = (
    "The following health check command failed:\n\n"
    "Command: {command}\n"
    "Exit code: {exit_code}\n"
    "Stderr:\n{stderr}\n\n"
    "Stdout (last 100 lines):\n{stdout_tail}\n\n"
    "Fix the issues. Do NOT add type: ignore, noqa, eslint-disable, or underscore "
    "prefix workarounds. Do NOT modify Makefile, pyproject.toml, setup.cfg, "
    "package.json, tsconfig.json, or other build/test configuration files. "
    "Fix the actual source code. Only modify files that are relevant "
    "to the errors shown above."
)


def run_single_check(
    command: str,
    repo_path: Path,
    timeout: int = 300,
    attempt: int = 1,
) -> HealthCheckResult:
    """Run a single health check command."""
    console.print(f"[dim]  Running: {command} (attempt {attempt})...[/dim]")
    start = time.time()

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.time() - start

        passed = result.returncode == 0
        if passed:
            console.print(f"[green]  ✓ {command} passed ({duration:.1f}s)[/green]")
        else:
            console.print(f"[red]  ✗ {command} failed ({duration:.1f}s)[/red]")

        return HealthCheckResult(
            passed=passed,
            command=command,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=duration,
            attempt=attempt,
        )
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        console.print(f"[red]  ✗ {command} timed out after {timeout}s[/red]")
        return HealthCheckResult(
            passed=False,
            command=command,
            stderr=f"Timed out after {timeout} seconds",
            duration_seconds=duration,
            attempt=attempt,
        )


def attempt_auto_fix(
    failed_check: HealthCheckResult,
    repo_path: Path,
    model: str = "claude-opus-4-6",
) -> bool:
    """Use Claude Code to fix a failed health check.

    Returns True if the fix agent exited successfully, False otherwise.
    """
    stdout_lines = failed_check.stdout.split("\n")
    stdout_tail = "\n".join(stdout_lines[-100:])

    prompt = FIX_PROMPT_TEMPLATE.format(
        command=failed_check.command,
        exit_code="non-zero",
        stderr=failed_check.stderr[-2000:] if failed_check.stderr else "(empty)",
        stdout_tail=stdout_tail[-2000:],
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

    console.print("[dim]  Running Claude Code to fix health check failure...[/dim]")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        console.print("[yellow]  Auto-fix agent timed out[/yellow]")
        return False

    if result.returncode != 0:
        console.print(f"[yellow]  Auto-fix agent failed (exit {result.returncode})[/yellow]")
        return False

    return True


def run_health_checks(
    config: HealthCheckConfig,
    repo_path: Path,
    model: str = "claude-opus-4-6",
    protected_files: list[str] | None = None,
) -> list[HealthCheckResult]:
    """Run all health checks, attempting auto-fixes on failure.

    Returns the list of final results (one per command).
    """
    console.print("\n[bold yellow]Running health checks...[/bold yellow]")
    all_results: list[HealthCheckResult] = []

    for command in config.commands:
        result = run_single_check(command, repo_path, config.timeout_seconds, attempt=1)

        # If failed, try to auto-fix up to max_fix_attempts times
        attempt = 1
        while not result.passed and attempt < config.max_fix_attempts:
            attempt += 1
            console.print(
                f"[yellow]  Attempting auto-fix (attempt {attempt}/{config.max_fix_attempts})...[/yellow]"
            )
            # Snapshot so we can rollback if the fix doesn't help
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
            ).stdout.strip()

            fix_succeeded = attempt_auto_fix(result, repo_path, model=model)
            if not fix_succeeded:
                console.print("[yellow]  Auto-fix failed — skipping remaining retries[/yellow]")
                break
            # Revert any changes to protected files before re-checking
            if protected_files:
                restore_protected_files(repo_path, protected_files)
            result = run_single_check(
                command,
                repo_path,
                config.timeout_seconds,
                attempt=attempt,
            )

            # If still failing, rollback this attempt so next try starts clean
            if not result.passed:
                subprocess.run(
                    ["git", "checkout", "--", "."],
                    cwd=str(repo_path),
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=str(repo_path),
                    capture_output=True,
                )
                console.print("[dim]  Rolled back failed fix attempt[/dim]")

        all_results.append(result)

        # If a check still fails after retries, stop early
        if not result.passed:
            console.print(
                f"[bold red]Health check '{command}' failed after "
                f"{config.max_fix_attempts} attempts — aborting[/bold red]"
            )
            break

    return all_results
