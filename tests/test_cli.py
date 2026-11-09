"""Tests for autopr CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from autopr.cli import main
from autopr.models import (
    DimensionResult,
    DimensionVerdict,
    ResearchReport,
    ReviewVerdict,
    StructuredReviewResult,
)


class TestReviewCommand:
    @patch("autopr.reviewer.structured_review_fix")
    def test_review_current_branch(
        self,
        mock_review: MagicMock,
        tmp_path: Path,
    ):
        """Review without --pr reviews the current branch."""
        repo = tmp_path / "repo"
        repo.mkdir()

        mock_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary="All good",
            dimensions=[
                DimensionResult(
                    dimension="correctness", verdict=DimensionVerdict.PASS, finding="ok"
                ),
            ],
        )

        runner = CliRunner()
        result = runner.invoke(main, ["review", str(repo)])

        assert result.exit_code == 0
        assert "all dimensions pass" in result.output.lower()
        mock_review.assert_called_once()

    @patch("autopr.reviewer.structured_review_fix")
    @patch("autopr.cli.subprocess.run")
    def test_review_with_pr_flag(
        self,
        mock_subprocess: MagicMock,
        mock_review: MagicMock,
        tmp_path: Path,
    ):
        """Review with --pr fetches PR body via gh and reviews."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # gh pr view returns PR data
        pr_data = json.dumps(
            {
                "body": "## Problem\nBug found\n## Solution\nFix it\n## Files Changed\n- `src/app.py`\n",
                "headRefName": "autopr/fix-bug",
                "title": "Fix the bug",
            }
        )
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=pr_data,
            stderr="",
        )

        mock_review.return_value = StructuredReviewResult(
            verdict=ReviewVerdict.APPROVE,
            summary="All good",
            dimensions=[
                DimensionResult(
                    dimension="correctness", verdict=DimensionVerdict.PASS, finding="ok"
                ),
            ],
        )

        runner = CliRunner()
        result = runner.invoke(main, ["review", str(repo), "--pr", "42"])

        assert result.exit_code == 0
        assert "all dimensions pass" in result.output.lower()

    @patch("autopr.cli.subprocess.run")
    def test_review_with_pr_flag_gh_failure(
        self,
        mock_subprocess: MagicMock,
        tmp_path: Path,
    ):
        """When gh pr view fails, exit with error message."""
        repo = tmp_path / "repo"
        repo.mkdir()

        mock_subprocess.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="not found",
        )

        runner = CliRunner()
        result = runner.invoke(main, ["review", str(repo), "--pr", "999"])

        assert (
            result.exit_code != 0
            or "error" in result.output.lower()
            or "failed" in result.output.lower()
        )


class TestResearchCommand:
    @patch("autopr.cli.run_research")
    def test_research_command_uses_overrides(
        self,
        mock_run_research: MagicMock,
        tmp_path: Path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        program = tmp_path / "program.md"
        program.write_text("# Optimize\n")

        mock_run_research.return_value = ResearchReport(
            repo_path=str(repo),
            branch_name="autopr/research-cli",
            status="success",
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "research",
                str(repo),
                "--program",
                str(program),
                "--max-iterations",
                "9",
                "--branch-name",
                "autopr/research-cli",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0
        assert "autopr research" in result.output
        mock_run_research.assert_called_once()
        _, kwargs = mock_run_research.call_args
        assert kwargs["program_file"] == program
        assert kwargs["max_iterations"] == 9
        assert kwargs["branch_name"] == "autopr/research-cli"


class TestInitCommand:
    @patch("autopr.cli._detect_tool_repo", return_value="https://github.com/autopr/autopr")
    @patch("autopr.cli.subprocess.run")
    def test_init_generates_yaml(
        self,
        mock_subprocess: MagicMock,
        _mock_tool_repo: MagicMock,
        tmp_path: Path,
    ):
        """init creates an autopr.yaml with detected settings."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Simulate git remote
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="https://github.com/org/repo\n", stderr=""
        )

        runner = CliRunner()
        out_path = tmp_path / "autopr.yaml"
        result = runner.invoke(main, ["init", str(repo), "-c", str(out_path)])

        assert result.exit_code == 0
        assert out_path.exists()
        import yaml

        data = yaml.safe_load(out_path.read_text())
        assert data["repo"]["remote_url"] == "https://github.com/org/repo"

    @patch("autopr.cli._detect_tool_repo", return_value="https://github.com/autopr/autopr")
    @patch("autopr.cli.subprocess.run")
    def test_init_detects_check_test_targets(
        self,
        mock_subprocess: MagicMock,
        _mock_tool_repo: MagicMock,
        tmp_path: Path,
    ):
        """init prefers check/test Makefile targets over backend- variants."""
        repo = tmp_path / "repo"
        repo.mkdir()
        makefile = repo / "Makefile"
        makefile.write_text(
            ".PHONY: check test backend-check-fix backend-test\n"
            "check:\n\truff check .\n"
            "test:\n\tpytest\n"
            "backend-check-fix:\n\tlegacy-lint\n"
            "backend-test:\n\tlegacy-test\n"
        )
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="")

        runner = CliRunner()
        out_path = tmp_path / "autopr.yaml"
        result = runner.invoke(main, ["init", str(repo), "-c", str(out_path)])

        assert result.exit_code == 0
        import yaml

        data = yaml.safe_load(out_path.read_text())
        commands = data["repo"]["health_checks"]["commands"]
        assert "make check" in commands
        assert "make test" in commands
        assert "make backend-check-fix" not in commands

    @patch("autopr.cli._detect_tool_repo", return_value="https://github.com/autopr/autopr")
    @patch("autopr.cli.subprocess.run")
    def test_init_falls_back_to_backend_targets(
        self,
        mock_subprocess: MagicMock,
        _mock_tool_repo: MagicMock,
        tmp_path: Path,
    ):
        """init falls back to backend-check-fix/backend-test when check/test absent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        makefile = repo / "Makefile"
        makefile.write_text(
            ".PHONY: backend-check-fix backend-test\n"
            "backend-check-fix:\n\tlegacy-lint\n"
            "backend-test:\n\tlegacy-test\n"
        )
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="")

        runner = CliRunner()
        out_path = tmp_path / "autopr.yaml"
        result = runner.invoke(main, ["init", str(repo), "-c", str(out_path)])

        assert result.exit_code == 0
        import yaml

        data = yaml.safe_load(out_path.read_text())
        commands = data["repo"]["health_checks"]["commands"]
        assert "make backend-check-fix" in commands
        assert "make backend-test" in commands

    @patch("autopr.cli._detect_tool_repo", return_value="https://github.com/autopr/autopr")
    @patch("autopr.cli.subprocess.run")
    def test_init_renders_workflow_templates(
        self,
        mock_subprocess: MagicMock,
        _mock_tool_repo: MagicMock,
        tmp_path: Path,
    ):
        """init writes rendered workflow templates with install steps."""
        repo = tmp_path / "repo"
        repo.mkdir()
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="https://github.com/org/repo\n", stderr=""
        )

        runner = CliRunner()
        out_path = tmp_path / "autopr.yaml"
        result = runner.invoke(main, ["init", str(repo), "-c", str(out_path)])

        assert result.exit_code == 0
        workflow = (repo / ".github" / "workflows" / "autopr-pr-react.yml").read_text()
        assert "__AUTOPR_TOOL_REPO__" not in workflow
        assert "repository: https://github.com/autopr/autopr" in workflow
        assert "python -m pip install -e ./.autopr-tool" in workflow
        assert 'workflows: ["CI"]' in workflow
        assert "group: autopr-react-${{ needs.resolve-context.outputs.pr_number }}" in workflow

    @patch("autopr.cli._detect_tool_repo", return_value="")
    @patch("autopr.cli.subprocess.run")
    def test_init_with_workflows_fails_without_tool_repo(
        self,
        mock_subprocess: MagicMock,
        _mock_tool_repo: MagicMock,
        tmp_path: Path,
    ):
        """Workflow generation fails fast when tool_repo is unavailable."""
        repo = tmp_path / "repo"
        repo.mkdir()
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="not found")

        runner = CliRunner()
        out_path = tmp_path / "autopr.yaml"
        result = runner.invoke(main, ["init", str(repo), "-c", str(out_path)])

        assert result.exit_code != 0
        assert "tool_repo" in result.output


class TestToolRepoNormalization:
    """`actions/checkout` needs `owner/repo`, not a clone URL (private-install regression)."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://github.com/mtthw13/autopr.git", "mtthw13/autopr"),
            ("https://github.com/mtthw13/autopr", "mtthw13/autopr"),
            ("git@github.com:mtthw13/autopr.git", "mtthw13/autopr"),
            ("ssh://git@github.com/mtthw13/autopr.git", "mtthw13/autopr"),
            ("mtthw13/autopr", "mtthw13/autopr"),
            ("", ""),
        ],
    )
    def test_normalizes_remote_forms_to_owner_repo(self, raw: str, expected: str) -> None:
        from autopr.cli import _normalize_tool_repo

        assert _normalize_tool_repo(raw) == expected

    @patch("autopr.cli._detect_tool_repo", return_value="mtthw13/autopr")
    def test_generated_workflow_checks_out_owner_repo_with_a_token(
        self,
        _mock_tool_repo: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A client's default GITHUB_TOKEN cannot read a private tool repo."""
        runner = CliRunner()
        result = runner.invoke(main, ["init", str(tmp_path), "--workflows"])
        assert result.exit_code == 0, result.output

        rendered = (tmp_path / ".github/workflows/autopr-reconcile.yml").read_text()
        assert "repository: mtthw13/autopr" in rendered
        assert "https://github.com/" not in rendered
        assert "AUTOPR_TOOL_TOKEN" in rendered
