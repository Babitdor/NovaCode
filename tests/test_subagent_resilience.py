"""Tests for subagent resilience hardening.

Subagents get no retry middleware from deepagents, so a transient provider
5xx/429 during a subagent's model call kills it (this is what broke /init's
semantic-extraction subagents). ``_harden_subagent_specs`` returns resilient
copies of the specs: each declarative subagent gets a ModelRetryMiddleware and
its ``interrupt_on`` cleared. It must NOT mutate the (cached) input specs, or a
second ``create_agent_with_config`` build would accumulate a duplicate retry
middleware and langchain aborts with "duplicate middleware instances".
"""

import pytest
from langchain.agents.middleware import ModelRetryMiddleware

from novacode_cli.agents.core_agent import _harden_subagent_specs
from novacode_cli.bootstrap import VisionCaptionMiddleware
from novacode_cli.security.middleware import SecurityMiddleware


def test_roster_combines_sources_without_duplicate_names(monkeypatch: pytest.MonkeyPatch) -> None:
    import novacode_cli.agents.core_agent as core
    from novacode_cli.plugins import claude_plugins

    builtin = {"name": "code-explorer", "description": "Built-in", "system_prompt": "Explore"}
    named = {**builtin, "description": "Named copy"}
    plugin = {**builtin, "name": "plugin-agent"}
    remote = {"name": "code-explorer", "graph_id": "explore", "url": "http://localhost"}
    monkeypatch.setattr(core, "retrieve_core_subagents", lambda **_kw: [builtin])
    monkeypatch.setattr(core, "build_named_subagents", lambda **_kw: [named])
    monkeypatch.setattr(claude_plugins, "plugin_agent_specs", lambda: [plugin, plugin])
    monkeypatch.setattr(core, "retrieve_async_subagents", lambda: [remote])
    monkeypatch.setattr(core, "_harden_subagent_specs", lambda specs, *_args: specs)
    result = core._build_subagent_roster(
        assistant_id="nova", tools=[], plugin_specs=[], skill_sources=[]
    )
    names = [s["name"] for s in result]
    assert len(names) == len(set(names))
    assert {"code-explorer", "general-purpose", "plugin-agent"} <= set(names)
    assert next(s for s in result if s["name"] == "code-explorer") is builtin


def test_declarative_specs_get_retry_vision_security_first_and_no_interrupts():
    specs = [
        {"name": "bug-fix", "description": "d", "system_prompt": "p", "tools": []},
        {"name": "general-purpose", "description": "d", "system_prompt": "p"},
    ]
    out = _harden_subagent_specs(specs)

    for s in out:
        # interrupt_on cleared (subagents never raise nested HITL).
        assert s["interrupt_on"] == {}
        # ModelRetryMiddleware, VisionCaptionMiddleware, and SecurityMiddleware are present.
        mw = s["middleware"]
        assert isinstance(mw[0], ModelRetryMiddleware)
        assert isinstance(mw[1], VisionCaptionMiddleware)
        assert isinstance(mw[2], SecurityMiddleware)


def test_existing_subagent_middleware_is_preserved_after_hardening():
    from novacode_cli.tracking.loop_guard import LoopGuardMiddleware

    sentinel = object()
    specs = [{"name": "x", "system_prompt": "p", "middleware": [sentinel]}]
    out = _harden_subagent_specs(specs)
    mw = out[0]["middleware"]
    assert isinstance(mw[0], ModelRetryMiddleware)
    assert isinstance(mw[1], VisionCaptionMiddleware)
    assert isinstance(mw[2], SecurityMiddleware)
    assert isinstance(mw[3], LoopGuardMiddleware)
    # The caller's own middleware must survive hardening and stay last. Asserted
    # by position rather than index so adding another hardening middleware (the
    # async-subagent one, for instance) doesn't false-alarm this test.
    assert mw[-1] is sentinel


def test_fresh_middleware_instances_per_spec_not_shared():
    specs = [
        {"name": "a", "system_prompt": "p"},
        {"name": "b", "system_prompt": "p"},
    ]
    out = _harden_subagent_specs(specs)
    # Middleware binds to a graph; each spec must get its own instance.
    assert out[0]["middleware"][0] is not out[1]["middleware"][0]
    assert out[0]["middleware"][1] is not out[1]["middleware"][1]
    assert out[0]["middleware"][2] is not out[1]["middleware"][2]


def test_does_not_mutate_input_specs():
    # The inputs may be cached/shared dicts reused across builds — leave them be.
    specs = [{"name": "a", "system_prompt": "p"}]
    original = specs[0]
    _harden_subagent_specs(specs)
    assert "middleware" not in original
    assert "interrupt_on" not in original


def test_repeated_hardening_never_accumulates_middleware():
    # Simulate two create_agent_with_config builds reusing the SAME cached spec.
    cached = {"name": "a", "system_prompt": "p"}
    cache = [cached]
    first = _harden_subagent_specs(cache)
    second = _harden_subagent_specs(cache)
    for built in (first, second):
        retries = [m for m in built[0]["middleware"] if isinstance(m, ModelRetryMiddleware)]
        visions = [m for m in built[0]["middleware"] if isinstance(m, VisionCaptionMiddleware)]
        securities = [m for m in built[0]["middleware"] if isinstance(m, SecurityMiddleware)]
        # Exactly one of each — never duplicates.
        assert len(retries) == 1
        assert len(visions) == 1
        assert len(securities) == 1
    # And the two builds get distinct instances.
    assert first[0]["middleware"][0] is not second[0]["middleware"][0]
    assert first[0]["middleware"][1] is not second[0]["middleware"][1]
    assert first[0]["middleware"][2] is not second[0]["middleware"][2]


def test_spec_already_carrying_vision_is_not_doubled():
    specs = [{"name": "a", "system_prompt": "p", "middleware": [VisionCaptionMiddleware()]}]
    out = _harden_subagent_specs(specs)
    visions = [m for m in out[0]["middleware"] if isinstance(m, VisionCaptionMiddleware)]
    assert len(visions) == 1


def test_spec_already_carrying_security_is_not_doubled():
    specs = [{"name": "a", "system_prompt": "p", "middleware": [SecurityMiddleware()]}]
    out = _harden_subagent_specs(specs)
    securities = [m for m in out[0]["middleware"] if isinstance(m, SecurityMiddleware)]
    assert len(securities) == 1


def test_spec_already_carrying_retry_is_not_doubled():
    specs = [{"name": "a", "system_prompt": "p", "middleware": [ModelRetryMiddleware()]}]
    out = _harden_subagent_specs(specs)
    retries = [m for m in out[0]["middleware"] if isinstance(m, ModelRetryMiddleware)]
    assert len(retries) == 1


def test_compiled_and_remote_subagents_untouched():
    runnable = object()
    specs = [
        {"name": "compiled", "runnable": runnable},
        {"name": "remote", "url": "http://example/agent"},
    ]
    out = _harden_subagent_specs(specs)
    # Passed through unchanged — these own their own config.
    assert out[0] is specs[0]
    assert "middleware" not in out[0]
    assert out[1] is specs[1]
    assert "middleware" not in out[1]


def test_non_dict_entries_are_skipped():
    specs = ["not-a-dict", 123]
    out = _harden_subagent_specs(specs)  # must not raise
    assert out == ["not-a-dict", 123]


# ── skill grant + curation (subagents had no skills before) ──────────────────


def _curations(spec: dict) -> list:
    return [m for m in spec["middleware"] if type(m).__name__ == "SkillCurationMiddleware"]


def test_skill_sources_grant_skills_and_curation():
    specs = [{"name": "a", "system_prompt": "p"}]
    out = _harden_subagent_specs(specs, ["/skills/", "/claude-skills/"])
    # The subagent now declares the skill sources (deepagents will attach a
    # SkillsMiddleware on the shared backend) ...
    assert out[0]["skills"] == ["/skills/", "/claude-skills/"]
    # ... and gets exactly one curation middleware to clamp them.
    assert len(_curations(out[0])) == 1


def test_no_skill_sources_means_no_skills_no_curation():
    specs = [{"name": "a", "system_prompt": "p"}]
    out = _harden_subagent_specs(specs)  # default None
    assert "skills" not in out[0]
    assert _curations(out[0]) == []


def test_pre_declared_skills_are_respected_and_still_curated():
    specs = [{"name": "a", "system_prompt": "p", "skills": ["/custom/"]}]
    out = _harden_subagent_specs(specs, ["/skills/"])
    # Spec's own skills win; we don't overwrite them ...
    assert out[0]["skills"] == ["/custom/"]
    # ... but they're still clamped to the curated set.
    assert len(_curations(out[0])) == 1


def test_curation_not_doubled_when_already_present():
    from novacode_cli.skills.curation_middleware import SkillCurationMiddleware

    specs = [{"name": "a", "system_prompt": "p", "middleware": [SkillCurationMiddleware()]}]
    out = _harden_subagent_specs(specs, ["/skills/"])
    assert len(_curations(out[0])) == 1


def test_compiled_and_remote_untouched_even_with_skill_sources():
    specs = [
        {"name": "compiled", "runnable": object()},
        {"name": "remote", "url": "http://example/agent"},
    ]
    out = _harden_subagent_specs(specs, ["/skills/"])
    assert out[0] is specs[0]
    assert "skills" not in out[0]
    assert out[1] is specs[1]
    assert "skills" not in out[1]
