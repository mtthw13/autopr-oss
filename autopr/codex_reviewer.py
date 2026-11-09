"""Codex reviewer — material-risk review using OpenAI Codex CLI."""

from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

from rich.console import Console

from autopr.models import CodexReviewResult, Opportunity
from autopr.reviewer import extract_json_from_claude_output

console = Console()

MATERIAL_RISK_PREAMBLE = textwrap.dedent("""\
    You are reviewing for material risks ONLY. Do NOT flag:
    - Style, formatting, naming conventions
    - Missing comments or documentation
    - Minor code organization preferences
    - Test coverage suggestions

    ONLY flag issues in these categories:
    - AUTH: Authentication/authorization bypasses, credential exposure, session handling
    - ARCH: Architectural violations, circular dependencies, wrong abstraction layer
    - DATA: Data loss, corruption, race conditions, migration safety
    - UX: User-facing regressions, broken flows, accessibility breakage

    If no material risks exist, approve.

    Your FINAL output MUST be ONLY a JSON object — no other text:
    {{"approve": true, "risks": [], "summary": "No material risks found"}}

    Or if risks exist:
    {{"approve": false, "risks": [{{"category": "AUTH", "detail": "..."}}], "summary": "..."}}
""")

SCOPE_REVIEW_PROMPT = textwrap.dedent("""\
    {preamble}

    ## Proposed Change
    **Title:** {title}
    **Type:** {type}
    **Problem:** {problem}
    **Proposed Solution:** {solution}
    **Files:** {files}

    Read the affected files and evaluate whether this proposed change is safe to attempt.
    Focus on whether the scope could introduce auth, architectural, data, or UX risks.
""")

DIFF_REVIEW_INSTRUCTIONS = textwrap.dedent("""\
    {preamble}

    ## Context
    **Title:** {title}
    **Type:** {type}
    **Problem:** {problem}
    **Solution:** {solution}

    Review the diff for material risks only. Approve if the change is safe.
""")

APPLY_FEEDBACK_PROMPT = textwrap.dedent("""\
    Codex (an independent reviewer) found material risks in your changes.
    Fix these issues without introducing new problems.

    ## Risks Found
    {risks}

    ## Original Opportunity
    **Title:** {title}
    **Problem:** {problem}
    **Solution:** {solution}

    Fix the issues raised above. Make minimal, targeted changes.
    Do NOT add type: ignore, noqa, or eslint-disable comments.
""")


def _is_claude_model(model: str) -> bool:
    """Return True if the model string refers to a Claude model."""
    return bool(model) and (model.startswith("claude") or model in ("opus", "sonnet", "haiku"))


def _run_review_command(
    prompt: str,
    repo_path: Path,
    *,
    timeout: int,
    model: str = "",
    codex_cmd: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Route review to Claude or Codex based on the model string.

    Both backends feed into the same _parse_codex_result parser, which
    handles JSON, natural language [P1]/[AUTH] markers, and plain text.
    """
    if _is_claude_model(model):
        cmd = [
            "claude",
            "-p",
            "--model",
            model,
            "--effort",
            "high",
            "--max-turns",
            "15",
            "--allowedTools",
            "Bash,Read,Grep,Glob",
            "--output-format",
            "json",
        ]
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # Unwrap --output-format json envelope
        if result.returncode == 0 and result.stdout.strip():
            try:
                import json as _json

                envelope = _json.loads(result.stdout)
                if isinstance(envelope, dict) and "result" in envelope:
                    text = envelope["result"]
                    if isinstance(text, str):
                        result = subprocess.CompletedProcess(
                            args=result.args,
                            returncode=result.returncode,
                            stdout=text,
                            stderr=result.stderr,
                        )
            except (_json.JSONDecodeError, ValueError):
                pass
        return result

    # Codex path: use -o to capture last message
    cmd = codex_cmd or ["codex", "exec", "--full-auto"]
    if model:
        cmd = list(cmd) + ["-m", model]

    output_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="codex-output-",
        delete=False,
    )
    output_path = output_file.name
    output_file.close()

    full_cmd = list(cmd) + ["-o", output_path]
    result = subprocess.run(
        full_cmd,
        cwd=str(repo_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    try:
        last_message = Path(output_path).read_text().strip()
        if last_message:
            result = subprocess.CompletedProcess(
                args=result.args,
                returncode=result.returncode,
                stdout=last_message,
                stderr=result.stderr,
            )
    except Exception:
        pass
    finally:
        try:
            Path(output_path).unlink()
        except OSError:
            pass

    return result


def _parse_codex_result(raw: str, review_type: str) -> CodexReviewResult | None:
    """Parse Codex output into a CodexReviewResult.

    Handles two formats:
    1. JSON: {"approve": bool, "risks": [...], "summary": "..."}
    2. Natural language with [AUTH]/[ARCH]/[DATA]/[UX] or [P1]/[P2]/[P3] markers
    """
    if not raw or not raw.strip():
        return None

    # Try JSON first
    data = extract_json_from_claude_output(raw)
    if data is not None and isinstance(data, dict):
        return CodexReviewResult(
            approve=bool(data.get("approve", True)),
            risks=data.get("risks", []),
            summary=data.get("summary", ""),
            review_type=review_type,
        )

    # Parse natural language with [AUTH]/[ARCH]/[DATA]/[UX] or [P1]/[P2]/[P3] markers
    text = raw.strip()
    risk_pattern = re.compile(
        r"^\s*[-*]\s*\[(AUTH|ARCH|DATA|UX|P[123])\]\s*(.*?)(?=^\s*[-*]\s*\[(?:AUTH|ARCH|DATA|UX|P[123])\]\s*|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    matches = list(risk_pattern.finditer(text))
    risks: list[dict[str, str]] = []
    for match in matches:
        category = match.group(1).upper()
        body = match.group(2).strip()
        lines = body.split("\n", 1)
        title = lines[0].strip().rstrip("—").rstrip(":").strip()
        detail = lines[1].strip() if len(lines) > 1 else ""
        risks.append(
            {
                "category": category,
                "detail": f"{title}\n{detail}".strip() if detail else title,
            }
        )

    if not risks:
        lower = text.lower()
        if any(
            w in lower
            for w in [
                "no issues",
                "no material risk",
                "lgtm",
                "looks good",
                "patch is clean",
                "no risks",
            ]
        ):
            return CodexReviewResult(
                approve=True,
                risks=[],
                summary=text.split("\n")[0][:300],
                review_type=review_type,
            )
        return None

    # Extract summary from text before the risk list
    summary = text[: matches[0].start()].strip() if matches else text.split("\n")[0]

    # P1/AUTH/ARCH/DATA risks block; P2/P3/UX are warnings
    blocking = {"P1", "AUTH", "ARCH", "DATA"}
    has_blocking = any(r["category"] in blocking for r in risks)

    return CodexReviewResult(
        approve=not has_blocking,
        risks=risks,
        summary=summary[:500],
        review_type=review_type,
    )


def codex_review_scope(
    opportunity: Opportunity,
    repo_path: Path,
    model: str = "",
) -> CodexReviewResult:
    """Run a Codex scope review on the proposed opportunity."""
    console.print(f"\n[bold blue]Codex scope review:[/bold blue] {opportunity.title}")

    prompt = SCOPE_REVIEW_PROMPT.format(
        preamble=MATERIAL_RISK_PREAMBLE,
        title=opportunity.title,
        type=opportunity.type,
        problem=opportunity.problem,
        solution=opportunity.solution,
        files=", ".join(opportunity.files),
    )

    cmd = ["codex", "exec", "--full-auto"]
    if model:
        cmd.extend(["-m", model])

    backend = "Claude" if _is_claude_model(model) else "Codex"
    console.print(f"[dim]  Running {backend} scope reviewer...[/dim]")
    try:
        result = _run_review_command(prompt, repo_path, timeout=300, model=model, codex_cmd=cmd)
    except subprocess.TimeoutExpired:
        console.print(
            f"[yellow]  {backend} scope review timed out — defaulting to approve[/yellow]"
        )
        return CodexReviewResult(approve=True, summary="Timed out", review_type="scope")

    if result.returncode != 0:
        console.print(
            f"[yellow]  {backend} scope review failed (exit {result.returncode})[/yellow]"
        )
        return CodexReviewResult(approve=True, summary="Process failed", review_type="scope")

    review = _parse_codex_result(result.stdout, "scope")
    if review is None:
        console.print(
            "[yellow]  Could not parse Codex scope output — defaulting to approve[/yellow]"
        )
        return CodexReviewResult(approve=True, summary="Unparseable output", review_type="scope")
    if review.approve:
        console.print("[green]  Codex scope: approved[/green]")
    else:
        console.print(f"[yellow]  Codex scope: risks found — {review.summary}[/yellow]")
    return review


def codex_review_diff(
    opportunity: Opportunity,
    repo_path: Path,
    base_branch: str = "main",
    model: str = "",
) -> CodexReviewResult:
    """Run a Codex diff review on the current branch's changes."""
    console.print("[dim]  Running Codex diff reviewer...[/dim]")

    instructions = DIFF_REVIEW_INSTRUCTIONS.format(
        preamble=MATERIAL_RISK_PREAMBLE,
        title=opportunity.title,
        type=opportunity.type,
        problem=opportunity.problem,
        solution=opportunity.solution,
    )

    codex_cmd = ["codex", "exec", "review", "--full-auto", "--base", base_branch]
    backend = "Claude" if _is_claude_model(model) else "Codex"
    console.print(f"[dim]  Running {backend} diff reviewer...[/dim]")

    try:
        result = _run_review_command(
            instructions, repo_path, timeout=600, model=model, codex_cmd=codex_cmd
        )
    except subprocess.TimeoutExpired:
        console.print(f"[yellow]  {backend} diff review timed out — defaulting to approve[/yellow]")
        return CodexReviewResult(approve=True, summary="Timed out", review_type="diff")

    if result.returncode != 0:
        console.print(f"[yellow]  {backend} diff review failed (exit {result.returncode})[/yellow]")
        return CodexReviewResult(approve=True, summary="Process failed", review_type="diff")

    review = _parse_codex_result(result.stdout, "diff")
    if review is None:
        console.print(
            "[yellow]  Could not parse Codex diff output — defaulting to approve[/yellow]"
        )
        return CodexReviewResult(approve=True, summary="Unparseable output", review_type="diff")
    if review.approve:
        console.print("[green]  Codex diff: approved[/green]")
    else:
        console.print(f"[yellow]  Codex diff: risks found — {review.summary}[/yellow]")
    return review


def apply_codex_feedback(
    codex_result: CodexReviewResult,
    repo_path: Path,
    opportunity: Opportunity,
    model: str = "claude-opus-4-6",
) -> None:
    """Run Claude (Opus) to fix issues raised by Codex."""
    risks_text = "\n".join(
        f"- [{r.get('category', '?')}] {r.get('detail', '')}" for r in codex_result.risks
    )

    prompt = APPLY_FEEDBACK_PROMPT.format(
        risks=risks_text,
        title=opportunity.title,
        problem=opportunity.problem,
        solution=opportunity.solution,
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

    console.print("[dim]  Opus fixing Codex-flagged issues...[/dim]")
    subprocess.run(
        cmd,
        cwd=str(repo_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )


def codex_review_loop(
    opportunity: Opportunity,
    repo_path: Path,
    base_branch: str = "main",
    review_model: str = "",
    fix_model: str = "claude-opus-4-6",
    max_iterations: int = 3,
) -> CodexReviewResult:
    """Run review → fix loop until reviewer approves or max iterations."""
    backend = "Claude" if _is_claude_model(review_model) else "Codex"
    console.print(f"\n[bold blue]{backend} diff review loop:[/bold blue] {opportunity.title}")

    for iteration in range(1, max_iterations + 1):
        console.print(f"[dim]  {backend} review iteration {iteration}/{max_iterations}[/dim]")

        review = codex_review_diff(
            opportunity,
            repo_path,
            base_branch=base_branch,
            model=review_model,
        )
        review.iterations = iteration

        if review.approve:
            console.print(f"[green]  Codex approved on iteration {iteration}[/green]")
            return review

        if iteration < max_iterations:
            console.print(
                f"[yellow]  Codex flagged {len(review.risks)} risk(s) — "
                f"Opus fixing (attempt {iteration})...[/yellow]"
            )
            apply_codex_feedback(review, repo_path, opportunity, model=fix_model)
        else:
            console.print(
                f"[red]  Codex still not satisfied after {max_iterations} iterations[/red]"
            )

    return review


def refine_opportunity_from_feedback(
    opportunity: Opportunity,
    codex_result: CodexReviewResult,
) -> Opportunity:
    """Refine an opportunity's solution based on Codex scope feedback.

    Appends Codex's risk warnings to the solution so the fixer accounts for them.
    """
    if not codex_result.risks:
        return opportunity

    warnings = "\n".join(
        f"- [{r.get('category', '?')}] {r.get('detail', '')}" for r in codex_result.risks
    )
    refined_solution = (
        f"{opportunity.solution}\n\n"
        f"IMPORTANT — Codex flagged these risks in the proposed scope. "
        f"Address them in your fix:\n{warnings}"
    )

    return opportunity.model_copy(update={"solution": refined_solution})
