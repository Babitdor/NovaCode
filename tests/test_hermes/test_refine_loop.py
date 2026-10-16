"""Tests for the /refine self-refinement loop (hermes/refine_loop.py).

Covers:
- ``_parse_plan`` — parsing the planner's <refine_plan> response, filtering to
  supported domains, capping item count.
- ``_parse_verdict`` — parsing the review gate's accept/reject verdict.
- ``_list_skills`` — listing user skills with their frontmatter description.
- ``_apply_skill`` — writing a planner-provided SKILL.md through the versioned
  path (snapshot + write + cooldown), with frontmatter ensured.
- ``_apply_prompt`` — writing a candidate prompt override (packaged template
  untouched).
- ``_rollback`` — restoring a skill / rolling back a prompt override.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from novacode_cli.hermes import refine_loop


@pytest.fixture(autouse=True)
def isolate_prompt_learning_ledger(tmp_path, monkeypatch):
    from novacode_cli.hermes.ledger import LearningLedger

    monkeypatch.setattr(
        "novacode_cli.hermes.ledger.main_agent_learning_ledger",
        lambda: LearningLedger(tmp_path / "learning.sqlite3"),
    )


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """A temporary skills root with one existing skill."""
    d = tmp_path / "skills"
    (d / "alpha").mkdir(parents=True)
    (d / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Use when doing alpha.\n---\n\n## Steps\n1. old\n",
        encoding="utf-8",
    )
    return d


class TestParsePlan:
    def test_parses_valid_items(self):
        text = (
            "<refine_plan>"
            "<item><domain>skill</domain><target>alpha</target>"
            "<action>patch</action><reason>fails often</reason>"
            "<change>---\nname: alpha\n---\n# new</change></item>"
            "<item><domain>prompt</domain><target>core_agent_system.jinja</target>"
            "<action>update</action><reason>unclear</reason>"
            "<change>new body</change></item>"
            "</refine_plan>"
        )
        items = refine_loop._parse_plan(text)
        assert len(items) == 2
        assert items[0]["domain"] == "skill"
        assert items[0]["target"] == "alpha"
        assert items[1]["domain"] == "prompt"

    def test_filters_unsupported_domain(self):
        plan = (
            "<refine_plan>"
            "<item><domain>subagent</domain><target>t</target>"
            "<action>update</action><reason>r</reason><change>c</change></item>"
            "<item><domain>skill</domain><target>alpha</target>"
            "<action>patch</action><reason>r</reason><change>c</change></item>"
            "</refine_plan>"
        )
        items = refine_loop._parse_plan(plan)
        assert len(items) == 1
        assert items[0]["domain"] == "skill"

    def test_accepts_memory_domain(self):
        plan = (
            "<refine_plan>"
            "<item><domain>memory</domain><target>lessons</target>"
            "<action>create</action><reason>durable fact</reason>"
            "<change>- Prefer X over Y</change></item>"
            "</refine_plan>"
        )
        items = refine_loop._parse_plan(plan)
        assert len(items) == 1
        assert items[0]["domain"] == "memory"
        assert items[0]["target"] == "lessons"

    def test_rejects_invalid_action(self):
        plan = (
            "<refine_plan>"
            "<item><domain>skill</domain><target>alpha</target>"
            "<action>explode</action><reason>r</reason><change>c</change></item>"
            "</refine_plan>"
        )
        assert refine_loop._parse_plan(plan) == []

    def test_requires_change_for_non_delete(self):
        plan = (
            "<refine_plan>"
            "<item><domain>skill</domain><target>alpha</target>"
            "<action>update</action><reason>r</reason></item>"
            "<item><domain>skill</domain><target>beta</target>"
            "<action>delete</action><reason>r</reason></item>"
            "</refine_plan>"
        )
        items = refine_loop._parse_plan(plan)
        assert len(items) == 1
        assert items[0]["action"] == "delete"

    def test_skips_malformed_item(self):
        plan = (
            "<refine_plan>"
            "<item><domain>skill</domain><target>alpha</target></item>"
            "<item><domain>skill</domain><target>beta</target>"
            "<action>patch</action><reason>r</reason><change>c</change></item>"
            "</refine_plan>"
        )
        items = refine_loop._parse_plan(plan)
        assert len(items) == 1
        assert items[0]["target"] == "beta"

    def test_empty_plan(self):
        assert refine_loop._parse_plan("<refine_plan></refine_plan>") == []


class TestParseVerdict:
    def test_accept(self):
        accepted, reason = refine_loop._parse_verdict("<verdict>accept</verdict>")
        assert accepted is True
        assert reason == ""

    def test_reject_with_reason(self):
        accepted, reason = refine_loop._parse_verdict(
            "<verdict>reject</verdict><reason>regresses step 2</reason>"
        )
        assert accepted is False
        assert "regresses" in reason

    def test_no_verdict(self):
        accepted, reason = refine_loop._parse_verdict("no verdict here")
        assert accepted is False
        assert "no <verdict>" in reason


class TestListSkills:
    def test_lists_skills_with_description(self, skills_dir: Path) -> None:
        skills = refine_loop._list_skills(skills_dir)
        assert skills == [{"name": "alpha", "description": "Use when doing alpha."}]

    def test_missing_dir_is_safe(self, tmp_path: Path) -> None:
        assert refine_loop._list_skills(tmp_path / "nope") == []


class TestApplySkill:
    async def test_writes_change_with_frontmatter(
        self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(refine_loop, "_user_skills_dir", lambda: skills_dir)
        store = MagicMock()
        store.aget = AsyncMock(return_value=None)
        store.aput = AsyncMock()

        item = {
            "domain": "skill",
            "target": "alpha",
            "action": "patch",
            "reason": "fails often",
            "change": "# Alpha\n\n1. new step",
        }
        assert await refine_loop._apply_skill(item, store) is True

        content = (skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "name: alpha" in content
        assert "1. new step" in content
        # Prior version snapshotted.
        assert (skills_dir / "alpha" / ".history").exists()

    async def test_missing_skill_returns_false(
        self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(refine_loop, "_user_skills_dir", lambda: skills_dir)
        item = {
            "domain": "skill",
            "target": "nope",
            "action": "patch",
            "reason": "r",
            "change": "c",
        }
        assert await refine_loop._apply_skill(item, MagicMock()) is False

    async def test_no_change_returns_false(
        self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(refine_loop, "_user_skills_dir", lambda: skills_dir)
        item = {
            "domain": "skill",
            "target": "alpha",
            "action": "patch",
            "reason": "r",
            "change": "",
        }
        assert await refine_loop._apply_skill(item, MagicMock()) is False


class TestApplyPrompt:
    async def test_writes_candidate_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novacode_cli.hermes import prompt_evolution

        # Point the engine at a temp prompts dir with a real template.
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "core_agent_system.jinja").write_text("original", encoding="utf-8")
        monkeypatch.setattr(prompt_evolution, "TEMPLATES_DIR", prompts_dir)
        monkeypatch.setattr(prompt_evolution, "PROMPT_HISTORY_DIR", tmp_path / "prompt_history")

        item = {
            "domain": "prompt",
            "target": "core_agent_system.jinja",
            "action": "update",
            "reason": "unclear",
            "change": "improved body",
        }
        assert await refine_loop._apply_prompt(item) is True

        candidate = tmp_path / "prompt_history" / "core_agent_system" / "candidate.jinja"
        assert candidate.exists()
        assert candidate.read_text(encoding="utf-8").strip() == "improved body"
        # Packaged template untouched.
        assert (prompts_dir / "core_agent_system.jinja").read_text(encoding="utf-8") == "original"

    async def test_unknown_template_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novacode_cli.hermes import prompt_evolution

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        monkeypatch.setattr(prompt_evolution, "TEMPLATES_DIR", prompts_dir)
        monkeypatch.setattr(prompt_evolution, "PROMPT_HISTORY_DIR", tmp_path / "prompt_history")

        item = {
            "domain": "prompt",
            "target": "does_not_exist.jinja",
            "action": "update",
            "reason": "r",
            "change": "c",
        }
        assert await refine_loop._apply_prompt(item) is False


class TestRollback:
    async def test_restores_skill(self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(refine_loop, "_user_skills_dir", lambda: skills_dir)
        # Snapshot the current version, then overwrite.
        from novacode_cli.skills import versioning

        skill_dir = skills_dir / "alpha"
        versioning.snapshot(skill_dir, reason="test", source="test")
        (skill_dir / "SKILL.md").write_text("---\nname: alpha\n---\n\n# changed", encoding="utf-8")

        item = {"domain": "skill", "target": "alpha", "action": "patch"}
        await refine_loop._rollback(item)
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "1. old" in content  # restored to the snapshot

    async def test_rollback_missing_skill_is_safe(
        self, skills_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(refine_loop, "_user_skills_dir", lambda: skills_dir)
        item = {"domain": "skill", "target": "nope", "action": "patch"}
        await refine_loop._rollback(item)  # must not raise


class TestApplyMemory:
    async def test_records_lesson_to_topic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_dir = tmp_path / "agent"
        monkeypatch.setattr(refine_loop, "_agent_dir", lambda: agent_dir)
        item = {
            "domain": "memory",
            "target": "lessons",
            "action": "create",
            "reason": "durable fact",
            "change": "- Prefer X over Y",
        }
        assert await refine_loop._apply_memory(item) is True
        topic_file = agent_dir / "memories" / "lessons.md"
        assert topic_file.exists()
        assert "Prefer X over Y" in topic_file.read_text(encoding="utf-8")

    async def test_empty_change_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(refine_loop, "_agent_dir", lambda: tmp_path / "agent")
        item = {
            "domain": "memory",
            "target": "lessons",
            "action": "create",
            "reason": "r",
            "change": "   ",
        }
        assert await refine_loop._apply_memory(item) is False


class TestShouldRefine:
    async def test_parses_true_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.content = "<should_refine>true</should_refine><rationale>clear pattern</rationale>"
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=resp)
        monkeypatch.setattr("novacode_cli.config.model_create.create_model", lambda: model)
        should, reason = await refine_loop.should_refine(MagicMock(), MagicMock())
        assert should is True
        assert "clear pattern" in reason

    async def test_parses_false_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.content = "<should_refine>false</should_refine><rationale>one-off noise</rationale>"
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=resp)
        monkeypatch.setattr("novacode_cli.config.model_create.create_model", lambda: model)
        should, reason = await refine_loop.should_refine(MagicMock(), MagicMock())
        assert should is False
        assert "one-off noise" in reason

    async def test_missing_verdict_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = MagicMock()
        resp.content = "no verdict here"
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=resp)
        monkeypatch.setattr("novacode_cli.config.model_create.create_model", lambda: model)
        should, _ = await refine_loop.should_refine(MagicMock(), MagicMock())
        assert should is True  # fail-open: the plan + review gates remain the barrier

    async def test_gate_error_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = MagicMock()
        model.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("novacode_cli.config.model_create.create_model", lambda: model)
        should, reason = await refine_loop.should_refine(MagicMock(), MagicMock())
        assert should is True
        assert "fail-open" in reason
