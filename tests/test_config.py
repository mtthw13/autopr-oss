"""Tests for autopr/config.py -- configuration classes and YAML round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from autopr.config import (
    AutomationConfig,
    AutoPRConfig,
    GatingConfig,
    HealthCheckConfig,
    RepoConfig,
    ResearchConfig,
    ResearchDeployConfig,
    ResearchGuardrailMetricConfig,
    ResearchLighthouseConfig,
    ResearchPrimaryMetricConfig,
    ResearchScoringConfig,
    ResearchTTFTProbeConfig,
    VerdictRule,
)

# ======================================================================
# HealthCheckConfig tests
# ======================================================================


class TestHealthCheckConfig:
    def test_defaults(self):
        hc = HealthCheckConfig()
        assert hc.commands == ["make check", "make test"]
        assert hc.timeout_seconds == 300
        assert hc.max_fix_attempts == 3

    def test_custom_values(self):
        hc = HealthCheckConfig(
            commands=["pytest -x"],
            timeout_seconds=60,
            max_fix_attempts=1,
        )
        assert hc.commands == ["pytest -x"]
        assert hc.timeout_seconds == 60
        assert hc.max_fix_attempts == 1


# ======================================================================
# RepoConfig tests
# ======================================================================


class TestRepoConfig:
    def test_required_path(self):
        with pytest.raises(ValidationError):
            RepoConfig()  # path is required

    def test_all_defaults(self, tmp_path: Path):
        rc = RepoConfig(path=tmp_path)
        assert rc.path == tmp_path
        assert rc.remote_url == ""
        assert rc.base_branch == "main"
        assert rc.branch_prefix == "autopr/"
        assert rc.worktree_dir is None
        assert isinstance(rc.health_checks, HealthCheckConfig)
        assert rc.include_paths == []
        assert len(rc.exclude_paths) == 7
        assert "node_modules" in rc.exclude_paths
        assert ".git" in rc.exclude_paths
        assert rc.opportunity_types == []
        assert rc.min_confidence == 0.8

    def test_custom_path_and_branch(self, tmp_path: Path):
        rc = RepoConfig(path=tmp_path / "myrepo", base_branch="develop")
        assert rc.base_branch == "develop"


# ======================================================================
# VerdictRule tests
# ======================================================================


class TestVerdictRule:
    def test_defaults(self):
        vr = VerdictRule(dimension="correctness")
        assert vr.on_fail == "reject"

    def test_custom(self):
        vr = VerdictRule(dimension="style", on_fail="warn")
        assert vr.dimension == "style"
        assert vr.on_fail == "warn"

    def test_dimension_required(self):
        with pytest.raises(ValidationError):
            VerdictRule()  # dimension is required


# ======================================================================
# GatingConfig tests
# ======================================================================


class TestGatingConfig:
    def test_enable_screener_default(self):
        gc = GatingConfig()
        assert gc.enable_screener is True

    def test_six_default_verdict_rules(self):
        gc = GatingConfig()
        assert len(gc.verdict_rules) == 6

    def test_default_rules_dimensions(self):
        gc = GatingConfig()
        dims = [r.dimension for r in gc.verdict_rules]
        assert "scope_hygiene" in dims
        assert "security" in dims
        assert "correctness" in dims
        assert "compatibility" in dims
        assert "maintainability" in dims
        assert "observability" in dims

    def test_default_rules_match_helper(self, default_verdict_rules):
        gc = GatingConfig()
        for rule, expected in zip(gc.verdict_rules, default_verdict_rules):
            assert rule.dimension == expected.dimension
            assert rule.on_fail == expected.on_fail

    def test_custom_rules(self):
        custom = [VerdictRule(dimension="perf", on_fail="reject")]
        gc = GatingConfig(enable_screener=False, verdict_rules=custom)
        assert gc.enable_screener is False
        assert len(gc.verdict_rules) == 1
        assert gc.verdict_rules[0].dimension == "perf"


# ======================================================================
# Research config tests
# ======================================================================


class TestResearchConfig:
    def test_defaults(self):
        cfg = ResearchConfig()
        assert cfg.program_file == ""
        assert cfg.editable_paths == []
        assert cfg.branch_name.startswith("autopr/research-")
        assert cfg.max_iterations == 20
        assert cfg.history_window == 10
        assert cfg.consecutive_failures_limit == 5
        assert isinstance(cfg.deploy, ResearchDeployConfig)
        assert isinstance(cfg.lighthouse, ResearchLighthouseConfig)
        assert isinstance(cfg.ttft_probe, ResearchTTFTProbeConfig)
        assert isinstance(cfg.scoring, ResearchScoringConfig)

    def test_custom_scoring(self):
        cfg = ResearchConfig(
            scoring=ResearchScoringConfig(
                primary_metric=ResearchPrimaryMetricConfig(
                    name="lcp_ms",
                    direction="minimize",
                    min_improvement_abs=100.0,
                ),
                guardrails=[
                    ResearchGuardrailMetricConfig(
                        name="ttfb_ms",
                        direction="minimize",
                        max_regression_abs=50.0,
                    ),
                ],
            )
        )
        assert cfg.scoring.primary_metric.name == "lcp_ms"
        assert cfg.scoring.primary_metric.direction == "minimize"
        assert cfg.scoring.primary_metric.min_improvement_abs == 100.0
        assert len(cfg.scoring.guardrails) == 1
        assert cfg.scoring.guardrails[0].name == "ttfb_ms"


# ======================================================================
# Automation config tests
# ======================================================================


class TestAutomationConfig:
    def test_defaults(self):
        cfg = AutomationConfig()
        assert cfg.tool_repo == ""
        assert cfg.tool_ref == "main"
        assert cfg.ci_workflow_names == ["CI"]
        assert cfg.managed_branch_prefixes == ["autopr/"]


# ======================================================================
# AutoPRConfig tests
# ======================================================================


class TestAutoPRConfig:
    def test_defaults(self, make_config):
        cfg = make_config()
        assert cfg.max_concurrent_prs == 3
        assert cfg.dry_run is False
        assert cfg.push_to_remote is True
        assert cfg.create_github_pr is True
        assert cfg.model == "claude-opus-4-6"
        assert cfg.reviewer_model == "claude-opus-4-6"
        assert cfg.patterns == []
        assert cfg.verbose is False
        assert isinstance(cfg.research, ResearchConfig)
        assert isinstance(cfg.automation, AutomationConfig)

    def test_gating_field_present(self, make_config):
        cfg = make_config()
        assert isinstance(cfg.gating, GatingConfig)
        assert cfg.gating.enable_screener is True
        assert len(cfg.gating.verdict_rules) == 6

    def test_research_field_present(self, make_config):
        cfg = make_config()
        assert isinstance(cfg.research, ResearchConfig)
        assert cfg.research.program_file == ""

    def test_repo_required(self):
        with pytest.raises(ValidationError):
            AutoPRConfig()  # repo is required

    def test_patterns_custom(self, tmp_path: Path):
        cfg = AutoPRConfig(
            repo=RepoConfig(path=tmp_path / "repo"),
            patterns=["open_redirect", "n_plus_one"],
        )
        assert cfg.patterns == ["open_redirect", "n_plus_one"]

    def test_from_yaml_round_trip(self, tmp_path: Path):
        yaml_path = tmp_path / "autopr.yaml"
        original = AutoPRConfig(
            repo=RepoConfig(
                path=tmp_path / "repo",
                base_branch="develop",
                min_confidence=0.7,
            ),
            max_concurrent_prs=5,
            dry_run=True,
            model="opus",
        )
        original.to_yaml(yaml_path)

        loaded = AutoPRConfig.from_yaml(yaml_path)
        assert loaded.max_concurrent_prs == 5
        assert loaded.dry_run is True
        assert loaded.model == "opus"
        assert loaded.repo.base_branch == "develop"
        assert loaded.repo.min_confidence == 0.7

    def test_from_yaml_round_trip_preserves_patterns(self, tmp_path: Path):
        yaml_path = tmp_path / "patterns.yaml"
        cfg = AutoPRConfig(
            repo=RepoConfig(path=tmp_path / "repo"),
            patterns=["open_redirect", "unused_file"],
        )
        cfg.to_yaml(yaml_path)
        loaded = AutoPRConfig.from_yaml(yaml_path)
        assert loaded.patterns == ["open_redirect", "unused_file"]

    def test_to_yaml_creates_file(self, tmp_path: Path):
        yaml_path = tmp_path / "out.yaml"
        cfg = AutoPRConfig(repo=RepoConfig(path=tmp_path / "repo"))
        cfg.to_yaml(yaml_path)
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "repo" in content
        assert "claude-opus-4-6" in content

    def test_to_yaml_round_trip_preserves_gating(self, tmp_path: Path):
        yaml_path = tmp_path / "gating.yaml"
        cfg = AutoPRConfig(
            repo=RepoConfig(path=tmp_path / "repo"),
            gating=GatingConfig(
                enable_screener=False,
                verdict_rules=[
                    VerdictRule(dimension="perf", on_fail="reject"),
                ],
            ),
        )
        cfg.to_yaml(yaml_path)
        loaded = AutoPRConfig.from_yaml(yaml_path)
        assert loaded.gating.enable_screener is False
        assert len(loaded.gating.verdict_rules) == 1
        assert loaded.gating.verdict_rules[0].dimension == "perf"

    def test_to_yaml_round_trip_preserves_research(self, tmp_path: Path):
        yaml_path = tmp_path / "research.yaml"
        cfg = AutoPRConfig(
            repo=RepoConfig(path=tmp_path / "repo"),
            research=ResearchConfig(
                program_file="research.md",
                editable_paths=["src", "app"],
                max_iterations=7,
                ttft_probe=ResearchTTFTProbeConfig(command="node scripts/ttft.js {url}"),
                scoring=ResearchScoringConfig(
                    primary_metric=ResearchPrimaryMetricConfig(
                        name="performance_score",
                        direction="maximize",
                        min_improvement_abs=2.0,
                    ),
                ),
            ),
        )
        cfg.to_yaml(yaml_path)
        loaded = AutoPRConfig.from_yaml(yaml_path)
        assert loaded.research.program_file == "research.md"
        assert loaded.research.editable_paths == ["src", "app"]
        assert loaded.research.max_iterations == 7
        assert loaded.research.ttft_probe.command == "node scripts/ttft.js {url}"
        assert loaded.research.scoring.primary_metric.min_improvement_abs == 2.0
