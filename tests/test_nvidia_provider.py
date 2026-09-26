"""The NVIDIA NIM provider is registered, and its key is never hardcoded.

Added because ChatNVIDIA was requested with the key inline in the constructor.
Nova resolves it from the keychain or NVIDIA_API_KEY instead, so these pin both
the wiring and that no literal key ever reaches the source tree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Keep config writes and key lookups out of the real ~/.nova and keychain.

    ``HOME``/``USERPROFILE`` alone are not enough: ``config.HOME_DIR`` is
    computed at import time and ``Settings.user_deepagents_dir`` returns that
    frozen constant, so ``NovaConfig`` kept resolving the real ``~/.nova`` — a
    test that saved a model overwrote the developer's actual
    ``Nova.config.json``. Patch the constant itself as well.
    """
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")


def test_preset_is_registered():
    from novacode_cli.config.model_manager import MODEL_PRESETS

    preset = MODEL_PRESETS["nvidia"]
    assert preset["api_key_var"] == "NVIDIA_API_KEY"
    assert preset["requires_api_key"] is True
    # Real NVIDIA model ids are namespaced (`vendor/model`); a bare name is a
    # sign someone invented one, and it 404s only at call time.
    assert all("/" in m for m in preset["models"])
    assert preset["default_model"] in preset["models"]


def test_key_is_read_from_the_environment(isolated):
    from novacode_cli.config.config import Settings

    settings = Settings.from_environment()
    assert settings.nvidia_api_key == "nvapi-test-key"
    assert settings.has_nvidia


def test_no_key_means_not_available(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    from novacode_cli.config.config import Settings

    # A keychain entry on the dev's own machine would also satisfy this, so only
    # assert the negative when neither source has one.
    if Settings.from_environment().nvidia_api_key is None:
        assert not Settings.from_environment().has_nvidia


def test_build_chat_model_returns_a_configured_client(isolated):
    from novacode_cli.config.model_create import build_chat_model

    model = build_chat_model("nvidia", "deepseek-ai/deepseek-v4-pro-0813")
    assert type(model).__name__ == "ChatNVIDIA"
    assert model.model == "deepseek-ai/deepseek-v4-pro-0813"


def test_provider_appears_in_the_model_picker(isolated):
    from novacode_cli.config.model_manager import ModelManager

    available = [p for p, _ in ModelManager().get_available_providers()]
    assert "nvidia" in available


def test_switching_to_nvidia_persists(isolated):
    from novacode_cli.config.model_manager import ModelManager
    from novacode_cli.config.nova_config import NovaConfig

    ModelManager().set_provider("nvidia", "nvidia/nemotron-3-super-120b-a12b")
    saved = NovaConfig().get_model_config()
    assert saved == {
        "provider": "nvidia",
        "model": "nvidia/nemotron-3-super-120b-a12b",
    }


def test_an_nvidia_key_alone_is_reported_as_the_active_provider(isolated, monkeypatch):
    """Without this the picker fell through to Ollama and hid the real provider."""
    from novacode_cli.config.model_manager import ModelManager

    manager = ModelManager()
    for attr in ("has_openai", "has_anthropic", "has_google", "has_openrouter",
                 "has_opencode"):
        monkeypatch.setattr(type(manager.settings), attr, property(lambda _: False))
    monkeypatch.setattr(manager.nova_config, "get_model_config", lambda: None)
    assert manager.get_current_provider()[0] == "NVIDIA NIM"


def test_no_api_key_is_hardcoded_anywhere():
    """The key pasted into the request must never have landed in the tree."""
    root = Path(__file__).resolve().parent.parent / "novacode_cli"
    hits = subprocess.run(
        ["git", "grep", "-rIn", "nvapi-", "--", str(root)],
        capture_output=True, text=True, cwd=root.parent, check=False,
    ).stdout.strip()
    assert not hits, f"an NVIDIA key literal is committed:\n{hits}"


def test_onboarding_offers_nvidia_and_stores_the_key_under_the_right_name():
    """The wizard derives the env var as f"{provider.upper()}_API_KEY"."""
    from novacode_cli.onboarding import API_KEY_NAMES, OnboardingWizard

    assert "nvidia" in {p["name"] for p in OnboardingWizard.PROVIDERS.values()}
    assert API_KEY_NAMES["nvidia"] == "nvidia_api_key"

    from novacode_cli.config.model_manager import MODEL_PRESETS

    assert f"{'nvidia'.upper()}_API_KEY" == MODEL_PRESETS["nvidia"]["api_key_var"]


# ── chat_template_kwargs (the thinking toggle) ──────────────────────────────
#
# ChatNVIDIA does not declare chat_template_kwargs as a field, so passing it
# top-level made the client relocate it into model_kwargs and warn on EVERY
# model build: "chat_template_kwargs is not default parameter ... please confirm
# that chat_template_kwargs is what you intended". Passing it inside
# model_kwargs is the declared way to say the same thing; these pin that the
# request payload is unchanged, because the payload is what the toggle rides on.


def _payload(model):
    return model._get_payload(inputs=[{"role": "user", "content": "hi"}], stop=None)


@pytest.mark.parametrize(
    ("effort", "thinking"), [("high", True), ("medium", True), ("off", False)]
)
def test_effort_drives_the_thinking_flag_in_the_request(isolated, effort, thinking):
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.config.nova_config import NovaConfig

    NovaConfig().set("reasoning_effort", effort)
    model = build_chat_model("nvidia", "deepseek-ai/deepseek-v4-pro-0813")
    assert _payload(model)["chat_template_kwargs"] == {"thinking": thinking}


def test_no_effort_sends_no_thinking_flag(isolated):
    """Unset means "don't express an opinion", not "thinking off"."""
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.config.nova_config import NovaConfig

    NovaConfig().set("reasoning_effort", None)
    model = build_chat_model("nvidia", "deepseek-ai/deepseek-v4-pro-0813")
    assert "chat_template_kwargs" not in _payload(model)


def test_building_the_model_does_not_warn_about_chat_template_kwargs(isolated):
    """The warning fired on every /model switch and every session start."""
    import warnings

    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.config.nova_config import NovaConfig

    NovaConfig().set("reasoning_effort", "high")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_chat_model("nvidia", "deepseek-ai/deepseek-v4-pro-0813")
    noisy = [w for w in caught if "chat_template_kwargs" in str(w.message)]
    assert not noisy, f"still warning: {[str(w.message)[:80] for w in noisy]}"


def test_the_token_limit_still_reaches_the_request(isolated):
    """max_completion_tokens is silently DROPPED by this client, so max_tokens
    stays despite its deprecation warning — swapping it would lose the limit."""
    from novacode_cli.config.model_create import build_chat_model

    assert _payload(build_chat_model("nvidia", "deepseek-ai/deepseek-v4-pro-0813"))[
        "max_tokens"
    ] == 16384
