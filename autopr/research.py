"""Iterative research mode for performance optimization against preview deploys.

This module provides the performance-specific orchestration layer. It uses:
- autopr/diagnose.py for Lighthouse-based opportunity discovery
- autopr/verify.py for post-fix performance verification
- autopr/orchestrator.py's prepare_candidate/finalize_candidate for the shared fix spine
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import textwrap
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rich.console import Console

from autopr.config import (
    AutoPRConfig,
    ResearchConfig,
    ResearchGuardrailMetricConfig,
    ResearchLighthouseConfig,
    ResearchScoringConfig,
)
from autopr.fixer import (
    remove_worktree,
)
from autopr.models import (
    ExperimentRecord,
    MetricSnapshot,
    Opportunity,
    ResearchReport,
    Severity,
)

console = Console()


# ---------------------------------------------------------------------------
# Shared utility functions (used by research, diagnose, and verify)
# ---------------------------------------------------------------------------


def sanitize_branch_name(branch_name: str) -> str:
    """Sanitize a branch name for use as an ID."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", branch_name)


def artifacts_dir_for_run(repo_path: Path, branch_name: str) -> Path:
    """Return the artifacts directory for a research run."""
    return repo_path / ".autopr-research" / sanitize_branch_name(branch_name)


def write_artifact(path: Path, content: str) -> None:
    """Write an artifact file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def parse_json_object(text: str) -> dict:
    """Parse a JSON object from raw command output."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("Expected JSON output, got empty text")

    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    for start_idx, char in enumerate(stripped):
        if char != "{":
            continue
        depth = 0
        for end_idx in range(start_idx, len(stripped)):
            if stripped[end_idx] == "{":
                depth += 1
            elif stripped[end_idx] == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start_idx : end_idx + 1]
                    data = json.loads(candidate)
                    if isinstance(data, dict):
                        return data
                    break

    raise ValueError("Could not parse JSON object from command output")


def normalize_lighthouse_metrics(payload: dict) -> dict[str, float]:
    """Map Lighthouse JSON to the fixed normalized metric names used by research mode."""
    metrics: dict[str, float] = {}

    performance_score = payload.get("categories", {}).get("performance", {}).get("score")
    if performance_score is not None:
        metrics["performance_score"] = float(performance_score) * 100.0

    audits = payload.get("audits", {})
    audit_map = {
        "lcp_ms": "largest-contentful-paint",
        "fcp_ms": "first-contentful-paint",
        "speed_index_ms": "speed-index",
        "ttfb_ms": "server-response-time",
    }
    for metric_name, audit_name in audit_map.items():
        numeric_value = audits.get(audit_name, {}).get("numericValue")
        if numeric_value is not None:
            metrics[metric_name] = float(numeric_value)

    return metrics


def aggregate_metric_runs(
    metric_runs: list[dict[str, float]],
    *,
    source_url: str = "",
    aggregation: str = "median",
) -> MetricSnapshot:
    """Aggregate repeated benchmark runs into a single metric snapshot."""
    aggregated: dict[str, float] = {}
    keys = {key for run in metric_runs for key in run}
    for key in sorted(keys):
        values = [run[key] for run in metric_runs if key in run]
        if values:
            aggregated[key] = float(statistics.median(values))

    return MetricSnapshot(
        metrics=aggregated,
        sample_count=len(metric_runs),
        aggregation=aggregation,
        source_url=source_url,
    )


def metric_improvement(candidate: float, baseline: float, direction: str) -> float:
    """Return positive numbers when the candidate is better than baseline."""
    if direction == "maximize":
        return candidate - baseline
    return baseline - candidate


def evaluate_candidate_metrics(
    candidate: MetricSnapshot,
    best: MetricSnapshot,
    scoring: ResearchScoringConfig,
) -> tuple[bool, str]:
    """Decide whether a candidate should be accepted."""
    primary = scoring.primary_metric
    candidate_value = candidate.value(primary.name)
    best_value = best.value(primary.name)
    if candidate_value is None or best_value is None:
        return False, f"Missing primary metric '{primary.name}'"

    primary_gain = metric_improvement(candidate_value, best_value, primary.direction)
    if primary_gain <= 0:
        return False, f"{primary.name} did not improve"
    if primary_gain < primary.min_improvement_abs:
        return False, (
            f"{primary.name} improved by {primary_gain:.3f}, below required "
            f"{primary.min_improvement_abs:.3f}"
        )

    for guardrail in scoring.guardrails:
        violation = check_guardrail_violation(candidate, best, guardrail)
        if violation:
            return False, violation

    return True, f"{primary.name} improved by {primary_gain:.3f}"


def check_guardrail_violation(
    candidate: MetricSnapshot,
    best: MetricSnapshot,
    guardrail: ResearchGuardrailMetricConfig,
) -> str:
    """Return a human-readable violation message when a guardrail regresses too far."""
    candidate_value = candidate.value(guardrail.name)
    best_value = best.value(guardrail.name)
    if candidate_value is None or best_value is None:
        return ""

    regression = -metric_improvement(candidate_value, best_value, guardrail.direction)
    if regression <= 0:
        return ""

    if guardrail.max_regression_abs is not None and regression > guardrail.max_regression_abs:
        return (
            f"{guardrail.name} regressed by {regression:.3f}, above allowed "
            f"{guardrail.max_regression_abs:.3f}"
        )

    if guardrail.max_regression_pct is not None:
        if best_value == 0:
            return f"{guardrail.name} regressed from a zero baseline"
        regression_pct = (regression / abs(best_value)) * 100.0
        if regression_pct > guardrail.max_regression_pct:
            return (
                f"{guardrail.name} regressed by {regression_pct:.2f}%, above allowed "
                f"{guardrail.max_regression_pct:.2f}%"
            )

    return ""


def format_metrics(snapshot: MetricSnapshot | None) -> str:
    """Return a compact markdown-ish rendering of normalized metrics."""
    if snapshot is None or not snapshot.metrics:
        return "- No metrics captured yet"
    return "\n".join(f"- {name}: {value:.3f}" for name, value in sorted(snapshot.metrics.items()))


def format_history(records: list[ExperimentRecord], limit: int) -> str:
    """Format recent experiment history for the agent prompt."""
    if not records:
        return "No experiments yet."

    recent = records[-limit:]
    lines = []
    for record in recent:
        status_line = f"- Iteration {record.iteration}: {record.status}"
        if record.rejection_reason:
            status_line += f" — {record.rejection_reason}"
        lines.append(status_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent, deploy, and benchmark helpers
# ---------------------------------------------------------------------------

RESEARCH_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are an expert performance engineer working in an iterative keep-or-discard loop.

    ## Program
    {program}

    ## Goal
    Improve deployed performance metrics for this repository.

    ## Current Best Metrics
    {best_metrics}

    ## Recent Experiment History
    {history}

    ## Constraints
    - Only make focused performance improvements.
    - Do not modify protected config/build files.
    - {editable_paths_instruction}
    - Prefer small, testable changes over broad rewrites.
    - **CRITICAL: Your changes MUST compile.** Before finishing, run the build
      command to verify. If prior iterations failed with build errors (see
      history above), do NOT repeat the same mistake — try a different approach.
    - Review your diff before finishing.

    Make one candidate improvement now.
""")


def render_research_prompt(
    research_config: ResearchConfig,
    program_text: str,
    best_metrics: MetricSnapshot | None,
    experiments: list[ExperimentRecord],
) -> str:
    """Build the prompt for a single candidate iteration."""
    editable_paths_instruction = (
        "Only edit these paths: " + ", ".join(research_config.editable_paths)
        if research_config.editable_paths
        else "You may edit any source files needed for a focused performance improvement"
    )
    return RESEARCH_PROMPT_TEMPLATE.format(
        program=program_text.strip(),
        best_metrics=format_metrics(best_metrics),
        history=format_history(experiments, research_config.history_window),
        editable_paths_instruction=editable_paths_instruction,
    )


def run_research_agent(worktree_path: Path, prompt: str, model: str) -> None:
    """Run Claude Code to propose one candidate improvement."""
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
    result = subprocess.run(
        cmd,
        cwd=str(worktree_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Research agent failed: {result.stderr.strip()}")


def list_changed_files(worktree_path: Path) -> list[str]:
    """Return tracked and untracked files currently changed in the worktree."""
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    files = []
    for line in (changed.stdout + "\n" + untracked.stdout).strip().split("\n"):
        f = line.strip()
        if f:
            files.append(f)
    return files


def path_matches_any(file_path: str, allowed_paths: list[str]) -> bool:
    """Check if file_path matches any of the allowed path prefixes."""
    import fnmatch

    for pattern in allowed_paths:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        if file_path.startswith(pattern.rstrip("/")):
            return True
    return False


def restore_disallowed_edits(worktree_path: Path, editable_paths: list[str]) -> None:
    """Revert any files changed outside the allowed editable paths."""
    if not editable_paths:
        return

    changed = list_changed_files(worktree_path)
    disallowed = [f for f in changed if not path_matches_any(f, editable_paths)]

    if not disallowed:
        return

    for f in disallowed:
        file_path = worktree_path / f
        if file_path.exists():
            subprocess.run(
                ["git", "checkout", "HEAD", "--", f],
                cwd=str(worktree_path),
                capture_output=True,
            )
        else:
            file_path.unlink(missing_ok=True)
        console.print(f"[dim]  Disallowed edit reverted: {f}[/dim]")


def get_head_sha(worktree_path: Path) -> str:
    """Return the current HEAD SHA in the worktree."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def restore_best_state(worktree_path: Path, commit_sha: str) -> None:
    """Reset the worktree to a known-good state."""
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=str(worktree_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=str(worktree_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "reset", "--hard", commit_sha],
        cwd=str(worktree_path),
        capture_output=True,
        check=True,
    )


def wait_for_url_ready(url: str, timeout_seconds: int) -> None:
    """Wait until an HTTP endpoint responds."""
    deadline = time.time() + timeout_seconds
    request = Request(url, headers={"User-Agent": "autopr-research/0.1"})
    while time.time() < deadline:
        try:
            with urlopen(request, timeout=10):
                return
        except HTTPError as exc:
            if exc.code < 500:
                return
        except URLError:
            pass
        time.sleep(5)

    raise TimeoutError(f"Timed out waiting for deploy URL to become ready: {url}")


def _copy_deploy_link(repo_path: Path, worktree_path: Path) -> None:
    """Copy .vercel/ project link into the worktree if it exists."""
    import shutil

    vercel_dir = repo_path / ".vercel"
    wt_vercel_dir = worktree_path / ".vercel"
    if vercel_dir.is_dir() and not wt_vercel_dir.exists():
        shutil.copytree(vercel_dir, wt_vercel_dir)


def deploy_candidate(
    worktree_path: Path,
    research_config: ResearchConfig,
    artifact_dir: Path,
    label: str,
    repo_path: Path | None = None,
) -> str:
    """Run the configured deploy command and return the preview URL."""
    if repo_path:
        _copy_deploy_link(repo_path, worktree_path)
    result = subprocess.run(
        research_config.deploy.command,
        shell=True,
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        timeout=research_config.deploy.ready_timeout_seconds + 120,
    )
    write_artifact(
        artifact_dir / f"{label}-deploy.stdout.log",
        result.stdout,
    )
    write_artifact(
        artifact_dir / f"{label}-deploy.stderr.log",
        result.stderr,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Deploy command failed with exit {result.returncode}: {result.stderr.strip()}"
        )

    combined_output = f"{result.stdout}\n{result.stderr}"
    match = re.search(research_config.deploy.url_regex, combined_output)
    if not match:
        raise RuntimeError("Could not extract preview URL from deploy output")

    url = match.group(0).rstrip(")")
    wait_for_url_ready(url, research_config.deploy.ready_timeout_seconds)
    return url


def refresh_auth_cookie(lighthouse_config: ResearchLighthouseConfig) -> None:
    """Auto-login via Supabase and set auth_cookie from configured credentials."""
    if lighthouse_config.auth_cookie:
        return
    if not all(
        [
            lighthouse_config.auth_email,
            lighthouse_config.auth_password,
            lighthouse_config.auth_supabase_url,
            lighthouse_config.auth_supabase_key,
        ]
    ):
        return

    console.print("[dim]  Logging in for authenticated benchmarking...[/dim]")
    try:
        req = Request(
            f"{lighthouse_config.auth_supabase_url}/auth/v1/token?grant_type=password",
            data=json.dumps(
                {
                    "email": lighthouse_config.auth_email,
                    "password": lighthouse_config.auth_password,
                }
            ).encode(),
            headers={
                "apikey": lighthouse_config.auth_supabase_key,
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        access_token = data.get("access_token", "")
        if not access_token:
            console.print("[yellow]  Auth failed — no access_token in response[/yellow]")
            return

        project_ref = lighthouse_config.auth_supabase_url.replace("https://", "").split(".")[0]
        lighthouse_config.auth_cookie = f"sb-{project_ref}-auth-token=base64-{access_token}"
        console.print("[green]  Authenticated successfully[/green]")
    except Exception as exc:
        console.print(f"[yellow]  Auth failed: {exc}[/yellow]")


def run_lighthouse_once(
    worktree_path: Path,
    lighthouse_config: ResearchLighthouseConfig,
    url: str,
    artifact_path: Path,
) -> dict[str, float]:
    """Run Lighthouse once and return normalized metrics."""
    command = lighthouse_config.command.format(url=url)
    if lighthouse_config.auth_cookie:
        cookie_json = json.dumps({"Cookie": lighthouse_config.auth_cookie})
        command += f" --extra-headers '{cookie_json}'"
    result = subprocess.run(
        command,
        shell=True,
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        timeout=900,
    )
    write_artifact(artifact_path.with_suffix(".stdout.json"), result.stdout)
    write_artifact(artifact_path.with_suffix(".stderr.log"), result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"Lighthouse command failed with exit {result.returncode}: {result.stderr.strip()}"
        )

    payload = parse_json_object(result.stdout)
    return normalize_lighthouse_metrics(payload)


def run_ttft_probe_once(
    worktree_path: Path,
    command_template: str,
    url: str,
    artifact_path: Path,
) -> dict[str, float]:
    """Run the optional TTFT probe and return its normalized metric."""
    result = subprocess.run(
        command_template.format(url=url),
        shell=True,
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        timeout=300,
    )
    write_artifact(artifact_path.with_suffix(".stdout.json"), result.stdout)
    write_artifact(artifact_path.with_suffix(".stderr.log"), result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"TTFT probe failed with exit {result.returncode}: {result.stderr.strip()}"
        )

    payload = parse_json_object(result.stdout)
    ttft_ms = payload.get("ttft_ms")
    if ttft_ms is None:
        raise RuntimeError("TTFT probe output is missing 'ttft_ms'")
    return {"ttft_ms": float(ttft_ms)}


def benchmark_preview_url(
    worktree_path: Path,
    research_config: ResearchConfig,
    url: str,
    artifact_dir: Path,
    label: str,
) -> MetricSnapshot:
    """Run warmup and measured benchmark passes against a deployed preview URL."""
    runs: list[dict[str, float]] = []
    total_runs = research_config.lighthouse.warmup_runs + research_config.lighthouse.measured_runs
    for index in range(total_runs):
        run_label = f"{label}-run-{index + 1}"
        run_metrics = run_lighthouse_once(
            worktree_path,
            research_config.lighthouse,
            url,
            artifact_dir / run_label,
        )
        if research_config.ttft_probe.command:
            run_metrics.update(
                run_ttft_probe_once(
                    worktree_path,
                    research_config.ttft_probe.command,
                    url,
                    artifact_dir / f"{run_label}-ttft",
                )
            )
        if index >= research_config.lighthouse.warmup_runs:
            runs.append(run_metrics)

    if not runs:
        raise RuntimeError("No measured Lighthouse runs were collected")

    return aggregate_metric_runs(runs, source_url=url)


def benchmark_all_pages(
    worktree_path: Path,
    research_config: ResearchConfig,
    base_url: str,
    artifact_dir: Path,
    label: str,
) -> MetricSnapshot:
    """Benchmark all target pages in parallel and return aggregated metrics."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pages = research_config.target_pages or ["/"]
    if len(pages) == 1 and pages[0] == "/":
        return benchmark_preview_url(
            worktree_path,
            research_config,
            base_url,
            artifact_dir,
            label,
        )

    base_url_stripped = base_url.rstrip("/")

    def _bench_page(page: str) -> dict[str, float]:
        page_path = page if page.startswith("/") else f"/{page}"
        page_url = f"{base_url_stripped}{page_path}"
        page_label = f"{label}-page-{page_path.strip('/').replace('/', '-') or 'root'}"
        snap = benchmark_preview_url(
            worktree_path,
            research_config,
            page_url,
            artifact_dir,
            page_label,
        )
        return snap.metrics

    all_page_metrics: list[dict[str, float]] = []
    max_parallel = min(len(pages), 4)
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(_bench_page, page): page for page in pages}
        for future in as_completed(futures):
            page = futures[future]
            try:
                all_page_metrics.append(future.result())
            except Exception as exc:
                console.print(f"  [yellow]Benchmark failed for {page}: {exc}[/yellow]")

    if not all_page_metrics:
        raise RuntimeError("All page benchmarks failed")

    return aggregate_metric_runs(all_page_metrics, source_url=base_url)


def build_research_opportunity(
    report: ResearchReport,
    worktree_path: Path,
    base_branch: str,
) -> Opportunity:
    """Construct a synthetic opportunity so review and PR creation can be reused."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=True,
    )
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not files:
        files = ["(research changes)"]

    return Opportunity(
        id=f"research-{sanitize_branch_name(report.branch_name)}",
        title="Improve performance metrics via research",
        type="research",
        severity=Severity.MEDIUM,
        files=files,
        problem="Performance trails baseline on tracked Lighthouse metrics.",
        solution="Apply benchmark-proven performance improvements.",
    )


def _build_default_program(research_config: ResearchConfig) -> str:
    """Generate a default research program from config when no program.md is provided."""
    primary = research_config.scoring.primary_metric
    pages = "\n".join(f"- `{p}`" for p in (research_config.target_pages or ["/"]))
    return (
        "# Performance Optimization\n\n"
        "Improve Lighthouse performance metrics for this codebase.\n\n"
        f"## Primary metric\n`{primary.name}` ({primary.direction})\n\n"
        f"## Target pages\n{pages}\n\n"
        "## Approach\n"
        "- Focus on one targeted improvement per iteration\n"
        "- Common wins: image optimization, code splitting, lazy loading, "
        "caching, reducing render-blocking resources, deferring non-critical JS/CSS\n"
        "- Do not break existing functionality\n"
    )


def _resolve_program_text(research_config: ResearchConfig, repo_path: Path) -> str:
    """Resolve the research program text: file if provided, otherwise built-in default."""
    if research_config.program_file:
        program_path = Path(research_config.program_file)
        if not program_path.is_absolute():
            program_path = repo_path / program_path
        if program_path.exists():
            return program_path.read_text()
        console.print(
            f"[yellow]Program file '{program_path}' not found, using default program[/yellow]"
        )
    return _build_default_program(research_config)


# ---------------------------------------------------------------------------
# Main research orchestrator: diagnose → prepare → verify → finalize
# ---------------------------------------------------------------------------


def run_research(
    config: AutoPRConfig,
    *,
    program_file: Path | None = None,
    max_iterations: int | None = None,
    branch_name: str | None = None,
) -> ResearchReport:
    """Run the performance research pipeline: diagnose → prepare → verify → finalize.

    Uses the shared execution spine (prepare_candidate/finalize_candidate) from
    the orchestrator, with a performance verification gate between them.
    """
    from autopr.diagnose import diagnose_performance
    from autopr.orchestrator import finalize_candidate, prepare_candidate
    from autopr.verify import verify_candidate

    research_config = config.research.model_copy(deep=True)
    if program_file is not None:
        research_config.program_file = str(program_file)
    if max_iterations is not None:
        research_config.max_iterations = max_iterations
    if branch_name:
        research_config.branch_name = branch_name

    repo_path = config.repo.path
    report = ResearchReport(
        repo_path=str(repo_path),
        base_branch=config.repo.base_branch,
        branch_name=research_config.branch_name,
        program_file=research_config.program_file,
        status="in_progress",
    )
    artifacts_dir = artifacts_dir_for_run(repo_path, research_config.branch_name)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    try:
        console.rule("[bold]Research Mode[/bold]")
        pages = research_config.target_pages or ["/"]
        console.print(f"  Target pages: {', '.join(pages)}")
        console.print(f"  Max opportunities: {research_config.max_iterations}")
        console.print()

        # ── Step 1: Diagnose ─────────────────────────────────────────
        console.print("[bold]Step 1: Diagnosing performance...[/bold]")
        baseline_url = research_config.baseline_url or None
        opportunities, baseline_metrics = diagnose_performance(
            repo_path,
            research_config,
            target_pages=pages,
            baseline_url=baseline_url,
            artifact_dir=artifacts_dir,
        )
        report.baseline_metrics = baseline_metrics
        report.best_metrics = baseline_metrics

        if not opportunities:
            console.print("[yellow]No performance opportunities found.[/yellow]")
            report.status = "no_improvement"
            return report

        # Limit to max_iterations
        opportunities = opportunities[: research_config.max_iterations]
        console.print(f"\n[bold]Processing {len(opportunities)} opportunity(s)...[/bold]")

        # ── Step 2-4: For each opportunity: prepare → verify → finalize ──
        for i, opp in enumerate(opportunities, 1):
            console.rule(f"[bold]Opportunity {i}/{len(opportunities)}: {opp.title}[/bold]")

            try:
                # ── Prepare (screen → fix → health checks) ──────────
                console.print("[dim]  Preparing candidate...[/dim]")
                prepared = prepare_candidate(opp, config)

                if prepared.status != "ready":
                    console.print(f"[yellow]  Skipped: {prepared.error}[/yellow]")
                    report.experiments.append(
                        ExperimentRecord(
                            iteration=i,
                            status=prepared.status,
                            rejection_reason=prepared.error,
                        )
                    )
                    continue

                # ── Verify (deploy → benchmark → compare) ───────────
                console.print("[dim]  Verifying performance impact...[/dim]")
                wt_path = Path(prepared.worktree_path)
                verification = verify_candidate(
                    wt_path,
                    research_config,
                    baseline_metrics,
                    artifacts_dir,
                    repo_path=repo_path,
                    target_page=pages[0] if pages else "/",
                )

                if not verification.passed:
                    console.print(
                        f"[yellow]  Performance not improved: {verification.reason}[/yellow]"
                    )
                    report.experiments.append(
                        ExperimentRecord(
                            iteration=i,
                            status="rejected",
                            rejection_reason=f"Perf verify failed: {verification.reason}",
                            deploy_url=verification.deploy_url,
                            metrics=verification.candidate_metrics,
                        )
                    )
                    try:
                        remove_worktree(repo_path, wt_path)
                    except Exception:
                        pass
                    continue

                # ── Finalize (review → commit → PR) ─────────────────
                if config.dry_run:
                    console.print("[bold yellow]  Dry run — skipping review and PR[/bold yellow]")
                    report.experiments.append(
                        ExperimentRecord(
                            iteration=i,
                            status="accepted",
                            deploy_url=verification.deploy_url,
                            metrics=verification.candidate_metrics,
                            commit_sha=get_head_sha(wt_path),
                        )
                    )
                    report.best_metrics = verification.candidate_metrics
                else:
                    console.print("[dim]  Finalizing...[/dim]")
                    finalized = finalize_candidate(opp, prepared, config)

                    exp_status = "accepted" if finalized.status == "success" else "failed"
                    report.experiments.append(
                        ExperimentRecord(
                            iteration=i,
                            status=exp_status,
                            rejection_reason=finalized.error,
                            deploy_url=verification.deploy_url,
                            metrics=verification.candidate_metrics,
                            commit_sha=get_head_sha(wt_path),
                        )
                    )

                    if finalized.status == "success":
                        report.best_metrics = verification.candidate_metrics
                        report.pr_url = finalized.pr_url
                        report.best_commit_sha = get_head_sha(wt_path)

                try:
                    remove_worktree(repo_path, wt_path)
                except Exception:
                    pass

            except Exception as exc:
                console.print(f"  [red]ERROR:[/red] {exc}")
                report.experiments.append(
                    ExperimentRecord(
                        iteration=i,
                        status="failed",
                        rejection_reason=str(exc),
                    )
                )

        # ── Summary ──────────────────────────────────────────────────
        accepted = [e for e in report.experiments if e.status == "accepted"]
        if accepted:
            report.status = "success"
        elif any(e.status == "failed" for e in report.experiments):
            report.status = "failed"
            report.error = "All opportunities failed"
        else:
            report.status = "no_improvement"

        return report

    except Exception as exc:
        report.status = "failed"
        report.error = str(exc)
        return report

    finally:
        report.completed_at = datetime.now()
        report_dir = Path(__file__).resolve().parent.parent / ".autopr-reports"
        report_dir.mkdir(exist_ok=True)
        report_path = report_dir / "research-report.json"
        report_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
