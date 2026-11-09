"""Tests for the conflict resolver module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autopr.config import AutomationConfig, HealthCheckConfig
from autopr.conflict_resolver import (
    abort_rebase,
    attempt_merge,
    attempt_rebase,
    continue_rebase,
    get_conflicting_files,
    resolve_conflicts_with_claude,
    resolve_pr_conflicts,
)
from autopr.models import HealthCheckResult


@pytest.fixture()
def automation_config() -> AutomationConfig:
    return AutomationConfig()


@pytest.fixture()
def pr_info() -> dict:
    return {
        "number": 42,
        "headRefName": "autopr/fix-batch",
        "title": "Batch entity queries",
        "problem": "N+1 query in loop",
        "solution": "Use bulk SELECT",
    }


class TestAttemptRebase:
    def test_clean_rebase_returns_true(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert attempt_rebase(tmp_path, "main") is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["git", "rebase", "origin/main"]

    def test_conflicting_rebase_returns_false(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert attempt_rebase(tmp_path, "main") is False


class TestAbortRebase:
    def test_calls_rebase_abort(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            abort_rebase(tmp_path)
            args = mock_run.call_args[0][0]
            assert args == ["git", "rebase", "--abort"]


class TestAttemptMerge:
    def test_clean_merge_returns_true(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert attempt_merge(tmp_path, "main") is True
            args = mock_run.call_args[0][0]
            assert args == ["git", "merge", "--no-commit", "origin/main"]

    def test_conflicting_merge_returns_false(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert attempt_merge(tmp_path, "main") is False


class TestGetConflictingFiles:
    def test_parses_output(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="src/api.py\nsrc/utils.py\n",
                returncode=0,
            )
            files = get_conflicting_files(tmp_path)
            assert files == ["src/api.py", "src/utils.py"]

    def test_empty_output(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            files = get_conflicting_files(tmp_path)
            assert files == []


class TestResolveConflictsWithClaude:
    def test_success_no_markers_remain(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            # Claude call succeeds, grep finds no markers
            mock_run.side_effect = [
                MagicMock(returncode=0),  # claude -p
                MagicMock(returncode=1, stdout=""),  # grep (no matches = rc 1)
            ]
            result = resolve_conflicts_with_claude(
                tmp_path,
                ["src/api.py"],
                {"title": "Fix", "problem": "Bug", "solution": "Patch"},
                "claude-opus-4-6",
                "rebase",
                "main",
            )
            assert result is True

    def test_failure_markers_remain(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # claude -p succeeds
                MagicMock(returncode=0, stdout="src/api.py:5:<<<<<<< HEAD"),  # grep finds markers
            ]
            result = resolve_conflicts_with_claude(
                tmp_path,
                ["src/api.py"],
                {"title": "Fix", "problem": "Bug", "solution": "Patch"},
                "claude-opus-4-6",
                "rebase",
                "main",
            )
            assert result is False

    def test_claude_timeout(self, tmp_path: Path):
        import subprocess as sp

        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = sp.TimeoutExpired("claude", 600)
            result = resolve_conflicts_with_claude(
                tmp_path,
                ["src/api.py"],
                {"title": "Fix", "problem": "Bug", "solution": "Patch"},
                "claude-opus-4-6",
                "rebase",
                "main",
            )
            assert result is False


class TestContinueRebase:
    def test_single_step_success(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # git add -A
                MagicMock(returncode=0),  # git rebase --continue
            ]
            assert continue_rebase(tmp_path) is True

    def test_multi_step_needs_retry(self, tmp_path: Path):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            with patch("autopr.conflict_resolver.get_conflicting_files") as mock_conflicts:
                mock_run.side_effect = [
                    MagicMock(returncode=0),  # git add -A (1st)
                    MagicMock(returncode=1),  # git rebase --continue fails
                    MagicMock(returncode=0),  # git add -A (2nd)
                    MagicMock(returncode=0),  # git rebase --continue succeeds
                ]
                mock_conflicts.return_value = []  # No new conflicts
                assert continue_rebase(tmp_path, max_retries=2) is True


class TestResolvePrConflicts:
    @patch("autopr.conflict_resolver._commit_if_needed", return_value=False)
    @patch("autopr.conflict_resolver._run_post_resolution_checks", return_value="")
    def test_clean_rebase_succeeds(
        self,
        _mock_post_checks: MagicMock,
        _mock_commit: MagicMock,
        tmp_path: Path,
        automation_config,
        pr_info,
    ):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # git fetch
                MagicMock(returncode=0),  # git rebase (clean)
                MagicMock(returncode=0),  # git push --force-with-lease
            ]
            result = resolve_pr_conflicts(
                tmp_path, tmp_path, pr_info, "main", "claude-opus-4-6", automation_config
            )
            assert result.status == "clean_rebase"
            assert result.strategy_used == "rebase"

    @patch("autopr.conflict_resolver._run_post_resolution_checks", return_value="")
    def test_rebase_conflict_falls_back_to_merge(
        self,
        _mock_post_checks: MagicMock,
        tmp_path: Path,
        automation_config,
        pr_info,
    ):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            with patch("autopr.conflict_resolver.get_conflicting_files") as mock_conflicts:
                with patch("autopr.conflict_resolver.resolve_conflicts_with_claude") as mock_claude:
                    mock_conflicts.side_effect = [
                        ["src/api.py"],  # rebase conflicts
                        [],  # merge conflicts check (empty = no conflicts after merge)
                    ]
                    mock_claude.return_value = False  # Claude can't resolve rebase
                    mock_run.side_effect = [
                        MagicMock(returncode=0),  # git fetch
                        MagicMock(returncode=1),  # git rebase (fails)
                        MagicMock(returncode=0),  # git rebase --abort
                        MagicMock(returncode=0),  # git reset --hard
                        MagicMock(returncode=0),  # git merge --no-commit (clean)
                        MagicMock(returncode=0),  # git add -A
                        MagicMock(returncode=0),  # git commit
                        MagicMock(returncode=0),  # git push
                    ]
                    result = resolve_pr_conflicts(
                        tmp_path, tmp_path, pr_info, "main", "claude-opus-4-6", automation_config
                    )
                    assert result.status == "conflict_resolved"
                    assert result.strategy_used == "merge"

    def test_all_strategies_fail_returns_failed(self, tmp_path: Path, automation_config, pr_info):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            with patch("autopr.conflict_resolver.get_conflicting_files") as mock_conflicts:
                with patch("autopr.conflict_resolver.resolve_conflicts_with_claude") as mock_claude:
                    mock_conflicts.side_effect = [
                        ["src/api.py"],  # rebase conflicts
                        ["src/api.py"],  # merge conflicts
                    ]
                    mock_claude.return_value = False  # Claude fails both times
                    mock_run.side_effect = [
                        MagicMock(returncode=0),  # git fetch
                        MagicMock(returncode=1),  # git rebase (fails)
                        MagicMock(returncode=0),  # git rebase --abort
                        MagicMock(returncode=0),  # git reset --hard
                        MagicMock(returncode=1),  # git merge (fails)
                        MagicMock(returncode=0),  # git merge --abort
                    ]
                    result = resolve_pr_conflicts(
                        tmp_path, tmp_path, pr_info, "main", "claude-opus-4-6", automation_config
                    )
                    assert result.status == "failed"
                    assert result.strategy_used == "none"
                    assert "src/api.py" in result.conflicting_files

    @patch("autopr.conflict_resolver._commit_if_needed", return_value=False)
    @patch("autopr.conflict_resolver._run_post_resolution_checks", return_value="")
    def test_force_with_lease_used_for_rebase_push(
        self,
        _mock_post_checks: MagicMock,
        _mock_commit: MagicMock,
        tmp_path: Path,
        automation_config,
        pr_info,
    ):
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # git fetch
                MagicMock(returncode=0),  # git rebase (clean)
                MagicMock(returncode=0),  # git push
            ]
            resolve_pr_conflicts(
                tmp_path, tmp_path, pr_info, "main", "claude-opus-4-6", automation_config
            )
            # The push call should use --force-with-lease
            push_call = mock_run.call_args_list[2]
            assert "--force-with-lease" in push_call[0][0]

    @patch("autopr.conflict_resolver._commit_if_needed")
    @patch("autopr.conflict_resolver._revert_protected_files")
    @patch("autopr.conflict_resolver._has_conflict_markers", return_value=False)
    @patch("autopr.conflict_resolver.run_health_checks")
    def test_clean_rebase_runs_health_checks_before_push(
        self,
        mock_health: MagicMock,
        _mock_markers: MagicMock,
        _mock_revert: MagicMock,
        mock_commit: MagicMock,
        tmp_path: Path,
        automation_config,
        pr_info,
    ):
        mock_health.return_value = [HealthCheckResult(passed=True, command="make test")]
        mock_commit.return_value = False
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # git fetch
                MagicMock(returncode=0),  # git rebase (clean)
                MagicMock(returncode=0),  # git push --force-with-lease
            ]
            result = resolve_pr_conflicts(
                tmp_path,
                tmp_path,
                pr_info,
                "main",
                "claude-opus-4-6",
                automation_config,
                health_checks=HealthCheckConfig(
                    commands=["make test"],
                    timeout_seconds=60,
                    max_fix_attempts=1,
                ),
                protected_files=["Makefile"],
            )
            assert result.status == "clean_rebase"
            mock_health.assert_called_once()
            push_call = mock_run.call_args_list[2]
            assert "--force-with-lease" in push_call[0][0]

    @patch("autopr.conflict_resolver._commit_if_needed")
    @patch("autopr.conflict_resolver._run_post_resolution_checks", return_value="")
    @patch("autopr.conflict_resolver.continue_rebase", return_value=True)
    @patch("autopr.conflict_resolver.resolve_conflicts_with_claude", return_value=True)
    @patch("autopr.conflict_resolver.get_conflicting_files", return_value=["Makefile"])
    def test_rebase_conflict_path_creates_follow_up_commit(
        self,
        _mock_conflicts: MagicMock,
        _mock_claude: MagicMock,
        _mock_continue: MagicMock,
        _mock_post_checks: MagicMock,
        mock_commit: MagicMock,
        tmp_path: Path,
        automation_config,
        pr_info,
    ):
        mock_commit.return_value = True
        with patch("autopr.conflict_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # git fetch
                MagicMock(returncode=1),  # git rebase (fails -> conflicts)
                MagicMock(returncode=0),  # git push --force-with-lease
            ]
            result = resolve_pr_conflicts(
                tmp_path, tmp_path, pr_info, "main", "claude-opus-4-6", automation_config
            )
            assert result.status == "conflict_resolved"
            mock_commit.assert_called_once()
