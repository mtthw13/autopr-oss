"""Independent reviewer agent — reviews a proposed fix using a separate Claude Code instance."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import textwrap
from pathlib import Path

from rich.console import Console

from autopr.config import GatingConfig, VerdictRule
from autopr.models import (
    DimensionResult,
    DimensionVerdict,
    Opportunity,
    ReviewResult,
    ReviewVerdict,
    StructuredReviewResult,
)

console = Console()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


def extract_json_from_claude_output(raw: str) -> dict | None:
    """Extract a JSON object from Claude output, handling envelopes, code fences, etc.

    Steps:
      1. Unwrap Claude Code --output-format json envelope.
      2. Direct JSON parse.
      3. Regex search for a JSON object containing known keys, with brace-depth matching.
      4. Brace-depth fallback — walk through text to find the first balanced { }.
    Returns the parsed dict, or None if nothing found.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # Step 1: unwrap envelope {"type":"result","result":"..."}
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and "result" in envelope:
            inner = envelope["result"]
            if isinstance(inner, dict):
                return inner
            if isinstance(inner, str):
                text = inner.strip()
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 2: direct JSON parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 2b: strip markdown code fences and try again
    stripped = _strip_code_fences(text)
    if stripped != text:
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Step 2c: strip <review> tags and try again
    tag_stripped = re.sub(r"</?review>", "", text).strip()
    if tag_stripped != text:
        try:
            data = json.loads(tag_stripped)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Step 3: regex with brace-depth support — find a JSON object starting with known keys
    json_pattern = re.compile(r'\{[^{}]*"(?:verdict|proceed|dimensions)"\s*:', re.DOTALL)
    match = json_pattern.search(text)
    if match:
        start_idx = match.start()
        depth = 0
        for end_idx in range(start_idx, len(text)):
            if text[end_idx] == "{":
                depth += 1
            elif text[end_idx] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start_idx : end_idx + 1]
                    try:
                        data = json.loads(candidate)
                        if isinstance(data, dict):
                            return data
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break

    # Step 4: brace-depth fallback — walk through text to find first balanced { }
    result = _extract_first_balanced_braces(text)
    if result is not None:
        return result

    # Also try after stripping code fences / tags
    for variant in (stripped, tag_stripped):
        result = _extract_first_balanced_braces(variant)
        if result is not None:
            return result

    return None


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ``` or ``` ... ```)."""
    pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
    m = pattern.search(text)
    if m:
        return m.group(1).strip()
    return text


def _extract_first_balanced_braces(text: str) -> dict | None:
    """Walk *text* and return the first balanced { ... } that parses as JSON."""
    for start_idx in range(len(text)):
        if text[start_idx] == "{":
            depth = 0
            for end_idx in range(start_idx, len(text)):
                if text[end_idx] == "{":
                    depth += 1
                elif text[end_idx] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start_idx : end_idx + 1]
                        try:
                            data = json.loads(candidate)
                            if isinstance(data, dict):
                                return data
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
    return None


def extract_json_array_from_claude_output(raw: str) -> list | None:
    """Extract a JSON array from Claude output.

    Steps:
      1. Unwrap envelope.
      2. Direct JSON parse.
      3. Bracket-depth search for [ ... ].
      4. Collect individual JSON objects scattered in the text.
    Returns the parsed list, or None if nothing found.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # Step 1: unwrap envelope
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict):
            if envelope.get("type") == "result" and "result" in envelope:
                inner = envelope["result"]
                if isinstance(inner, list):
                    return inner
                if isinstance(inner, str):
                    text = inner.strip()
            # max_turns error envelopes have no usable result
            if envelope.get("is_error"):
                return None
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 2: direct JSON parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 2b: strip code fences
    stripped = _strip_code_fences(text)
    if stripped != text:
        try:
            data = json.loads(stripped)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Step 3: bracket-depth search
    result = _extract_first_balanced_brackets(text)
    if result is not None:
        return result

    if stripped != text:
        result = _extract_first_balanced_brackets(stripped)
        if result is not None:
            return result

    # Step 4: collect individual JSON objects from text
    objects: list[dict] = []
    for start_idx in range(len(text)):
        if text[start_idx] == "{":
            depth = 0
            for end_idx in range(start_idx, len(text)):
                if text[end_idx] == "{":
                    depth += 1
                elif text[end_idx] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start_idx : end_idx + 1]
                        try:
                            data = json.loads(candidate)
                            if isinstance(data, dict):
                                objects.append(data)
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
    if objects:
        return objects

    return None


def _extract_first_balanced_brackets(text: str) -> list | None:
    """Walk *text* and return the first balanced [ ... ] that parses as a JSON array."""
    for start_idx in range(len(text)):
        if text[start_idx] == "[":
            depth = 0
            for end_idx in range(start_idx, len(text)):
                if text[end_idx] == "[":
                    depth += 1
                elif text[end_idx] == "]":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start_idx : end_idx + 1]
                        try:
                            data = json.loads(candidate)
                            if isinstance(data, list):
                                return data
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
    return None


# ---------------------------------------------------------------------------
# Structured review gating
# ---------------------------------------------------------------------------


def compute_verdict(
    dimensions: list[dict],
    rules: list[VerdictRule] | None = None,
) -> ReviewVerdict:
    """Compute an overall verdict from per-dimension pass/fail results.

    Each dimension dict has at least {"dimension": "...", "verdict": "pass"|"fail"|"skip"}.
    Rules (from GatingConfig) map dimension names to actions on failure.

    The *rules* list uses VerdictRule objects whose fields are:
      - dimension: the dimension name (e.g. "correctness")
      - on_fail: "reject" (maps to REJECT), "request_changes", or "warn" (advisory only)

    Dimensions whose name does not appear in any rule are treated as "warn" by default.

    Returns the most severe verdict: reject > request_changes > approve.
    """
    if not dimensions:
        return ReviewVerdict.APPROVE

    if rules is None:
        rules = GatingConfig().verdict_rules

    # Build lookup: dimension -> on_fail action
    rules_by_dim = {r.dimension: r.on_fail for r in rules}

    worst = ReviewVerdict.APPROVE

    for dim in dimensions:
        dim_verdict = dim.get("verdict", "").lower()
        if dim_verdict in ("pass", "skip"):
            continue
        if dim_verdict != "fail":
            continue

        name = dim.get("dimension", dim.get("name", ""))
        action = rules_by_dim.get(name, "warn")

        if action == "reject":
            return ReviewVerdict.REJECT
        elif action == "request_changes":
            worst = ReviewVerdict.REQUEST_CHANGES
        # "warn" does not change the verdict — just log it
        elif action == "warn":
            logger.info("Dimension %s failed (warn only)", name)

    return worst


def parse_structured_review(
    raw_output: str,
    rules: list[VerdictRule] | None = None,
) -> StructuredReviewResult:
    """Parse a structured 6-dimension review from Claude output.

    Expected JSON shape:
      {
        "dimensions": [
          {"dimension": "correctness", "verdict": "pass", "finding": "..."},
          ...
        ],
        "summary": "...",
        "suggestions": ["..."]
      }

    Uses compute_verdict() to derive the overall verdict.
    Falls back to request_changes if the output cannot be parsed.
    """
    data = extract_json_from_claude_output(raw_output)
    if data is None or "dimensions" not in data:
        console.print("[yellow]  Could not parse structured review dimensions.[/yellow]")
        if data is not None:
            console.print(f"[dim]  Parsed JSON keys: {list(data.keys())}[/dim]")
        else:
            console.print(f"[dim]  Raw output (first 500 chars): {raw_output[:500]}[/dim]")
        return StructuredReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Could not parse structured review",
            dimensions=[],
            suggestions=[],
        )

    dimensions = data["dimensions"]
    if not isinstance(dimensions, list):
        return StructuredReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Could not parse structured review",
            dimensions=[],
            suggestions=[],
        )

    # Filter out dimensions with invalid verdicts silently
    valid_dims = []
    for dim in dimensions:
        if isinstance(dim, dict) and dim.get("verdict", "").lower() in ("pass", "fail", "skip"):
            valid_dims.append(dim)

    verdict = compute_verdict(valid_dims, rules=rules)

    summary = data.get("summary", "")
    suggestions = data.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []

    # Build DimensionResult objects
    dim_results = []
    for dim in valid_dims:
        dim_name: str = dim.get("dimension") or dim.get("name") or "unknown"
        dim_verdict_str = dim.get("verdict", "skip").lower()
        dim_finding: str = dim.get("finding") or dim.get("comment") or ""
        try:
            dv = DimensionVerdict(dim_verdict_str)
        except ValueError:
            dv = DimensionVerdict.SKIP
        dim_results.append(
            DimensionResult(
                dimension=dim_name,
                verdict=dv,
                finding=dim_finding,
            )
        )

    return StructuredReviewResult(
        verdict=verdict,
        summary=summary,
        dimensions=dim_results,
        suggestions=suggestions,
    )


REVIEW_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are an independent code reviewer. A separate AI agent has made changes to fix
    an issue. Your job is to review the diff and determine if the fix is correct, safe,
    and complete.

    ## Original Opportunity
    **Title:** {title}
    **Type:** {type}
    **Problem:** {problem}
    **Proposed Solution:** {solution}
    **Files:** {files}

    ## Your Task
    1. Run `git diff {base_branch}...HEAD` to see exactly what changed.
    2. For each changed file, read the surrounding context to verify correctness.
    3. Check for:
       - Behavior changes that weren't intended
       - Missing edge cases (null checks, empty arrays, etc.)
       - Broken imports or references
       - Type errors or signature mismatches
       - Any code that was accidentally deleted or modified
       - Whether the fix actually solves the stated problem
    4. If the fix removes code, verify it's truly unused (grep for references).
    5. If the fix batches queries, verify the return shape is identical.

    ## CRITICAL: Output format
    After completing your review, your FINAL output MUST be ONLY a JSON object
    with these exact fields — no other text before or after:

    {{"verdict": "approve", "summary": "...", "issues": [], "suggestions": []}}

    - verdict: "approve", "request_changes", or "reject"
    - summary: 1-3 sentence review summary
    - issues: list of strings — specific problems found (empty list if approved)
    - suggestions: list of strings — optional improvements (won't block approval)

    Do NOT wrap the JSON in markdown code fences. Do NOT add any text outside the JSON.
""")


STRUCTURED_REVIEW_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are an independent code reviewer. A separate AI agent has made changes to fix
    an issue. Your job is to review the diff and determine if the fix is correct, safe,
    and complete.

    ## Original Opportunity
    **Title:** {title}
    **Type:** {type}
    **Problem:** {problem}
    **Proposed Solution:** {solution}
    **Files:** {files}

    ## Your Task
    1. Run `git diff {base_branch}...HEAD` to see exactly what changed.
    2. For each changed file, read the surrounding context to verify correctness.
    3. Evaluate the fix across these six dimensions:

    | Dimension       | What to check                                           |
    |-----------------|---------------------------------------------------------|
    | scope_hygiene   | The diff must contain ONLY changes described in the Problem/Solution above. FAIL if: (1) files outside the listed Files were modified, (2) build infrastructure (Makefile, CI config, package.json) was changed — that's test hacking, (3) code was deleted or added within in-scope files that is unrelated to the stated task (e.g. removing a function, import, or code block that the task never mentioned). Every hunk in the diff must be justified by the Problem/Solution. |
    | security        | No secrets, no injection, no auth bypass                |
    | correctness     | Logic is right, edge cases handled, types match         |
    | compatibility   | No breaking changes to public APIs or data formats      |
    | maintainability | Code is clear, well-structured, no unnecessary duplication |
    | observability   | Adequate logging, metrics, error messages               |

    ## CRITICAL: Output format
    After completing your review, your FINAL output MUST be ONLY a JSON object — no
    other text before or after:

    {{
      "dimensions": [
        {{"dimension": "scope_hygiene", "verdict": "pass", "finding": "..."}},
        {{"dimension": "security",      "verdict": "pass", "finding": "..."}},
        {{"dimension": "correctness",   "verdict": "pass", "finding": "..."}},
        {{"dimension": "compatibility", "verdict": "pass", "finding": "..."}},
        {{"dimension": "maintainability","verdict": "pass", "finding": "..."}},
        {{"dimension": "observability", "verdict": "pass", "finding": "..."}}
      ],
      "summary": "1-3 sentence overall summary",
      "suggestions": ["optional improvement 1", "..."]
    }}

    Each dimension verdict is "pass", "fail", or "skip".
    Do NOT wrap the JSON in markdown code fences.
""")


def review_fix(
    opportunity: Opportunity,
    repo_path: Path,
    base_branch: str = "main",
    model: str = "claude-opus-4-6",
) -> ReviewResult:
    """Run an independent review of the current branch's changes."""
    console.print(f"\n[bold magenta]Reviewing:[/bold magenta] {opportunity.title}")

    prompt = REVIEW_PROMPT_TEMPLATE.format(
        title=opportunity.title,
        type=opportunity.type,
        problem=opportunity.problem,
        solution=opportunity.solution,
        files=", ".join(opportunity.files),
        base_branch=base_branch,
    )

    # First try: use --output-format json to get structured output
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        "high",
        "--max-turns",
        "100",
        "--allowedTools",
        "Bash,Read,Grep,Glob",
        "--output-format",
        "json",
    ]

    console.print("[dim]  Running reviewer agent...[/dim]")
    result = subprocess.run(
        cmd,
        cwd=str(repo_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode != 0:
        return ReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary=f"Reviewer agent failed: {result.stderr[:200]}",
            issues=["Reviewer process failed — manual review needed"],
        )

    return parse_review(result.stdout)


def parse_review(raw_output: str) -> ReviewResult:
    """Parse the reviewer's output, handling multiple formats.

    Claude Code with --output-format json wraps the response in:
      {"type": "result", "result": "...the text..."}

    The text inside "result" contains the actual review JSON,
    possibly surrounded by conversational text from tool use.
    """
    text = raw_output.strip()

    # 1. Try to unwrap --output-format json envelope
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and "result" in envelope:
            text = envelope["result"]
    except json.JSONDecodeError:
        pass  # Not a JSON envelope, use raw text

    # 2. Try direct JSON parse
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict) and "verdict" in data:
            return ReviewResult(**data)
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. Find JSON object containing "verdict" anywhere in the text
    #    This handles cases where Claude wraps it in markdown or conversation
    json_pattern = re.compile(r'\{[^{}]*"verdict"\s*:\s*"[^"]+?"[^{}]*\}', re.DOTALL)
    match = json_pattern.search(text)
    if match:
        try:
            data = json.loads(match.group())
            return ReviewResult(**data)
        except (json.JSONDecodeError, ValueError):
            pass

    # 4. More aggressive: find the outermost { } that contains "verdict"
    for start_idx in range(len(text)):
        if text[start_idx] == "{":
            depth = 0
            for end_idx in range(start_idx, len(text)):
                if text[end_idx] == "{":
                    depth += 1
                elif text[end_idx] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start_idx : end_idx + 1]
                        if '"verdict"' in candidate:
                            try:
                                data = json.loads(candidate)
                                return ReviewResult(**data)
                            except (json.JSONDecodeError, ValueError):
                                pass
                        break

    # 5. Last resort: infer verdict from natural language
    text_lower = text.lower()
    if any(word in text_lower for word in ["lgtm", "looks good", "approve", "no issues"]):
        return ReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary=text[:300],
        )

    console.print("[yellow]  Could not parse reviewer JSON. Raw output (first 300 chars):[/yellow]")
    console.print(f"[dim]  {text[:300]}[/dim]")

    return ReviewResult(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="Could not parse structured review — see raw output",
        issues=[f"Raw reviewer output: {text[:500]}"],
    )


def analyze_diff_scope(
    repo_path: Path,
    base_branch: str,
) -> list[dict]:
    """Parse git diff and find net deletions — code removed but not relocated.

    Returns a list of deletion blocks:
      [{"file": "foo.py", "lines": ["import re", "..."], "context": "near line 120"}]

    These are code blocks that were deleted without appearing anywhere in the
    added lines. They may be legitimate (removing dead code as stated in the
    task) or scope violations (removing unrelated code the task didn't mention).
    """
    result = subprocess.run(
        ["git", "diff", f"origin/{base_branch}...HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return []

    # Collect all added lines (normalized) across the entire diff
    added_lines: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            if stripped:
                added_lines.add(stripped)

    # Walk diff again, collecting deleted lines per file
    current_file = ""
    current_hunk_start = 0
    deletion_blocks: list[dict] = []
    current_block: list[str] = []

    def _flush_block() -> None:
        # Filter: only keep blocks with at least one substantive line
        substantive = [line for line in current_block if len(line.strip()) > 3]
        if substantive:
            deletion_blocks.append(
                {
                    "file": current_file,
                    "lines": list(current_block),
                    "context": f"near line {current_hunk_start}",
                }
            )
        current_block.clear()

    for line in result.stdout.splitlines():
        if line.startswith("--- a/"):
            _flush_block()
            current_file = line[6:]
        elif line.startswith("@@ "):
            _flush_block()
            # Parse hunk header: @@ -120,20 +119,8 @@
            import re as _re

            m = _re.search(r"^@@ -(\d+)", line)
            current_hunk_start = int(m.group(1)) if m else 0
        elif line.startswith("-") and not line.startswith("---"):
            stripped = line[1:].strip()
            if stripped and stripped not in added_lines:
                current_block.append(stripped)
            else:
                _flush_block()
        else:
            _flush_block()

    _flush_block()
    return deletion_blocks


def format_scope_warnings(deletion_blocks: list[dict]) -> str:
    """Format deletion blocks into a warnings section for the review prompt."""
    if not deletion_blocks:
        return ""

    lines = [
        "\n## Scope Warnings (automated pre-check)",
        "The following code blocks were DELETED without being relocated elsewhere",
        "in the diff. For each block, verify the deletion is justified by the",
        "Problem/Solution above. If ANY deletion is unrelated to the stated task,",
        "you MUST fail scope_hygiene.\n",
    ]
    for i, block in enumerate(deletion_blocks, 1):
        lines.append(f"### Deletion {i}: `{block['file']}` ({block['context']})")
        lines.append("```")
        for code_line in block["lines"][:15]:  # cap display
            lines.append(code_line)
        if len(block["lines"]) > 15:
            lines.append(f"... ({len(block['lines']) - 15} more lines)")
        lines.append("```\n")

    return "\n".join(lines)


def structured_review_fix(
    opportunity: Opportunity,
    repo_path: Path,
    base_branch: str = "main",
    model: str = "claude-opus-4-6",
    verdict_rules: list[VerdictRule] | None = None,
) -> StructuredReviewResult:
    """Run a structured 6-dimension review of the current branch's changes."""
    console.print(f"\n[bold magenta]Structured review:[/bold magenta] {opportunity.title}")

    # Mechanical pre-check: find deleted code that wasn't relocated
    deletion_blocks = analyze_diff_scope(repo_path, base_branch)
    scope_warnings = format_scope_warnings(deletion_blocks)
    if deletion_blocks:
        console.print(
            f"[yellow]  Scope pre-check: {len(deletion_blocks)} net deletion(s) "
            f"found — reviewer must verify each is justified[/yellow]"
        )
        for i, block in enumerate(deletion_blocks, 1):
            preview = block["lines"][0] if block["lines"] else ""
            if len(block["lines"]) > 1:
                preview += f"  (+{len(block['lines']) - 1} more lines)"
            console.print(f"    [dim]{i}. {block['file']} ({block['context']}): {preview}[/dim]")
    else:
        console.print("[dim]  Scope pre-check: no net deletions found[/dim]")

    prompt = STRUCTURED_REVIEW_PROMPT_TEMPLATE.format(
        title=opportunity.title,
        type=opportunity.type,
        problem=opportunity.problem,
        solution=opportunity.solution,
        files=", ".join(opportunity.files),
        base_branch=base_branch,
    )

    if scope_warnings:
        prompt += scope_warnings

    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        "high",
        "--max-turns",
        "100",
        "--allowedTools",
        "Bash,Read,Grep,Glob",
        "--output-format",
        "json",
    ]

    console.print("[dim]  Running structured reviewer agent...[/dim]")
    result = subprocess.run(
        cmd,
        cwd=str(repo_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )

    # Check for error envelopes — can happen even with returncode 0
    try:
        envelope = json.loads(result.stdout)
        if isinstance(envelope, dict) and "result" not in envelope:
            is_error = envelope.get("is_error", False)
            subtype = envelope.get("subtype", "")
            errors = envelope.get("errors", [])

            if subtype == "error_max_turns":
                console.print("[yellow]  Reviewer ran out of turns with no output[/yellow]")
                return StructuredReviewResult(
                    verdict=ReviewVerdict.REQUEST_CHANGES,
                    summary="Reviewer ran out of turns before producing structured output",
                )

            if is_error or errors:
                error_detail = str(errors[0])[:200] if errors else f"exit {result.returncode}"
                console.print(f"[yellow]  Reviewer agent errored: {error_detail}[/yellow]")
                return StructuredReviewResult(
                    verdict=ReviewVerdict.REQUEST_CHANGES,
                    summary=f"Reviewer agent errored: {error_detail}",
                )
    except (json.JSONDecodeError, ValueError):
        pass

    if result.returncode != 0:
        return StructuredReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary=f"Structured reviewer agent failed: {result.stderr[:200]}",
        )

    return parse_structured_review(result.stdout, rules=verdict_rules)


# ---------------------------------------------------------------------------
# Review fix-loop helpers
# ---------------------------------------------------------------------------

REVIEW_FIX_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are fixing code review failures in a PR branch.

    ## Context
    **Title:** {title}
    **Problem:** {problem}
    **Solution:** {solution}
    **Files:** {files}

    ## Failing review dimensions
    {failures}

    ## Instructions
    1. Run `git diff {base_branch}...HEAD` to see the current changes.
    2. Fix ONLY the issues identified in the failing dimensions above.
    3. Do NOT make any unrelated changes.
    4. Do NOT add TODOs, comments about future improvements, or debug logs.
    5. Review your diff before finishing.
""")


def build_review_fix_prompt(
    opportunity: Opportunity,
    failed_dimensions: list[DimensionResult],
    base_branch: str = "main",
) -> str:
    """Build a prompt for the fixer agent from failed review dimensions."""
    failures = "\n".join(f"- **{d.dimension}**: {d.finding}" for d in failed_dimensions)
    return REVIEW_FIX_PROMPT_TEMPLATE.format(
        title=opportunity.title,
        problem=opportunity.problem,
        solution=opportunity.solution,
        files=", ".join(opportunity.files),
        failures=failures,
        base_branch=base_branch,
    )


def run_fixer_agent(worktree_path: Path, prompt: str, model: str = "sonnet") -> None:
    """Run a Claude fixer agent in the worktree."""
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        "high",
        "--max-turns",
        "100",
        "--allowedTools",
        "Bash,Read,Grep,Glob,Write,Edit",
        "--permission-mode",
        "bypassPermissions",
    ]
    console.print("[dim]  Running fixer agent...[/dim]")
    subprocess.run(
        cmd,
        cwd=str(worktree_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )


def check_fix_in_scope(
    changed_files: list[str],
    original_files: list[str],
) -> tuple[bool, str]:
    """Check if fixer changes stay within scope of the original opportunity.

    Doc-only expansions (.md, .txt, .rst) are allowed — updating stale docs
    that reference changed files is in scope. Build infrastructure changes
    (Makefile, CI config, package.json) are never allowed — that's test hacking.
    New source files outside the original scope are rejected.
    """
    from pathlib import Path as _Path

    # Build infrastructure files that should never be modified by a fix
    build_infra = {
        "Makefile",
        "makefile",
        "package.json",
        "pyproject.toml",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
    build_infra_dirs = {".github", ".gitlab-ci", ".circleci"}

    original_set = set(original_files)
    new_files = [f for f in changed_files if f not in original_set]

    if not new_files:
        return True, "All changes within original scope"

    # Reject build infrastructure changes
    infra_changes = []
    for f in new_files:
        name = _Path(f).name
        parts = _Path(f).parts
        if name in build_infra or any(d in parts for d in build_infra_dirs):
            infra_changes.append(f)

    if infra_changes:
        return False, f"Fix modified build infrastructure: {', '.join(infra_changes)}"

    doc_extensions = {".md", ".txt", ".rst"}
    non_doc_new = [f for f in new_files if _Path(f).suffix not in doc_extensions]

    if non_doc_new:
        return False, f"Fix touched source files outside scope: {', '.join(non_doc_new)}"

    return True, f"Scope expanded to include docs: {', '.join(new_files)}"
