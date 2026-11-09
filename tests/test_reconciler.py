"""Tests for the reconciler module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autopr.config import AutoPRConfig, RepoConfig
from autopr.models import ConflictResolutionResult
from autopr.reconciler import list_managed_prs, reconcile_open_prs


@pytest.fixture()
def autopr_config(tmp_path: Path) -> AutoPRConfig:
    return AutoPRConfig(repo=RepoConfig(path=tmp_path / "repo"))


@pytest.fixture()
def sample_prs() -> list[dict]:
    return [
        {
            "number": 1,
            "headRefName": "autopr/fix-batch",
            "title": "Batch queries",
            "body": "",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BEHIND",
            "labels": [],
        },
        {
            "number": 2,
            "headRefName": "autopr/remove-dead-code",
            "title": "Remove dead code",
            "body": "",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "labels": [],
        },
        {
            "number": 3,
            "headRefName": "feature/unrelated",
            "title": "Unrelated feature",
            "body": "",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BEHIND",
            "labels": [{"name": "autopr-managed"}],
        },
        {
            "number": 4,
            "headRefName": "feature/not-managed",
            "title": "Not managed",
            "body": "",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BEHIND",
            "labels": [],
        },
        {
            "number": 5,
            "headRefName": "autopr/conflicting",
            "title": "Conflicting PR",
            "body": "",
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
            "labels": [],
        },
    ]


class TestListManagedPrs:
    def test_filters_by_prefix(self, tmp_path: Path, sample_prs):
        with patch("autopr.reconciler.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(sample_prs),
                returncode=0,
            )
            managed = list_managed_prs(tmp_path, ["autopr/"], [], "main")
            branch_names = [pr["headRefName"] for pr in managed]
            assert "autopr/fix-batch" in branch_names
            assert "autopr/remove-dead-code" in branch_names
            assert "autopr/conflicting" in branch_names
            assert "feature/not-managed" not in branch_names
            assert "feature/unrelated" not in branch_names

    def test_filters_by_label(self, tmp_path: Path, sample_prs):
        with patch("autopr.reconciler.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(sample_prs),
                returncode=0,
            )
            managed = list_managed_prs(tmp_path, [], ["autopr-managed"], "main")
            branch_names = [pr["headRefName"] for pr in managed]
            assert "feature/unrelated" in branch_names
            assert "feature/not-managed" not in branch_names

    def test_prefix_and_label_combined(self, tmp_path: Path, sample_prs):
        with patch("autopr.reconciler.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(sample_prs),
                returncode=0,
            )
            managed = list_managed_prs(tmp_path, ["autopr/"], ["autopr-managed"], "main")
            assert len(managed) == 4  # 3 autopr/ + 1 labeled

    def test_non_managed_skipped(self, tmp_path: Path, sample_prs):
        with patch("autopr.reconciler.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(sample_prs),
                returncode=0,
            )
            managed = list_managed_prs(tmp_path, ["autopr/"], ["autopr-managed"], "main")
            branch_names = [pr["headRefName"] for pr in managed]
            assert "feature/not-managed" not in branch_names


class TestReconcileOpenPrs:
    def test_behind_triggers_update_branch(self, tmp_path: Path, autopr_config):
        prs = [
            {
                "number": 1,
                "headRefName": "autopr/fix",
                "title": "Fix",
                "body": "",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "BEHIND",
                "labels": [],
            }
        ]
        with patch("autopr.reconciler.list_managed_prs", return_value=prs):
            with patch("autopr.reconciler.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0),  # git fetch
                    MagicMock(returncode=0),  # gh pr update-branch
                ]
                result = reconcile_open_prs(tmp_path, autopr_config)
                assert result.behind_updated == 1
                # Verify update-branch was called with --rebase
                update_call = mock_run.call_args_list[1]
                assert "update-branch" in update_call[0][0]
                assert "--rebase" in update_call[0][0]

    def test_clean_is_noop(self, tmp_path: Path, autopr_config):
        prs = [
            {
                "number": 1,
                "headRefName": "autopr/fix",
                "title": "Fix",
                "body": "",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "labels": [],
            }
        ]
        with patch("autopr.reconciler.list_managed_prs", return_value=prs):
            with patch("autopr.reconciler.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)  # git fetch
                result = reconcile_open_prs(tmp_path, autopr_config)
                assert result.skipped == 1
                assert result.behind_updated == 0

    def test_conflicting_enters_resolver(self, tmp_path: Path, autopr_config):
        prs = [
            {
                "number": 5,
                "headRefName": "autopr/conflicting",
                "title": "Conflicting",
                "body": "",
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "labels": [],
            }
        ]
        with patch("autopr.reconciler.list_managed_prs", return_value=prs):
            with patch("autopr.reconciler.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                with patch("autopr.reconciler.resolve_pr_conflicts") as mock_resolve:
                    mock_resolve.return_value = ConflictResolutionResult(
                        pr_number=5,
                        branch_name="autopr/conflicting",
                        status="conflict_resolved",
                        strategy_used="rebase",
                    )
                    result = reconcile_open_prs(tmp_path, autopr_config)
                    assert result.conflicts_resolved == 1
                    mock_resolve.assert_called_once()

    def test_unknown_mergeable_skipped(self, tmp_path: Path, autopr_config):
        prs = [
            {
                "number": 1,
                "headRefName": "autopr/fix",
                "title": "Fix",
                "body": "",
                "mergeable": "UNKNOWN",
                "mergeStateStatus": "",
                "labels": [],
            }
        ]
        with patch("autopr.reconciler.list_managed_prs", return_value=prs):
            with patch("autopr.reconciler.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = reconcile_open_prs(tmp_path, autopr_config)
                assert result.skipped == 1

    def test_dry_run_skips_all(self, tmp_path: Path, autopr_config):
        prs = [
            {
                "number": 1,
                "headRefName": "autopr/fix",
                "title": "Fix",
                "body": "",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "BEHIND",
                "labels": [],
            }
        ]
        with patch("autopr.reconciler.list_managed_prs", return_value=prs):
            with patch("autopr.reconciler.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = reconcile_open_prs(tmp_path, autopr_config, dry_run=True)
                assert result.skipped == 1
                assert result.behind_updated == 0
