"""Tests for autopr/pr_creator.py — pure-logic functions."""

from __future__ import annotations

from autopr.models import (
    DimensionResult,
    DimensionVerdict,
    ExperimentRecord,
    MetricSnapshot,
    Opportunity,
    ResearchReport,
    ReviewResult,
    ReviewVerdict,
    Severity,
    StructuredReviewResult,
)
from autopr.pr_creator import (
    create_pr_description,
    format_research_section,
    format_review_section,
    parse_opportunity_from_pr_body,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_opportunity(**overrides) -> Opportunity:
    defaults = dict(
        id="batch-entity-queries",
        title="Batch entity queries in /api/users",
        type="batch_queries",
        severity=Severity.MEDIUM,
        files=["backend/src/api/users.py", "backend/src/api/utils.py"],
        problem="N+1 query inside a for-loop fetches each entity individually.",
        solution="Replace the loop with a single bulk SELECT using an IN clause.",
        estimated_impact="~200 ms reduction per request on the /users endpoint",
        complexity="low",
        autonomous_confidence=0.95,
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def _make_review(**overrides) -> ReviewResult:
    defaults = dict(
        verdict=ReviewVerdict.APPROVE,
        summary="Fix looks correct and complete.",
        issues=[],
        suggestions=["Consider adding a brief inline comment."],
    )
    defaults.update(overrides)
    return ReviewResult(**defaults)


def _make_structured_review(**overrides) -> StructuredReviewResult:
    defaults = dict(
        verdict=ReviewVerdict.APPROVE,
        summary="All dimensions pass. Clean refactor.",
        dimensions=[
            DimensionResult(
                dimension="correctness", verdict=DimensionVerdict.PASS, finding="Logic preserved"
            ),
            DimensionResult(
                dimension="safety", verdict=DimensionVerdict.PASS, finding="No side effects"
            ),
            DimensionResult(
                dimension="completeness", verdict=DimensionVerdict.SKIP, finding="Minor gap"
            ),
            DimensionResult(
                dimension="style", verdict=DimensionVerdict.FAIL, finding="Inconsistent naming"
            ),
        ],
    )
    defaults.update(overrides)
    return StructuredReviewResult(**defaults)


def _make_research_report(**overrides) -> ResearchReport:
    defaults = dict(
        repo_path="/tmp/repo",
        branch_name="autopr/research-test",
        program_file="program.md",
        baseline_metrics=MetricSnapshot(metrics={"performance_score": 80.0, "lcp_ms": 2500.0}),
        best_metrics=MetricSnapshot(metrics={"performance_score": 84.0, "lcp_ms": 2200.0}),
        experiments=[
            ExperimentRecord(
                iteration=1,
                status="accepted",
                rejection_reason="performance_score improved by 4.000",
                metrics=MetricSnapshot(metrics={"performance_score": 84.0}),
            ),
            ExperimentRecord(
                iteration=2,
                status="rejected",
                rejection_reason="lcp_ms regressed by 8.00%",
                metrics=MetricSnapshot(metrics={"performance_score": 82.0}),
            ),
        ],
    )
    defaults.update(overrides)
    return ResearchReport(**defaults)


# ── TestCreatePrDescription ─────────────────────────────────────────────────


class TestCreatePrDescription:
    """Tests for create_pr_description()."""

    def test_all_sections_present(self):
        """All major sections should appear in the output."""
        opp = _make_opportunity()
        review = _make_review()
        body = create_pr_description(opp, review=review)

        assert "## Problem" in body
        assert "## Solution" in body
        assert "## Files Changed" in body
        assert "## Verification" in body
        assert "## AI Review" in body

    def test_problem_and_solution_from_opportunity(self):
        """Problem and solution text should come directly from the opportunity."""
        opp = _make_opportunity(
            problem="The widget factory leaks memory.",
            solution="Replace manual alloc with a pool.",
        )
        body = create_pr_description(opp)

        assert "The widget factory leaks memory." in body
        assert "Replace manual alloc with a pool." in body

    def test_files_listed_with_backticks(self):
        """Each file should be listed as a backtick-wrapped markdown item."""
        opp = _make_opportunity(files=["src/a.py", "src/b.py", "src/c.py"])
        body = create_pr_description(opp)

        assert "- `src/a.py`" in body
        assert "- `src/b.py`" in body
        assert "- `src/c.py`" in body

    def test_estimated_impact_included_when_non_empty(self):
        """estimated_impact should appear when it has a value."""
        opp = _make_opportunity(estimated_impact="Saves 50ms per request")
        body = create_pr_description(opp)

        assert "**Estimated impact:** Saves 50ms per request" in body

    def test_estimated_impact_omitted_when_empty(self):
        """estimated_impact should not appear when it's an empty string."""
        opp = _make_opportunity(estimated_impact="")
        body = create_pr_description(opp)

        assert "Estimated impact" not in body

    def test_structured_review_section_used_when_provided(self):
        """When structured_review is given, it should use format_review_section."""
        opp = _make_opportunity()
        sr = _make_structured_review()
        body = create_pr_description(opp, structured_review=sr)

        assert "## AI Review" in body
        # Should contain dimensions table
        assert "| Dimension | Verdict | Finding |" in body
        assert "| correctness | pass | Logic preserved |" in body
        assert "| style | fail | Inconsistent naming |" in body

    def test_review_section_fallback_when_no_structured_review(self):
        """When only review (not structured_review) is given, use the simple format."""
        opp = _make_opportunity()
        review = _make_review(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Needs a null check.",
            suggestions=["Add guard clause"],
        )
        body = create_pr_description(opp, review=review)

        assert "**Verdict:** request_changes" in body
        assert "**Summary:** Needs a null check." in body
        assert "- Add guard clause" in body

    def test_no_review_section_when_both_none(self):
        """When neither review nor structured_review is given, no AI Review section."""
        opp = _make_opportunity()
        body = create_pr_description(opp)

        assert "## AI Review" not in body

    def test_footer_generated_by_present(self):
        """Footer should contain the 'Generated by' attribution."""
        opp = _make_opportunity()
        body = create_pr_description(opp)

        assert "*Generated by" in body
        assert "autopr" in body

    def test_structured_review_takes_precedence_over_review(self):
        """When both review and structured_review are provided, structured wins."""
        opp = _make_opportunity()
        review = _make_review(summary="Simple review summary")
        sr = _make_structured_review(summary="Structured review summary")
        body = create_pr_description(opp, review=review, structured_review=sr)

        # structured_review's table should be present
        assert "| Dimension | Verdict | Finding |" in body
        assert "Structured review summary" in body
        # The simple review's summary should NOT be present
        assert "Simple review summary" not in body

    def test_research_summary_included_when_provided(self):
        """Research PRs should include a performance summary section."""
        opp = _make_opportunity()
        body = create_pr_description(opp, research_report=_make_research_report())

        assert "## Research Summary" in body
        assert "| Metric | Baseline | Best | Delta |" in body
        assert "Accepted experiments: 1" in body


# ── TestFormatReviewSection ─────────────────────────────────────────────────


class TestFormatReviewSection:
    """Tests for format_review_section()."""

    def test_verdict_shown(self):
        """The verdict line should be present."""
        sr = _make_structured_review(verdict=ReviewVerdict.APPROVE)
        section = format_review_section(sr)

        assert "**Verdict:** approve" in section

    def test_dimensions_table_with_pass_and_fail(self):
        """Dimensions should be rendered as a markdown table."""
        sr = _make_structured_review(
            dimensions=[
                DimensionResult(
                    dimension="correctness", verdict=DimensionVerdict.PASS, finding="All good"
                ),
                DimensionResult(
                    dimension="safety", verdict=DimensionVerdict.FAIL, finding="Risk detected"
                ),
            ]
        )
        section = format_review_section(sr)

        assert "| Dimension | Verdict | Finding |" in section
        assert "| correctness | pass | All good |" in section
        assert "| safety | fail | Risk detected |" in section

    def test_summary_included(self):
        """The summary should appear in the output."""
        sr = _make_structured_review(summary="Clean refactor, no issues.")
        section = format_review_section(sr)

        assert "**Summary:** Clean refactor, no issues." in section

    def test_pipe_characters_in_findings_escaped(self):
        """Pipe characters in dimension findings should be escaped for markdown tables."""
        sr = _make_structured_review(
            dimensions=[
                DimensionResult(
                    dimension="correctness",
                    verdict=DimensionVerdict.PASS,
                    finding="Choice A | Choice B both valid",
                ),
            ]
        )
        section = format_review_section(sr)

        assert "Choice A \\| Choice B both valid" in section
        # The raw unescaped pipe should NOT appear — only the backslash-escaped form
        lines = section.split("\n")
        data_lines = [line for line in lines if line.startswith("| correctness")]
        assert len(data_lines) == 1
        # After splitting by " | " (the column separator pattern), there should be
        # exactly 4 columns: empty-before-first-pipe, dimension, verdict, finding+trailing
        # The escaped \| should NOT create extra columns
        assert "\\|" in data_lines[0]


class TestFormatResearchSection:
    def test_metrics_table_rendered(self):
        section = format_research_section(_make_research_report())

        assert "| Metric | Baseline | Best | Delta |" in section
        assert "| performance_score | 80.000 | 84.000 | +4.000 |" in section
        assert "| lcp_ms | 2500.000 | 2200.000 | -300.000 |" in section

    def test_experiment_table_rendered(self):
        section = format_research_section(_make_research_report())

        assert "| Iteration | Status | Metrics | Reason |" in section
        assert (
            "| 1 | accepted | performance_score=84.000 | performance_score improved by 4.000 |"
            in section
        )


# ── TestParseOpportunityFromPrBody ──────────────────────────────────────────


class TestParseOpportunityFromPrBody:
    """Tests for parse_opportunity_from_pr_body()."""

    def test_round_trip(self):
        """create_pr_description -> parse_opportunity_from_pr_body should extract fields."""
        opp = _make_opportunity(
            problem="Query runs N+1 times.",
            solution="Batch into single SELECT.",
            files=["src/db.py", "src/api.py"],
        )
        body = create_pr_description(opp)

        parsed = parse_opportunity_from_pr_body(body, pr_number=1, title="Test PR")

        assert parsed.problem == "Query runs N+1 times."
        assert parsed.solution == "Batch into single SELECT."
        assert parsed.files == ["src/db.py", "src/api.py"]

    def test_missing_sections_return_defaults(self):
        """A body with none of the expected sections returns default values."""
        body = "Just some random PR description without any sections."

        parsed = parse_opportunity_from_pr_body(body, pr_number=42)

        assert parsed.problem == "Parsed from PR body"
        assert parsed.solution == "See PR description"
        assert parsed.files == ["unknown"]
        assert parsed.id == "pr-42"

    def test_files_extracted_from_backtick_format(self):
        """Files are extracted from '- `path`' format lines."""
        body = (
            "## Problem\nSome problem\n"
            "## Solution\nSome fix\n"
            "## Files Changed\n"
            "- `src/main.py`\n"
            "- `src/utils.py`\n"
            "- `tests/test_main.py`\n"
            "## Verification\n"
        )

        parsed = parse_opportunity_from_pr_body(body, pr_number=10)

        assert parsed.files == ["src/main.py", "src/utils.py", "tests/test_main.py"]

    def test_round_trip_with_estimated_impact(self):
        """estimated_impact line should not leak into the problem text."""
        opp = _make_opportunity(
            problem="Slow endpoint.",
            estimated_impact="100ms improvement",
        )
        body = create_pr_description(opp)

        parsed = parse_opportunity_from_pr_body(body, pr_number=5)

        assert parsed.problem == "Slow endpoint."
        assert "100ms" not in parsed.problem

    def test_round_trip_with_review_sections(self):
        """PR body with review sections should still extract problem/solution/files."""
        opp = _make_opportunity(
            problem="Dead import found.",
            solution="Remove the import.",
            files=["src/app.py"],
        )
        review = _make_review()
        body = create_pr_description(opp, review=review)

        parsed = parse_opportunity_from_pr_body(body, pr_number=7, title="Dead import fix")

        assert parsed.problem == "Dead import found."
        assert parsed.solution == "Remove the import."
        assert parsed.files == ["src/app.py"]
        assert parsed.title == "Dead import fix"
