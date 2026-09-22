"""Tests for prompt-template hill climbing (Loop-Engineering Enhancement 2).

Covers the A/B decision logic, issue/template parsing, name-safety, the
engine's file lifecycle (candidate → promote / discard / rollback), persistent
detection, outcome-driven resolution, and render-time A/B routing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

import novacode_cli.prompts as P
from novacode_cli.hermes import config
from novacode_cli.hermes.prompt_evolution import (
    PromptEvolutionEngine,
    _decide_ab,
    _parse_new_template,
    parse_prompt_issues,
)


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple, dict] = {}

    async def aget(self, namespace, key):
        ns = self.data.get(tuple(namespace), {})
        return SimpleNamespace(value=dict(ns[key])) if key in ns else None

    async def aput(self, namespace, key, value):
        self.data.setdefault(tuple(namespace), {})[key] = dict(value)

    async def adelete(self, namespace, key):
        self.data.get(tuple(namespace), {}).pop(key, None)

    async def asearch(self, namespace):
        ns = self.data.get(tuple(namespace), {})
        return [SimpleNamespace(key=k, value=dict(v)) for k, v in ns.items()]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    history = tmp_path / "history"
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "test_tpl.jinja").write_text("ORIGINAL BODY\n", encoding="utf-8")
    monkeypatch.setattr(P, "PROMPT_HISTORY_DIR", history)
    monkeypatch.setattr("novacode_cli.hermes.prompt_evolution.PROMPT_HISTORY_DIR", history)
    P.reset_ab_choices()
    return PromptEvolutionEngine(FakeStore(), prompts_dir=prompts_dir), history


# ── pure logic ───────────────────────────────────────────────────────────────


class TestDecideAB:
    def _recs(self, cand, act):
        return [{"variant": "candidate", "passed": p} for p in cand] + [
            {"variant": "active", "passed": p} for p in act
        ]

    def test_continue_when_insufficient_data(self):
        assert _decide_ab(self._recs([True] * 2, [True] * 2)) == "continue"

    def test_promote_when_candidate_clearly_better(self, monkeypatch):
        monkeypatch.setattr(config, "PROMPT_AB_MIN_RUNS", 5)
        recs = self._recs([True] * 5, [False] * 5)
        assert _decide_ab(recs) == "promote"

    def test_discard_when_candidate_worse(self, monkeypatch):
        monkeypatch.setattr(config, "PROMPT_AB_MIN_RUNS", 5)
        recs = self._recs([False] * 5, [True] * 5)
        assert _decide_ab(recs) == "discard"


class TestParsing:
    def test_prompt_issues(self):
        content = (
            'noise <prompt_issue template="core_agent_system">'
            "agent keeps misreading X</prompt_issue> more noise"
        )
        issues = parse_prompt_issues(content)
        assert issues == [("core_agent_system", "agent keeps misreading X")]

    def test_new_template_extraction(self):
        assert _parse_new_template("<new_template>\nBODY\n</new_template>") == "BODY"
        assert _parse_new_template("no block") == ""


# ── name safety ──────────────────────────────────────────────────────────────


class TestNameSafety:
    def test_valid_existing_name(self, engine):
        eng, _ = engine
        assert eng._normalise_name("test_tpl") == "test_tpl.jinja"
        assert eng._normalise_name("test_tpl.jinja") == "test_tpl.jinja"

    def test_rejects_traversal_and_unknown(self, engine):
        eng, _ = engine
        assert eng._normalise_name("../../etc/passwd") is None
        assert eng._normalise_name("does_not_exist") is None


# ── file lifecycle ───────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_write_candidate_then_promote(self, engine):
        eng, history = engine
        await eng._write_candidate("test_tpl.jinja", "NEW BODY")
        cand = history / "test_tpl" / "candidate.jinja"
        assert cand.exists()
        assert "NEW BODY" in cand.read_text(encoding="utf-8")

        assert await eng.promote("test_tpl.jinja") is True
        active = history / "test_tpl" / "active.jinja"
        assert "NEW BODY" in active.read_text(encoding="utf-8")
        assert not cand.exists()

    async def test_discard(self, engine):
        eng, history = engine
        await eng._write_candidate("test_tpl.jinja", "X")
        assert await eng.discard("test_tpl.jinja") is True
        assert not (history / "test_tpl" / "candidate.jinja").exists()
        assert await eng.discard("test_tpl.jinja") is False

    async def test_rollback_drops_candidate_then_reverts_active(self, engine):
        eng, history = engine
        await eng._write_candidate("test_tpl.jinja", "CAND")
        assert "discarded" in await eng.rollback("test_tpl.jinja")
        # Promote a new candidate, then rollback should revert active → package.
        await eng._write_candidate("test_tpl.jinja", "PROMO")
        await eng.promote("test_tpl.jinja")
        assert (history / "test_tpl" / "active.jinja").exists()
        assert "reverted" in await eng.rollback("test_tpl.jinja")
        assert not (history / "test_tpl" / "active.jinja").exists()

    async def test_status_reflects_state(self, engine):
        eng, _ = engine
        assert eng.status() == []
        await eng._write_candidate("test_tpl.jinja", "C")
        rows = eng.status()
        assert rows[0]["template"] == "test_tpl.jinja"
        assert rows[0]["has_candidate"] is True
        assert rows[0]["has_active"] is False


# ── detection ────────────────────────────────────────────────────────────────


class TestDetection:
    async def test_persistent_issue_triggers_evolution(self, engine, monkeypatch):
        eng, _ = engine
        launched = []

        def _spawn(coro):
            launched.append(coro)
            coro.close()  # we only assert it was launched

        eng._spawn = _spawn
        issue = '<prompt_issue template="test_tpl">agent misreads X</prompt_issue>'
        # Three of the recent reviews carry the same issue → persistent.
        store = eng._store
        store.data[("nova", "reviews")] = {
            f"r{i}": {"timestamp": float(i), "content": issue} for i in range(3)
        }
        await eng.detect_and_maybe_evolve(issue)
        assert len(launched) == 1

    async def test_one_off_issue_does_not_trigger(self, engine):
        eng, _ = engine
        launched = []
        eng._spawn = lambda c: (launched.append(c), c.close())
        issue = '<prompt_issue template="test_tpl">one-off</prompt_issue>'
        eng._store.data[("nova", "reviews")] = {
            "r0": {"timestamp": 0.0, "content": issue}  # only one occurrence
        }
        await eng.detect_and_maybe_evolve(issue)
        assert launched == []


# ── proactive auto-evolve (periodic trigger) ────────────────────────────────


class TestAutoEvolve:
    async def test_proposes_candidate_for_configured_template(self, engine, monkeypatch):
        eng, history = engine
        # Point the configured template list at the fixture's template.
        monkeypatch.setattr(config, "PROMPT_EVOLVE_TEMPLATES", ("test_tpl.jinja",))
        launched = []
        eng._spawn = lambda c: (launched.append(c), c.close())
        await eng.maybe_auto_evolve()
        assert len(launched) == 1

    async def test_skips_template_already_under_test(self, engine, monkeypatch):
        eng, history = engine
        monkeypatch.setattr(config, "PROMPT_EVOLVE_TEMPLATES", ("test_tpl.jinja",))
        await eng._write_candidate("test_tpl.jinja", "EXISTING")
        launched = []
        eng._spawn = lambda c: (launched.append(c), c.close())
        await eng.maybe_auto_evolve()
        assert launched == []  # already A/B testing → no new candidate

    async def test_skips_unknown_template(self, engine, monkeypatch):
        eng, _ = engine
        monkeypatch.setattr(config, "PROMPT_EVOLVE_TEMPLATES", ("does_not_exist.jinja",))
        launched = []
        eng._spawn = lambda c: (launched.append(c), c.close())
        await eng.maybe_auto_evolve()
        assert launched == []

    async def test_disabled_is_noop(self, engine, monkeypatch):
        eng, _ = engine
        monkeypatch.setattr(config, "PROMPT_EVOLVE_TEMPLATES", ("test_tpl.jinja",))
        eng._enabled = False
        launched = []
        eng._spawn = lambda c: (launched.append(c), c.close())
        await eng.maybe_auto_evolve()
        assert launched == []


# ── outcome-driven resolution ────────────────────────────────────────────────


class TestResolution:
    async def test_record_outcome_promotes_winning_candidate(self, engine, monkeypatch):
        eng, history = engine
        monkeypatch.setattr(config, "PROMPT_AB_MIN_RUNS", 2)
        await eng._write_candidate("test_tpl.jinja", "WINNER")
        # Force this process onto a known variant so attribution is deterministic.
        P.reset_ab_choices()
        P._AB_CHOICE["test_tpl.jinja"] = "candidate"
        await eng.record_outcome(passed=True)
        await eng.record_outcome(passed=True)
        # Switch the cached variant to feed the 'active' arm losing records.
        P._AB_CHOICE["test_tpl.jinja"] = "active"
        await eng.record_outcome(passed=False)
        await eng.record_outcome(passed=False)
        # Candidate (100%) beat active (0%) over >= min runs → promoted.
        assert (history / "test_tpl" / "active.jinja").exists()
        assert not (history / "test_tpl" / "candidate.jinja").exists()


# ── render routing ───────────────────────────────────────────────────────────


class TestRenderRouting:
    @pytest.fixture
    def routed(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "demo.jinja").write_text("PACKAGE {{ x }}", encoding="utf-8")
        (pkg / "other.jinja").write_text("OTHER {{ x }}", encoding="utf-8")
        # Overrides written AFTER the package template: fresh, so they apply.
        history = tmp_path / "history"
        (history / "demo").mkdir(parents=True)
        (history / "demo" / "candidate.jinja").write_text("CANDIDATE {{ x }}", encoding="utf-8")
        (history / "demo" / "active.jinja").write_text("ACTIVE {{ x }}", encoding="utf-8")

        def _new_env(root):
            return Environment(
                loader=FileSystemLoader(str(root)),
                autoescape=select_autoescape(default=False),
                trim_blocks=True,
                lstrip_blocks=True,
            )

        monkeypatch.setattr(P, "PROMPT_HISTORY_DIR", history)
        monkeypatch.setattr(P, "_override_env", _new_env(history))
        monkeypatch.setattr(P, "_env", _new_env(pkg))
        P.reset_ab_choices()
        return history

    def test_candidate_variant_used(self, routed):
        P._AB_CHOICE["demo.jinja"] = "candidate"
        assert P.render_template("demo.jinja", x=1) == "CANDIDATE 1"

    def test_active_variant_used(self, routed):
        P._AB_CHOICE["demo.jinja"] = "active"
        assert P.render_template("demo.jinja", x=1) == "ACTIVE 1"

    def test_a_package_edit_retires_stale_overrides(self, routed):
        """An override is a full copy of the OLD template; serving it after the
        package was edited silently reverts that edit."""
        import os
        import time

        future = time.time() + 60
        pkg = P._env.loader.searchpath[0]
        os.utime(os.path.join(pkg, "demo.jinja"), (future, future))
        for variant in ("candidate", "active"):
            P._AB_CHOICE["demo.jinja"] = variant
            assert P.render_template("demo.jinja", x=1) == "PACKAGE 1"
        assert P.current_variant("demo.jinja") is None

    def test_no_override_uses_package(self, routed):
        # 'other' has no override dir → fast path to the packaged template.
        assert P.render_template("other.jinja", x=2) == "OTHER 2"

    def test_current_variant_none_without_candidate(self, routed):
        assert P.current_variant("other.jinja") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
