"""Pre-fix screener — quick sanity check before committing to a full fix."""

from __future__ import annotations

import logging
import subprocess
import textwrap
from pathlib import Path

from rich.console import Console

from autopr.models import DimensionResult, DimensionVerdict, Opportunity, ScreenResult
from autopr.reviewer import extract_json_from_claude_output

logger = logging.getLogger(__name__)
console = Console()


SCREEN_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a pre-fix screener. Before an AI agent attempts a code fix, you do a
    quick sanity check on the proposed opportunity to decide whether to proceed.

    ## Opportunity
    **ID:** {id}
    **Title:** {title}
    **Type:** {type}
    **Problem:** {problem}
    **Proposed Solution:** {solution}
    **Files:** {files}

    ## Your Task
    1. Read the affected files and surrounding context.
    2. Evaluate whether the proposed fix is feasible and safe.
    3. Check for:
       - Whether the problem actually exists in the code
       - Whether the proposed solution would introduce regressions
       - Whether the fix is within the agent's capabilities

    ## CRITICAL: Output format
    Your FINAL output MUST be ONLY a JSON object — no other text:

    {{
      "proceed": true,
      "dimensions": [
        {{"dimension": "scope_hygiene", "verdict": "pass", "finding": "..."}},
        {{"dimension": "security",      "verdict": "pass", "finding": "..."}},
        {{"dimension": "correctness",   "verdict": "pass", "finding": "..."}},
        {{"dimension": "compatibility", "verdict": "pass", "finding": "..."}}
      ],
      "summary": "1-2 sentence summary of screening decision"
    }}

    - proceed: true if the fix should be attempted, false to skip
    - Each dimension verdict is "pass", "fail", or "skip"
    - Do NOT wrap in markdown code fences
""")


def screen_opportunity(
    opportunity: Opportunity,
    repo_path: Path,
    model: str = "claude-opus-4-6",
    max_turns: int = 3,
) -> ScreenResult:
    """Run a quick pre-fix screen on the opportunity."""
    console.print(f"\n[bold cyan]Screening:[/bold cyan] {opportunity.title}")

    prompt = SCREEN_PROMPT_TEMPLATE.format(
        id=opportunity.id,
        title=opportunity.title,
        type=opportunity.type,
        problem=opportunity.problem,
        solution=opportunity.solution,
        files=", ".join(opportunity.files),
    )

    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        "high",
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        "Bash,Read,Grep,Glob",
        "--output-format",
        "json",
    ]

    console.print("[dim]  Running screener agent...[/dim]")
    result = subprocess.run(
        cmd,
        cwd=str(repo_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        logger.warning("Screener failed for %s, proceeding by default", opportunity.id)
        return ScreenResult(
            proceed=True,
            summary=f"Screener process failed — proceeding by default: {result.stderr[:200]}",
            opportunity_id=opportunity.id,
        )

    return _parse_screen_result(result.stdout, opportunity.id)


def _parse_screen_result(raw: str, opportunity_id: str) -> ScreenResult:
    """Parse the screener's raw output into a ScreenResult.

    Fail-open: if parsing fails or the "proceed" key is missing,
    defaults to proceed=True so a broken screener doesn't block the pipeline.

    Expected JSON format:
    {
        "proceed": true/false,
        "dimensions": [
            {"dimension": "correctness", "verdict": "pass", "finding": "..."},
            ...
        ],
        "summary": "..."
    }
    """
    data = extract_json_from_claude_output(raw)

    if data is None:
        logger.warning(
            "Screener output unparseable for %s, proceeding by default",
            opportunity_id,
        )
        return ScreenResult(
            proceed=True,
            summary="Unparseable screener output — proceeding by default",
            opportunity_id=opportunity_id,
        )

    if not isinstance(data, dict):
        logger.warning(
            "Screener output is not a dict for %s, proceeding by default",
            opportunity_id,
        )
        return ScreenResult(
            proceed=True,
            summary="Unparseable screener output — proceeding by default",
            opportunity_id=opportunity_id,
        )

    raw_proceed = data.get("proceed", True)
    if isinstance(raw_proceed, str):
        proceed = raw_proceed.lower() not in ("false", "no", "0")
    else:
        proceed = bool(raw_proceed)
    summary = data.get("summary", data.get("reason", ""))

    # Process dimensions — skip invalid ones silently
    raw_dimensions = data.get("dimensions", [])
    dim_results = []
    for dim in raw_dimensions:
        if not isinstance(dim, dict):
            continue
        dim_name: str = dim.get("dimension") or dim.get("name") or ""
        verdict_str = dim.get("verdict", "").lower()
        finding: str = dim.get("finding") or dim.get("comment") or ""

        # Validate verdict value
        valid_verdicts = {v.value for v in DimensionVerdict}
        if verdict_str not in valid_verdicts:
            continue

        try:
            dv = DimensionVerdict(verdict_str)
        except ValueError:
            continue

        dim_results.append(
            DimensionResult(
                dimension=dim_name,
                verdict=dv,
                finding=finding,
            )
        )

    # Build summary from failed dimensions if no summary was provided
    if not summary:
        fail_findings = [
            f"{d.dimension}: {d.finding}" for d in dim_results if d.verdict == DimensionVerdict.FAIL
        ]
        if fail_findings:
            summary = "; ".join(fail_findings)

    return ScreenResult(
        proceed=bool(proceed),
        dimensions=dim_results,
        summary=summary,
        opportunity_id=opportunity_id,
    )
