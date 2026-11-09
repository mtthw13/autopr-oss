"""Performance verification — deploy a candidate branch and compare metrics against baseline."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from autopr.config import ResearchConfig
from autopr.models import MetricSnapshot
from autopr.research import (
    benchmark_preview_url,
    deploy_candidate,
    evaluate_candidate_metrics,
    refresh_auth_cookie,
)

console = Console()


class VerifyResult:
    """Result of a performance verification run."""

    def __init__(
        self,
        *,
        passed: bool,
        reason: str,
        baseline_metrics: MetricSnapshot,
        candidate_metrics: MetricSnapshot,
        deploy_url: str = "",
    ):
        self.passed = passed
        self.reason = reason
        self.baseline_metrics = baseline_metrics
        self.candidate_metrics = candidate_metrics
        self.deploy_url = deploy_url

    def format_comparison(self) -> str:
        """Format a markdown comparison table for PR body or console."""
        lines = [
            "| Metric | Baseline | Candidate | Delta |",
            "|--------|----------|-----------|-------|",
        ]
        all_names = sorted(set(self.baseline_metrics.metrics) | set(self.candidate_metrics.metrics))
        for name in all_names:
            bval = self.baseline_metrics.value(name)
            cval = self.candidate_metrics.value(name)
            if bval is None or cval is None:
                continue
            delta = cval - bval
            sign = "+" if delta >= 0 else ""
            lines.append(f"| {name} | {bval:.1f} | {cval:.1f} | {sign}{delta:.1f} |")
        return "\n".join(lines)

    def print_comparison(self) -> None:
        """Print the metric comparison to console."""
        all_names = sorted(set(self.baseline_metrics.metrics) | set(self.candidate_metrics.metrics))
        for name in all_names:
            bval = self.baseline_metrics.value(name)
            cval = self.candidate_metrics.value(name)
            if bval is None or cval is None:
                continue
            delta = cval - bval
            sign = "+" if delta >= 0 else ""
            icon = (
                "[green]OK[/green]"
                if delta <= 0 or name == "performance_score" and delta > 0
                else "[yellow]!![/yellow]"
            )
            console.print(f"    {name}: {bval:.1f} -> {cval:.1f} ({sign}{delta:.1f}) {icon}")


def verify_candidate(
    worktree_path: Path,
    research_config: ResearchConfig,
    baseline_metrics: MetricSnapshot,
    artifact_dir: Path,
    *,
    repo_path: Path | None = None,
    target_page: str = "/",
) -> VerifyResult:
    """Deploy a candidate branch, benchmark it, and compare against baseline.

    Returns a VerifyResult with pass/fail and metric comparison.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Deploy the candidate
    console.print("[dim]  Deploying candidate for verification...[/dim]")
    deploy_url = deploy_candidate(
        worktree_path,
        research_config,
        artifact_dir,
        "verify-candidate",
        repo_path=repo_path,
    )
    console.print(f"  [dim]Preview:[/dim] {deploy_url}")

    # Auth if needed
    refresh_auth_cookie(research_config.lighthouse)

    # Benchmark the candidate
    page_path = target_page if target_page.startswith("/") else f"/{target_page}"
    candidate_url = f"{deploy_url.rstrip('/')}{page_path}"
    console.print(f"[dim]  Benchmarking {candidate_url}...[/dim]")
    candidate_metrics = benchmark_preview_url(
        worktree_path,
        research_config,
        candidate_url,
        artifact_dir,
        "verify-candidate",
    )

    # Compare
    accepted, reason = evaluate_candidate_metrics(
        candidate_metrics,
        baseline_metrics,
        research_config.scoring,
    )

    result = VerifyResult(
        passed=accepted,
        reason=reason,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        deploy_url=deploy_url,
    )

    if accepted:
        console.print(f"  [bold green]VERIFIED:[/bold green] {reason}")
    else:
        console.print(f"  [bold red]FAILED VERIFICATION:[/bold red] {reason}")

    result.print_comparison()
    return result
