"""Configuration for autopr."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_PROTECTED_FILES: list[str] = [
    "Makefile",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "docker-compose.yml",
    "Dockerfile",
    ".github/**",
]


class HealthCheckConfig(BaseModel):
    """Health check commands to run after each fix."""

    commands: list[str] = Field(default_factory=lambda: ["make check", "make test"])
    timeout_seconds: int = 300
    max_fix_attempts: int = 3


class RepoConfig(BaseModel):
    """Configuration for a target repository."""

    path: Path
    remote_url: str = ""
    base_branch: str = "main"
    branch_prefix: str = "autopr/"
    # Directory where git worktrees are created — keeps your main checkout clean.
    # Defaults to <repo>-autopr/ next to the repo directory.
    worktree_dir: Path | None = None
    health_checks: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    # Paths to scan (relative to repo root). Empty = scan everything.
    include_paths: list[str] = Field(default_factory=list)
    # Paths to skip
    exclude_paths: list[str] = Field(
        default_factory=lambda: [
            "node_modules",
            ".git",
            "__pycache__",
            ".venv",
            "dist",
            "build",
            ".next",
        ]
    )
    # Opportunity types to look for. Empty = all types.
    opportunity_types: list[str] = Field(default_factory=list)
    # Minimum confidence to attempt a fix
    min_confidence: float = 0.8
    # Files that autopr must never modify. Checked before every commit —
    # any changes to these are silently reverted via git checkout.
    protected_files: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_FILES))


class VerdictRule(BaseModel):
    """A single rule that maps a dimension to an action on failure."""

    dimension: str = Field(description="Review dimension name, e.g. 'correctness'")
    on_fail: str = Field(
        default="reject", description="What to do on fail: 'reject', 'request_changes', or 'warn'"
    )


class GatingConfig(BaseModel):
    """Configuration for structured review gating."""

    enable_screener: bool = True
    screener_model: str = ""
    screener_max_turns: int = 3
    verdict_rules: list[VerdictRule] = Field(
        default_factory=lambda: [
            VerdictRule(dimension="scope_hygiene", on_fail="reject"),
            VerdictRule(dimension="security", on_fail="reject"),
            VerdictRule(dimension="correctness", on_fail="reject"),
            VerdictRule(dimension="compatibility", on_fail="reject"),
            VerdictRule(dimension="maintainability", on_fail="request_changes"),
            VerdictRule(dimension="observability", on_fail="warn"),
        ]
    )
    enable_material_review: bool = True
    review_model: str = ""  # empty = codex default; set to "claude-opus-4-6" to use Claude
    codex_max_fix_iterations: int = 3


class ResearchDeployConfig(BaseModel):
    """Configuration for preview deployments used by research mode."""

    command: str = "vercel deploy --yes"
    url_regex: str = r"https://[^\s\"']+"
    ready_timeout_seconds: int = 300


class ResearchLighthouseConfig(BaseModel):
    """Configuration for Lighthouse benchmarking."""

    command: str = (
        "lighthouse {url} --preset=perf --quiet "
        '--chrome-flags="--headless" '
        "--output=json --output-path=stdout"
    )
    warmup_runs: int = 1
    measured_runs: int = 3
    auth_cookie: str = ""  # cookie string (auto-generated from credentials below)
    auth_email: str = Field(default_factory=lambda: os.environ.get("AUTOPR_AUTH_EMAIL", ""))
    auth_password: str = Field(default_factory=lambda: os.environ.get("AUTOPR_AUTH_PASSWORD", ""))
    auth_supabase_url: str = Field(
        default_factory=lambda: os.environ.get("AUTOPR_AUTH_SUPABASE_URL", "")
    )
    auth_supabase_key: str = Field(
        default_factory=lambda: os.environ.get("AUTOPR_AUTH_SUPABASE_KEY", "")
    )


class ResearchTTFTProbeConfig(BaseModel):
    """Optional separate TTFT probe command."""

    command: str = ""


class ResearchPrimaryMetricConfig(BaseModel):
    """Primary optimization target for research mode."""

    name: str = "performance_score"
    direction: str = "maximize"
    min_improvement_abs: float = 1.0


class ResearchGuardrailMetricConfig(BaseModel):
    """Guardrail metric that must not regress beyond a threshold."""

    name: str
    direction: str = "minimize"
    max_regression_pct: float | None = None
    max_regression_abs: float | None = None


class ResearchScoringConfig(BaseModel):
    """Scoring rules for iterative benchmark comparisons."""

    primary_metric: ResearchPrimaryMetricConfig = Field(default_factory=ResearchPrimaryMetricConfig)
    guardrails: list[ResearchGuardrailMetricConfig] = Field(
        default_factory=lambda: [
            ResearchGuardrailMetricConfig(
                name="lcp_ms", direction="minimize", max_regression_pct=5.0
            ),
            ResearchGuardrailMetricConfig(
                name="fcp_ms", direction="minimize", max_regression_pct=5.0
            ),
            ResearchGuardrailMetricConfig(
                name="speed_index_ms", direction="minimize", max_regression_pct=5.0
            ),
        ]
    )


def _default_research_branch_name() -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"autopr/research-{timestamp}"


class ResearchConfig(BaseModel):
    """Configuration for the iterative research mode."""

    program_file: str = ""
    editable_paths: list[str] = Field(default_factory=list)
    target_pages: list[str] = Field(default_factory=lambda: ["/"])
    target_page: str = ""  # single target page — skips multi-page discovery
    baseline_url: str = ""  # use existing deployment as baseline — skips baseline deploy
    pre_deploy_check: str = ""  # e.g. "pnpm run build" — run before deploying to catch build breaks
    pre_deploy_check_cwd: str = ""  # subdirectory for pre_deploy_check (e.g. "frontend")
    branch_name: str = Field(default_factory=_default_research_branch_name)
    max_iterations: int = 20
    history_window: int = 10
    consecutive_failures_limit: int = 5
    deploy: ResearchDeployConfig = Field(default_factory=ResearchDeployConfig)
    lighthouse: ResearchLighthouseConfig = Field(default_factory=ResearchLighthouseConfig)
    ttft_probe: ResearchTTFTProbeConfig = Field(default_factory=ResearchTTFTProbeConfig)
    scoring: ResearchScoringConfig = Field(default_factory=ResearchScoringConfig)


class AutomationConfig(BaseModel):
    """Configuration for event-driven PR automation (react / reconcile)."""

    managed_branch_prefixes: list[str] = Field(default_factory=lambda: ["autopr/"])
    managed_labels: list[str] = Field(default_factory=lambda: ["autopr-managed"])
    tool_repo: str = ""
    tool_ref: str = "main"
    ci_workflow_names: list[str] = Field(default_factory=lambda: ["CI"])
    allow_forks: bool = False
    merge_update_strategy: str = "rebase"  # "rebase" or "merge"
    auto_resolve_conflicts: bool = True
    comment_command: str = "/autopr fix"
    max_event_fix_attempts: int = 2
    max_cumulative_workflow_runs: int = 5
    max_conflict_retries: int = 2
    reconcile_behind_prs: bool = True
    bot_name: str = "autopr[bot]"
    bot_email: str = "autopr[bot]@users.noreply.github.com"


class AutoPRConfig(BaseModel):
    """Top-level configuration."""

    repo: RepoConfig
    gating: GatingConfig = Field(default_factory=GatingConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    max_concurrent_prs: int = 3
    dry_run: bool = False
    push_to_remote: bool = True
    create_github_pr: bool = True
    # Claude model to use (via Claude Code CLI)
    model: str = "claude-opus-4-6"
    reviewer_model: str = "claude-opus-4-6"
    # Playbook pattern IDs to run. Empty = all patterns.
    patterns: list[str] = Field(default_factory=list)
    verbose: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> AutoPRConfig:
        """Load configuration from YAML file."""
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def to_yaml(self, path: Path) -> None:
        """Save configuration to YAML file."""
        import yaml

        with open(path, "w") as f:
            yaml.dump(self.model_dump(mode="json"), f, default_flow_style=False)
