"""Tests for autopr/fixer.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autopr.config import AutoPRConfig, RepoConfig
from autopr.fixer import (
    commit_changes,
    create_worktree,
    ensure_worktree_clean,
    push_branch,
    resolve_worktree_dir,
    run_fix,
)
from autopr.models import Opportunity, Severity

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_config(
    repo_path: Path | None = None,
    worktree_dir: Path | None = None,
    base_branch: str = "main",
    branch_prefix: str = "autopr/",
) -> AutoPRConfig:
    return AutoPRConfig(
        repo=RepoConfig(
            path=repo_path or Path("/repos/myproject"),
            worktree_dir=worktree_dir,
            base_branch=base_branch,
            branch_prefix=branch_prefix,
        ),
    )


def _make_opportunity(**overrides) -> Opportunity:
    defaults = dict(
        id="fix-something",
        title="Fix something",
        type="dead_code",
        severity=Severity.LOW,
        files=["src/app.py"],
        problem="There is a problem.",
        solution="Apply this solution.",
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


# ── TestResolveWorktreeDir ───────────────────────────────────────────────────


class TestResolveWorktreeDir:
    def test_default_no_worktree_dir(self):
        config = _make_config(repo_path=Path("/repos/myproject"))
        result = resolve_worktree_dir(config)
        assert result == Path("/repos/myproject-autopr")

    def test_custom_worktree_dir(self):
        custom = Path("/tmp/worktrees")
        config = _make_config(repo_path=Path("/repos/myproject"), worktree_dir=custom)
        result = resolve_worktree_dir(config)
        assert result == custom


# ── TestCreateWorktree ───────────────────────────────────────────────────────


class TestCreateWorktree:
    @patch("autopr.fixer.subprocess.run")
    def test_success_flow(self, mock_run: MagicMock, tmp_path: Path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_dir = tmp_path / "wt"

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = create_worktree(repo_path, worktree_dir, "autopr/fix-it", "main")

        assert result == worktree_dir / "autopr-fix-it"
        assert mock_run.call_count == 4

        # Verify calls are in the correct order
        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["git", "fetch", "origin", "main"]
        assert calls[1][0][0] == ["git", "branch", "-D", "autopr/fix-it"]
        assert calls[2][0][0] == ["git", "branch", "autopr/fix-it", "origin/main"]
        assert calls[3][0][0] == [
            "git",
            "worktree",
            "add",
            str(worktree_dir / "autopr-fix-it"),
            "autopr/fix-it",
        ]

    @patch("autopr.fixer.subprocess.run")
    def test_slash_in_branch_name_replaced_with_dash(self, mock_run: MagicMock, tmp_path: Path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_dir = tmp_path / "wt"

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = create_worktree(repo_path, worktree_dir, "feat/sub/thing", "main")

        assert result == worktree_dir / "feat-sub-thing"

    @patch("autopr.fixer.remove_worktree")
    @patch("autopr.fixer.subprocess.run")
    def test_stale_worktree_cleaned_up(
        self, mock_run: MagicMock, mock_remove: MagicMock, tmp_path: Path
    ):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_dir = tmp_path / "wt"
        # Simulate that the stale worktree directory already exists
        stale_path = worktree_dir / "autopr-fix-it"
        stale_path.mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        create_worktree(repo_path, worktree_dir, "autopr/fix-it", "main")

        mock_remove.assert_called_once_with(repo_path, stale_path)


# ── TestCommitChanges ────────────────────────────────────────────────────────


class TestCommitChanges:
    @patch("autopr.fixer.subprocess.run")
    def test_stages_and_commits_when_changes_exist(self, mock_run: MagicMock, tmp_path: Path):
        diff_result = MagicMock(stdout=" src/app.py | 2 +-\n 1 file changed\n", returncode=0)

        def side_effect(cmd, **kwargs):
            if cmd[1:3] == ["diff", "--cached"]:
                return diff_result
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result = commit_changes(tmp_path, "fix: something")

        assert result is True
        # git add -A, git diff --cached -U0 (lint check), git diff --cached --stat, git commit
        assert mock_run.call_count == 4

        calls = mock_run.call_args_list
        # git add -A
        assert calls[0][0][0] == ["git", "add", "-A"]
        # git diff --cached -U0 (lint suppression check)
        assert calls[1][0][0] == ["git", "diff", "--cached", "-U0"]
        # git diff --cached --stat
        assert calls[2][0][0] == ["git", "diff", "--cached", "--stat"]
        # git commit -m ...
        assert calls[3][0][0] == ["git", "commit", "-m", "fix: something"]

    @patch("autopr.fixer.subprocess.run")
    def test_returns_false_when_no_changes(self, mock_run: MagicMock, tmp_path: Path):
        diff_result = MagicMock(stdout="", returncode=0)

        def side_effect(cmd, **kwargs):
            if cmd[1:3] == ["diff", "--cached"]:
                return diff_result
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result = commit_changes(tmp_path, "fix: nothing")

        assert result is False
        # git add -A, git diff --cached -U0 (lint check), git diff --cached --stat — no commit
        assert mock_run.call_count == 3


# ── TestEnsureWorktreeClean ──────────────────────────────────────────────────


class TestEnsureWorktreeClean:
    @patch("autopr.fixer.subprocess.run")
    def test_clean_worktree_is_noop(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = ensure_worktree_clean(tmp_path)

        assert result is False
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0] == ["git", "status", "--porcelain"]

    @patch("autopr.fixer.subprocess.run")
    def test_dirty_worktree_commits(self, mock_run: MagicMock, tmp_path: Path):
        def side_effect(cmd, **kwargs):
            if cmd[1:3] == ["status", "--porcelain"]:
                return MagicMock(returncode=0, stdout=" M src/app.py\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result = ensure_worktree_clean(tmp_path, context="test push")

        assert result is True
        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["git", "status", "--porcelain"]
        assert calls[1][0][0] == ["git", "add", "-A"]
        assert calls[2][0][0][0:2] == ["git", "commit"]

    @patch("autopr.fixer.subprocess.run")
    def test_context_in_commit_message(self, mock_run: MagicMock, tmp_path: Path):
        def side_effect(cmd, **kwargs):
            if cmd[1:3] == ["status", "--porcelain"]:
                return MagicMock(returncode=0, stdout="?? new_file.py\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        ensure_worktree_clean(tmp_path, context="for opportunity-123")

        commit_call = mock_run.call_args_list[2]
        commit_msg = commit_call[1].get("input") or commit_call[0][0][3]
        assert "for opportunity-123" in commit_msg


# ── TestPushBranch ───────────────────────────────────────────────────────────


class TestPushBranch:
    @patch("autopr.fixer.ensure_worktree_clean")
    @patch("autopr.fixer.subprocess.run")
    def test_calls_ensure_clean_then_git_push(
        self, mock_run: MagicMock, mock_ensure: MagicMock, tmp_path: Path
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_ensure.return_value = False

        push_branch(tmp_path, "autopr/fix-it")

        mock_ensure.assert_called_once_with(tmp_path, context="push autopr/fix-it")
        mock_run.assert_called_once_with(
            ["git", "push", "-u", "origin", "autopr/fix-it"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )


# ── TestRunFix ───────────────────────────────────────────────────────────────


class TestRunFix:
    @patch("autopr.fixer.commit_changes", return_value=True)
    @patch("autopr.fixer.create_worktree")
    @patch("autopr.fixer.subprocess.run")
    def test_success_flow(
        self,
        mock_run: MagicMock,
        mock_create_wt: MagicMock,
        mock_commit: MagicMock,
        tmp_path: Path,
    ):
        wt_path = tmp_path / "wt" / "autopr-fix-something"
        mock_create_wt.return_value = wt_path
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        branch, result_path = run_fix(opp, config)

        assert branch == "autopr/fix-something"
        assert result_path == wt_path
        mock_create_wt.assert_called_once()
        mock_commit.assert_called_once()
        # Claude subprocess was invoked
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][0] == "claude"

    @patch("autopr.fixer.commit_changes")
    @patch("autopr.fixer.create_worktree")
    @patch("autopr.fixer.subprocess.run")
    def test_claude_failure_raises_runtime_error(
        self,
        mock_run: MagicMock,
        mock_create_wt: MagicMock,
        mock_commit: MagicMock,
        tmp_path: Path,
    ):
        wt_path = tmp_path / "wt" / "autopr-fix-something"
        mock_create_wt.return_value = wt_path
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Claude crashed")

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        with pytest.raises(RuntimeError, match="Claude Code fixer failed"):
            run_fix(opp, config)

        # commit_changes should never be called when claude fails
        mock_commit.assert_not_called()

    @patch("autopr.fixer.commit_changes", return_value=False)
    @patch("autopr.fixer.create_worktree")
    @patch("autopr.fixer.subprocess.run")
    def test_no_changes_raises_runtime_error(
        self,
        mock_run: MagicMock,
        mock_create_wt: MagicMock,
        mock_commit: MagicMock,
        tmp_path: Path,
    ):
        wt_path = tmp_path / "wt" / "autopr-fix-something"
        mock_create_wt.return_value = wt_path
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        with pytest.raises(RuntimeError, match="no changes"):
            run_fix(opp, config)
