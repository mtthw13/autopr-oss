"""Lighthouse-based performance diagnostics — turns audit findings into opportunities."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from rich.console import Console

from autopr.config import ResearchConfig
from autopr.models import MetricSnapshot, Opportunity, Severity
from autopr.research import (
    deploy_candidate,
    parse_json_object,
    refresh_auth_cookie,
    run_lighthouse_once,
)

console = Console()


# ---------------------------------------------------------------------------
# Lighthouse audit parsing
# ---------------------------------------------------------------------------


def extract_opportunities_from_audits(
    payload: dict,
    *,
    min_savings_ms: float = 100.0,
    min_savings_bytes: int = 10_000,
    limit: int = 10,
) -> list[dict]:
    """Extract actionable Lighthouse opportunity audits with estimated savings.

    Returns a list of dicts with: audit_id, title, savings_ms, savings_bytes,
    display_value, details_items (URLs/paths from the audit).
    """
    results: list[tuple[float, dict]] = []

    for audit_id, audit in (payload.get("audits") or {}).items():
        details = audit.get("details") or {}
        savings_ms = details.get("overallSavingsMs")
        savings_bytes = details.get("overallSavingsBytes")

        # Skip audits with no quantified savings
        if savings_ms is None and savings_bytes is None:
            continue

        # Apply minimum thresholds
        if savings_ms is not None and savings_ms < min_savings_ms:
            if savings_bytes is None or savings_bytes < min_savings_bytes:
                continue
        if savings_ms is None and savings_bytes is not None and savings_bytes < min_savings_bytes:
            continue

        title = audit.get("title") or audit_id
        weight = float(savings_ms or 0) + float(savings_bytes or 0) / 1024.0

        # Extract detail items (URLs, file paths, resource info)
        items = []
        for item in (details.get("items") or [])[:5]:
            url = item.get("url", "")
            if url:
                items.append(url)

        results.append(
            (
                weight,
                {
                    "audit_id": audit_id,
                    "title": title,
                    "savings_ms": savings_ms,
                    "savings_bytes": savings_bytes,
                    "display_value": audit.get("displayValue", ""),
                    "detail_urls": items,
                },
            )
        )

    results.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in results[:limit]]


def extract_low_scoring_diagnostics(
    payload: dict,
    *,
    max_score: float = 0.5,
    limit: int = 5,
) -> list[dict]:
    """Extract low-scoring Lighthouse diagnostics that aren't opportunity audits."""
    results: list[tuple[float, dict]] = []

    for audit_id, audit in (payload.get("audits") or {}).items():
        score = audit.get("score")
        if not isinstance(score, (int, float)):
            continue
        if audit.get("scoreDisplayMode") in ("notApplicable", "manual", "informative"):
            continue
        # Skip opportunity audits (already handled)
        details = audit.get("details") or {}
        if (
            details.get("overallSavingsMs") is not None
            or details.get("overallSavingsBytes") is not None
        ):
            continue
        if score > max_score:
            continue

        title = audit.get("title") or audit_id
        results.append(
            (
                float(score),
                {
                    "audit_id": audit_id,
                    "title": title,
                    "score": float(score),
                    "display_value": audit.get("displayValue", ""),
                },
            )
        )

    results.sort(key=lambda x: x[0])
    return [entry for _, entry in results[:limit]]


# ---------------------------------------------------------------------------
# Source file mapping
# ---------------------------------------------------------------------------


def map_audit_to_source_files(
    audit: dict,
    repo_path: Path,
) -> list[str]:
    """Best-effort mapping from Lighthouse audit URLs to source files.

    Uses chunk filenames from URLs and greps the codebase for imports/references.
    Returns a list of candidate source file paths (may be empty).
    """
    matched_files: list[str] = []

    for url in audit.get("detail_urls", []):
        # Extract the chunk/file name from the URL
        # e.g. "https://example.com/_next/static/chunks/pages/dashboard-abc123.js"
        # → look for "dashboard" component
        filename_match = re.search(r"/([^/]+?)(?:-[a-f0-9]+)?\.(?:js|css|mjs)$", url)
        if not filename_match:
            continue

        chunk_name = filename_match.group(1)
        if chunk_name in ("webpack", "framework", "main", "polyfills", "commons", "app", "layout"):
            continue  # framework chunks, not actionable

        # Search for files matching this chunk name
        result = subprocess.run(
            [
                "grep",
                "-rl",
                "--include=*.tsx",
                "--include=*.ts",
                "--include=*.jsx",
                "--include=*.js",
                chunk_name,
                ".",
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n")[:3]:
                path = line.strip().lstrip("./")
                if path and path not in matched_files:
                    matched_files.append(path)

    return matched_files


# ---------------------------------------------------------------------------
# Opportunity construction
# ---------------------------------------------------------------------------

AUDIT_TO_SOLUTION: dict[str, str] = {
    "render-blocking-resources": "Defer or async-load render-blocking CSS/JS resources",
    "unused-javascript": "Code-split or lazy-load unused JavaScript modules",
    "unused-css-rules": "Remove unused CSS or use CSS modules for scoping",
    "offscreen-images": "Lazy-load images that are below the fold",
    "unminified-javascript": "Minify JavaScript bundles",
    "unminified-css": "Minify CSS files",
    "uses-responsive-images": "Serve appropriately-sized images for the viewport",
    "uses-optimized-images": "Compress images (WebP/AVIF) to reduce transfer size",
    "uses-text-compression": "Enable gzip/brotli compression for text resources",
    "uses-rel-preconnect": "Add rel=preconnect for critical third-party origins",
    "server-response-time": "Optimize server response time (TTFB)",
    "dom-size": "Reduce DOM size by simplifying component hierarchy",
    "total-byte-weight": "Reduce total page weight by removing unused dependencies",
    "mainthread-work-breakdown": "Reduce main thread work (script evaluation, layout, painting)",
    "bootup-time": "Reduce JavaScript execution time",
    "layout-shifts": "Add explicit width/height to images and embeds to prevent layout shifts",
    "largest-contentful-paint-element": "Optimize the LCP element (preload, reduce size, prioritize)",
    "third-party-summary": "Reduce impact of third-party scripts",
}


def audit_to_opportunity(
    audit: dict,
    repo_path: Path,
    target_page: str,
    *,
    opportunity_id_prefix: str = "perf",
) -> Opportunity:
    """Convert a single Lighthouse audit finding into an Opportunity."""
    audit_id = audit["audit_id"]
    title = audit["title"]

    # Build problem description with savings
    savings_parts = []
    if audit.get("savings_ms"):
        savings_parts.append(f"{audit['savings_ms']:.0f}ms")
    if audit.get("savings_bytes"):
        savings_parts.append(f"{audit['savings_bytes'] / 1024:.0f}KB")
    savings_text = f" (estimated savings: {', '.join(savings_parts)})" if savings_parts else ""

    problem = f"Lighthouse audit '{title}' flagged on {target_page}{savings_text}"
    if audit.get("display_value"):
        problem += f". {audit['display_value']}"

    solution = AUDIT_TO_SOLUTION.get(audit_id, f"Address the '{title}' Lighthouse finding")

    # Map to source files
    files = map_audit_to_source_files(audit, repo_path)
    if not files:
        files = [f"(see Lighthouse audit: {audit_id})"]

    opp_id = f"{opportunity_id_prefix}-{audit_id}"

    return Opportunity(
        id=opp_id,
        title=f"Performance: {title}",
        type="performance",
        severity=Severity.MEDIUM,
        files=files,
        problem=problem,
        solution=solution,
    )


def diagnostic_to_opportunity(
    diagnostic: dict,
    target_page: str,
    *,
    opportunity_id_prefix: str = "perf-diag",
) -> Opportunity:
    """Convert a low-scoring Lighthouse diagnostic into an Opportunity."""
    audit_id = diagnostic["audit_id"]
    title = diagnostic["title"]
    score = diagnostic.get("score", 0)
    display_value = diagnostic.get("display_value", "")

    problem = f"Lighthouse diagnostic '{title}' scored {score:.2f} on {target_page}"
    if display_value:
        problem += f". {display_value}"

    solution = AUDIT_TO_SOLUTION.get(audit_id, f"Investigate and address the '{title}' diagnostic")

    opp_id = f"{opportunity_id_prefix}-{audit_id}"

    return Opportunity(
        id=opp_id,
        title=f"Performance: {title}",
        type="performance",
        severity=Severity.MEDIUM,
        files=[f"(see Lighthouse diagnostic: {audit_id})"],
        problem=problem,
        solution=solution,
    )


# ---------------------------------------------------------------------------
# Main diagnosis function
# ---------------------------------------------------------------------------


def diagnose_performance(
    repo_path: Path,
    research_config: ResearchConfig,
    *,
    target_pages: list[str] | None = None,
    baseline_url: str | None = None,
    artifact_dir: Path | None = None,
) -> tuple[list[Opportunity], MetricSnapshot]:
    """Run Lighthouse on target pages, extract opportunities, and return them.

    Returns (opportunities, baseline_metrics).
    """
    pages = target_pages or research_config.target_pages or ["/"]
    if artifact_dir is None:
        artifact_dir = repo_path / ".autopr-research"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Get a URL to benchmark
    if baseline_url:
        url = baseline_url.rstrip("/")
        console.print(f"  Using baseline URL: {url}")
    else:
        console.print("[bold]Deploying for diagnosis...[/bold]")
        url = deploy_candidate(
            repo_path,
            research_config,
            artifact_dir,
            "diagnose",
            repo_path=repo_path,
        )
        console.print(f"  [green]Deployed:[/green] {url}")

    refresh_auth_cookie(research_config.lighthouse)

    # Benchmark each page and collect audits
    all_opportunities: list[Opportunity] = []
    all_metrics: list[dict[str, float]] = []
    seen_audit_ids: set[str] = set()

    for page in pages:
        page_path = page if page.startswith("/") else f"/{page}"
        page_url = f"{url}{page_path}"
        page_label = f"diagnose-{page_path.strip('/').replace('/', '-') or 'root'}"

        console.print(f"\n[bold]Diagnosing {page_path}...[/bold]")

        # Run Lighthouse (1 run is enough for diagnostics)
        artifact_path = artifact_dir / page_label
        metrics = run_lighthouse_once(
            repo_path, research_config.lighthouse, page_url, artifact_path
        )
        all_metrics.append(metrics)

        # Parse the raw Lighthouse JSON for audit details
        raw_json = (artifact_path.with_suffix(".stdout.json")).read_text()
        payload = parse_json_object(raw_json)

        # Extract opportunity audits
        audits = extract_opportunities_from_audits(payload)
        diagnostics = extract_low_scoring_diagnostics(payload)

        console.print(
            f"  Metrics: perf={metrics.get('performance_score', 0):.1f} "
            f"lcp={metrics.get('lcp_ms', 0):.0f}ms "
            f"fcp={metrics.get('fcp_ms', 0):.0f}ms"
        )
        console.print(
            f"  Found {len(audits)} opportunities, {len(diagnostics)} low-scoring diagnostics"
        )

        for audit in audits:
            if audit["audit_id"] in seen_audit_ids:
                continue
            seen_audit_ids.add(audit["audit_id"])
            opp = audit_to_opportunity(audit, repo_path, page_path)
            all_opportunities.append(opp)
            savings = audit.get("display_value", "")
            console.print(f"    {opp.title}: {savings}")

        for diag in diagnostics:
            if diag["audit_id"] in seen_audit_ids:
                continue
            seen_audit_ids.add(diag["audit_id"])
            opp = diagnostic_to_opportunity(diag, page_path)
            all_opportunities.append(opp)
            console.print(f"    {opp.title}: score={diag.get('score', 0):.2f}")

    baseline_metrics = MetricSnapshot(
        metrics={k: v for m in all_metrics for k, v in m.items()},
        source_url=url,
    )

    console.print(
        f"\n[bold]Diagnosis complete:[/bold] {len(all_opportunities)} performance opportunities"
    )
    return all_opportunities, baseline_metrics
