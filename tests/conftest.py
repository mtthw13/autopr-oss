"""Shared fixtures for the autopr test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from autopr.config import (
    AutoPRConfig,
    GatingConfig,
    VerdictRule,
)
from autopr.models import (
    HealthCheckResult,
    Opportunity,
    ReviewResult,
    ReviewVerdict,
    Severity,
)

# ---------------------------------------------------------------------------
# Model fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_opportunity() -> Opportunity:
    """Return a fully populated Opportunity instance."""
    return Opportunity(
        id="batch-entity-queries",
        title="Batch entity queries in /api/users",
        type="batch_queries",
        severity=Severity.MEDIUM,
        files=["backend/src/api/users.py", "backend/src/api/utils.py"],
        line_ranges={"backend/src/api/users.py": "42-58"},
        problem="N+1 query inside a for-loop fetches each entity individually.",
        solution="Replace the loop with a single bulk SELECT using an IN clause.",
        estimated_impact="~200 ms reduction per request on the /users endpoint",
        complexity="low",
        autonomous_confidence=0.95,
    )


@pytest.fixture()
def sample_review_approve() -> ReviewResult:
    """Return a ReviewResult with APPROVE verdict."""
    return ReviewResult(
        verdict=ReviewVerdict.APPROVE,
        summary="Fix looks correct and complete.",
        issues=[],
        suggestions=["Consider adding a brief inline comment."],
    )


@pytest.fixture()
def sample_review_reject() -> ReviewResult:
    """Return a ReviewResult with REJECT verdict."""
    return ReviewResult(
        verdict=ReviewVerdict.REJECT,
        summary="The fix introduces a type error.",
        issues=["Return type changed from list to dict without updating callers."],
        suggestions=[],
    )


@pytest.fixture()
def sample_review_request_changes() -> ReviewResult:
    """Return a ReviewResult with REQUEST_CHANGES verdict."""
    return ReviewResult(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="Mostly good, but missing a null check.",
        issues=["Possible None dereference on line 47."],
        suggestions=["Add a guard clause at the top of the function."],
    )


@pytest.fixture()
def sample_health_check_passed() -> HealthCheckResult:
    """Return a passing HealthCheckResult."""
    return HealthCheckResult(
        passed=True,
        command="make test",
        stdout="OK: 42 tests passed",
        stderr="",
        duration_seconds=12.3,
        attempt=1,
    )


@pytest.fixture()
def sample_health_check_failed() -> HealthCheckResult:
    """Return a failing HealthCheckResult."""
    return HealthCheckResult(
        passed=False,
        command="make check",
        stdout="",
        stderr="FAILED: ruff found 3 issues",
        duration_seconds=2.1,
        attempt=2,
    )


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_config(tmp_path: Path):
    """Factory fixture that returns an AutoPRConfig rooted in *tmp_path*."""

    def _make(**overrides) -> AutoPRConfig:
        repo_kwargs = {"path": tmp_path / "repo"}
        repo_kwargs.update(overrides.pop("repo", {}))
        defaults = {"repo": repo_kwargs}
        defaults.update(overrides)
        return AutoPRConfig(**defaults)

    return _make


@pytest.fixture()
def playbook_dir(tmp_path: Path) -> Path:
    """Create a temp directory with 2 valid and 1 malformed .md pattern files."""
    d = tmp_path / "playbooks"
    d.mkdir()

    (d / "dead_code.md").write_text(
        "# Dead Code\n\nLook for functions or imports that are never referenced.\n"
    )
    (d / "n_plus_one.md").write_text(
        "# N+1 Queries\n\nIdentify loops that issue one DB query per iteration.\n"
    )
    # Malformed: completely empty file
    (d / "bad_pattern.md").write_text("")

    return d


@pytest.fixture()
def default_verdict_rules() -> list[VerdictRule]:
    """Return the 6 default VerdictRule entries."""
    return GatingConfig().verdict_rules
