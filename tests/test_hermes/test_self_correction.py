"""Tests for Phase 2 — failure-grounded self-correction.

Covers per-skill failure-sample capture and refinement proposals grounded in
those samples.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import novacode_cli.config.model_create as model_create_mod
from novacode_cli.hermes.skill_discovery import refine_skill
from novacode_cli.hermes.tracker import ToolUsageTracker
from novacode_cli.skills import versioning

if TYPE_CHECKING:
    from pathlib import Path


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple, dict] = {}

    async def aget(self, namespace, key):
        ns = self.data.get(tuple(namespace), {})
        return SimpleNamespace(value=dict(ns[key])) if key in ns else None

    async def aput(self, namespace, key, value):
        self.data.setdefault(tuple(namespace), {})[key] = dict(value)

    async def asearch(self, namespace):
        ns = self.data.get(tuple(namespace), {})
        return [SimpleNamespace(key=k, value=dict(v)) for k, v in ns.items()]


@pytest.fixture
def tracker():
    return ToolUsageTracker(FakeStore(), enabled=True)


@pytest.fixture(autouse=True)
def isolated_learning_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep refinement proposals in the ledger asserted by these tests."""
    from novacode_cli.hermes.ledger import LearningLedger

    monkeypatch.setattr(
        "novacode_cli.hermes.skill_proposals.main_agent_learning_ledger",
        lambda: LearningLedger(tmp_path / "learning.sqlite3"),
    )


# ── failure-sample capture ───────────────────────────────────────────────────


class TestFailureSampleCapture:
    async def test_failure_excerpt_recorded(self, tracker):
        await tracker.record_tool_usage("read_file", True, skill_invoked="s")
        await tracker.record_tool_usage(
            "execute", False, error_excerpt="Traceback ... ValueError: boom"
        )
        usage = await tracker.get_skill_usage()
        samples = usage["s"]["failure_samples"]
        assert len(samples) == 1
        assert "boom" in samples[0]["excerpt"]
        assert samples[0]["tool"] == "execute"

    async def test_success_records_no_sample(self, tracker):
        await tracker.record_tool_usage("read_file", True, skill_invoked="s")
        await tracker.record_tool_usage("execute", True, error_excerpt="ignored")
        usage = await tracker.get_skill_usage()
        assert usage["s"].get("failure_samples", []) == []

    async def test_samples_capped(self, tracker):
        from novacode_cli.hermes.tracker import (
            _MAX_FAILURE_SAMPLES,
            _SKILL_ATTRIBUTION_BUDGET,
        )

        await tracker.record_tool_usage("read_file", True, skill_invoked="s")
        # Stay within the attribution window so every failure is attributed.
        n = _SKILL_ATTRIBUTION_BUDGET
        for i in range(n):
            await tracker.record_tool_usage(
                "execute", False, error_excerpt=f"err {i}"
            )
        usage = await tracker.get_skill_usage()
        # Capped to _MAX_FAILURE_SAMPLES, keeping the most recent.
        assert len(usage["s"]["failure_samples"]) == _MAX_FAILURE_SAMPLES
        assert f"err {n - 1}" in usage["s"]["failure_samples"][-1]["excerpt"]


# ── grounded refinement ──────────────────────────────────────────────────────


class FakeModel:
    def __init__(self, output: str) -> None:
        self.output = output
        self.last_prompt = ""

    async def ainvoke(self, messages, config=None):
        self.last_prompt = str(messages[0].content)
        return SimpleNamespace(content=self.output)


def _make_skill(skills_dir, name, body):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "old"\n---\n\n{body}\n', encoding="utf-8"
    )
    return d


class TestGroundedRefinement:
    async def test_refine_uses_failure_samples_and_proposes(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "flaky", "Run pytest -x")
        improved = '---\nname: flaky\ndescription: "fixed"\n---\n\n# flaky\n\nRun uv run pytest\n'
        fake = FakeModel(improved)
        monkeypatch.setattr(model_create_mod, "create_model", lambda *a, **k: fake)

        samples = [{"tool": "execute", "excerpt": "pytest: command not found"}]
        ok = await refine_skill("flaky", skills_dir, "high_failure", failure_samples=samples)

        assert ok is True
        content = (skills_dir / "flaky" / "SKILL.md").read_text(encoding="utf-8")
        assert "Run pytest -x" in content
        from novacode_cli.hermes.ledger import LearningLedger
        proposal = LearningLedger(tmp_path / "learning.sqlite3").list()[-1]
        assert "uv run pytest" in proposal.content
        assert proposal.status == "candidate"
        # The failure sample was injected into the refinement prompt.
        assert "command not found" in fake.last_prompt

    async def test_refine_preserves_source_until_approval(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        d = _make_skill(skills_dir, "flaky", "original step")
        improved = '---\nname: flaky\ndescription: "x"\n---\n\nnew step\n'
        monkeypatch.setattr(
            model_create_mod, "create_model", lambda *a, **k: FakeModel(improved)
        )
        await refine_skill("flaky", skills_dir, "high_failure", failure_samples=[])
        reasons = [v["reason"] for v in versioning.list_versions(d)]
        assert reasons == []
        assert "original step" in (d / "SKILL.md").read_text(encoding="utf-8")
        from novacode_cli.hermes.ledger import LearningLedger
        proposal = LearningLedger(tmp_path / "learning.sqlite3").list()[-1]
        assert "new step" in proposal.content
        assert proposal.source_hash
        assert proposal.status == "candidate"

    async def test_no_write_when_unchanged(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        same = '---\nname: flaky\ndescription: "old"\n---\n\nsame body\n'
        _make_skill(skills_dir, "flaky", "same body")
        # Model echoes back equivalent content → no-op.
        monkeypatch.setattr(
            model_create_mod, "create_model", lambda *a, **k: FakeModel(same)
        )
        ok = await refine_skill("flaky", skills_dir, "high_failure", failure_samples=[])
        assert ok is False

    async def test_missing_skill_returns_false(self, tmp_path):
        ok = await refine_skill("ghost", tmp_path / "skills", "high_failure")
        assert ok is False
