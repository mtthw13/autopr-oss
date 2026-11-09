"""Tests for autopr/codex_reviewer.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from autopr.codex_reviewer import (
    _parse_codex_result,
    codex_review_diff,
    codex_review_loop,
    codex_review_scope,
)
from autopr.models import CodexReviewResult, Opportunity, Severity


def make_opportunity() -> Opportunity:
    return Opportunity(
        id="research-performance",
        title="Improve performance metrics via research",
        type="research",
        severity=Severity.MEDIUM,
        files=["frontend/app/layout.tsx"],
        problem="Performance is worse than baseline.",
        solution="Apply iterative performance improvements.",
    )


class TestParseCodexResult:
    def test_accepts_valid_json_approval(self):
        review = _parse_codex_result(
            '{"approve": true, "risks": [], "summary": "No material risks found"}',
            "diff",
        )

        assert review is not None
        assert review.approve is True
        assert review.risks == []
        assert review.summary == "No material risks found"

    def test_accepts_valid_json_rejection(self):
        review = _parse_codex_result(
            json.dumps(
                {
                    "approve": False,
                    "risks": [{"category": "AUTH", "detail": "Session token can leak"}],
                    "summary": "Found a material auth regression",
                }
            ),
            "diff",
        )

        assert review is not None
        assert review.approve is False
        assert review.risks == [{"category": "AUTH", "detail": "Session token can leak"}]
        assert review.summary == "Found a material auth regression"

    def test_accepts_markdown_wrapped_json(self):
        review = _parse_codex_result(
            '```json\n{"approve": true, "risks": [], "summary": "Safe plan"}\n```',
            "plan",
        )

        assert review is not None
        assert review.approve is True
        assert review.summary == "Safe plan"

    def test_accepts_auth_category_bullets(self):
        raw = (
            "Material risks found.\n\n"
            "- [AUTH] Middleware skips auth checks on refresh.\n"
            "  A signed-out user could see protected content during a stale refresh window.\n"
            "- [UX] Loading fallback disappears before modal content is ready.\n"
            "  First-login users can briefly interact with the page underneath.\n"
        )

        review = _parse_codex_result(raw, "diff")

        assert review is not None
        assert review.approve is False
        assert [risk["category"] for risk in review.risks] == ["AUTH", "UX"]
        assert review.summary == "Material risks found."

    def test_accepts_p1_bullets(self):
        raw = (
            "One blocking issue.\n\n"
            "- [P1] Background task can run before transaction commit.\n"
            "  Users may observe state that never committed.\n"
        )

        review = _parse_codex_result(raw, "diff")

        assert review is not None
        assert review.approve is False
        assert review.risks == [
            {
                "category": "P1",
                "detail": (
                    "Background task can run before transaction commit.\n"
                    "Users may observe state that never committed."
                ),
            }
        ]

    def test_returns_none_for_empty_input(self):
        assert _parse_codex_result("", "diff") is None
        assert _parse_codex_result("   ", "diff") is None

    def test_approves_on_lgtm_text(self):
        review = _parse_codex_result("LGTM — no material risks found.", "diff")
        assert review is not None
        assert review.approve is True


class TestCodexReviewScope:
    @patch("autopr.codex_reviewer._run_review_command")
    def test_codex_review_scope_parses_successful_result(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"approve": true, "risks": [], "summary": "Safe scope"}',
            stderr="",
        )

        review = codex_review_scope(make_opportunity(), tmp_path)

        assert review.approve is True
        assert review.summary == "Safe scope"
        assert review.review_type == "scope"
        mock_run.assert_called_once()

    @patch("autopr.codex_reviewer._run_review_command")
    def test_codex_review_scope_defaults_approve_on_timeout(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ):
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=300)

        review = codex_review_scope(make_opportunity(), tmp_path)

        assert review.approve is True
        assert review.review_type == "scope"


class TestCodexReviewDiff:
    @patch("autopr.codex_reviewer._run_review_command")
    def test_codex_review_diff_uses_review_subcommand(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"approve": true, "risks": [], "summary": "No material risks found"}',
            stderr="",
        )

        review = codex_review_diff(make_opportunity(), tmp_path, base_branch="main")

        assert review.approve is True
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        # codex_review_diff passes codex_cmd with "review" subcommand
        assert call_kwargs.kwargs.get("codex_cmd") == [
            "codex",
            "exec",
            "review",
            "--full-auto",
            "--base",
            "main",
        ]

    @patch("autopr.codex_reviewer._run_review_command")
    def test_codex_review_diff_unparseable_defaults_approve(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="random gibberish that cannot be parsed",
            stderr="",
        )

        review = codex_review_diff(make_opportunity(), tmp_path)

        assert review.approve is True
        assert review.summary == "Unparseable output"


class TestCodexReviewLoop:
    @patch("autopr.codex_reviewer.apply_codex_feedback")
    @patch("autopr.codex_reviewer.codex_review_diff")
    def test_returns_on_first_approval(
        self,
        mock_review_diff: MagicMock,
        mock_apply_feedback: MagicMock,
        tmp_path: Path,
    ):
        mock_review_diff.return_value = CodexReviewResult(
            approve=True,
            summary="No material risks",
            review_type="diff",
        )

        review = codex_review_loop(make_opportunity(), tmp_path, max_iterations=3)

        assert review.approve is True
        assert review.iterations == 1
        mock_apply_feedback.assert_not_called()
        mock_review_diff.assert_called_once()

    @patch("autopr.codex_reviewer.apply_codex_feedback")
    @patch("autopr.codex_reviewer.codex_review_diff")
    def test_applies_feedback_once_on_reject_then_approve(
        self,
        mock_review_diff: MagicMock,
        mock_apply_feedback: MagicMock,
        tmp_path: Path,
    ):
        reject = CodexReviewResult(
            approve=False,
            risks=[{"category": "AUTH", "detail": "Token leak"}],
            summary="Auth issue found",
            review_type="diff",
        )
        approve = CodexReviewResult(
            approve=True,
            summary="Fixed",
            review_type="diff",
        )
        mock_review_diff.side_effect = [reject, approve]

        review = codex_review_loop(make_opportunity(), tmp_path, max_iterations=3)

        assert review.approve is True
        assert review.iterations == 2
        mock_apply_feedback.assert_called_once()

    @patch("autopr.codex_reviewer.apply_codex_feedback")
    @patch("autopr.codex_reviewer.codex_review_diff")
    def test_exhausts_iterations_on_persistent_rejection(
        self,
        mock_review_diff: MagicMock,
        mock_apply_feedback: MagicMock,
        tmp_path: Path,
    ):
        reject = CodexReviewResult(
            approve=False,
            risks=[{"category": "DATA", "detail": "Race condition"}],
            summary="Data issue",
            review_type="diff",
        )
        mock_review_diff.return_value = reject

        review = codex_review_loop(make_opportunity(), tmp_path, max_iterations=2)

        assert review.approve is False
        assert mock_review_diff.call_count == 2
        # Feedback applied only between iterations, not after the last one
        assert mock_apply_feedback.call_count == 1
