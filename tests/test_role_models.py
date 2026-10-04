"""Per-role model storage and resolution.

The behaviour worth pinning here is inheritance: until a role is set it must resolve
to None so callers keep the pre-existing path, and setting one role must never
disturb another or the main agent's own ``model`` key.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novacode_cli.config.nova_config import ROLE_NAMES, NovaConfig
from novacode_cli.config.role_models import (
    ROLE_LABELS,
    async_server_env,
    describe_roles,
    dynamic_spec,
    panel_row_model,
    role_specs,
    subagent_spec,
)


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


def test_every_role_inherits_by_default():
    cfg = NovaConfig()
    assert cfg.all_role_models() == {}
    assert role_specs(cfg) == dict.fromkeys(ROLE_NAMES, None)
    assert subagent_spec(cfg) is None
    assert dynamic_spec(cfg) is None
    assert async_server_env(cfg) == {}


def test_setting_one_role_leaves_the_others_and_the_main_model_alone():
    cfg = NovaConfig()
    cfg.set_model_config("ollama", "gemma4:31b-cloud")
    cfg.set_role_model("subagent", "openai", "gpt-5-mini")

    assert cfg.get_model_config() == {"provider": "ollama", "model": "gemma4:31b-cloud"}
    assert cfg.get_role_model("subagent") == {"provider": "openai", "model": "gpt-5-mini"}
    assert cfg.get_role_model("async") is None
    assert subagent_spec(cfg) == "openai:gpt-5-mini"


def test_main_role_is_the_existing_model_key():
    """`main` must not duplicate the model: it IS the saved model."""
    cfg = NovaConfig()
    cfg.set_role_model("main", "anthropic", "claude-sonnet-4-5-20250929")

    assert cfg.get_model_config() == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5-20250929",
    }
    assert role_specs(cfg)["main"] == "anthropic:claude-sonnet-4-5-20250929"


def test_two_instances_writing_different_roles_both_survive():
    """The merge guarantee `_save` documents, applied to roles.

    A long-lived instance loaded before the other's write must not put its stale
    snapshot back. One top-level key per role is what makes this hold.
    """
    first, second = NovaConfig(), NovaConfig()
    first.set_role_model("subagent", "openai", "gpt-5-mini")
    second.set_role_model("async", "anthropic", "claude-sonnet-4-5-20250929")

    fresh = NovaConfig()
    assert fresh.get_role_model("subagent") == {"provider": "openai", "model": "gpt-5-mini"}
    assert fresh.get_role_model("async") == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5-20250929",
    }


def test_clear_restores_inheritance():
    cfg = NovaConfig()
    cfg.set_role_model("subagent", "openai", "gpt-5-mini")
    cfg.clear_role_model("subagent")

    assert cfg.get_role_model("subagent") is None
    assert subagent_spec(NovaConfig()) is None


def test_dynamic_falls_back_to_the_subagent_role():
    cfg = NovaConfig()
    cfg.set_role_model("subagent", "openai", "gpt-5-mini")
    assert dynamic_spec(cfg) == "openai:gpt-5-mini"

    cfg.set_role_model("dynamic", "google", "gemini-3-pro-preview")
    assert dynamic_spec(cfg) == "google:gemini-3-pro-preview"


def test_unknown_role_is_rejected():
    cfg = NovaConfig()
    with pytest.raises(ValueError, match="Unknown model role"):
        cfg.get_role_model("nonsense")


def test_async_role_becomes_the_env_the_graphs_read():
    cfg = NovaConfig()
    assert async_server_env(cfg) == {}

    cfg.set_role_model("async", "openai", "gpt-4o-mini", base_url="http://localhost:1234/v1")
    assert async_server_env(cfg) == {
        "ASYNC_AGENT_PROVIDER": "openai",
        "ASYNC_AGENT_MODEL": "gpt-4o-mini",
        "OPENAI_BASE_URL": "http://localhost:1234/v1",
    }


def test_describe_roles_never_says_a_bare_inherit():
    cfg = NovaConfig()
    cfg.set_role_model("subagent", "openai", "gpt-5-mini")
    rows = describe_roles("ollama", "gemma4:31b-cloud", cfg)

    assert [role for role, _, _ in rows] == list(ROLE_NAMES)
    assert {role: effective for role, _, effective in rows}["subagent"] == "openai:gpt-5-mini"
    for role, label, effective in rows:
        assert label == ROLE_LABELS[role]
        if role != "main":
            assert effective != "inherit"
    by_role = {role: effective for role, _, effective in rows}
    # An unset async role follows the main agent's model, like the others.
    assert by_role["async"] == by_role["main"]


# ── the panel's MODEL column ─────────────────────────────────────────────────


def test_panel_rows_fall_back_to_the_session_model(monkeypatch):
    """Unset roles must show exactly what the column showed before."""
    for kind in ("direct", "eval", "async", "something-new"):
        assert panel_row_model(kind, "ollama:gemma4:31b-cloud") == "ollama:gemma4:31b-cloud"
    assert panel_row_model("direct", "deepseek-v4.1-flash") == "deepseek-v4.1-flash"
    assert panel_row_model("eval", "deepseek-v4.1-flash") == "deepseek-v4.1-flash"


def test_panel_rows_report_the_role_that_ran(tmp_config):
    cfg = NovaConfig()
    cfg.set_role_model("subagent", "openai", "gpt-5-mini")
    cfg.set_role_model("async", "anthropic", "claude-sonnet-4-5-20250929")

    session = "deepseek-v4.1-flash"
    assert panel_row_model("direct", session, cfg) == "openai:gpt-5-mini"
    assert panel_row_model("eval", session, cfg) == "openai:gpt-5-mini"
    assert panel_row_model("async", session, cfg) == "anthropic:claude-sonnet-4-5-20250929"
    # An unknown kind is not a subagent row, so it keeps the session model.
    assert panel_row_model("mystery", session, cfg) == session


def test_async_rows_show_the_session_model_when_the_role_is_unset(tmp_config):
    """An unset async role runs on the session's model, so the row says so."""
    assert panel_row_model("async", "deepseek-v4.1-flash", NovaConfig()) == "deepseek-v4.1-flash"
