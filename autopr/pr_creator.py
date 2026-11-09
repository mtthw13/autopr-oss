"""PR creator — pushes branch and creates GitHub PR via gh CLI."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from rich.console import Console

from autopr.fixer import ensure_worktree_clean
from autopr.models import (
    CodexReviewResult,
    HealthCheckResult,
    Opportunity,
    ResearchReport,
    ReviewResult,
    Severity,
    StructuredReviewResult,
)

console = Console()


def format_review_section(structured_review: StructuredReviewResult) -> str:
    """Format a structured review into a markdown section for the PR body.

    Includes a verdict line, a dimensions table (pass/fail/skip), and the summary.
    Pipe characters in dimension findings are escaped for markdown table safety.
    """
    lines = []
    lines.append(f"**Verdict:** {structured_review.verdict.value}")

    if structured_review.dimensions:
        lines.append("")
        lines.append("| Dimension | Verdict | Finding |")
        lines.append("|-----------|---------|---------|")
        for dim in structured_review.dimensions:
            escaped_finding = dim.finding.replace("|", "\\|")
            lines.append(f"| {dim.dimension} | {dim.verdict.value} | {escaped_finding} |")

    lines.append("")
    lines.append(f"**Summary:** {structured_review.summary}")

    return "\n".join(lines)


def format_research_section(research_report: ResearchReport) -> str:
    """Format research metrics and experiment summary for a PR body."""
    lines = ["## Research Summary"]
    lines.append(f"- Program file: `{research_report.program_file}`")
    lines.append(f"- Accepted experiments: {len(research_report.accepted_experiments)}")

    baseline = research_report.baseline_metrics
    best = research_report.best_metrics
    if baseline and best:
        lines.append("")
        lines.append("| Metric | Baseline | Best | Delta |")
        lines.append("|--------|----------|------|-------|")
        metric_names = sorted(set(baseline.metrics) | set(best.metrics))
        for name in metric_names:
            baseline_value = baseline.value(name)
            best_value = best.value(name)
            if baseline_value is None or best_value is None:
                continue
            delta = best_value - baseline_value
            lines.append(f"| {name} | {baseline_value:.3f} | {best_value:.3f} | {delta:+.3f} |")

    if research_report.experiments:
        lines.append("")
        lines.append("| Iteration | Status | Metrics | Reason |")
        lines.append("|-----------|--------|---------|--------|")
        for record in research_report.experiments:
            metric_text = (
                ", ".join(
                    f"{name}={value:.3f}" for name, value in sorted(record.metrics.metrics.items())
                )
                if record.metrics and record.metrics.metrics
                else "—"
            )
            reason = record.rejection_reason.replace("|", "\\|") if record.rejection_reason else "—"
            lines.append(f"| {record.iteration} | {record.status} | {metric_text} | {reason} |")

    return "\n".join(lines)


def create_pr_description(
    opportunity: Opportunity,
    review: ReviewResult | None = None,
    structured_review: StructuredReviewResult | None = None,
    health_checks: list[HealthCheckResult] | None = None,
    research_report: ResearchReport | None = None,
    codex_review: CodexReviewResult | None = None,
) -> str:
    """Build a structured PR description: problem -> solution -> test."""
    sections = []

    sections.append("## Problem")
    sections.append(opportunity.problem)

    if opportunity.estimated_impact:
        sections.append(f"\n**Estimated impact:** {opportunity.estimated_impact}")

    sections.append("\n## Solution")
    sections.append(opportunity.solution)

    sections.append("\n## Files Changed")
    for f in opportunity.files:
        sections.append(f"- `{f}`")

    if health_checks:
        sections.append("\n## Health Checks")
        for hc in health_checks:
            icon = "pass" if hc.passed else "FAIL"
            sections.append(f"- [{icon}] `{hc.command}` ({hc.duration_seconds:.1f}s)")

    sections.append("\n## Verification")
    sections.append("- [ ] Manual smoke test of affected flow")

    if research_report:
        sections.append("")
        sections.append(format_research_section(research_report))

    if structured_review:
        sections.append("\n## AI Review")
        sections.append(format_review_section(structured_review))
    elif review:
        sections.append("\n## AI Review")
        sections.append(f"**Verdict:** {review.verdict.value}")
        sections.append(f"**Summary:** {review.summary}")
        if review.suggestions:
            sections.append("\n**Suggestions:**")
            for s in review.suggestions:
                sections.append(f"- {s}")

    if codex_review:
        sections.append("\n## Codex Review")
        if codex_review.approve:
            sections.append("**Result:** Approved — no material risks found")
        else:
            sections.append("**Result:** Risks flagged")
        if codex_review.risks:
            sections.append("")
            sections.append("| Category | Detail |")
            sections.append("|----------|--------|")
            for risk in codex_review.risks:
                category = risk.get("category", "?")
                detail = risk.get("detail", "").replace("|", "\\|")
                sections.append(f"| {category} | {detail} |")
        if codex_review.iterations > 1:
            sections.append(f"\n*Resolved after {codex_review.iterations} review iterations*")

    sections.append("\n---")
    sections.append("*Generated by [autopr](https://github.com/autopr) — autonomous PR agent*")

    return "\n".join(sections)


def parse_opportunity_from_pr_body(body: str, pr_number: int, title: str = "") -> Opportunity:
    """Extract opportunity fields from a PR description body.

    Returns an Opportunity with fields parsed from the body sections.
    Missing sections return empty strings / empty lists.
    """
    problem = ""
    solution = ""
    files: list[str] = []

    # Extract problem
    problem_match = re.search(r"## Problem\n(.+?)(?=\n##|\n---|\Z)", body, re.DOTALL)
    if problem_match:
        text = problem_match.group(1).strip()
        # Remove estimated impact line if present
        text = re.sub(r"\n\*\*Estimated impact:\*\*.*", "", text).strip()
        problem = text

    # Extract solution
    solution_match = re.search(r"## Solution\n(.+?)(?=\n##|\n---|\Z)", body, re.DOTALL)
    if solution_match:
        solution = solution_match.group(1).strip()

    # Extract files
    files_match = re.search(r"## Files Changed\n(.+?)(?=\n##|\n---|\Z)", body, re.DOTALL)
    if files_match:
        file_lines = files_match.group(1).strip().split("\n")
        for line in file_lines:
            match = re.match(r"- `(.+?)`", line.strip())
            if match:
                files.append(match.group(1))

    return Opportunity(
        id=f"pr-{pr_number}",
        title=title or f"PR #{pr_number}",
        type="unknown",
        severity=Severity.MEDIUM,
        files=files or ["unknown"],
        problem=problem or "Parsed from PR body",
        solution=solution or "See PR description",
    )


def push_and_create_pr(
    opportunity: Opportunity,
    branch_name: str,
    repo_path: Path,
    base_branch: str = "main",
    review: ReviewResult | None = None,
    structured_review: StructuredReviewResult | None = None,
    dry_run: bool = False,
    health_checks: list[HealthCheckResult] | None = None,
    research_report: ResearchReport | None = None,
    codex_review: CodexReviewResult | None = None,
) -> str:
    """Push the branch and create a GitHub PR. Returns the PR URL."""
    if dry_run:
        console.print("[yellow]Dry run — skipping push and PR creation[/yellow]")
        return "dry-run://no-pr-created"

    # Safety net: commit any lingering uncommitted changes
    ensure_worktree_clean(repo_path, context=f"PR {branch_name}")

    # Push
    console.print(f"[dim]Pushing {branch_name}...[/dim]")
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )

    # Create PR via gh CLI
    description = create_pr_description(
        opportunity,
        review,
        structured_review=structured_review,
        health_checks=health_checks,
        research_report=research_report,
        codex_review=codex_review,
    )
    title = f"[AutoPR] {opportunity.title}"

    console.print(f"[dim]Creating PR: {title}[/dim]")
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            description,
            "--base",
            base_branch,
            "--head",
            branch_name,
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed:\n{result.stderr}")

    pr_url = result.stdout.strip()
    console.print(f"[bold green]PR created: {pr_url}[/bold green]")
    return pr_url
