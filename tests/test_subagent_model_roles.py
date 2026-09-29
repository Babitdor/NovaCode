"""Per-role models reaching the subagent specs.

Pins the two things that make the feature safe: when a role is unset the specs are
byte-for-byte what they were before (no `model` key at all, so deepagents inherits),
and when it is set the spec carries a model **object**.

Not a 'provider:model' string -- the form deepagents documents for this field, but
one it resolves with langchain's ``init_chat_model``, which only knows langchain's own
provider names. A role set to one of Nova's (``opencode``, ``nvidia``) therefore raised
"Unable to infer model provider" and no agent could be built at all.
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from novacode_cli.config.nova_config import NovaConfig


@pytest.fixture(autouse=True)
def tmp_config(monkeypatch, tmp_path):
    """Point NovaConfig at a throwaway directory, and assert that it did.

    `NovaConfig.__init__` resolves its directory from `Settings.from_environment()`,
    so that is the name to patch. The assertion is the point: without it a green suite
    cannot distinguish real isolation from the isolation having quietly stopped
    working, which for a test that writes roles is the difference between a temp file
    and someone's real config.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(
        "novacode_cli.config.nova_config.Settings.from_environment",
        lambda: SimpleNamespace(user_deepagents_dir=tmp_path / "nova"),
        raising=True,
    )
    from novacode_cli.config.nova_config import NovaConfig

    cfg = NovaConfig()
    assert str(cfg.config_path).startswith(str(tmp_path)), (
        f"isolation failed: config_path is {cfg.config_path}, not under {tmp_path}"
    )
    return tmp_path


def _specs_with_role(monkeypatch, model: object | None) -> list[dict]:
    """The roster's specs, with the subagent role resolving to *model* (or unset).

    ``retrieve_core_subagents`` imports ``build_role_model`` inside the function, so
    patching the attribute on the module it imports from does take effect.
    """
    from novacode_cli.agents.default_subagents import subagents as mod

    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_role_model",
        lambda *_a, **_k: model,
        raising=True,
    )
    return mod.retrieve_core_subagents([])


def test_role_unset_leaves_every_spec_inheriting(monkeypatch):
    specs = _specs_with_role(monkeypatch, None)
    assert specs, "no specs built"
    with_model = [s for s in specs if "model" in s]
    assert with_model == [], (
        "a spec carried a model with the role unset, so behaviour is no longer "
        "identical to before per-role models existed: "
        f"{[s['name'] for s in with_model]}"
    )


def test_role_set_stamps_a_model_object_on_every_spec(monkeypatch):
    """An object, not a spec string: a string is unresolvable for Nova's providers."""
    model = object()
    specs = _specs_with_role(monkeypatch, model)
    assert specs, "no specs built"
    assert all(s.get("model") is model for s in specs), [
        (s["name"], s.get("model")) for s in specs
    ]
    assert not any(isinstance(s.get("model"), str) for s in specs), (
        "a spec carries a string again -- the exact shape that raised "
        "'Unable to infer model provider' and stopped the agent building"
    )


def test_a_nova_only_provider_role_builds_a_real_model_object(monkeypatch, tmp_path):
    """The regression: `opencode` is not a langchain provider, so Nova must build it.

    Before this, choosing `opencode` for a role put the spec string on every subagent
    and deepagents handed it to `init_chat_model`, which raised at agent construction.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")

    from novacode_cli.config.model_create import build_role_model

    assert build_role_model("subagent") is None, "an unset role must stay unset"

    NovaConfig().set_role_model("subagent", "opencode", "deepseek-v4.1-flash")
    model = build_role_model("subagent")
    assert model is not None
    assert not isinstance(model, str), "a string here is the crash shape"
    assert getattr(model, "model_name", None) == "deepseek-v4.1-flash"


def test_frontmatter_model_beats_the_dynamic_role(monkeypatch, tmp_path):
    """A single discovered agent can name its own model."""
    from novacode_cli.agents import core_agent

    agents_dir = tmp_path / "agents" / "custom-agent"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent.md").write_text(
        textwrap.dedent(
            """            ---
            name: custom-agent
            model: anthropic:claude-sonnet-4-5-20250929
            ---
            A custom agent that does one thing.
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "novacode_cli.config.config.settings.get_all_agents",
        lambda: [("custom-agent", agents_dir, "global")],
        raising=False,
    )
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_dynamic_role_model",
        lambda *_a, **_k: object(),
        raising=True,
    )
    core_agent.clear_named_subagents_cache()

    specs = core_agent.build_named_subagents("nova-agent", [])
    (spec,) = [s for s in specs if s["name"] == "custom-agent"]
    assert spec["model"] == "anthropic:claude-sonnet-4-5-20250929"


def test_cache_clear_makes_a_role_change_take_effect(monkeypatch, tmp_path):
    """Without the clear, the TTL makes a change look ignored for a minute."""
    from novacode_cli.agents import core_agent

    calls = {"model": object()}
    monkeypatch.setattr(
        "novacode_cli.config.model_create.build_dynamic_role_model",
        lambda *_a, **_k: calls["model"],
        raising=True,
    )
    monkeypatch.setattr(
        "novacode_cli.config.config.settings.get_all_agents",
        lambda: [("custom-agent", tmp_path / "agents" / "custom-agent", "global")],
        raising=False,
    )
    agents_dir = tmp_path / "agents" / "custom-agent"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent.md").write_text("---\nname: custom-agent\n---\nDoes one thing.\n", encoding="utf-8")

    core_agent.clear_named_subagents_cache()
    first = core_agent.build_named_subagents("nova-agent", [])
    assert first[0]["model"] is calls["model"]

    stale = calls["model"]
    calls["model"] = object()
    cached = core_agent.build_named_subagents("nova-agent", [])
    assert cached[0]["model"] is stale, "expected the cache to serve the old build"

    core_agent.clear_named_subagents_cache()
    refreshed = core_agent.build_named_subagents("nova-agent", [])
    assert refreshed[0]["model"] is calls["model"]


def test_the_stored_async_role_reaches_a_spawned_servers_environment(monkeypatch):
    from novacode_cli.agents import server_launcher

    NovaConfig().set_role_model("async", "openai", "gpt-4o-mini")
    assert server_launcher.async_server_env() == {
        "ASYNC_AGENT_PROVIDER": "openai",
        "ASYNC_AGENT_MODEL": "gpt-4o-mini",
    }
