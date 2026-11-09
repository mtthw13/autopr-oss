"""Tests for autopr.reviewer — JSON extraction, verdict computation, and review parsing.

This test module recreates and expands the deleted test_gating.py, covering:
  - extract_json_from_claude_output
  - extract_json_array_from_claude_output
  - compute_verdict
  - parse_review
  - parse_structured_review
"""

from __future__ import annotations

import json

from autopr.config import GatingConfig, VerdictRule
from autopr.models import DimensionVerdict, ReviewVerdict, StructuredReviewResult
from autopr.reviewer import (
    compute_verdict,
    extract_json_array_from_claude_output,
    extract_json_from_claude_output,
    format_scope_warnings,
    parse_review,
    parse_structured_review,
)

# ===================================================================
# TestExtractJson (~12 tests)
# ===================================================================


class TestExtractJson:
    """Tests for extract_json_from_claude_output()."""

    def test_envelope_unwrap_string_result(self):
        """Envelope with a string result containing JSON is unwrapped."""
        inner = json.dumps({"verdict": "approve", "summary": "ok"})
        envelope = json.dumps({"type": "result", "result": inner})
        result = extract_json_from_claude_output(envelope)
        assert result is not None
        assert result["verdict"] == "approve"
        assert result["summary"] == "ok"

    def test_envelope_with_nested_result_dict(self):
        """Envelope whose 'result' is already a dict (not a string)."""
        envelope = json.dumps(
            {
                "type": "result",
                "result": {"verdict": "reject", "issues": ["bad"]},
            }
        )
        result = extract_json_from_claude_output(envelope)
        assert result is not None
        assert result["verdict"] == "reject"
        assert result["issues"] == ["bad"]

    def test_direct_json_parse(self):
        """Plain JSON object with no envelope."""
        raw = json.dumps({"key": "value", "count": 42})
        result = extract_json_from_claude_output(raw)
        assert result == {"key": "value", "count": 42}

    def test_nested_braces_dimensions_array(self):
        """Regression: JSON with nested objects (dimensions array) must be extracted.

        The old regex used [^{}]* which fails on nested braces.  The current
        implementation uses brace-depth tracking which handles this correctly.
        """
        inner_json = {
            "dimensions": [
                {"name": "correctness", "verdict": "pass", "comment": "looks good"},
                {"name": "safety", "verdict": "pass", "comment": "no issues"},
            ],
            "summary": "All good",
        }
        raw = f"Here is my review:\n{json.dumps(inner_json)}\nEnd of review."
        result = extract_json_from_claude_output(raw)
        assert result is not None
        assert "dimensions" in result
        assert len(result["dimensions"]) == 2
        assert result["dimensions"][0]["name"] == "correctness"

    def test_brace_depth_fallback(self):
        """Step 4 brace-depth fallback extracts JSON surrounded by prose."""
        raw = 'Some preamble text. {"data": {"nested": true}, "flag": 1} trailing.'
        result = extract_json_from_claude_output(raw)
        assert result is not None
        assert result["data"] == {"nested": True}
        assert result["flag"] == 1

    def test_empty_string_returns_none(self):
        assert extract_json_from_claude_output("") is None

    def test_whitespace_only_returns_none(self):
        assert extract_json_from_claude_output("   \n\t  ") is None

    def test_no_json_returns_none(self):
        assert extract_json_from_claude_output("Just some plain text, no JSON here.") is None

    def test_code_fences_inside_envelope(self):
        """Envelope whose string result contains JSON wrapped in code fences."""
        inner = '```json\n{"verdict": "approve", "summary": "clean"}\n```'
        envelope = json.dumps({"type": "result", "result": inner})
        result = extract_json_from_claude_output(envelope)
        assert result is not None
        assert result["verdict"] == "approve"

    def test_json_in_markdown_code_fences(self):
        """JSON wrapped in markdown code fences (no envelope)."""
        raw = '```json\n{"status": "ok", "count": 5}\n```'
        result = extract_json_from_claude_output(raw)
        assert result is not None
        assert result["status"] == "ok"
        assert result["count"] == 5

    def test_json_embedded_in_markdown_text(self):
        """JSON object embedded in surrounding markdown prose."""
        raw = (
            "## Review\n\n"
            'The fix looks correct. {"verdict": "approve", "summary": "LGTM"}\n\n'
            "No further issues."
        )
        result = extract_json_from_claude_output(raw)
        assert result is not None
        assert result["verdict"] == "approve"

    def test_json_in_review_tags(self):
        """JSON wrapped in <review> tags."""
        raw = '<review>{"verdict": "approve", "summary": "ok"}</review>'
        result = extract_json_from_claude_output(raw)
        assert result is not None
        assert result["verdict"] == "approve"

    def test_nested_json_objects(self):
        """Deeply nested JSON structures are handled correctly."""
        data = {"outer": {"middle": {"inner": "value"}}, "flag": True}
        raw = f"prefix {json.dumps(data)} suffix"
        result = extract_json_from_claude_output(raw)
        assert result is not None
        assert result["outer"]["middle"]["inner"] == "value"
        assert result["flag"] is True


# ===================================================================
# TestExtractJsonArray (~9 tests)
# ===================================================================


class TestExtractJsonArray:
    """Tests for extract_json_array_from_claude_output()."""

    def test_direct_json_array(self):
        raw = json.dumps([{"a": 1}, {"b": 2}])
        result = extract_json_array_from_claude_output(raw)
        assert result == [{"a": 1}, {"b": 2}]

    def test_empty_array(self):
        result = extract_json_array_from_claude_output("[]")
        assert result == []

    def test_empty_string_returns_none(self):
        assert extract_json_array_from_claude_output("") is None

    def test_whitespace_only_returns_none(self):
        assert extract_json_array_from_claude_output("  \n  ") is None

    def test_envelope_with_direct_list_result(self):
        """Envelope whose result is already a list (not a string)."""
        envelope = json.dumps(
            {
                "type": "result",
                "result": [{"name": "item1"}, {"name": "item2"}],
            }
        )
        result = extract_json_array_from_claude_output(envelope)
        assert result is not None
        assert len(result) == 2
        assert result[0]["name"] == "item1"

    def test_envelope_wrapped_array_string(self):
        """Envelope whose result is a string containing a JSON array."""
        inner = json.dumps([{"x": 10}])
        envelope = json.dumps({"type": "result", "result": inner})
        result = extract_json_array_from_claude_output(envelope)
        assert result is not None
        assert len(result) == 1
        assert result[0]["x"] == 10

    def test_array_embedded_in_text_bracket_depth(self):
        """Array embedded in surrounding text, found via bracket-depth walk."""
        raw = 'Here are the results: [{"id": 1}, {"id": 2}] end of output.'
        result = extract_json_array_from_claude_output(raw)
        assert result is not None
        assert len(result) == 2
        assert result[0]["id"] == 1

    def test_markdown_fenced_array(self):
        """JSON array inside markdown code fences."""
        raw = '```json\n[{"name": "a"}, {"name": "b"}]\n```'
        result = extract_json_array_from_claude_output(raw)
        assert result is not None
        assert len(result) == 2

    def test_individual_objects_in_text_step4_collection(self):
        """Step 4: individual JSON objects scattered in text are collected."""
        raw = (
            'First item: {"id": 1, "value": "alpha"}\n'
            'Second item: {"id": 2, "value": "beta"}\n'
            "That's all."
        )
        result = extract_json_array_from_claude_output(raw)
        assert result is not None
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["value"] == "beta"

    def test_max_turns_error_envelope_returns_none(self):
        """An error envelope (is_error=true) returns None."""
        envelope = json.dumps(
            {
                "type": "error",
                "is_error": True,
                "result": "max turns reached",
            }
        )
        result = extract_json_array_from_claude_output(envelope)
        assert result is None


# ===================================================================
# TestComputeVerdict (~10 tests)
# ===================================================================


class TestComputeVerdict:
    """Tests for compute_verdict()."""

    def _default_rules(self) -> list[VerdictRule]:
        return GatingConfig().verdict_rules

    def test_all_pass_approve(self):
        dims = [
            {"dimension": "correctness", "verdict": "pass"},
            {"dimension": "security", "verdict": "pass"},
            {"dimension": "observability", "verdict": "pass"},
        ]
        assert compute_verdict(dims, self._default_rules()) == ReviewVerdict.APPROVE

    def test_correctness_fail_reject(self):
        """correctness/fail is mapped to 'reject' -> REJECT."""
        dims = [
            {"dimension": "correctness", "verdict": "fail"},
            {"dimension": "security", "verdict": "pass"},
        ]
        assert compute_verdict(dims, self._default_rules()) == ReviewVerdict.REJECT

    def test_security_fail_reject(self):
        """security/fail is mapped to 'reject' -> REJECT."""
        dims = [
            {"dimension": "correctness", "verdict": "pass"},
            {"dimension": "security", "verdict": "fail"},
        ]
        assert compute_verdict(dims, self._default_rules()) == ReviewVerdict.REJECT

    def test_observability_fail_warn_only(self):
        """observability/fail is mapped to 'warn' -> APPROVE (no blocking)."""
        dims = [
            {"dimension": "correctness", "verdict": "pass"},
            {"dimension": "security", "verdict": "pass"},
            {"dimension": "observability", "verdict": "fail"},
        ]
        assert compute_verdict(dims, self._default_rules()) == ReviewVerdict.APPROVE

    def test_skip_verdict_not_counted_as_fail(self):
        dims = [
            {"dimension": "correctness", "verdict": "skip"},
            {"dimension": "security", "verdict": "pass"},
        ]
        assert compute_verdict(dims, self._default_rules()) == ReviewVerdict.APPROVE

    def test_unknown_dimension_treated_as_warn(self):
        """A dimension not listed in any rule defaults to warn (no blocking)."""
        dims = [
            {"dimension": "unknown_dimension", "verdict": "fail"},
        ]
        assert compute_verdict(dims, self._default_rules()) == ReviewVerdict.APPROVE

    def test_multiple_failures_first_reject_wins(self):
        """When multiple dimensions fail, the first 'reject' causes immediate REJECT."""
        dims = [
            {"dimension": "observability", "verdict": "fail"},  # warn
            {"dimension": "correctness", "verdict": "fail"},  # reject -> REJECT
            {"dimension": "security", "verdict": "fail"},  # reject (not reached)
        ]
        assert compute_verdict(dims, self._default_rules()) == ReviewVerdict.REJECT

    def test_custom_rules_request_changes(self):
        """Custom rules with on_fail='request_changes' produce REQUEST_CHANGES."""
        custom_rules = [
            VerdictRule(dimension="maintainability", on_fail="request_changes"),
        ]
        dims = [
            {"dimension": "maintainability", "verdict": "fail"},
        ]
        assert compute_verdict(dims, custom_rules) == ReviewVerdict.REQUEST_CHANGES

    def test_custom_rules_reject_overrides_request_changes(self):
        """Reject is more severe than request_changes; reject wins immediately."""
        custom_rules = [
            VerdictRule(dimension="maintainability", on_fail="request_changes"),
            VerdictRule(dimension="security", on_fail="reject"),
        ]
        dims = [
            {"dimension": "maintainability", "verdict": "fail"},
            {"dimension": "security", "verdict": "fail"},
        ]
        # security/fail -> reject -> REJECT takes priority
        assert compute_verdict(dims, custom_rules) == ReviewVerdict.REJECT

    def test_empty_dimensions_approve(self):
        assert compute_verdict([], self._default_rules()) == ReviewVerdict.APPROVE

    def test_empty_dimensions_no_rules_approve(self):
        assert compute_verdict([]) == ReviewVerdict.APPROVE


# ===================================================================
# TestParseReview (~5 tests)
# ===================================================================


class TestParseReview:
    """Tests for parse_review()."""

    def test_valid_json_with_verdict(self):
        raw = json.dumps(
            {
                "verdict": "approve",
                "summary": "Fix is correct.",
                "issues": [],
                "suggestions": ["Add a comment."],
            }
        )
        result = parse_review(raw)
        assert result.verdict == ReviewVerdict.APPROVE
        assert result.summary == "Fix is correct."
        assert result.suggestions == ["Add a comment."]

    def test_envelope_wrapped(self):
        inner = json.dumps(
            {
                "verdict": "reject",
                "summary": "Type mismatch.",
                "issues": ["Return type changed."],
            }
        )
        envelope = json.dumps({"type": "result", "result": inner})
        result = parse_review(envelope)
        assert result.verdict == ReviewVerdict.REJECT
        assert "Type mismatch" in result.summary

    def test_natural_language_lgtm_approve(self):
        result = parse_review("Everything looks fine. LGTM!")
        assert result.verdict == ReviewVerdict.APPROVE

    def test_natural_language_looks_good_approve(self):
        result = parse_review("The changes looks good to me, ship it.")
        assert result.verdict == ReviewVerdict.APPROVE

    def test_unparseable_request_changes(self):
        """Completely unparseable output falls back to REQUEST_CHANGES."""
        result = parse_review("zzzzzz random noise 12345")
        assert result.verdict == ReviewVerdict.REQUEST_CHANGES
        assert "Could not parse" in result.summary


# ===================================================================
# TestParseStructuredReview (~8 tests)
# ===================================================================


class TestParseStructuredReview:
    """Tests for parse_structured_review()."""

    def _six_dim_json(self, overrides: dict[str, str] | None = None) -> str:
        """Build a 6-dimension review JSON string.

        By default all dimensions pass.  *overrides* maps dimension name to
        verdict (e.g. {"correctness": "fail"}).
        """
        dim_names = [
            "scope_hygiene",
            "security",
            "correctness",
            "compatibility",
            "maintainability",
            "observability",
        ]
        overrides = overrides or {}
        dims = []
        for name in dim_names:
            verdict = overrides.get(name, "pass")
            dims.append({"dimension": name, "verdict": verdict, "finding": f"{name} ok"})
        return json.dumps(
            {
                "dimensions": dims,
                "summary": "Structured review complete.",
                "suggestions": ["Consider refactoring."],
            }
        )

    def test_valid_6dim_all_pass_approve(self):
        raw = self._six_dim_json()
        result = parse_structured_review(raw)
        assert isinstance(result, StructuredReviewResult)
        assert result.verdict == ReviewVerdict.APPROVE
        assert result.summary == "Structured review complete."

    def test_correctness_fail_reject(self):
        """correctness fail -> REJECT per default rules (on_fail=reject)."""
        raw = self._six_dim_json({"correctness": "fail"})
        result = parse_structured_review(raw)
        assert result.verdict == ReviewVerdict.REJECT
        failed_findings = [d.finding for d in result.dimensions if d.verdict.value == "fail"]
        assert any("correctness" in f for f in failed_findings)

    def test_missing_dimensions_key_request_changes(self):
        raw = json.dumps({"summary": "no dims"})
        result = parse_structured_review(raw)
        assert result.verdict == ReviewVerdict.REQUEST_CHANGES

    def test_invalid_dimension_verdict_skipped_silently(self):
        """A dimension with an unrecognized verdict is silently filtered out."""
        raw = json.dumps(
            {
                "dimensions": [
                    {"dimension": "correctness", "verdict": "pass"},
                    {"dimension": "security", "verdict": "banana"},  # invalid
                ],
                "summary": "partial",
            }
        )
        result = parse_structured_review(raw)
        assert result.verdict == ReviewVerdict.APPROVE
        # "banana" dimension should not appear in dimensions
        assert not any(d.dimension == "security" for d in result.dimensions)

    def test_suggestions_field_preserved(self):
        raw = self._six_dim_json()
        result = parse_structured_review(raw)
        assert result.suggestions == ["Consider refactoring."]

    def test_empty_dimensions_list(self):
        """An empty dimensions list means nothing to fail -> APPROVE."""
        raw = json.dumps({"dimensions": [], "summary": "empty"})
        result = parse_structured_review(raw)
        assert result.verdict == ReviewVerdict.APPROVE
        assert result.summary == "empty"

    def test_summary_field_captured(self):
        raw = self._six_dim_json()
        result = parse_structured_review(raw)
        assert result.summary == "Structured review complete."

    def test_custom_rules_override_default(self):
        """Pass custom rules that treat observability/fail as reject."""
        custom_rules = [
            VerdictRule(dimension="observability", on_fail="reject"),
        ]
        raw = self._six_dim_json({"observability": "fail"})
        result = parse_structured_review(raw, rules=custom_rules)
        assert result.verdict == ReviewVerdict.REJECT

    def test_security_fail_reject(self):
        """security fail -> REJECT per default rules (on_fail=reject)."""
        raw = self._six_dim_json({"security": "fail"})
        result = parse_structured_review(raw)
        assert result.verdict == ReviewVerdict.REJECT
        failed_findings = [d.finding for d in result.dimensions if d.verdict.value == "fail"]
        assert any("security" in f for f in failed_findings)

    def test_non_list_suggestions_normalized(self):
        """If suggestions is not a list, it is replaced with an empty list."""
        raw = json.dumps(
            {
                "dimensions": [{"dimension": "correctness", "verdict": "pass"}],
                "summary": "ok",
                "suggestions": "not a list",
            }
        )
        result = parse_structured_review(raw)
        assert result.suggestions == []

    def test_fail_finding_included_in_dimensions(self):
        """The finding from a failing dimension appears in the dimensions list."""
        raw = json.dumps(
            {
                "dimensions": [
                    {"dimension": "correctness", "verdict": "fail", "finding": "wrong return type"},
                ],
                "summary": "needs fix",
            }
        )
        result = parse_structured_review(raw)
        failed_dims = [d for d in result.dimensions if d.verdict == DimensionVerdict.FAIL]
        assert any("wrong return type" in d.finding for d in failed_dims)


# ===================================================================
# format_scope_warnings
# ===================================================================
class TestFormatScopeWarnings:
    def test_empty_blocks_returns_empty_string(self):
        assert format_scope_warnings([]) == ""

    def test_single_block_includes_file_and_code(self):
        blocks = [{"file": "foo.py", "lines": ["import re", "x = 1"], "context": "near line 5"}]
        result = format_scope_warnings(blocks)
        assert "foo.py" in result
        assert "import re" in result
        assert "x = 1" in result
        assert "near line 5" in result
        assert "DELETED" in result

    def test_multiple_blocks(self):
        blocks = [
            {"file": "a.py", "lines": ["line_a"], "context": "near line 10"},
            {"file": "b.py", "lines": ["line_b"], "context": "near line 20"},
        ]
        result = format_scope_warnings(blocks)
        assert "Deletion 1" in result
        assert "Deletion 2" in result
        assert "a.py" in result
        assert "b.py" in result

    def test_long_block_truncated(self):
        blocks = [
            {"file": "big.py", "lines": [f"line {i}" for i in range(25)], "context": "near line 1"}
        ]
        result = format_scope_warnings(blocks)
        assert "10 more lines" in result
