"""Data models for the autopr system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """Severity levels for opportunities."""

    TRIVIAL = "trivial"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OpportunityType(StrEnum):
    """Well-known types of improvement opportunities.

    Playbook patterns can use these or define custom type strings.
    """

    DEAD_CODE = "dead_code"
    UNUSED_FILE = "unused_file"
    N_PLUS_ONE = "n_plus_one"
    BATCH_QUERIES = "batch_queries"
    REDUNDANT_LOOKUP = "redundant_lookup"
    DUPLICATE_CODE = "duplicate_code"
    COMMENTED_CODE = "commented_code"


class Opportunity(BaseModel):
    """A single low-hanging-fruit fix opportunity found in the codebase."""

    id: str = Field(description="Unique slug, e.g. 'batch-entity-queries'")
    title: str = Field(description="Short PR title")
    type: str = Field(
        description="Pattern type — matches a playbook pattern id or OpportunityType value"
    )
    severity: Severity
    files: list[str] = Field(description="Relative paths of files to touch")
    line_ranges: dict[str, str] = Field(
        default_factory=dict, description="file_path -> 'start-end' line range hint"
    )
    problem: str = Field(description="What's wrong (1-3 sentences)")
    solution: str = Field(description="How to fix it (1-3 sentences)")
    estimated_impact: str = Field(default="", description="Performance or quality impact")
    complexity: str = Field(default="low", description="low / medium / high")
    autonomous_confidence: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="How confident we are this can be fixed autonomously",
    )


class ReviewVerdict(StrEnum):
    """Verdicts from code review."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class DimensionVerdict(StrEnum):
    """Per-dimension verdict used in structured reviews."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class ReviewResult(BaseModel):
    """Result from the independent reviewer agent."""

    verdict: ReviewVerdict
    summary: str
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class DimensionResult(BaseModel):
    """Result for a single review dimension (e.g. correctness, style)."""

    dimension: str
    verdict: DimensionVerdict
    finding: str = ""


class ScreenResult(BaseModel):
    """Quick pre-screen result deciding whether a full review is needed."""

    proceed: bool
    dimensions: list[DimensionResult] = Field(default_factory=list)
    summary: str = ""
    opportunity_id: str = ""


class StructuredReviewResult(BaseModel):
    """Rich review result that breaks down feedback by dimension."""

    verdict: ReviewVerdict
    dimensions: list[DimensionResult] = Field(default_factory=list)
    summary: str = ""
    suggestions: list[str] = Field(default_factory=list)

    def to_review_result(self) -> ReviewResult:
        """Flatten to a plain ReviewResult."""
        failed_dims = [d for d in self.dimensions if d.verdict == DimensionVerdict.FAIL]
        issues = [d.finding for d in failed_dims]
        return ReviewResult(
            verdict=self.verdict,
            summary=self.summary,
            issues=issues,
            suggestions=self.suggestions,
        )


class HealthCheckResult(BaseModel):
    """Result of running health checks."""

    passed: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    attempt: int = 1


class MetricSnapshot(BaseModel):
    """Aggregated benchmark metrics for a single deployed candidate."""

    metrics: dict[str, float] = Field(default_factory=dict)
    sample_count: int = 1
    aggregation: str = "median"
    source_url: str = ""

    def value(self, name: str) -> float | None:
        """Return a metric value if present."""
        return self.metrics.get(name)


class ExperimentRecord(BaseModel):
    """One iteration in a research loop."""

    iteration: int
    commit_sha: str = ""
    status: str = "pending"  # pending | accepted | rejected | failed | skipped
    deploy_url: str = ""
    metrics: MetricSnapshot | None = None
    rejection_reason: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class CodexReviewResult(BaseModel):
    """Result from a Codex material-risk review."""

    approve: bool
    risks: list[dict] = Field(
        default_factory=list
    )  # {"category": "AUTH|ARCH|DATA|UX", "detail": "..."}
    summary: str = ""
    review_type: str = ""  # "scope" or "diff"
    iterations: int = 1


class PreparedCandidate(BaseModel):
    """Result of preparing a candidate fix (screen → fix → health checks).

    Shared by both `autopr run` and `autopr research` pipelines.
    """

    opportunity: Opportunity
    branch_name: str
    worktree_path: str  # stored as str for serialization
    changed_files: list[str] = Field(default_factory=list)
    health_checks: list[HealthCheckResult] = Field(default_factory=list)
    screen: ScreenResult | None = None
    codex_scope_review: CodexReviewResult | None = None
    status: str = "ready"  # ready | skipped | failed
    error: str = ""


class FinalizedResult(BaseModel):
    """Result of finalizing a candidate (review → commit → push → PR).

    Shared by both `autopr run` and `autopr research` pipelines.
    """

    review: ReviewResult | None = None
    structured_review: StructuredReviewResult | None = None
    codex_diff_review: CodexReviewResult | None = None
    pr_url: str = ""
    status: str = "pending"  # success | failed
    error: str = ""


class PRResult(BaseModel):
    """Final result for a single PR."""

    opportunity: Opportunity
    branch_name: str
    review: ReviewResult | None = None
    screen: ScreenResult | None = None
    codex_scope_review: CodexReviewResult | None = None
    codex_diff_review: CodexReviewResult | None = None
    health_checks: list[HealthCheckResult] = Field(default_factory=list)
    pr_url: str = ""
    status: str = "pending"  # pending | in_progress | success | failed | skipped
    error: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class ResearchReport(BaseModel):
    """Summary of a research-mode performance optimization run."""

    repo_path: str
    base_branch: str = "main"
    branch_name: str = ""
    program_file: str = ""
    baseline_metrics: MetricSnapshot | None = None
    best_metrics: MetricSnapshot | None = None
    experiments: list[ExperimentRecord] = Field(default_factory=list)
    best_commit_sha: str = ""
    pr_url: str = ""
    status: str = "pending"
    error: str = ""
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None

    @property
    def accepted_experiments(self) -> list[ExperimentRecord]:
        """Return accepted experiment records."""
        return [e for e in self.experiments if e.status == "accepted"]


class ReactResult(BaseModel):
    """Result of reacting to a single PR event (CI failure, review comment, etc.)."""

    pr_number: int
    event: str
    action: str = ""  # "fixed" | "no_action" | "needs_input" | "needs_human" | "skipped"
    summary: str = ""
    pushed_sha: str = ""


class ConflictResolutionResult(BaseModel):
    """Result of resolving merge conflicts on a single PR branch."""

    pr_number: int
    branch_name: str
    status: str = ""  # "clean_rebase" | "conflict_resolved" | "failed" | "skipped"
    strategy_used: str = ""  # "rebase" | "merge" | "none"
    conflicting_files: list[str] = Field(default_factory=list)
    error: str = ""


class ReconcileResult(BaseModel):
    """Summary of a batch reconcile across all open managed PRs."""

    total_prs: int = 0
    behind_updated: int = 0
    conflicts_resolved: int = 0
    needs_human: int = 0
    skipped: int = 0
    details: list[ConflictResolutionResult] = Field(default_factory=list)


class RunReport(BaseModel):
    """Summary of an entire autopr run."""

    repo_path: str
    repo_url: str = ""
    base_branch: str = "main"
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    results: list[PRResult] = Field(default_factory=list)

    @property
    def succeeded(self) -> list[PRResult]:
        """Get all successful PR results."""
        return [r for r in self.results if r.status == "success"]

    @property
    def failed(self) -> list[PRResult]:
        """Get all failed PR results."""
        return [r for r in self.results if r.status == "failed"]
