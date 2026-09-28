"""A custom OpenAI-compatible endpoint can be set from the TUI.

The `openai` provider always went to api.openai.com — there was no way to point
Nova at Azure, LM Studio, vLLM or a LiteLLM proxy from the TUI, and
``create_model`` never passed ``base_url`` for plain OpenAI at all.

The field lives in ``/auth`` beside the key it belongs to, not in ``/model``: a
key and the endpoint it was issued for are one pair, and keeping the endpoint in
the picker meant two writers for one setting, plus a switch that could carry a
stale URL onto a gateway.
"""

from __future__ import annotations

import asyncio

import pytest

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Keep config writes out of the real ~/.nova.

    ``HOME``/``USERPROFILE`` alone are not enough: ``config.HOME_DIR`` is
    computed at import time and ``Settings.user_deepagents_dir`` returns that
    frozen constant, so ``NovaConfig`` ignored them and these tests overwrote the
    developer's actual ``Nova.config.json``. Patch the constant itself.
    """
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from novacode_cli.config.nova_config import NovaConfig

    return NovaConfig


class _NullSecretManager:
    """SecretManager stand-in: stores nothing, touches no real keychain."""

    def __init__(self, *args, **kwargs):
        pass

    def get_secret(self, name):
        return None

    def store_secret(self, name, value):
        return True

    def delete_secret(self, name):
        return True


def test_endpoint_is_saved_and_read_back(isolated_config):
    cfg = isolated_config()
    cfg.set_model_config("openai", "gpt-4o", "http://localhost:1234/v1")
    assert isolated_config().get_model_base_url() == "http://localhost:1234/v1"


def test_blank_endpoint_clears_the_override(isolated_config):
    """Switching back to stock OpenAI must not leave a stale URL behind."""
    cfg = isolated_config()
    cfg.set_model_config("openai", "gpt-4o", "http://localhost:1234/v1")
    cfg.set_model_config("openai", "gpt-4o", None)
    assert isolated_config().get_model_base_url() is None


def test_model_manager_persists_the_endpoint(isolated_config):
    from novacode_cli.config.model_manager import ModelManager

    ModelManager().set_provider("openai", "gpt-4o", "http://127.0.0.1:8000/v1")
    assert isolated_config().get_model_base_url() == "http://127.0.0.1:8000/v1"


def test_create_model_actually_uses_the_endpoint(isolated_config, monkeypatch):
    """The point of the feature: ChatOpenAI must be built against the URL."""
    from novacode_cli.config.model_manager import ModelManager

    ModelManager().set_provider("openai", "gpt-4o", "http://127.0.0.1:8000/v1")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from novacode_cli.config.model_create import create_model

    model = create_model()
    base = getattr(model, "openai_api_base", None) or getattr(model, "base_url", None)
    assert str(base).rstrip("/") == "http://127.0.0.1:8000/v1"


def _meta(base_url: str):
    from novacode_cli.config.credentials import CredentialMeta

    return CredentialMeta(env_var="OPENAI_API_KEY", base_url=base_url)


async def _drive_prompt():
    """Drive the /auth key prompt: visibility, prefill, and what it saves."""
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    from novacode_cli.config.credentials import credential_meta
    from novacode_cli.tui.auth_screens import AuthPromptScreen

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

    out: dict = {}
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        prompt = AuthPromptScreen(
            "openai",
            existing=credential_meta("OPENAI_API_KEY") or _meta("http://saved:1234/v1"),
        )
        app.push_screen(prompt)
        await pilot.pause()

        box = prompt.query_one("#auth-endpoint", Input)
        out["openai_visible_before_f2"] = bool(box.display)
        await pilot.press("f2")
        out["openai_visible"] = bool(box.display)
        out["prefilled"] = box.value

        prompt.query_one("#auth-key", Input).value = "sk-typed"
        box.value = "http://localhost:11434/v1"
        await pilot.press("enter")
        await pilot.pause()
        meta = credential_meta("OPENAI_API_KEY")
        out["submitted"] = meta.base_url if meta else None

        # A gateway pins its own base URL; offering one would only break it.
        other = AuthPromptScreen("openrouter")
        app.push_screen(other)
        await pilot.pause()
        await pilot.press("f2")
        out["openrouter_visible"] = bool(other.query_one("#auth-endpoint", Input).display)
    return out


def test_endpoint_box_is_scoped_to_openai_and_saved_with_the_key(tmp_path, monkeypatch):
    if not _HAS_TEXTUAL:
        return
    import novacode_cli.config.config as config_mod

    monkeypatch.setattr(config_mod, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setattr("novacode_cli.onboarding.SecretManager", _NullSecretManager)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    out = asyncio.run(_drive_prompt())

    # Hidden until F2: the field is an escape hatch, not the common case.
    assert not out["openai_visible_before_f2"], "endpoint box shown without F2"
    assert out["openai_visible"], "endpoint box hidden for OpenAI after F2"
    assert out["prefilled"] == "http://saved:1234/v1", "saved endpoint not prefilled"
    assert out["submitted"] == "http://localhost:11434/v1"
    assert not out["openrouter_visible"], "a gateway must not offer an endpoint"
