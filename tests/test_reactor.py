"""Tests for the reactor module."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autopr.config import AutoPRConfig, RepoConfig
from autopr.models import ReactResult
from autopr.reactor import (
    dispatch_event,
    filter_actionable_comments,
    get_latest_bot_commit_timestamp,
    handle_base_branch_updated,
    handle_pr_comment,
    handle_review_feedback,
    post_result_comment,
)


@pytest.fixture()
def autopr_config(tmp_path: Path) -> AutoPRConfig:
    return AutoPRConfig(repo=RepoConfig(path=tmp_path / "repo"))


class TestGetLatestBotCommitTimestamp:
    def test_returns_timestamp(self, tmp_path: Path):
        with patch("autopr.reactor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="2025-01-15T10:30:00+00:00\n",
                returncode=0,
            )
            ts = get_latest_bot_commit_timestamp(
                tmp_path, "autopr[bot]", "autopr[bot]@users.noreply.github.com"
            )
            assert ts is not None
            assert ts.year == 2025
            assert ts.month == 1

    def test_returns_none_when_no_commits(self, tmp_path: Path):
        with patch("autopr.reactor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            ts = get_latest_bot_commit_timestamp(
                tmp_path, "autopr[bot]", "autopr[bot]@users.noreply.github.com"
            )
            assert ts is None


class TestFilterActionableComments:
    def test_drops_empty_comments(self):
        comments = [{"body": "", "author": {"login": "user1"}}]
        assert filter_actionable_comments(comments, None) == []

    def test_drops_bot_authored(self):
        comments = [{"body": "Fixed.", "author": {"login": "autopr[bot]"}}]
        assert filter_actionable_comments(comments, None, bot_name="autopr[bot]") == []

    def test_drops_comments_before_cutoff(self):
        cutoff = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        comments = [
            {
                "body": "Please fix this",
                "author": {"login": "reviewer"},
                "createdAt": "2025-01-15T09:00:00+00:00",  # Before cutoff
            },
        ]
        assert filter_actionable_comments(comments, cutoff) == []

    def test_keeps_comments_after_cutoff(self):
        cutoff = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        comments = [
            {
                "body": "Please fix this",
                "author": {"login": "reviewer"},
                "createdAt": "2025-01-15T11:00:00+00:00",  # After cutoff
            },
        ]
        result = filter_actionable_comments(comments, cutoff)
        assert len(result) == 1
        assert result[0]["body"] == "Please fix this"

    def test_keeps_all_when_no_cutoff(self):
        comments = [
            {"body": "Fix this", "author": {"login": "reviewer"}},
            {"body": "And this", "author": {"login": "reviewer2"}},
        ]
        result = filter_actionable_comments(comments, None)
        assert len(result) == 2


class TestPostResultComment:
    def test_fixed_format(self, tmp_path: Path):
        result = ReactResult(
            pr_number=42,
            event="ci_failed",
            action="fixed",
            summary="Fixed CI failure.",
            pushed_sha="abc123def456",
        )
        with patch("autopr.reactor.subprocess.run") as mock_run:
            post_result_comment(tmp_path, 42, result)
            args = mock_run.call_args[0][0]
            assert "gh" in args
            assert "pr" in args
            assert "comment" in args
            # Check body contains short sha
            body_arg = args[args.index("--body") + 1]
            assert "abc123de" in body_arg

    def test_needs_human_format(self, tmp_path: Path):
        result = ReactResult(
            pr_number=42,
            event="base_branch_updated",
            action="needs_human",
            summary="Merge conflict not resolved safely.",
        )
        with patch("autopr.reactor.subprocess.run") as mock_run:
            post_result_comment(tmp_path, 42, result)
            body_arg = mock_run.call_args[0][0][mock_run.call_args[0][0].index("--body") + 1]
            assert "human" in body_arg.lower()


class TestHandlePrComment:
    def test_no_command_returns_no_action(self, tmp_path: Path, autopr_config):
        result = handle_pr_comment(
            tmp_path,
            42,
            "autopr/test",
            "Just a regular comment",
            "/autopr fix",
            "claude-opus-4-6",
            autopr_config,
        )
        assert result.action == "no_action"

    def test_command_triggers_fix(self, tmp_path: Path, autopr_config):
        with patch("autopr.reactor.handle_review_feedback") as mock_handler:
            mock_handler.return_value = ReactResult(
                pr_number=42, event="pr_comment", action="fixed", summary="Done"
            )
            result = handle_pr_comment(
                tmp_path,
                42,
                "autopr/test",
                "/autopr fix please add tests",
                "/autopr fix",
                "claude-opus-4-6",
                autopr_config,
            )
            assert result.action == "fixed"
            # Check that instructions after command are passed
            call_kwargs = mock_handler.call_args
            assert "please add tests" in call_kwargs.kwargs.get(
                "comment_body", call_kwargs[1].get("comment_body", "")
            )

    def test_command_with_no_trailing_text(self, tmp_path: Path, autopr_config):
        with patch("autopr.reactor.handle_review_feedback") as mock_handler:
            mock_handler.return_value = ReactResult(
                pr_number=42, event="pr_comment", action="fixed", summary="Done"
            )
            handle_pr_comment(
                tmp_path,
                42,
                "autopr/test",
                "/autopr fix",
                "/autopr fix",
                "claude-opus-4-6",
                autopr_config,
            )
            # When no trailing instructions, the full comment body is used
            call_kwargs = mock_handler.call_args
            comment = call_kwargs.kwargs.get("comment_body", call_kwargs[1].get("comment_body", ""))
            assert "/autopr fix" in comment


class TestHandleReviewFeedback:
    @patch("autopr.reactor._cleanup_worktree")
    @patch("autopr.reactor._commit_and_push")
    @patch("autopr.reviewer.run_fixer_agent")
    @patch("autopr.reactor.subprocess.run")
    @patch("autopr.reactor._fetch_review_comments")
    @patch("autopr.reactor.get_latest_bot_commit_timestamp")
    @patch("autopr.reactor._create_temp_worktree")
    def test_review_submission_uses_new_inline_comments(
        self,
        mock_worktree: MagicMock,
        mock_cutoff: MagicMock,
        mock_fetch_comments: MagicMock,
        mock_subprocess: MagicMock,
        mock_fixer: MagicMock,
        mock_commit: MagicMock,
        _mock_cleanup: MagicMock,
        tmp_path: Path,
        autopr_config,
    ):
        mock_worktree.return_value = tmp_path
        mock_cutoff.return_value = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        mock_fetch_comments.return_value = [
            {
                "body": "Please add the null guard",
                "author": {"login": "reviewer"},
                "createdAt": "2025-01-15T11:00:00+00:00",
            }
        ]
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"body": "## Problem\nBug\n## Solution\nFix\n", "title": "Fix bug"}),
            stderr="",
        )
        mock_commit.return_value = "abc12345"

        result = handle_review_feedback(
            repo_path=tmp_path,
            pr_number=42,
            pr_branch="autopr/test",
            comment_body="",
            comment_id=None,
            review_id=9,
            comment_author="reviewer",
            created_at="2025-01-15T11:00:00+00:00",
            model="claude-opus-4-6",
            config=autopr_config,
        )

        assert result.action == "fixed"
        prompt = mock_fixer.call_args[0][1]
        assert "Please add the null guard" in prompt

    @patch("autopr.reactor._cleanup_worktree")
    @patch("autopr.reactor._commit_and_push")
    @patch("autopr.reviewer.run_fixer_agent")
    @patch("autopr.reactor.subprocess.run")
    @patch("autopr.reactor._fetch_review_comments")
    @patch("autopr.reactor.get_latest_bot_commit_timestamp")
    @patch("autopr.reactor._create_temp_worktree")
    def test_review_submission_with_only_old_comments_is_no_action(
        self,
        mock_worktree: MagicMock,
        mock_cutoff: MagicMock,
        mock_fetch_comments: MagicMock,
        mock_subprocess: MagicMock,
        mock_fixer: MagicMock,
        mock_commit: MagicMock,
        _mock_cleanup: MagicMock,
        tmp_path: Path,
        autopr_config,
    ):
        mock_worktree.return_value = tmp_path
        mock_cutoff.return_value = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        mock_fetch_comments.return_value = [
            {
                "body": "Old feedback",
                "author": {"login": "reviewer"},
                "createdAt": "2025-01-15T09:00:00+00:00",
            }
        ]
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="{}", stderr="")

        result = handle_review_feedback(
            repo_path=tmp_path,
            pr_number=42,
            pr_branch="autopr/test",
            comment_body="",
            comment_id=None,
            review_id=9,
            comment_author="reviewer",
            created_at="2025-01-15T11:00:00+00:00",
            model="claude-opus-4-6",
            config=autopr_config,
        )

        assert result.action == "no_action"
        mock_fixer.assert_not_called()
        mock_commit.assert_not_called()


class TestHandleBaseBranchUpdated:
    @patch("autopr.reactor.subprocess.run")
    def test_failed_update_branch_returns_needs_human(
        self,
        mock_subprocess: MagicMock,
        tmp_path: Path,
        autopr_config,
    ):
        mock_subprocess.side_effect = [
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "mergeStateStatus": "BEHIND",
                        "mergeable": "MERGEABLE",
                        "title": "Fix bug",
                        "body": "",
                    }
                ),
                stderr="",
            ),
            MagicMock(returncode=1, stdout="", stderr="branch update failed"),
        ]

        result = handle_base_branch_updated(
            repo_path=tmp_path,
            pr_number=42,
            pr_branch="autopr/test",
            base_branch="main",
            model="claude-opus-4-6",
            config=autopr_config,
        )

        assert result.action == "needs_human"
        assert "branch update failed" in result.summary


class TestDispatchEvent:
    def test_routes_ci_failed(self, tmp_path: Path, autopr_config):
        with patch("autopr.reactor.handle_ci_failed") as mock_handler:
            mock_handler.return_value = ReactResult(
                pr_number=42, event="ci_failed", action="fixed", summary="Done"
            )
            with patch("autopr.reactor.post_result_comment"):
                result = dispatch_event(
                    "ci_failed",
                    tmp_path,
                    42,
                    "autopr/test",
                    "claude-opus-4-6",
                    autopr_config,
                    head_sha="abc123",
                )
            assert result.action == "fixed"
            mock_handler.assert_called_once()

    def test_routes_review_submitted(self, tmp_path: Path, autopr_config):
        with patch("autopr.reactor.handle_review_feedback") as mock_handler:
            mock_handler.return_value = ReactResult(
                pr_number=42, event="review_submitted", action="fixed", summary="Done"
            )
            with patch("autopr.reactor.post_result_comment"):
                result = dispatch_event(
                    "review_submitted",
                    tmp_path,
                    42,
                    "autopr/test",
                    "claude-opus-4-6",
                    autopr_config,
                    head_sha="abc123",
                    comment_body="Fix this",
                )
            assert result.action == "fixed"

    def test_routes_pr_comment(self, tmp_path: Path, autopr_config):
        with patch("autopr.reactor.handle_pr_comment") as mock_handler:
            mock_handler.return_value = ReactResult(
                pr_number=42, event="pr_comment", action="no_action", summary="No command"
            )
            result = dispatch_event(
                "pr_comment",
                tmp_path,
                42,
                "autopr/test",
                "claude-opus-4-6",
                autopr_config,
                head_sha="abc123",
                comment_body="Just a comment",
            )
            assert result.action == "no_action"

    def test_routes_base_branch_updated(self, tmp_path: Path, autopr_config):
        with patch("autopr.reactor.handle_base_branch_updated") as mock_handler:
            mock_handler.return_value = ReactResult(
                pr_number=42, event="base_branch_updated", action="fixed", summary="Rebased"
            )
            with patch("autopr.reactor.post_result_comment"):
                result = dispatch_event(
                    "base_branch_updated",
                    tmp_path,
                    42,
                    "autopr/test",
                    "claude-opus-4-6",
                    autopr_config,
                    head_sha="abc123",
                )
            assert result.action == "fixed"

    def test_unknown_event_returns_skipped(self, tmp_path: Path, autopr_config):
        result = dispatch_event(
            "unknown_event",
            tmp_path,
            42,
            "autopr/test",
            "claude-opus-4-6",
            autopr_config,
            head_sha="abc123",
        )
        assert result.action == "skipped"
        assert "Unknown" in result.summary

    def test_dry_run_skips(self, tmp_path: Path, autopr_config):
        result = dispatch_event(
            "ci_failed",
            tmp_path,
            42,
            "autopr/test",
            "claude-opus-4-6",
            autopr_config,
            head_sha="abc123",
            dry_run=True,
        )
        assert result.action == "skipped"
        assert "Dry run" in result.summary

    def test_no_action_skips_comment(self, tmp_path: Path, autopr_config):
        with patch("autopr.reactor.handle_pr_comment") as mock_handler:
            mock_handler.return_value = ReactResult(
                pr_number=42, event="pr_comment", action="no_action", summary=""
            )
            with patch("autopr.reactor.post_result_comment") as mock_comment:
                dispatch_event(
                    "pr_comment",
                    tmp_path,
                    42,
                    "autopr/test",
                    "claude-opus-4-6",
                    autopr_config,
                    head_sha="abc123",
                )
                mock_comment.assert_not_called()
