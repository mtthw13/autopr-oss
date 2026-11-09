"""Tests for autopr/orchestrator.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from autopr.config import AutoPRConfig, RepoConfig
from autopr.models import (
    DimensionResult,
    DimensionVerdict,
    HealthCheckResult,
    Opportunity,
    ReviewVerdict,
    ScreenResult,
    Severity,
    StructuredReviewResult,
)
from autopr.orchestrator import (
    _get_existing_branches,
    filter_already_handled,
    get_open_or_merged_prs,
    get_open_pr_changed_files,
    process_single_opportunity,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_opportunity(id: str = "fix-thing", title: str = "Fix thing", **overrides) -> Opportunity:
    defaults = dict(
        id=id,
        title=title,
        type="dead_code",
        severity=Severity.LOW,
        files=["src/app.py"],
        problem="There is a problem.",
        solution="Apply this solution.",
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def _make_config(
    repo_path: Path | None = None,
    branch_prefix: str = "autopr/",
    push_to_remote: bool = True,
    create_github_pr: bool = True,
    dry_run: bool = False,
) -> AutoPRConfig:
    from autopr.config import GatingConfig

    return AutoPRConfig(
        repo=RepoConfig(
            path=repo_path or Path("/repos/myproject"),
            branch_prefix=branch_prefix,
        ),
        gating=GatingConfig(enable_material_review=False),
        push_to_remote=push_to_remote,
        create_github_pr=create_github_pr,
        dry_run=dry_run,
    )


# ── TestGetOpenOrMergedPrs ───────────────────────────────────────────────────


class TestGetOpenOrMergedPrs:
    @patch("autopr.orchestrator.subprocess.run")
    def test_parses_open_prs(self, mock_run: MagicMock, tmp_path: Path):
        prs = [
            {"headRefName": "autopr/fix-a", "state": "OPEN"},
            {"headRefName": "autopr/fix-b", "state": "OPEN"},
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(prs),
            stderr="",
        )

        result = get_open_or_merged_prs(tmp_path, "autopr/")

        assert result == {"fix-a", "fix-b"}

    @patch("autopr.orchestrator.subprocess.run")
    def test_parses_merged_prs(self, mock_run: MagicMock, tmp_path: Path):
        prs = [
            {"headRefName": "autopr/fix-c", "state": "MERGED"},
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(prs),
            stderr="",
        )

        result = get_open_or_merged_prs(tmp_path, "autopr/")

        assert "fix-c" in result

    @patch("autopr.orchestrator.subprocess.run")
    def test_ignores_closed_prs(self, mock_run: MagicMock, tmp_path: Path):
        prs = [
            {"headRefName": "autopr/fix-d", "state": "CLOSED"},
            {"headRefName": "autopr/fix-e", "state": "OPEN"},
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(prs),
            stderr="",
        )

        result = get_open_or_merged_prs(tmp_path, "autopr/")

        assert "fix-d" not in result
        assert "fix-e" in result

    @patch("autopr.orchestrator._get_existing_branches")
    @patch("autopr.orchestrator.subprocess.run")
    def test_gh_failure_falls_back_to_branches(
        self,
        mock_run: MagicMock,
        mock_branches: MagicMock,
        tmp_path: Path,
    ):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="gh not found")
        mock_branches.return_value = {"fallback-id"}

        result = get_open_or_merged_prs(tmp_path, "autopr/")

        assert result == {"fallback-id"}
        mock_branches.assert_called_once_with(tmp_path, "autopr/")

    @patch("autopr.orchestrator.subprocess.run")
    def test_invalid_json_returns_empty(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json at all {{{",
            stderr="",
        )

        result = get_open_or_merged_prs(tmp_path, "autopr/")

        assert result == set()


# ── TestGetExistingBranches ──────────────────────────────────────────────────


class TestGetExistingBranches:
    @patch("autopr.orchestrator.subprocess.run")
    def test_parses_local_branches(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="  autopr/fix-x\n  autopr/fix-y\n",
            stderr="",
        )

        result = _get_existing_branches(tmp_path, "autopr/")

        assert result == {"fix-x", "fix-y"}

    @patch("autopr.orchestrator.subprocess.run")
    def test_strips_current_branch_marker(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="* autopr/fix-current\n  autopr/fix-other\n",
            stderr="",
        )

        result = _get_existing_branches(tmp_path, "autopr/")

        assert "fix-current" in result
        assert "fix-other" in result

    @patch("autopr.orchestrator.subprocess.run")
    def test_empty_output_returns_empty(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )

        result = _get_existing_branches(tmp_path, "autopr/")

        assert result == set()


class TestGetOpenPrChangedFiles:
    @patch("autopr.orchestrator.subprocess.run")
    def test_parses_open_pr_changed_files(self, mock_run: MagicMock, tmp_path: Path):
        prs = [
            {
                "headRefName": "autopr/fix-a",
                "files": [{"path": "src/app.py"}, {"path": "src/utils.py"}],
            },
            {
                "headRefName": "autopr/fix-b",
                "files": [{"path": "src/other.py"}],
            },
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(prs),
            stderr="",
        )

        result = get_open_pr_changed_files(tmp_path, "autopr/")

        assert result == {
            "fix-a": ["src/app.py", "src/utils.py"],
            "fix-b": ["src/other.py"],
        }


# ── TestFilterAlreadyHandled ─────────────────────────────────────────────────


class TestFilterAlreadyHandled:
    @patch("autopr.orchestrator.get_open_pr_changed_files")
    @patch("autopr.orchestrator.get_open_or_merged_prs")
    def test_filters_out_existing(
        self,
        mock_prs: MagicMock,
        mock_open_files: MagicMock,
        tmp_path: Path,
    ):
        mock_prs.return_value = {"fix-a"}
        mock_open_files.return_value = {}

        opps = [
            _make_opportunity(id="fix-a", title="A"),
            _make_opportunity(id="fix-b", title="B"),
        ]

        result = filter_already_handled(opps, tmp_path, "autopr/")

        assert len(result) == 1
        assert result[0].id == "fix-b"

    @patch("autopr.orchestrator.get_open_pr_changed_files")
    @patch("autopr.orchestrator.get_open_or_merged_prs")
    def test_no_existing_returns_all(
        self,
        mock_prs: MagicMock,
        mock_open_files: MagicMock,
        tmp_path: Path,
    ):
        mock_prs.return_value = set()
        mock_open_files.return_value = {}

        opps = [
            _make_opportunity(id="fix-a"),
            _make_opportunity(id="fix-b"),
        ]

        result = filter_already_handled(opps, tmp_path, "autopr/")

        assert len(result) == 2

    @patch("autopr.orchestrator.get_open_pr_changed_files")
    @patch("autopr.orchestrator.get_open_or_merged_prs")
    def test_all_existing_returns_empty(
        self,
        mock_prs: MagicMock,
        mock_open_files: MagicMock,
        tmp_path: Path,
    ):
        mock_prs.return_value = {"fix-a", "fix-b"}
        mock_open_files.return_value = {}

        opps = [
            _make_opportunity(id="fix-a"),
            _make_opportunity(id="fix-b"),
        ]

        result = filter_already_handled(opps, tmp_path, "autopr/")

        assert len(result) == 0

    @patch("autopr.orchestrator.get_open_pr_changed_files")
    @patch("autopr.orchestrator.get_open_or_merged_prs")
    def test_preserves_order(
        self,
        mock_prs: MagicMock,
        mock_open_files: MagicMock,
        tmp_path: Path,
    ):
        mock_prs.return_value = {"fix-b"}
        mock_open_files.return_value = {}

        opps = [
            _make_opportunity(id="fix-c", title="C"),
            _make_opportunity(id="fix-a", title="A"),
            _make_opportunity(id="fix-b", title="B"),
            _make_opportunity(id="fix-d", title="D"),
        ]

        result = filter_already_handled(opps, tmp_path, "autopr/")

        assert [o.id for o in result] == ["fix-c", "fix-a", "fix-d"]

    @patch("autopr.orchestrator.get_open_pr_changed_files")
    @patch("autopr.orchestrator.get_open_or_merged_prs")
    def test_filters_out_file_overlap(
        self,
        mock_prs: MagicMock,
        mock_open_files: MagicMock,
        tmp_path: Path,
    ):
        mock_prs.return_value = set()
        mock_open_files.return_value = {"fix-a": ["src/app.py"]}

        opps = [
            _make_opportunity(id="fix-b", files=["src/app.py"]),
            _make_opportunity(id="fix-c", files=["src/other.py"]),
        ]

        result = filter_already_handled(opps, tmp_path, "autopr/")

        assert [o.id for o in result] == ["fix-c"]


# ── TestProcessSingleOpportunity ────────────────────────────────────────────


class TestProcessSingleOpportunity:
    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch(
        "autopr.orchestrator.push_and_create_pr", return_value="https://github.com/org/repo/pull/42"
    )
    @patch("autopr.orchestrator.structured_review_fix")
    @patch("autopr.orchestrator.commit_changes")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_success_flow(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_commit: MagicMock,
        mock_structured_review: MagicMock,
        mock_create_pr: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=True, command="make test"),
        ]
        mock_commit.return_value = True
        mock_structured_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary="Looks good",
        )

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "success"
        assert result.pr_url == "https://github.com/org/repo/pull/42"
        mock_run_fix.assert_called_once()
        mock_health.assert_called_once()
        mock_structured_review.assert_called_once()
        mock_create_pr.assert_called_once()

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch("autopr.orchestrator.run_fix")
    def test_fix_exception_caught(
        self,
        mock_run_fix: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        mock_run_fix.side_effect = RuntimeError("Claude crashed")

        config = _make_config()
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "failed"
        assert "Claude crashed" in result.error

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_health_failure(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=False, command="make test", stderr="tests failed"),
        ]

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "failed"
        assert "Health checks failed" in result.error

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch("autopr.orchestrator.structured_review_fix")
    @patch("autopr.orchestrator.commit_changes")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_reviewer_reject(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_commit: MagicMock,
        mock_structured_review: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=True, command="make test"),
        ]
        mock_commit.return_value = True
        mock_structured_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.REJECT,
            summary="Changes are incorrect",
        )

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "failed"
        assert "rejected" in result.error.lower()

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch(
        "autopr.orchestrator.push_and_create_pr", return_value="https://github.com/org/repo/pull/99"
    )
    @patch("autopr.orchestrator.structured_review_fix")
    @patch("autopr.orchestrator.commit_changes")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_request_changes_still_creates_pr(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_commit: MagicMock,
        mock_structured_review: MagicMock,
        mock_create_pr: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=True, command="make test"),
        ]
        mock_commit.return_value = True
        mock_structured_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Minor issues",
        )

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "success"
        mock_create_pr.assert_called_once()

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch("autopr.orchestrator.run_fix")
    def test_generic_exception_caught(
        self,
        mock_run_fix: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        mock_run_fix.side_effect = ValueError("unexpected error")

        config = _make_config()
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "failed"
        assert "unexpected error" in result.error


# ── TestProcessSingleOpportunityScreener ─────────────────────────────────────


class TestProcessSingleOpportunityScreener:
    @patch("autopr.orchestrator.screen_opportunity")
    def test_screener_skips_when_proceed_false(
        self,
        mock_screen: MagicMock,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=False,
            summary="Problem does not exist",
            opportunity_id="fix-thing",
        )

        config = _make_config()
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "skipped"
        assert result.screen is not None
        assert result.screen.proceed is False

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch(
        "autopr.orchestrator.push_and_create_pr", return_value="https://github.com/org/repo/pull/42"
    )
    @patch("autopr.orchestrator.structured_review_fix")
    @patch("autopr.orchestrator.commit_changes")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_screener_proceeds_when_proceed_true(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_commit: MagicMock,
        mock_structured_review: MagicMock,
        mock_create_pr: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True,
            summary="Looks feasible",
            opportunity_id="fix-thing",
        )
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=True, command="make test"),
        ]
        mock_commit.return_value = True
        mock_structured_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary="Looks good",
        )

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "success"
        assert result.screen is not None
        assert result.screen.proceed is True
        mock_run_fix.assert_called_once()

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch(
        "autopr.orchestrator.push_and_create_pr", return_value="https://github.com/org/repo/pull/42"
    )
    @patch("autopr.orchestrator.structured_review_fix")
    @patch("autopr.orchestrator.commit_changes")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_screener_not_called_when_disabled(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_commit: MagicMock,
        mock_structured_review: MagicMock,
        mock_create_pr: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=True, command="make test"),
        ]
        mock_commit.return_value = True
        mock_structured_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary="Looks good",
        )

        config = _make_config(repo_path=tmp_path / "repo")
        config.gating.enable_screener = False
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "success"
        assert result.screen is None
        mock_screen.assert_not_called()


# ── TestProcessSingleOpportunityStructuredReview ─────────────────────────────


class TestProcessSingleOpportunityStructuredReview:
    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch(
        "autopr.orchestrator.push_and_create_pr", return_value="https://github.com/org/repo/pull/55"
    )
    @patch("autopr.orchestrator.structured_review_fix")
    @patch("autopr.orchestrator.commit_changes")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_structured_review_used(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_commit: MagicMock,
        mock_structured_review: MagicMock,
        mock_create_pr: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=True, command="make test"),
        ]
        mock_commit.return_value = True
        mock_structured_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary="All pass",
            dimensions=[
                DimensionResult(
                    dimension="correctness", verdict=DimensionVerdict.PASS, finding="OK"
                ),
                DimensionResult(dimension="security", verdict=DimensionVerdict.PASS, finding="OK"),
            ],
        )

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "success"
        mock_structured_review.assert_called_once()
        # Verify structured_review was passed to push_and_create_pr
        call_kwargs = mock_create_pr.call_args
        assert call_kwargs is not None
        assert "structured_review" in call_kwargs.kwargs
        assert call_kwargs.kwargs["structured_review"].verdict == ReviewVerdict.APPROVE

    @patch("autopr.orchestrator.screen_opportunity")
    @patch("autopr.orchestrator.remove_worktree")
    @patch("autopr.orchestrator.push_and_create_pr")
    @patch("autopr.orchestrator.structured_review_fix")
    @patch("autopr.orchestrator.commit_changes")
    @patch("autopr.orchestrator.run_health_checks")
    @patch("autopr.orchestrator.run_fix")
    def test_structured_review_reject_fails(
        self,
        mock_run_fix: MagicMock,
        mock_health: MagicMock,
        mock_commit: MagicMock,
        mock_structured_review: MagicMock,
        mock_create_pr: MagicMock,
        mock_remove_wt: MagicMock,
        mock_screen: MagicMock,
        tmp_path: Path,
    ):
        mock_screen.return_value = ScreenResult(
            proceed=True, summary="ok", opportunity_id="fix-thing"
        )
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        mock_run_fix.return_value = ("autopr/fix-thing", wt_path)
        mock_health.return_value = [
            HealthCheckResult(passed=True, command="make test"),
        ]
        mock_commit.return_value = True
        mock_structured_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.REJECT,
            summary="Security issue found",
            dimensions=[
                DimensionResult(
                    dimension="security", verdict=DimensionVerdict.FAIL, finding="SQL injection"
                ),
            ],
        )

        config = _make_config(repo_path=tmp_path / "repo")
        opp = _make_opportunity()

        result = process_single_opportunity(opp, config)

        assert result.status == "failed"
        assert "rejected" in result.error.lower()
        mock_create_pr.assert_not_called()
