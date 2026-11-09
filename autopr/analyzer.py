"""Codebase analyzer — finds opportunities by running each playbook pattern as a focused scan."""

from __future__ import annotations

import json
import subprocess
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from rich.console import Console

from autopr.config import RepoConfig
from autopr.models import Opportunity

console = Console()

# ── Playbook loading ─────────────────────────────────────────────────────────


class PlaybookPattern:
    """A single pattern loaded from a playbook .md file."""

    def __init__(self, path: Path) -> None:
        raw = path.read_text()
        frontmatter, body = _split_frontmatter(raw)

        self.source_file = path
        self.id: str = frontmatter["id"]
        self.title: str = frontmatter["title"]
        self.severity: str = frontmatter.get("severity", "low")
        self.complexity: str = frontmatter.get("complexity", "low")
        self.confidence: float = float(frontmatter.get("confidence", 0.9))
        self.max_turns: int = int(frontmatter.get("max_turns", 5))
        self.body: str = body.strip()

    def __repr__(self) -> str:
        return f"PlaybookPattern({self.id!r})"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file with YAML frontmatter into (metadata, body)."""
    text = text.strip()
    if not text.startswith("---"):
        return {}, text

    end = text.index("---", 3)
    fm_text = text[3:end].strip()
    body = text[end + 3 :].strip()
    return yaml.safe_load(fm_text), body


def load_playbook(playbook_dir: Path) -> list[PlaybookPattern]:
    """Load all .md pattern files from the playbook directory."""
    if not playbook_dir.is_dir():
        raise FileNotFoundError(f"Playbook directory not found: {playbook_dir}")

    patterns = []
    for md_file in sorted(playbook_dir.rglob("*.md")):
        try:
            patterns.append(PlaybookPattern(md_file))
        except Exception as e:
            console.print(f"[yellow]Warning: skipping {md_file.name}: {e}[/yellow]")

    return patterns


# ── Per-pattern scanning ─────────────────────────────────────────────────────

SCAN_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are an expert code reviewer scanning a repository for a SPECIFIC type of issue.

    ## Repository
    Path: {repo_path}
    {include_hint}

    ## Pattern: {pattern_title}

    {pattern_body}

    ## Output format

    For each instance you find, output a JSON object with these exact fields:
    - id: unique kebab-case slug (e.g. "remove-unused-utils-helper")
    - title: short PR title (imperative mood, <72 chars)
    - type: "{pattern_id}"
    - severity: "{severity}"
    - files: list of relative file paths to modify
    - line_ranges: dict mapping file path to "startLine-endLine" hint (empty dict if not applicable)
    - problem: 1-3 sentences describing this specific instance
    - solution: 1-3 sentences describing the fix for this specific instance
    - estimated_impact: performance or quality impact (1 sentence)
    - complexity: "{complexity}"
    - autonomous_confidence: float 0.0-1.0 (your confidence this specific instance can be auto-fixed safely)

    ## Rules
    - Only report instances with autonomous_confidence >= {min_confidence}.
    - Only report pure refactors with NO behavior change, or dead code removal.
    - Do NOT suggest changes that require new dependencies or schema changes.
    - If you find ZERO instances, output an empty JSON array: []

    ## CRITICAL: Turn budget
    You have a LIMITED number of tool-call turns. Use them efficiently:
    1. Start with broad searches (grep/glob) to identify candidate files — don't read files one by one.
    2. Only read specific files to confirm a candidate is a real issue.
    3. STOP SEARCHING and OUTPUT YOUR RESULTS well before you run out of turns.
       It is MUCH better to output 2-3 confirmed findings than to exhaustively search
       and hit the turn limit with no output.
    4. Your FINAL message MUST be ONLY the JSON array. No markdown fences, no commentary.

    Output ONLY a JSON array of opportunity objects. No markdown, no commentary.
""")


def scan_for_pattern(
    pattern: PlaybookPattern,
    config: RepoConfig,
    model: str = "claude-opus-4-6",
) -> list[Opportunity]:
    """Run a focused scan for a single playbook pattern."""
    include_hint = ""
    if config.include_paths:
        paths = ", ".join(config.include_paths)
        include_hint = f"Focus your analysis on these paths: {paths}"

    prompt = SCAN_PROMPT_TEMPLATE.format(
        repo_path=config.path,
        include_hint=include_hint,
        pattern_title=pattern.title,
        pattern_body=pattern.body,
        pattern_id=pattern.id,
        severity=pattern.severity,
        complexity=pattern.complexity,
        min_confidence=config.min_confidence,
    )

    console.print(
        f"  [dim]Scanning for:[/dim] [cyan]{pattern.title}[/cyan] "
        f"[dim](max {pattern.max_turns} turns)[/dim]"
    )

    raw = _run_claude_code(
        prompt,
        config.path,
        model=model,
        max_turns=pattern.max_turns,
    )
    opportunities = _parse_opportunities(raw)

    # Filter by confidence
    filtered = [o for o in opportunities if o.autonomous_confidence >= config.min_confidence]

    if filtered:
        console.print(f"    [green]→ Found {len(filtered)} instance(s)[/green]")
    else:
        console.print("    [dim]→ None found[/dim]")

    return filtered


# ── Main entry points ────────────────────────────────────────────────────────


def analyze_repo(
    config: RepoConfig,
    model: str = "claude-opus-4-6",
    playbook_dir: Path | None = None,
    pattern_ids: list[str] | None = None,
    max_workers: int = 4,
) -> list[Opportunity]:
    """Analyze a repo by running each playbook pattern as a focused scan.

    Pattern scans run in parallel (subprocess-bound, not CPU-bound) to
    cut wall-clock time from N × single-scan to roughly ceil(N / workers).

    Args:
        config: Repository configuration.
        model: Claude model to use for scanning.
        playbook_dir: Path to playbook directory. None = built-in playbook/.
        pattern_ids: If set, only run these pattern IDs (e.g. ["unused_file", "n_plus_one"]).
        max_workers: Max concurrent Claude Code scanner processes.
    """
    if playbook_dir is None:
        playbook_dir = Path(__file__).parent.parent / "playbook"

    console.print(f"\n[bold blue]Analyzing repository:[/bold blue] {config.path}")

    patterns = load_playbook(playbook_dir)
    if not patterns:
        console.print("[yellow]No playbook patterns found. Nothing to scan.[/yellow]")
        return []

    # Filter to requested patterns
    if pattern_ids:
        requested = set(pattern_ids)
        patterns = [p for p in patterns if p.id in requested]
        unknown = requested - {p.id for p in patterns}
        if unknown:
            console.print(
                f"[yellow]Warning: unknown pattern IDs: {', '.join(sorted(unknown))}[/yellow]"
            )
        if not patterns:
            console.print("[yellow]No matching patterns. Nothing to scan.[/yellow]")
            return []
    else:
        # Exclude patterns in the "nah" folder unless explicitly requested
        excluded = [p for p in patterns if "nah" in p.source_file.parts]
        if excluded:
            patterns = [p for p in patterns if p not in excluded]
            console.print(
                f"[dim]Skipped {len(excluded)} pattern(s) in nah/ "
                f"(use --patterns to include them)[/dim]"
            )

    console.print(f"[dim]Loaded {len(patterns)} patterns from {playbook_dir}[/dim]")
    workers = min(max_workers, len(patterns))
    console.print(f"[dim]Scanning with {workers} parallel workers[/dim]\n")

    all_opportunities: list[Opportunity] = []
    seen_ids: set[str] = set()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_pattern = {
            pool.submit(scan_for_pattern, pattern, config, model): pattern for pattern in patterns
        }

        for future in as_completed(future_to_pattern):
            pattern = future_to_pattern[future]
            try:
                found = future.result()
                for opp in found:
                    if opp.id not in seen_ids:
                        seen_ids.add(opp.id)
                        all_opportunities.append(opp)
            except Exception as e:
                console.print(f"    [red]Error scanning for {pattern.title}: {e}[/red]")

    console.print(
        f"\n[green]Found {len(all_opportunities)} total opportunities "
        f"across {len(patterns)} patterns[/green]"
    )

    return sorted(
        all_opportunities,
        key=lambda o: (-o.autonomous_confidence, o.complexity),
    )


def analyze_from_manifest(manifest_path: Path, config: RepoConfig) -> list[Opportunity]:
    """Load opportunities from a pre-written JSON manifest instead of scanning.

    This is useful when you already know what to fix (e.g. from a prior audit)
    and want to skip the analysis step.
    """
    console.print(f"\n[bold blue]Loading manifest:[/bold blue] {manifest_path}")
    with open(manifest_path) as f:
        data = json.load(f)

    if not isinstance(data, list):
        data = [data]

    opportunities = [Opportunity(**item) for item in data]
    filtered = [o for o in opportunities if o.autonomous_confidence >= config.min_confidence]

    console.print(f"[green]Loaded {len(filtered)} opportunities from manifest[/green]")
    return filtered


# ── Internal helpers ─────────────────────────────────────────────────────────


def _run_claude_code(
    prompt: str,
    repo_path: Path,
    model: str = "claude-opus-4-6",
    max_turns: int = 5,
) -> str:
    """Execute Claude Code CLI and return the raw text output."""
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        "high",
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
    ]
    result = subprocess.run(
        cmd,
        cwd=str(repo_path),
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        if result.stdout.strip():
            # Claude hit max turns or errored but may have partial output
            console.print(
                f"[yellow]  Claude exited with code {result.returncode}, "
                f"attempting to parse partial output[/yellow]"
            )
            return result.stdout.strip()
        raise RuntimeError(f"Claude Code failed (exit {result.returncode}):\n{result.stderr}")
    return result.stdout.strip()


def _parse_opportunities(raw_output: str) -> list[Opportunity]:
    """Parse Claude's JSON output into Opportunity models.

    Never raises — returns [] on unparseable input with a warning.
    """
    from autopr.reviewer import extract_json_array_from_claude_output

    items = extract_json_array_from_claude_output(raw_output)
    if items is None:
        if raw_output.strip():
            console.print(
                f"[yellow]  Could not parse opportunities from output "
                f"(first 200 chars): {raw_output.strip()[:200]}[/yellow]"
            )
        return []

    opportunities: list[Opportunity] = []
    for i, item in enumerate(items):
        try:
            opportunities.append(Opportunity(**item))
        except Exception as e:
            console.print(f"[yellow]  Skipping malformed opportunity at index {i}: {e}[/yellow]")

    return opportunities
