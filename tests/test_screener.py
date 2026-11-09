"""Tests for autopr/screener.py — _parse_screen_result()."""

from __future__ import annotations

import json

from autopr.screener import _parse_screen_result


class TestParseScreenResult:
    """Tests for _parse_screen_result()."""

    def test_valid_pass_all_dimensions_pass(self):
        """JSON with proceed=true and 4 dimensions all passing."""
        raw = json.dumps(
            {
                "proceed": True,
                "summary": "All checks passed",
                "dimensions": [
                    {"dimension": "correctness", "verdict": "pass", "finding": "Looks good"},
                    {"dimension": "security", "verdict": "pass", "finding": "No side effects"},
                    {
                        "dimension": "compatibility",
                        "verdict": "pass",
                        "finding": "All files covered",
                    },
                    {
                        "dimension": "scope_hygiene",
                        "verdict": "pass",
                        "finding": "Consistent formatting",
                    },
                ],
            }
        )

        result = _parse_screen_result(raw, "test-opp-1")

        assert result.proceed is True
        assert result.summary == "All checks passed"
        assert result.opportunity_id == "test-opp-1"
        assert len(result.dimensions) == 4

    def test_valid_fail_some_dimension_fails(self):
        """JSON with proceed=false and a failing dimension."""
        raw = json.dumps(
            {
                "proceed": False,
                "summary": "Correctness issue found",
                "dimensions": [
                    {"dimension": "correctness", "verdict": "fail", "finding": "Type mismatch"},
                    {"dimension": "security", "verdict": "pass", "finding": "OK"},
                    {"dimension": "compatibility", "verdict": "skip", "finding": "Minor gap"},
                    {"dimension": "scope_hygiene", "verdict": "pass", "finding": "Fine"},
                ],
            }
        )

        result = _parse_screen_result(raw, "test-opp-2")

        assert result.proceed is False
        assert result.summary == "Correctness issue found"

    def test_unparseable_input_defaults_to_proceed_true(self):
        """Completely unparseable input should fail-open with proceed=True."""
        raw = "this is not json at all, just random text!!!"

        result = _parse_screen_result(raw, "test-opp-3")

        assert result.proceed is True
        assert "Unparseable" in result.summary

    def test_missing_proceed_key_defaults_to_true(self):
        """JSON dict without a 'proceed' key should default to proceed=True."""
        raw = json.dumps(
            {
                "summary": "Some reason",
                "dimensions": [
                    {"dimension": "correctness", "verdict": "pass", "finding": "OK"},
                ],
            }
        )

        result = _parse_screen_result(raw, "test-opp-4")

        assert result.proceed is True

    def test_invalid_dimension_verdict_silently_skipped(self):
        """A dimension with an invalid verdict value should be skipped without error."""
        raw = json.dumps(
            {
                "proceed": False,
                "summary": "",
                "dimensions": [
                    {"dimension": "correctness", "verdict": "pass", "finding": "OK"},
                    {
                        "dimension": "security",
                        "verdict": "INVALID_VALUE",
                        "finding": "Should be skipped",
                    },
                    {"dimension": "scope_hygiene", "verdict": "fail", "finding": "Bad formatting"},
                ],
            }
        )

        result = _parse_screen_result(raw, "test-opp-5")

        assert result.proceed is False
        # The invalid verdict dimension should not be included
        dim_names = [d.dimension for d in result.dimensions]
        assert "security" not in dim_names
        # The summary should be built from fail dimensions only, skipping the invalid one
        assert "scope_hygiene" in result.summary
        assert "INVALID_VALUE" not in result.summary

    def test_empty_dimensions_list(self):
        """An empty dimensions list should work fine."""
        raw = json.dumps(
            {
                "proceed": True,
                "summary": "Quick pass",
                "dimensions": [],
            }
        )

        result = _parse_screen_result(raw, "test-opp-6")

        assert result.proceed is True
        assert result.summary == "Quick pass"

    def test_envelope_wrapped_json(self):
        """Claude --output-format json wraps in {"type": "result", "result": "..."}."""
        inner = json.dumps(
            {
                "proceed": False,
                "summary": "Issue found",
                "dimensions": [
                    {"dimension": "correctness", "verdict": "fail", "finding": "Bug"},
                ],
            }
        )
        envelope = json.dumps({"type": "result", "result": inner})

        result = _parse_screen_result(envelope, "test-opp-7")

        assert result.proceed is False
        assert result.summary == "Issue found"
