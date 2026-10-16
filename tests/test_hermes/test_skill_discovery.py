"""Tests for Nova skill discovery — episode-grounded creation + refinement.

Covers:
- Skill effectiveness tracking (high_failure / low_usage)
- Parsing the review LLM's <skill> block
- Writing an episode skill to disk
- Legacy nova-<hash> auto-skill cleanup
"""

from novacode_cli.hermes.skill_discovery import check_skill_effectiveness


class TestSkillEffectiveness:
    """Skill usage tracking and effectiveness analysis."""

    async def test_check_empty_stats(self):
        """No skill stats should return empty list."""
        from unittest.mock import AsyncMock, MagicMock

        store_mock = MagicMock()
        store_mock.asearch = AsyncMock(return_value=[])
        issues = await check_skill_effectiveness(store_mock)
        assert issues == []

    async def test_high_failure_rate(self):
        """Skills with high failure rate should be flagged."""
        from unittest.mock import AsyncMock, MagicMock

        store_mock = MagicMock()

        class MockItem:
            key = "my-skill"
            value = {"invocations": 10, "successes": 3, "failures": 7}

        store_mock.asearch = AsyncMock(return_value=[MockItem()])
        issues = await check_skill_effectiveness(store_mock)
        assert ("my-skill", "high_failure") in issues

    async def test_low_usage_not_flagged_for_refinement(self):
        """Low usage no longer drives refinement — the curator archives instead."""
        from unittest.mock import AsyncMock, MagicMock

        store_mock = MagicMock()

        class MockItem:
            key = "rare-skill"
            value = {"invocations": 1, "successes": 1, "failures": 0}

        store_mock.asearch = AsyncMock(return_value=[MockItem()])
        issues = await check_skill_effectiveness(store_mock)
        assert issues == []


class TestParseSkillSpec:
    """Parsing the <skill> block the review LLM emits (episode-grounded)."""

    def test_valid_block(self):
        from novacode_cli.hermes.skill_discovery import parse_skill_spec

        text = (
            "preamble\n<skill>\n<name>Add TUI Command</name>\n"
            "<description>Use when adding a /command to the TUI.</description>\n"
            "<body># Steps\n1. edit app.py</body>\n</skill>\nepilogue"
        )
        spec = parse_skill_spec(text)
        assert spec == {
            "name": "add-tui-command",  # slugified
            "description": "Use when adding a /command to the TUI.",
            "body": "# Steps\n1. edit app.py",
        }

    def test_no_block_returns_none(self):
        from novacode_cli.hermes.skill_discovery import parse_skill_spec

        assert parse_skill_spec("just a normal review, no skill") is None

    def test_missing_body_returns_none(self):
        from novacode_cli.hermes.skill_discovery import parse_skill_spec

        assert parse_skill_spec("<skill><name>x-y</name></skill>") is None

    def test_legacy_style_name_rejected(self):
        from novacode_cli.hermes.skill_discovery import parse_skill_spec

        text = "<skill><name>nova-edit-5628de</name><body>steps</body></skill>"
        assert parse_skill_spec(text) is None


class TestWriteSkillFromSpec:
    """Writing an episode skill to disk with real frontmatter."""

    async def test_writes_skill_md_with_frontmatter(self, tmp_path, monkeypatch):
        from novacode_cli.hermes.skill_discovery import write_skill_from_spec
        from novacode_cli.hermes.ledger import LearningLedger

        monkeypatch.setattr(
            "novacode_cli.hermes.skill_proposals.main_agent_learning_ledger",
            lambda: LearningLedger(tmp_path.parent / "learning.sqlite3"),
        )

        spec = {
            "name": "add-tui-command",
            "description": "Use when adding a /command",
            "body": "# Steps\n1. do it",
        }
        name = await write_skill_from_spec(spec, tmp_path, store=None)
        assert name == "add-tui-command"
        skill_md = tmp_path / "add-tui-command" / "SKILL.md"
        assert not skill_md.exists()
        from novacode_cli.hermes.ledger import LearningLedger
        record = LearningLedger(tmp_path.parent / "learning.sqlite3").list()[-1]
        assert record.status == "candidate"
        assert record.target == "add-tui-command"
        content = record.content
        assert content.startswith("---\n")
        assert "name: add-tui-command" in content
        assert 'description: "Use when adding a /command"' in content
        assert "# Steps\n1. do it" in content

    async def test_dedup_existing_dir_returns_none(self, tmp_path):
        from novacode_cli.hermes.skill_discovery import write_skill_from_spec

        (tmp_path / "add-tui-command").mkdir()
        spec = {"name": "add-tui-command", "description": "d", "body": "b"}
        assert await write_skill_from_spec(spec, tmp_path, store=None) is None


class TestCleanupLegacySkills:
    """Removing the old opaque nova-<hash> auto-skills, sparing real ones."""

    def test_proposes_only_legacy_archives(self, tmp_path, monkeypatch):
        from novacode_cli.hermes.skill_discovery import cleanup_legacy_pattern_skills
        from novacode_cli.hermes import skill_proposals

        proposed = []
        monkeypatch.setattr(skill_proposals, "propose_skill",
                            lambda name, content, **kwargs: proposed.append((name, kwargs)))

        for name in ("nova-exec-b2c94a", "nova-edit-test-5628de", "add-tui-command", "my-skill"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "SKILL.md").write_text(name, encoding="utf-8")

        removed = cleanup_legacy_pattern_skills(tmp_path)

        assert set(removed) == {"nova-exec-b2c94a", "nova-edit-test-5628de"}
        assert {name for name, _ in proposed} == set(removed)
        assert all(options["archive"] for _, options in proposed)
        assert (tmp_path / "nova-exec-b2c94a").exists()
        assert (tmp_path / "add-tui-command").exists()  # real skill spared
        assert (tmp_path / "my-skill").exists()

    def test_missing_dir_is_safe(self, tmp_path):
        from novacode_cli.hermes.skill_discovery import cleanup_legacy_pattern_skills

        assert cleanup_legacy_pattern_skills(tmp_path / "nope") == []
