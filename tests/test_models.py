"""Tests for autopr/models.py -- enums, Pydantic models, and conversions."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from autopr.models import (
    DimensionResult,
    DimensionVerdict,
    ExperimentRecord,
    HealthCheckResult,
    MetricSnapshot,
    Opportunity,
    OpportunityType,
    PRResult,
    ResearchReport,
    ReviewResult,
    ReviewVerdict,
    RunReport,
    ScreenResult,
    Severity,
    StructuredReviewResult,
)

# ======================================================================
# Enum tests
# ======================================================================


class TestSeverityEnum:
    def test_values(self):
        assert Severity.TRIVIAL == "trivial"
        assert Severity.LOW == "low"
        assert Severity.MEDIUM == "medium"
        assert Severity.HIGH == "high"

    def test_is_str_subclass(self):
        assert isinstance(Severity.HIGH, str)

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            Severity("critical")


class TestOpportunityTypeEnum:
    def test_values(self):
        assert OpportunityType.DEAD_CODE == "dead_code"
        assert OpportunityType.UNUSED_FILE == "unused_file"
        assert OpportunityType.N_PLUS_ONE == "n_plus_one"
        assert OpportunityType.BATCH_QUERIES == "batch_queries"
        assert OpportunityType.REDUNDANT_LOOKUP == "redundant_lookup"
        assert OpportunityType.DUPLICATE_CODE == "duplicate_code"
        assert OpportunityType.COMMENTED_CODE == "commented_code"

    def test_is_str_subclass(self):
        assert isinstance(OpportunityType.DEAD_CODE, str)

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            OpportunityType("nonexistent_type")


class TestReviewVerdictEnum:
    def test_values(self):
        assert ReviewVerdict.APPROVE == "approve"
        assert ReviewVerdict.REQUEST_CHANGES == "request_changes"
        assert ReviewVerdict.REJECT == "reject"

    def test_is_str_subclass(self):
        assert isinstance(ReviewVerdict.APPROVE, str)

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            ReviewVerdict("maybe")


class TestDimensionVerdictEnum:
    def test_values(self):
        assert DimensionVerdict.PASS == "pass"
        assert DimensionVerdict.FAIL == "fail"
        assert DimensionVerdict.SKIP == "skip"

    def test_is_str_subclass(self):
        assert isinstance(DimensionVerdict.PASS, str)

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            DimensionVerdict("warn")


# ======================================================================
# Opportunity tests
# ======================================================================


class TestOpportunity:
    """Tests for the Opportunity model."""

    def test_full_construction(self, sample_opportunity: Opportunity):
        assert sample_opportunity.id == "batch-entity-queries"
        assert sample_opportunity.severity == Severity.MEDIUM
        assert len(sample_opportunity.files) == 2
        assert sample_opportunity.line_ranges == {"backend/src/api/users.py": "42-58"}
        assert sample_opportunity.autonomous_confidence == 0.95

    def test_minimal_construction(self):
        opp = Opportunity(
            id="rm-dead",
            title="Remove dead code",
            type="dead_code",
            severity=Severity.LOW,
            files=["a.py"],
            problem="Unused function",
            solution="Delete it",
        )
        # Defaults kick in
        assert opp.line_ranges == {}
        assert opp.estimated_impact == ""
        assert opp.complexity == "low"
        assert opp.autonomous_confidence == 0.9

    def test_confidence_lower_bound(self):
        opp = Opportunity(
            id="t",
            title="t",
            type="t",
            severity=Severity.LOW,
            files=["x.py"],
            problem="p",
            solution="s",
            autonomous_confidence=0.0,
        )
        assert opp.autonomous_confidence == 0.0

    def test_confidence_upper_bound(self):
        opp = Opportunity(
            id="t",
            title="t",
            type="t",
            severity=Severity.LOW,
            files=["x.py"],
            problem="p",
            solution="s",
            autonomous_confidence=1.0,
        )
        assert opp.autonomous_confidence == 1.0

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            Opportunity(
                id="t",
                title="t",
                type="t",
                severity=Severity.LOW,
                files=["x.py"],
                problem="p",
                solution="s",
                autonomous_confidence=-0.1,
            )

    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValidationError):
            Opportunity(
                id="t",
                title="t",
                type="t",
                severity=Severity.LOW,
                files=["x.py"],
                problem="p",
                solution="s",
                autonomous_confidence=1.1,
            )

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            Opportunity(
                id="t",
                title="t",
                # type is missing
                severity=Severity.LOW,
                files=["x.py"],
                problem="p",
                solution="s",
            )


# ======================================================================
# ReviewResult tests
# ======================================================================


class TestReviewResult:
    def test_defaults(self):
        r = ReviewResult(verdict=ReviewVerdict.APPROVE, summary="LGTM")
        assert r.issues == []
        assert r.suggestions == []

    def test_required_fields(self):
        with pytest.raises(ValidationError):
            ReviewResult(verdict=ReviewVerdict.APPROVE)  # missing summary

    def test_with_issues(self, sample_review_reject: ReviewResult):
        assert sample_review_reject.verdict == ReviewVerdict.REJECT
        assert len(sample_review_reject.issues) == 1


# ======================================================================
# DimensionResult tests
# ======================================================================


class TestDimensionResult:
    def test_defaults(self):
        d = DimensionResult(dimension="correctness", verdict=DimensionVerdict.PASS)
        assert d.finding == ""

    def test_required_fields(self):
        with pytest.raises(ValidationError):
            DimensionResult(verdict=DimensionVerdict.PASS)  # missing dimension


# ======================================================================
# ScreenResult tests
# ======================================================================


class TestScreenResult:
    def test_defaults(self):
        s = ScreenResult(proceed=True)
        assert s.dimensions == []
        assert s.summary == ""
        assert s.opportunity_id == ""

    def test_with_summary(self):
        s = ScreenResult(
            proceed=False, summary="Diff too large for auto-review", opportunity_id="opp-1"
        )
        assert not s.proceed
        assert "too large" in s.summary
        assert s.opportunity_id == "opp-1"


# ======================================================================
# StructuredReviewResult tests
# ======================================================================


class TestStructuredReviewResult:
    def test_defaults(self):
        sr = StructuredReviewResult(verdict=ReviewVerdict.APPROVE, summary="All good")
        assert sr.dimensions == []
        assert sr.suggestions == []

    def test_to_review_result_extracts_issues(self):
        sr = StructuredReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Some problems",
            suggestions=["Consider renaming variable"],
            dimensions=[
                DimensionResult(
                    dimension="correctness",
                    verdict=DimensionVerdict.FAIL,
                    finding="Off-by-one error",
                ),
                DimensionResult(
                    dimension="style",
                    verdict=DimensionVerdict.SKIP,
                    finding="Skipped",
                ),
                DimensionResult(
                    dimension="safety",
                    verdict=DimensionVerdict.PASS,
                    finding="Looks safe",
                ),
            ],
        )
        rr = sr.to_review_result()
        assert isinstance(rr, ReviewResult)
        assert rr.verdict == ReviewVerdict.REQUEST_CHANGES
        assert rr.summary == "Some problems"
        assert len(rr.issues) == 1
        assert "Off-by-one error" in rr.issues[0]
        assert len(rr.suggestions) == 1
        assert "Consider renaming variable" in rr.suggestions[0]

    def test_to_review_result_empty_dimensions(self):
        sr = StructuredReviewResult(verdict=ReviewVerdict.APPROVE, summary="Clean")
        rr = sr.to_review_result()
        assert rr.issues == []
        assert rr.suggestions == []

    def test_suggestions_field(self):
        sr = StructuredReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary="ok",
            suggestions=["Add tests"],
        )
        assert sr.suggestions == ["Add tests"]


# ======================================================================
# HealthCheckResult tests
# ======================================================================


class TestHealthCheckResult:
    def test_defaults(self):
        h = HealthCheckResult(passed=True, command="pytest")
        assert h.stdout == ""
        assert h.stderr == ""
        assert h.duration_seconds == 0.0
        assert h.attempt == 1

    def test_fixture_passed(self, sample_health_check_passed: HealthCheckResult):
        assert sample_health_check_passed.passed is True
        assert sample_health_check_passed.duration_seconds == 12.3

    def test_fixture_failed(self, sample_health_check_failed: HealthCheckResult):
        assert sample_health_check_failed.passed is False
        assert sample_health_check_failed.attempt == 2


# ======================================================================
# Research model tests
# ======================================================================


class TestMetricSnapshot:
    def test_defaults(self):
        snapshot = MetricSnapshot()
        assert snapshot.metrics == {}
        assert snapshot.sample_count == 1
        assert snapshot.aggregation == "median"
        assert snapshot.source_url == ""

    def test_value_lookup(self):
        snapshot = MetricSnapshot(metrics={"lcp_ms": 2100.0})
        assert snapshot.value("lcp_ms") == 2100.0
        assert snapshot.value("ttft_ms") is None


class TestExperimentRecord:
    def test_defaults(self):
        record = ExperimentRecord(iteration=3)
        assert record.status == "pending"
        assert record.deploy_url == ""
        assert record.metrics is None
        assert record.rejection_reason == ""


class TestResearchReport:
    def test_defaults(self):
        report = ResearchReport(repo_path="/tmp/repo")
        assert report.base_branch == "main"
        assert report.branch_name == ""
        assert report.program_file == ""
        assert report.baseline_metrics is None
        assert report.best_metrics is None
        assert report.experiments == []
        assert report.pr_url == ""
        assert report.status == "pending"
        assert report.error == ""

    def test_accepted_experiments_property(self):
        report = ResearchReport(
            repo_path="/tmp/repo",
            experiments=[
                ExperimentRecord(iteration=1, status="accepted"),
                ExperimentRecord(iteration=2, status="rejected"),
            ],
        )
        assert len(report.accepted_experiments) == 1
        assert report.accepted_experiments[0].iteration == 1


# ======================================================================
# PRResult tests
# ======================================================================


class TestPRResult:
    def test_defaults(self, sample_opportunity: Opportunity):
        pr = PRResult(opportunity=sample_opportunity, branch_name="autopr/batch-entity-queries")
        assert pr.review is None
        assert pr.health_checks == []
        assert pr.pr_url == ""
        assert pr.status == "pending"
        assert pr.error == ""
        assert isinstance(pr.created_at, datetime)

    def test_optional_review(
        self,
        sample_opportunity: Opportunity,
        sample_review_approve: ReviewResult,
    ):
        pr = PRResult(
            opportunity=sample_opportunity,
            branch_name="autopr/batch-entity-queries",
            review=sample_review_approve,
            status="success",
        )
        assert pr.review is not None
        assert pr.review.verdict == ReviewVerdict.APPROVE


# ======================================================================
# RunReport tests
# ======================================================================


class TestRunReport:
    def test_defaults(self):
        rr = RunReport(repo_path="/tmp/repo")
        assert rr.repo_url == ""
        assert rr.base_branch == "main"
        assert rr.completed_at is None
        assert rr.results == []
        assert isinstance(rr.started_at, datetime)

    def test_succeeded_property(self, sample_opportunity: Opportunity):
        success_pr = PRResult(
            opportunity=sample_opportunity,
            branch_name="autopr/a",
            status="success",
        )
        failed_pr = PRResult(
            opportunity=sample_opportunity,
            branch_name="autopr/b",
            status="failed",
        )
        pending_pr = PRResult(
            opportunity=sample_opportunity,
            branch_name="autopr/c",
            status="pending",
        )
        report = RunReport(
            repo_path="/tmp/repo",
            results=[success_pr, failed_pr, pending_pr],
        )
        assert len(report.succeeded) == 1
        assert report.succeeded[0].branch_name == "autopr/a"

    def test_failed_property(self, sample_opportunity: Opportunity):
        failed_pr = PRResult(
            opportunity=sample_opportunity,
            branch_name="autopr/x",
            status="failed",
            error="Health check failed",
        )
        report = RunReport(repo_path="/tmp/repo", results=[failed_pr])
        assert len(report.failed) == 1
        assert report.failed[0].error == "Health check failed"

    def test_empty_results(self):
        report = RunReport(repo_path="/tmp/repo")
        assert report.succeeded == []
        assert report.failed == []
