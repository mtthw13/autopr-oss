"""Tests for autopr/research.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from autopr.config import (
    AutoPRConfig,
    RepoConfig,
    ResearchConfig,
    ResearchGuardrailMetricConfig,
    ResearchPrimaryMetricConfig,
    ResearchScoringConfig,
)
from autopr.models import (
    MetricSnapshot,
    Opportunity,
    Severity,
)
from autopr.research import (
    aggregate_metric_runs,
    evaluate_candidate_metrics,
    normalize_lighthouse_metrics,
    run_research,
)


def make_research_config(tmp_path: Path, **research_overrides) -> AutoPRConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "program.md").write_text("# Research\n")
    research = ResearchConfig(**research_overrides)
    return AutoPRConfig(repo=RepoConfig(path=repo), research=research)


def make_research_opportunity() -> Opportunity:
    return Opportunity(
        id="research-performance",
        title="Improve performance metrics via research",
        type="research",
        severity=Severity.MEDIUM,
        files=["src/app.ts"],
        problem="Performance is worse than baseline.",
        solution="Apply iterative performance improvements.",
    )


class TestBenchmarkHelpers:
    def test_normalize_lighthouse_metrics(self):
        payload = {
            "categories": {"performance": {"score": 0.88}},
            "audits": {
                "largest-contentful-paint": {"numericValue": 2300},
                "first-contentful-paint": {"numericValue": 1200},
                "speed-index": {"numericValue": 2100},
                "server-response-time": {"numericValue": 180},
            },
        }

        metrics = normalize_lighthouse_metrics(payload)

        assert metrics == {
            "performance_score": 88.0,
            "lcp_ms": 2300.0,
            "fcp_ms": 1200.0,
            "speed_index_ms": 2100.0,
            "ttfb_ms": 180.0,
        }

    def test_aggregate_metric_runs_uses_median(self):
        snapshot = aggregate_metric_runs(
            [
                {"performance_score": 80.0, "lcp_ms": 2600.0},
                {"performance_score": 90.0, "lcp_ms": 2400.0},
                {"performance_score": 85.0, "lcp_ms": 2500.0},
            ],
            source_url="https://example.vercel.app",
        )

        assert snapshot.metrics["performance_score"] == 85.0
        assert snapshot.metrics["lcp_ms"] == 2500.0
        assert snapshot.source_url == "https://example.vercel.app"
        assert snapshot.sample_count == 3


class TestScoring:
    def test_primary_metric_maximize(self):
        scoring = ResearchScoringConfig(
            primary_metric=ResearchPrimaryMetricConfig(
                name="performance_score",
                direction="maximize",
                min_improvement_abs=2.0,
            ),
            guardrails=[],
        )

        accepted, reason = evaluate_candidate_metrics(
            MetricSnapshot(metrics={"performance_score": 84.0}),
            MetricSnapshot(metrics={"performance_score": 80.0}),
            scoring,
        )

        assert accepted is True
        assert "performance_score improved" in reason

    def test_primary_metric_minimize(self):
        scoring = ResearchScoringConfig(
            primary_metric=ResearchPrimaryMetricConfig(
                name="lcp_ms",
                direction="minimize",
                min_improvement_abs=100.0,
            ),
            guardrails=[],
        )

        accepted, _ = evaluate_candidate_metrics(
            MetricSnapshot(metrics={"lcp_ms": 2200.0}),
            MetricSnapshot(metrics={"lcp_ms": 2400.0}),
            scoring,
        )

        assert accepted is True

    def test_guardrail_percent_rejection(self):
        scoring = ResearchScoringConfig(
            primary_metric=ResearchPrimaryMetricConfig(
                name="performance_score",
                direction="maximize",
                min_improvement_abs=1.0,
            ),
            guardrails=[
                ResearchGuardrailMetricConfig(
                    name="lcp_ms",
                    direction="minimize",
                    max_regression_pct=5.0,
                )
            ],
        )

        accepted, reason = evaluate_candidate_metrics(
            MetricSnapshot(metrics={"performance_score": 82.0, "lcp_ms": 2650.0}),
            MetricSnapshot(metrics={"performance_score": 80.0, "lcp_ms": 2400.0}),
            scoring,
        )

        assert accepted is False
        assert "lcp_ms regressed" in reason

    def test_guardrail_absolute_rejection(self):
        scoring = ResearchScoringConfig(
            primary_metric=ResearchPrimaryMetricConfig(
                name="performance_score",
                direction="maximize",
                min_improvement_abs=1.0,
            ),
            guardrails=[
                ResearchGuardrailMetricConfig(
                    name="ttft_ms",
                    direction="minimize",
                    max_regression_abs=50.0,
                )
            ],
        )

        accepted, reason = evaluate_candidate_metrics(
            MetricSnapshot(metrics={"performance_score": 82.0, "ttft_ms": 190.0}),
            MetricSnapshot(metrics={"performance_score": 80.0, "ttft_ms": 120.0}),
            scoring,
        )

        assert accepted is False
        assert "ttft_ms regressed" in reason


class TestRunResearch:
    """Tests for the new diagnose → prepare → verify → finalize flow."""

    @patch("autopr.diagnose.diagnose_performance")
    @patch("autopr.orchestrator.prepare_candidate")
    @patch("autopr.verify.verify_candidate")
    @patch("autopr.orchestrator.finalize_candidate")
    @patch("autopr.research.remove_worktree")
    @patch("autopr.research.get_head_sha")
    def test_accepted_with_verified_perf_creates_pr(
        self,
        mock_head_sha: MagicMock,
        mock_remove_worktree: MagicMock,
        mock_finalize: MagicMock,
        mock_verify: MagicMock,
        mock_prepare: MagicMock,
        mock_diagnose: MagicMock,
        tmp_path: Path,
    ):
        from autopr.models import FinalizedResult, PreparedCandidate
        from autopr.verify import VerifyResult

        config = make_research_config(tmp_path, max_iterations=1)
        opp = make_research_opportunity()
        baseline = MetricSnapshot(metrics={"performance_score": 80.0})
        candidate = MetricSnapshot(metrics={"performance_score": 85.0})

        mock_diagnose.return_value = ([opp], baseline)

        wt = tmp_path / "wt"
        wt.mkdir()
        mock_prepare.return_value = PreparedCandidate(
            opportunity=opp,
            branch_name="autopr/perf",
            worktree_path=str(wt),
            status="ready",
        )
        mock_verify.return_value = VerifyResult(
            passed=True,
            reason="improved by 5",
            baseline_metrics=baseline,
            candidate_metrics=candidate,
            deploy_url="https://preview.vercel.app",
        )
        mock_finalize.return_value = FinalizedResult(
            status="success",
            pr_url="https://github.com/org/repo/pull/1",
        )
        mock_head_sha.return_value = "accepted-sha"

        report = run_research(config)

        assert report.status == "success"
        assert report.pr_url == "https://github.com/org/repo/pull/1"
        assert report.best_metrics.metrics["performance_score"] == 85.0
        assert len(report.accepted_experiments) == 1
        mock_prepare.assert_called_once()
        mock_verify.assert_called_once()
        mock_finalize.assert_called_once()

    @patch("autopr.diagnose.diagnose_performance")
    @patch("autopr.orchestrator.prepare_candidate")
    @patch("autopr.verify.verify_candidate")
    @patch("autopr.research.remove_worktree")
    def test_failed_verification_rejects_candidate(
        self,
        mock_remove_worktree: MagicMock,
        mock_verify: MagicMock,
        mock_prepare: MagicMock,
        mock_diagnose: MagicMock,
        tmp_path: Path,
    ):
        from autopr.models import PreparedCandidate
        from autopr.verify import VerifyResult

        config = make_research_config(tmp_path, max_iterations=1)
        opp = make_research_opportunity()
        baseline = MetricSnapshot(metrics={"performance_score": 80.0})
        candidate = MetricSnapshot(metrics={"performance_score": 80.5})

        mock_diagnose.return_value = ([opp], baseline)

        wt = tmp_path / "wt"
        wt.mkdir()
        mock_prepare.return_value = PreparedCandidate(
            opportunity=opp,
            branch_name="autopr/perf",
            worktree_path=str(wt),
            status="ready",
        )
        mock_verify.return_value = VerifyResult(
            passed=False,
            reason="performance_score did not improve enough",
            baseline_metrics=baseline,
            candidate_metrics=candidate,
            deploy_url="https://preview.vercel.app",
        )

        report = run_research(config)

        assert report.status == "no_improvement"
        assert len(report.experiments) == 1
        assert report.experiments[0].status == "rejected"
        assert "Perf verify failed" in report.experiments[0].rejection_reason

    @patch("autopr.diagnose.diagnose_performance")
    def test_no_opportunities_returns_no_improvement(
        self,
        mock_diagnose: MagicMock,
        tmp_path: Path,
    ):
        config = make_research_config(tmp_path, max_iterations=3)
        baseline = MetricSnapshot(metrics={"performance_score": 95.0})
        mock_diagnose.return_value = ([], baseline)

        report = run_research(config)

        assert report.status == "no_improvement"
        assert len(report.experiments) == 0
