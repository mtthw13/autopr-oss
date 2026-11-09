# Architecture

autopr is an autonomous PR generation system that scans a repository for low-hanging-fruit improvements (dead code, unused files, batchable queries, N+1 patterns), generates minimal fixes in isolated git worktrees, validates them with health checks and an independent reviewer, and creates GitHub PRs — all without touching your main checkout.

## Pipeline

```
                         your main checkout (never modified)
                                      │
                                      ▼
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ ANALYZE  │───▸│  SCREEN  │───▸│   FIX    │───▸│  HEALTH  │───▸│  REVIEW  │───▸│    PR    │
│          │    │          │    │          │    │  CHECK   │    │          │    │          │
│ playbook │    │ 4-dim    │    │ worktree │    │ shell    │    │ separate │    │ gh pr    │
│ patterns │    │ eval     │    │ + Claude │    │ cmds +   │    │ Claude   │    │ create   │
│ parallel │    │ (opt.)   │    │ Code     │    │ auto-fix │    │ instance │    │          │
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
      │               │               │               │               │               │
  Opportunity    pass/fail         branch +        pass/fail      approve/         PR URL
  JSON list                        commit                       reject/RC
```

Each opportunity is processed sequentially. Failed opportunities are logged and skipped; the pipeline continues with the next one.

## Components

### `cli.py` — Entry point

Click-based CLI with three commands:

| Command | Purpose |
|---------|---------|
| `autopr run <repo>` | Full pipeline: scan, fix, health check, review, PR |
| `autopr scan <repo>` | Analysis only — outputs JSON manifest |
| `autopr init <repo>` | Generates default `autopr.yaml` config |

Assembles `AutoPRConfig` from YAML file or CLI flags, detects remote URL and health check commands from Makefile, and delegates to the orchestrator.

### `orchestrator.py` — Pipeline coordinator

Owns the end-to-end flow for each opportunity:

1. Loads opportunities (from manifest or analyzer)
2. Filters out opportunities with existing open/merged PRs via `gh pr list`
3. For each opportunity: fix → health check → review → PR
4. Cleans up worktrees and prints summary

Key functions:
- `run_pipeline()` — main entry point
- `process_single_opportunity()` — single opportunity lifecycle
- `filter_already_handled()` — deduplicates against GitHub PRs, falls back to branch names

### `analyzer.py` — Pattern scanner

Discovers improvement opportunities by running each playbook pattern as a separate Claude Code CLI subprocess.

- `load_playbook()` — loads `.md` pattern files from a directory, parses YAML frontmatter
- `scan_for_pattern()` — builds prompt from pattern template, runs Claude Code, parses JSON output
- `analyze_repo()` — runs all patterns in parallel via `ThreadPoolExecutor`, deduplicates by opportunity ID, sorts by confidence descending
- `analyze_from_manifest()` — loads pre-written JSON instead of scanning

Pattern scans run in parallel (default 4 workers). Each scan gets its own turn budget to prevent runaway searches.

### `fixer.py` — Fix generator

Generates fixes in isolated git worktrees so your main checkout is never modified.

Worktree lifecycle:
1. `create_worktree()` — creates `<repo>-autopr/<branch-name>/` with a fresh branch from base
2. Claude Code runs inside the worktree with `--permission-mode bypassPermissions`
3. `commit_changes()` — stages and commits all changes
4. Worktree is cleaned up after push (or immediately on failure)

The fix prompt instructs Claude to make minimal changes, avoid adding suppressions (`noqa`, `type: ignore`), and verify correctness before finishing.

### `health.py` — Health check runner

Runs configured shell commands (e.g., `make backend-check-fix`, `make backend-test`) inside the worktree after a fix is applied.

On failure:
1. Sends the error output to a Claude Code instance to attempt an auto-fix
2. Re-runs the failing command
3. Repeats up to `max_fix_attempts` times (default 3)
4. Stops early if a command still fails after exhausting retries

### `reviewer.py` — Independent reviewer

A separate Claude Code instance (read-only tools: Bash, Read, Grep, Glob) reviews the diff between the fix branch and base.

Checks for: unintended behavior changes, broken imports, type mismatches, incomplete fixes, and whether removed code is truly unused.

`parse_review()` handles multiple output formats with a fallback chain:
1. Unwrap `--output-format json` envelope
2. Direct JSON parse
3. Regex search for JSON containing `"verdict"`
4. Brace-matching for nested JSON
5. Natural language inference (last resort)

### `pr_creator.py` — PR creation

Pushes the branch and creates a GitHub PR via `gh pr create`.

PR description is structured: Problem → Estimated Impact → Solution → Files Changed → Verification checklist → AI Review verdict and summary.

### `models.py` — Data models

Pydantic v2 models:

| Model | Purpose |
|-------|---------|
| `Opportunity` | A single improvement finding (id, title, type, severity, files, problem, solution, confidence) |
| `ReviewResult` | Reviewer output (verdict, summary, issues, suggestions) |
| `HealthCheckResult` | Single command result (passed, command, stdout/stderr, duration, attempt) |
| `PRResult` | End-to-end result for one opportunity (opportunity, branch, review, health checks, PR URL, status) |
| `RunReport` | Summary of an entire autopr run (all PRResults, timestamps, succeeded/failed counts) |

Enums: `Severity` (trivial/low/medium/high), `OpportunityType` (7 well-known types), `ReviewVerdict` (approve/request_changes/reject).

### `config.py` — Configuration

```
AutoPRConfig
├── repo: RepoConfig
│   ├── path, remote_url, base_branch, branch_prefix
│   ├── worktree_dir (default: <repo>-autopr/)
│   ├── health_checks: HealthCheckConfig
│   │   ├── commands: list[str]
│   │   ├── timeout_seconds: 300
│   │   └── max_fix_attempts: 3
│   ├── include_paths, exclude_paths
│   ├── opportunity_types
│   └── min_confidence: 0.8
├── max_concurrent_prs: 3
├── dry_run, push_to_remote, create_github_pr
├── model: "sonnet"
├── reviewer_model: "sonnet"
└── verbose
```

Supports YAML serialization via `from_yaml()` / `to_yaml()`.

## Playbook

Patterns live in `playbook/` as markdown files with YAML frontmatter:

```yaml
---
id: batch_queries          # used as opportunity.type
title: Batchable sequential queries
severity: high
complexity: low
confidence: 0.92
max_turns: 15              # Claude Code turn budget for scanning
---

# Pattern title
## Detection strategy
## Verification
## Output rules
```

### Built-in patterns

| ID | Severity | What it finds |
|----|----------|---------------|
| `batch_queries` | high | Sequential calls that could be batched into one |
| `n_plus_one` | high | DB/API calls inside loops |
| `unused_file` | low | Modules never imported (50+ lines) |
| `commented_code` | low | Commented-out code blocks (3+ lines) |
| `duplicate_code` | medium | Near-identical logic across files |
| `redundant_lookup` | medium | Same data fetched multiple times |

### Disabled patterns

Patterns in `playbook/nah/` are skipped by `load_playbook()` (it only globs `*.md` in the top-level directory). Currently: `dead_code`.

## Data flow

```
PlaybookPattern ──▸ SCAN_PROMPT_TEMPLATE ──▸ Claude Code CLI ──▸ JSON
                                                                  │
                                                                  ▼
                                                           list[Opportunity]
                                                                  │
                                          filter_already_handled()│
                                                                  ▼
                                                          for each Opportunity:
                                                                  │
                    ┌─────────────────────────────────────────────┤
                    ▼                                             │
              run_fix() ──▸ (branch_name, worktree_path)         │
                    │                                             │
                    ▼                                             │
          run_health_checks() ──▸ list[HealthCheckResult]        │
                    │                                             │
                    ▼                                             │
            review_fix() ──▸ ReviewResult                        │
                    │                                             │
                    ▼                                             │
        push_and_create_pr() ──▸ pr_url                          │
                    │                                             │
                    ▼                                             │
                PRResult ─────────────────────────────────────────▸ RunReport
```

`RunReport` is serialized to `.autopr-report.json` at the end of a run.

## Key design decisions

**Worktree isolation.** Every fix runs in `<repo>-autopr/<branch>/`. Your main checkout is never modified, so you can keep working while autopr generates PRs. Failed worktrees are cleaned up immediately; successful ones persist until push completes.

**Claude Code CLI as subprocess.** Each stage (scan, fix, health-fix, review) spawns a separate `claude` process via `subprocess.run()`. This provides natural isolation between stages — the fixer can't influence the reviewer's judgment.

**Parallel scanning, sequential fixing.** Pattern scans run in parallel (ThreadPoolExecutor, default 4 workers) since they're read-only. Fixes run sequentially to avoid worktree conflicts.

**Fail-open screener.** If the screener process fails (timeout, parse error), the opportunity proceeds by default. Availability over strictness — a false negative (missed opportunity) is cheaper than a false positive (blocked good fix).

**Multi-strategy JSON parsing.** Both `reviewer.py` and `analyzer.py` have robust fallback chains for parsing Claude's output, since LLM output format is inherently unreliable. The reviewer falls back through 5 strategies before giving up.

**Turn budgets.** Each Claude Code invocation gets a capped turn count (scanner: per-pattern, fixer: 10, reviewer: 5, health-fixer: 10) to prevent runaway cost.

## Dependencies

**Runtime:** pydantic (v2), pyyaml, click, gitpython, rich

**Dev:** pytest, ruff

**External:** Claude Code CLI (`claude`), GitHub CLI (`gh`), git
