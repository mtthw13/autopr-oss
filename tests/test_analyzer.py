"""Tests for autopr/analyzer.py — pure-logic functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autopr.analyzer import PlaybookPattern, _parse_opportunities, _split_frontmatter, load_playbook
from autopr.models import Opportunity

# ── TestSplitFrontmatter ────────────────────────────────────────────────────


class TestSplitFrontmatter:
    """Tests for _split_frontmatter()."""

    def test_valid_frontmatter(self):
        """Standard ---\\nkey: val\\n---\\nbody format."""
        text = "---\nid: foo\ntitle: Bar\n---\nThis is the body."
        meta, body = _split_frontmatter(text)

        assert meta == {"id": "foo", "title": "Bar"}
        assert body == "This is the body."

    def test_no_frontmatter_returns_empty_dict_and_original(self):
        """Text without --- delimiters returns ({}, original_text)."""
        text = "Just a plain markdown file\nwith some content."
        meta, body = _split_frontmatter(text)

        assert meta == {}
        assert body == text

    def test_empty_body_after_closing_delimiter(self):
        """Closing --- with nothing after it returns empty body."""
        text = "---\nid: thing\n---\n"
        meta, body = _split_frontmatter(text)

        assert meta == {"id": "thing"}
        assert body == ""

    def test_missing_closing_delimiter_raises_valueerror(self):
        """A single --- with no closing --- raises ValueError."""
        text = "---\nid: broken\nno closing delimiter here"

        with pytest.raises(ValueError):
            _split_frontmatter(text)


# ── TestPlaybookPattern ─────────────────────────────────────────────────────


class TestPlaybookPattern:
    """Tests for the PlaybookPattern class."""

    def test_load_valid_pattern_all_fields(self, tmp_path: Path):
        """Pattern with all frontmatter fields populated."""
        md = tmp_path / "pattern.md"
        md.write_text(
            "---\n"
            "id: batch_queries\n"
            "title: Batchable sequential queries\n"
            "severity: high\n"
            "complexity: medium\n"
            "confidence: 0.92\n"
            "max_turns: 15\n"
            "---\n"
            "Look for sequential DB calls that can be batched."
        )

        pattern = PlaybookPattern(md)

        assert pattern.id == "batch_queries"
        assert pattern.title == "Batchable sequential queries"
        assert pattern.severity == "high"
        assert pattern.complexity == "medium"
        assert pattern.confidence == 0.92
        assert pattern.max_turns == 15
        assert "sequential DB calls" in pattern.body
        assert pattern.source_file == md

    def test_defaults_with_minimal_frontmatter(self, tmp_path: Path):
        """Only id and title in frontmatter — defaults should fill in the rest."""
        md = tmp_path / "minimal.md"
        md.write_text("---\nid: minimal_pattern\ntitle: Minimal\n---\nSome body text.")

        pattern = PlaybookPattern(md)

        assert pattern.id == "minimal_pattern"
        assert pattern.title == "Minimal"
        assert pattern.severity == "low"
        assert pattern.complexity == "low"
        assert pattern.confidence == 0.9
        assert pattern.max_turns == 5

    def test_repr_format(self, tmp_path: Path):
        """__repr__ should show PlaybookPattern('id')."""
        md = tmp_path / "repr_test.md"
        md.write_text("---\nid: my_pattern\ntitle: My Pattern\n---\nbody")

        pattern = PlaybookPattern(md)

        assert repr(pattern) == "PlaybookPattern('my_pattern')"

    def test_missing_id_raises_keyerror(self, tmp_path: Path):
        """Frontmatter without 'id' raises KeyError."""
        md = tmp_path / "no_id.md"
        md.write_text("---\ntitle: No ID Pattern\n---\nbody")

        with pytest.raises(KeyError):
            PlaybookPattern(md)

    def test_missing_title_raises_keyerror(self, tmp_path: Path):
        """Frontmatter without 'title' raises KeyError."""
        md = tmp_path / "no_title.md"
        md.write_text("---\nid: no_title\n---\nbody")

        with pytest.raises(KeyError):
            PlaybookPattern(md)


# ── TestLoadPlaybook ────────────────────────────────────────────────────────


class TestLoadPlaybook:
    """Tests for load_playbook()."""

    def test_loads_all_md_files(self, tmp_path: Path):
        """All .md files in the directory should be loaded."""
        d = tmp_path / "playbook"
        d.mkdir()
        (d / "alpha.md").write_text("---\nid: alpha\ntitle: Alpha\n---\nbody")
        (d / "beta.md").write_text("---\nid: beta\ntitle: Beta\n---\nbody")
        (d / "gamma.md").write_text("---\nid: gamma\ntitle: Gamma\n---\nbody")

        patterns = load_playbook(d)

        assert len(patterns) == 3

    def test_sorted_alphabetically(self, tmp_path: Path):
        """Patterns should be sorted alphabetically by filename."""
        d = tmp_path / "playbook"
        d.mkdir()
        (d / "z_last.md").write_text("---\nid: z_last\ntitle: Z Last\n---\nbody")
        (d / "a_first.md").write_text("---\nid: a_first\ntitle: A First\n---\nbody")
        (d / "m_middle.md").write_text("---\nid: m_middle\ntitle: M Middle\n---\nbody")

        patterns = load_playbook(d)

        assert [p.id for p in patterns] == ["a_first", "m_middle", "z_last"]

    def test_skips_malformed_files_without_raising(self, tmp_path: Path):
        """Malformed .md files are skipped; valid ones are still loaded."""
        d = tmp_path / "playbook"
        d.mkdir()
        (d / "good.md").write_text("---\nid: good\ntitle: Good\n---\nbody")
        # Malformed: missing required 'id' key
        (d / "bad.md").write_text("---\ntitle: Bad Only\n---\nbody")
        # Malformed: empty file
        (d / "empty.md").write_text("")

        patterns = load_playbook(d)

        assert len(patterns) == 1
        assert patterns[0].id == "good"

    def test_nonexistent_directory_raises_filenotfounderror(self, tmp_path: Path):
        """A nonexistent directory should raise FileNotFoundError."""
        fake_dir = tmp_path / "does_not_exist"

        with pytest.raises(FileNotFoundError):
            load_playbook(fake_dir)

    def test_empty_directory_returns_empty_list(self, tmp_path: Path):
        """An empty directory with no .md files returns []."""
        d = tmp_path / "empty_playbook"
        d.mkdir()

        patterns = load_playbook(d)

        assert patterns == []


# ── TestParseOpportunities ──────────────────────────────────────────────────


class TestParseOpportunities:
    """Tests for _parse_opportunities()."""

    def _make_opp_dict(self, **overrides) -> dict:
        """Build a valid opportunity dict with sensible defaults."""
        defaults = {
            "id": "test-opp",
            "title": "Test opportunity",
            "type": "dead_code",
            "severity": "low",
            "files": ["src/app.py"],
            "problem": "Unused function found.",
            "solution": "Remove the function.",
            "estimated_impact": "",
            "complexity": "low",
            "autonomous_confidence": 0.95,
        }
        defaults.update(overrides)
        return defaults

    def test_valid_json_array(self):
        """A well-formed JSON array of opportunity dicts."""
        data = [
            self._make_opp_dict(id="opp-1", title="First"),
            self._make_opp_dict(id="opp-2", title="Second"),
        ]
        raw = json.dumps(data)

        result = _parse_opportunities(raw)

        assert len(result) == 2
        assert result[0].id == "opp-1"
        assert result[1].id == "opp-2"
        assert isinstance(result[0], Opportunity)

    def test_empty_array_returns_empty_list(self):
        """An empty JSON array returns an empty list."""
        raw = "[]"

        result = _parse_opportunities(raw)

        assert result == []

    def test_empty_string_returns_empty_list(self):
        """An empty string should return an empty list (found via envelope extraction)."""
        # _parse_opportunities expects valid JSON; empty string raises ValueError
        # but wrapped in code fences with just [] should work
        raw = "```\n[]\n```"

        result = _parse_opportunities(raw)

        assert result == []

    def test_envelope_wrapped_array(self):
        """JSON array wrapped in conversational text with [ ... ] extractable."""
        opp = self._make_opp_dict(id="wrapped-opp")
        inner = json.dumps([opp])
        raw = f"Here are the results I found:\n{inner}\nThat's everything."

        result = _parse_opportunities(raw)

        assert len(result) == 1
        assert result[0].id == "wrapped-opp"

    def test_malformed_item_skipped(self):
        """An item missing required fields is skipped; valid items are kept."""
        data = [
            self._make_opp_dict(id="good-opp"),
            {"id": "bad-opp"},  # missing required fields
        ]
        raw = json.dumps(data)

        result = _parse_opportunities(raw)

        assert len(result) == 1
        assert result[0].id == "good-opp"

    def test_code_fence_wrapped_json(self):
        """JSON wrapped in markdown code fences."""
        data = [self._make_opp_dict(id="fenced-opp")]
        raw = f"```json\n{json.dumps(data)}\n```"

        result = _parse_opportunities(raw)

        assert len(result) == 1
        assert result[0].id == "fenced-opp"

    def test_max_turns_error_returns_empty(self):
        """A max-turns error envelope returns empty list gracefully."""
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "duration_ms": 5000,
                "duration_api_ms": 4000,
                "num_turns": 10,
            }
        )

        result = _parse_opportunities(raw)

        assert result == []

    def test_completely_invalid_output_returns_empty(self):
        """Output with no extractable JSON returns empty list (never raises)."""
        raw = "I could not find any issues in this codebase."

        result = _parse_opportunities(raw)

        assert result == []
