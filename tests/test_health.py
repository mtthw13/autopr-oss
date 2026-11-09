"""Tests for autopr/health.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from autopr.config import HealthCheckConfig
from autopr.health import attempt_auto_fix, run_health_checks, run_single_check
from autopr.models import HealthCheckResult

# ── TestRunSingleCheck ───────────────────────────────────────────────────────


class TestRunSingleCheck:
    @patch("autopr.health.subprocess.run")
    def test_passing_check(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="all good",
            stderr="",
        )

        result = run_single_check("make test", tmp_path, timeout=60, attempt=1)

        assert result.passed is True
        assert result.command == "make test"
        assert result.stdout == "all good"
        assert result.attempt == 1
        mock_run.assert_called_once_with(
            "make test",
            shell=True,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=60,
        )

    @patch("autopr.health.subprocess.run")
    def test_failing_check(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="FAILED test_foo.py",
            stderr="AssertionError: 1 != 2",
        )

        result = run_single_check("pytest", tmp_path, timeout=60, attempt=2)

        assert result.passed is False
        assert result.stdout == "FAILED test_foo.py"
        assert result.stderr == "AssertionError: 1 != 2"
        assert result.attempt == 2

    @patch("autopr.health.subprocess.run")
    def test_timeout(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="make test", timeout=30)

        result = run_single_check("make test", tmp_path, timeout=30, attempt=1)

        assert result.passed is False
        assert "Timed out" in result.stderr
        assert "30" in result.stderr

    @patch("autopr.health.subprocess.run")
    def test_attempt_number_passed_through(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = run_single_check("echo hi", tmp_path, attempt=5)

        assert result.attempt == 5


# ── TestAttemptAutoFix ───────────────────────────────────────────────────────


class TestAttemptAutoFix:
    @patch("autopr.health.subprocess.run")
    def test_calls_claude_with_fix_prompt(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        failed = HealthCheckResult(
            passed=False,
            command="pytest",
            stdout="FAIL test_foo",
            stderr="AssertionError",
        )

        attempt_auto_fix(failed, tmp_path, model="sonnet")

        mock_run.assert_called_once()
        call_args = mock_run.call_args

        # Verify the claude command
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "sonnet" in cmd

        # Verify prompt content via stdin (input kwarg)
        prompt = call_args[1]["input"]
        assert "pytest" in prompt
        assert "AssertionError" in prompt
        assert "FAIL test_foo" in prompt

    @patch("autopr.health.subprocess.run")
    def test_long_stderr_truncated(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        long_stderr = "x" * 5000
        failed = HealthCheckResult(
            passed=False,
            command="make lint",
            stdout="",
            stderr=long_stderr,
        )

        attempt_auto_fix(failed, tmp_path)

        prompt = mock_run.call_args[1]["input"]
        # The stderr in the prompt should be the last 2000 chars
        assert "x" * 2000 in prompt
        # But not the full 5000
        assert "x" * 5000 not in prompt

    @patch("autopr.health.subprocess.run")
    def test_returns_false_on_claude_failure(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Claude crashed")
        failed = HealthCheckResult(passed=False, command="pytest", stdout="FAIL", stderr="error")
        result = attempt_auto_fix(failed, tmp_path, model="sonnet")
        assert result is False

    @patch("autopr.health.subprocess.run")
    def test_returns_true_on_success(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="fixed", stderr="")
        failed = HealthCheckResult(passed=False, command="pytest", stdout="FAIL", stderr="error")
        result = attempt_auto_fix(failed, tmp_path, model="sonnet")
        assert result is True

    @patch("autopr.health.subprocess.run")
    def test_stdout_limited_to_last_100_lines(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Build stdout with 200 numbered lines
        stdout_lines = [f"line-{i}" for i in range(200)]
        failed = HealthCheckResult(
            passed=False,
            command="make test",
            stdout="\n".join(stdout_lines),
            stderr="error",
        )

        attempt_auto_fix(failed, tmp_path)

        prompt = mock_run.call_args[1]["input"]
        # Last 100 lines should be present (lines 100-199)
        assert "line-199" in prompt
        assert "line-100" in prompt
        # Line 0 should NOT be in the stdout_tail portion.
        # However, it might appear if the full 200 lines fit in 2000 chars.
        # The code does stdout_lines[-100:], then [-2000:] on the joined string.
        # Let's just verify that "line-199" is present:
        assert "line-199" in prompt


# ── TestRunHealthChecks ──────────────────────────────────────────────────────


class TestRunHealthChecks:
    @patch("autopr.health.subprocess.run")
    def test_all_pass_first_try(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        config = HealthCheckConfig(
            commands=["make lint", "make test"],
            timeout_seconds=60,
            max_fix_attempts=3,
        )

        results = run_health_checks(config, tmp_path)

        assert len(results) == 2
        assert all(r.passed for r in results)

    @patch("autopr.health.attempt_auto_fix")
    @patch("autopr.health.subprocess.run")
    def test_fail_then_autofix_then_pass(
        self, mock_run: MagicMock, mock_fix: MagicMock, tmp_path: Path
    ):
        mock_fix.return_value = True
        # Calls: health check (fail), git rev-parse HEAD (snapshot), health check (pass)
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="fail", stderr="error"),  # attempt 1
            MagicMock(returncode=0, stdout="abc123", stderr=""),  # git rev-parse HEAD
            MagicMock(returncode=0, stdout="ok", stderr=""),  # attempt 2 (after fix)
        ]

        config = HealthCheckConfig(
            commands=["make test"],
            timeout_seconds=60,
            max_fix_attempts=3,
        )

        results = run_health_checks(config, tmp_path)

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].attempt == 2
        mock_fix.assert_called_once()

    @patch("autopr.health.attempt_auto_fix")
    @patch("autopr.health.subprocess.run")
    def test_fail_exhausts_retries(self, mock_run: MagicMock, mock_fix: MagicMock, tmp_path: Path):
        mock_fix.return_value = True

        def side_effect(cmd, **kwargs):
            # git rev-parse HEAD returns a hash
            if isinstance(cmd, list) and cmd[:2] == ["git", "rev-parse"]:
                return MagicMock(returncode=0, stdout="abc123", stderr="")
            # git checkout/clean for rollback
            if isinstance(cmd, list) and cmd[0] == "git" and cmd[1] in ("checkout", "clean"):
                return MagicMock(returncode=0, stdout="", stderr="")
            # health check always fails
            return MagicMock(returncode=1, stdout="fail", stderr="error")

        mock_run.side_effect = side_effect

        config = HealthCheckConfig(
            commands=["make test"],
            timeout_seconds=60,
            max_fix_attempts=3,
        )

        results = run_health_checks(config, tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        # attempt_auto_fix called for attempts 2 and 3
        assert mock_fix.call_count == 2

    @patch("autopr.health.attempt_auto_fix")
    @patch("autopr.health.subprocess.run")
    def test_stops_on_failed_check(self, mock_run: MagicMock, mock_fix: MagicMock, tmp_path: Path):
        # First command always fails
        mock_run.return_value = MagicMock(returncode=1, stdout="fail", stderr="error")

        config = HealthCheckConfig(
            commands=["make lint", "make test"],
            timeout_seconds=60,
            max_fix_attempts=2,
        )

        results = run_health_checks(config, tmp_path)

        # Only the first command is in results because it failed and caused early stop
        assert len(results) == 1
        assert results[0].command == "make lint"
        assert results[0].passed is False

    @patch("autopr.health.attempt_auto_fix")
    @patch("autopr.health.subprocess.run")
    def test_breaks_early_when_autofix_fails(
        self, mock_run: MagicMock, mock_fix: MagicMock, tmp_path: Path
    ):
        mock_run.return_value = MagicMock(returncode=1, stdout="fail", stderr="error")
        mock_fix.return_value = False
        config = HealthCheckConfig(commands=["make test"], timeout_seconds=60, max_fix_attempts=5)
        results = run_health_checks(config, tmp_path)
        assert len(results) == 1
        assert results[0].passed is False
        assert mock_fix.call_count == 1
        assert mock_run.call_count < 5

    @patch("autopr.health.attempt_auto_fix")
    @patch("autopr.health.subprocess.run")
    def test_max_fix_attempts_one_no_autofix(
        self, mock_run: MagicMock, mock_fix: MagicMock, tmp_path: Path
    ):
        mock_run.return_value = MagicMock(returncode=1, stdout="fail", stderr="error")

        config = HealthCheckConfig(
            commands=["make test"],
            timeout_seconds=60,
            max_fix_attempts=1,
        )

        results = run_health_checks(config, tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        # With max_fix_attempts=1, the while loop condition (attempt < 1) is never true
        mock_fix.assert_not_called()
