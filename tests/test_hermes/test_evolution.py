"""Tests for the self-evolution engine — scoring + create-or-level-up.

Covers:
- score_task_complexity (trivial vs complex)
- parse_level_up
- run_evolution: <skill> -> new skill + evolution_log + unlocked counter
- run_evolution: <level_up> -> snapshot + rewrite + leveled counter
- maybe_evolve gate: no LLM below threshold, spawns above
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novacode_cli.hermes.evolution import (
    COMPLEXITY_THRESHOLD,
    EvolutionEngine,
    parse_level_up,
    score_task_complexity,
)
from novacode_cli.hermes.skill_manager import SkillManager
from novacode_cli.hermes.tracker import ToolUsageTracker


class FakeStore:
    """Minimal in-memory async store: (namespace, key) -> value dict."""

    def __init__(self) -> None:
        self.data: dict[tuple, dict] = {}

    async def aget(self, namespace, key):
        ns = self.data.get(tuple(namespace), {})
        if key not in ns:
            return None
        return SimpleNamespace(value=dict(ns[key]))

    async def aput(self, namespace, key, value):
        self.data.setdefault(tuple(namespace), {})[key] = dict(value)

    async def asearch(self, namespace):
        ns = self.data.get(tuple(namespace), {})
        return [SimpleNamespace(key=k, value=dict(v)) for k, v in ns.items()]


class FakeModel:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list = []

    async def ainvoke(self, messages, config=None):  # noqa: ARG002
        self.calls.append(messages)
        return SimpleNamespace(content=self._content)


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture(autouse=True)
def isolated_learning_ledger(tmp_path, monkeypatch):
    from novacode_cli.hermes.ledger import LearningLedger

    monkeypatch.setattr(
        "novacode_cli.hermes.skill_proposals.main_agent_learning_ledger",
        lambda: LearningLedger(tmp_path / "learning.sqlite3"),
    )


def _engine(store, skills_dir):
    tracker = ToolUsageTracker(store, enabled=True)
    sm = SkillManager(store, skills_dir=skills_dir, enabled=True)
    return EvolutionEngine(store, tracker, sm, skills_dir=skills_dir, enabled=True)


_FULL_BREAKDOWN = {
    "substantive": 3,
    "distinct_tools": 3,
    "subagents": 1,
    "todos_completed": 1,
    "recovered": True,
    "score": 19,
}


# ── Scoring ──────────────────────────────────────────────────────────────────


class TestScoring:
    def test_trivial_below_threshold(self):
        window = [{"tool": "read_file", "success": True}] * 3
        score, _ = score_task_complexity(window, {})
        assert score < COMPLEXITY_THRESHOLD

    def test_complex_above_threshold(self):
        window = [
            {"tool": "edit_file", "success": True},
            {"tool": "execute", "success": False},
            {"tool": "execute", "success": True},
            {"tool": "task", "success": True},
        ]
        state = {"todos": [{"status": "completed"}, {"status": "pending"}]}
        score, breakdown = score_task_complexity(window, state)
        assert score >= COMPLEXITY_THRESHOLD
        assert breakdown["subagents"] == 1
        assert breakdown["recovered"] is True
        assert breakdown["todos_completed"] == 1

    def test_task_tool_not_double_counted(self):
        # `task` counts as a subagent, not as substantive.
        window = [{"tool": "task", "success": True}]
        _, breakdown = score_task_complexity(window, {})
        assert breakdown["substantive"] == 0
        assert breakdown["subagents"] == 1


# ── level_up parsing ─────────────────────────────────────────────────────────


class TestParseLevelUp:
    def test_valid(self):
        text = '<level_up skill="add-tui-command"><body>---\nname: x\n---\nsteps</body></level_up>'
        parsed = parse_level_up(text)
        assert parsed["skill"] == "add-tui-command"
        assert "steps" in parsed["body"]

    def test_no_block(self):
        assert parse_level_up("just a <skill>...</skill>") is None

    def test_missing_skill_attr(self):
        assert parse_level_up("<level_up><body>x</body></level_up>") is None


# ── run_evolution: create new skill ──────────────────────────────────────────


class TestRunEvolutionCreate:
    async def test_new_skill_unlocked(self, store, tmp_path, monkeypatch):
        skill_block = (
            "<skill>\n<name>add-tui-command</name>\n"
            "<description>Use when adding a /command to the TUI.</description>\n"
            "<body>1. edit app.py\n2. add to help</body>\n</skill>"
        )
        monkeypatch.setattr(
            "novacode_cli.config.model_create.create_model",
            lambda: FakeModel(skill_block),
        )
        engine = _engine(store, tmp_path)
        await engine.run_evolution([], _FULL_BREAKDOWN)

        assert not (tmp_path / "add-tui-command" / "SKILL.md").exists()
        from novacode_cli.hermes.ledger import LearningLedger
        proposal = LearningLedger(tmp_path / "learning.sqlite3").list()[-1]
        assert proposal.status == "candidate"
        assert proposal.target == "add-tui-command"
        assert "edit app.py" in proposal.content
        # evolution_log + counter bumped
        log = store.data.get(("nova", "evolution_log"), {})
        assert any(v["kind"] == "unlock" for v in log.values())
        assert store.data[("nova", "meta")]["evolution"]["unlocked"] == 1


# ── run_evolution: level up existing skill ───────────────────────────────────


class TestRunEvolutionLevelUp:
    async def test_existing_skill_levelled_up(self, store, tmp_path, monkeypatch):
        skill_dir = tmp_path / "add-tui-command"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: add-tui-command\ndescription: old\n---\nold steps\n",
            encoding="utf-8",
        )
        new_md = "---\nname: add-tui-command\ndescription: better\n---\nimproved steps\n"
        level_block = f'<level_up skill="add-tui-command"><body>{new_md}</body></level_up>'
        monkeypatch.setattr(
            "novacode_cli.config.model_create.create_model",
            lambda: FakeModel(level_block),
        )
        engine = _engine(store, tmp_path)
        await engine.run_evolution([], _FULL_BREAKDOWN)

        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "old steps" in content
        from novacode_cli.hermes.ledger import LearningLedger
        proposal = LearningLedger(tmp_path / "learning.sqlite3").list()[-1]
        assert proposal.status == "candidate"
        assert "improved steps" in proposal.content
        assert proposal.source_hash
        assert not (skill_dir / ".history").exists()
        assert store.data[("nova", "meta")]["evolution"]["leveled"] == 1


# ── maybe_evolve gate ────────────────────────────────────────────────────────


class TestMaybeEvolveGate:
    async def test_below_threshold_no_spawn(self, store, tmp_path, monkeypatch):
        engine = _engine(store, tmp_path)
        # Seed a trivial window.
        await store.aput(
            ("nova", engine._tracker.owner, "tool_history"),
            "history",
            {"entries": [{"tool": "read_file", "success": True, "timestamp": 1.0}]},
        )
        spawned: list = []
        monkeypatch.setattr(engine._skill_manager, "spawn_task", spawned.append)
        await engine.maybe_evolve({"messages": []}, task_start_ts=None)
        assert spawned == []

    async def test_above_threshold_spawns(self, store, tmp_path, monkeypatch):
        engine = _engine(store, tmp_path)
        window = [
            {"tool": "edit_file", "success": True, "timestamp": 1.0},
            {"tool": "execute", "success": False, "timestamp": 2.0},
            {"tool": "execute", "success": True, "timestamp": 3.0},
            {"tool": "task", "success": True, "timestamp": 4.0},
        ]
        await store.aput(("nova", engine._tracker.owner, "tool_history"), "history", {"entries": window})

        spawned: list = []

        def _capture(coro):
            spawned.append(coro)
            coro.close()  # don't actually run the OOB model call

        monkeypatch.setattr(engine._skill_manager, "spawn_task", _capture)
        await engine.maybe_evolve(
            {"messages": [], "todos": [{"status": "completed"}]},
            task_start_ts=None,
        )
        assert len(spawned) == 1
