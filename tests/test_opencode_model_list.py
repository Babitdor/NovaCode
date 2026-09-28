"""OpenCode Go's live model list feeds the TUI /model picker.

The preset list in MODEL_PRESETS["opencode"] is hand-maintained and drifts from
what the gateway actually serves — six of its eighteen ids are already
``deprecated`` and 400 ("Model is unavailable") on first use. So the picker
fetches the gateway's own ``/models`` endpoint instead, exactly as the Ollama
option lists what is installed locally.

These tests pin the fetch, the fallback, and the screen wiring. The network is
never touched: httpx.get is stubbed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from novacode_cli.config import model_manager
from novacode_cli.config.model_manager import MODEL_PRESETS

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


class _StubResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _FakeSettings:
    """Stand-in for ``Settings`` carrying a chosen OpenCode key."""

    def __init__(self, key: str | None) -> None:
        self.opencode_api_key = key


def _fake_get(payload: Any, status_code: int = 200, captured: dict | None = None) -> Any:
    """Build a fake ``httpx.get`` serving *payload* and recording its args."""

    def get(
        url: str, *, headers: dict | None = None, timeout: float | None = None
    ) -> _StubResponse:
        if captured is not None:
            captured["url"] = url
            captured["headers"] = headers or {}
        return _StubResponse(payload, status_code=status_code)

    return get


def _settings_returning(key: str | None) -> Any:
    """A ``Settings.from_environment`` classmethod yielding a fixed key."""

    def from_env(_cls: type, **_kwargs: object) -> _FakeSettings:
        return _FakeSettings(key)

    return classmethod(from_env)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from a real OPENCODE_API_KEY in the environment."""
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# get_opencode_models — fetch, parse, fallback
# ---------------------------------------------------------------------------


def test_returns_ids_from_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    payload = {"object": "list", "data": [{"id": "glm-5.3"}, {"id": "kimi-k3"}]}
    monkeypatch.setattr("httpx.get", _fake_get(payload, captured=captured))

    models = model_manager.get_opencode_models()

    assert models == ["glm-5.3", "kimi-k3"]
    assert captured["url"] == f"{model_manager.OPENCODE_BASE_URL}/models"
    # The gateway 403s a default UA (same gotcha as context/_models_dev.py).
    assert "Mozilla" in captured["headers"]["User-Agent"]


def test_ids_are_sorted_for_a_stable_display(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"data": [{"id": "zeta"}, {"id": "alpha"}, {"id": "mid"}]}
    monkeypatch.setattr("httpx.get", _fake_get(payload))

    assert model_manager.get_opencode_models() == ["alpha", "mid", "zeta"]


def test_resolved_key_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr("httpx.get", _fake_get({"data": [{"id": "glm-5.3"}]}, captured=captured))
    monkeypatch.setattr(model_manager.Settings, "from_environment", _settings_returning("sk-oc"))

    model_manager.get_opencode_models()

    assert captured["headers"]["Authorization"] == "Bearer sk-oc"


def test_env_key_is_used_when_settings_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr("httpx.get", _fake_get({"data": [{"id": "glm-5.3"}]}, captured=captured))
    monkeypatch.setattr(model_manager.Settings, "from_environment", _settings_returning(None))
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-env")

    model_manager.get_opencode_models()

    assert captured["headers"]["Authorization"] == "Bearer sk-env"


def test_falls_back_to_preset_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_url: str, **_kwargs: object) -> _StubResponse:
        msg = "offline"
        raise RuntimeError(msg)

    monkeypatch.setattr("httpx.get", boom)
    assert model_manager.get_opencode_models() == MODEL_PRESETS["opencode"]["models"]


def test_falls_back_to_preset_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("httpx.get", _fake_get({"data": []}, status_code=503))
    assert model_manager.get_opencode_models() == MODEL_PRESETS["opencode"]["models"]


def test_falls_back_when_payload_has_no_usable_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty/malformed body must not empty the picker."""
    monkeypatch.setattr("httpx.get", _fake_get({"data": []}))
    assert model_manager.get_opencode_models() == MODEL_PRESETS["opencode"]["models"]

    monkeypatch.setattr("httpx.get", _fake_get({"nope": 1}))
    assert model_manager.get_opencode_models() == MODEL_PRESETS["opencode"]["models"]


def test_entries_without_an_id_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"data": [{"id": "a"}, {}, {"id": None}, "junk"]}
    monkeypatch.setattr("httpx.get", _fake_get(payload))

    assert model_manager.get_opencode_models() == ["a"]


# ---------------------------------------------------------------------------
# /model wiring — the gateway's ids reach the picker
# ---------------------------------------------------------------------------


async def _drive_screen(monkeypatch: pytest.MonkeyPatch) -> dict:
    from textual.app import App, ComposeResult
    from textual.widgets import OptionList

    import novacode_cli.config.model_catalog as catalog
    from novacode_cli.config import provider_auth
    from novacode_cli.tui.screens import ModelScreen

    # Stub the two blocking sources: the picker loads off the UI thread, so
    # replacing the screen's loader would bypass Textual's scheduling.
    monkeypatch.setattr(
        model_manager, "get_opencode_models", lambda: ["gateway-only-id"]
    )
    monkeypatch.setattr(model_manager, "get_ollama_models", lambda: ["llama3.3:70b"])
    monkeypatch.setattr(provider_auth, "get_all_auth_statuses", lambda **kwargs: {})
    catalog.clear_catalog_cache()

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

    out: dict = {}
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = ModelScreen()
        app.push_screen(screen)
        ol = None
        for _ in range(120):
            await pilot.pause()
            try:
                ol = screen.query_one("#model-options", OptionList)
            except Exception:  # noqa: BLE001 — not mounted yet
                continue
            if ol.option_count:
                break

        assert ol is not None
        out["options"] = [
            ol.get_option_at_index(i).id for i in range(ol.option_count)
        ]
    return out


def test_the_picker_offers_the_gateways_live_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway is the source of truth, not the hand-maintained preset.

    Six of the preset's eighteen ids are already `deprecated` and 400 on first
    use, so an id only the gateway reports must still be selectable.
    """
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_screen(monkeypatch))

    assert "opencode:gateway-only-id" in out["options"]
    assert "ollama:llama3.3:70b" in out["options"]


def test_the_catalog_is_what_the_picker_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """No second source of model ids grew back inside the screen."""
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_screen(monkeypatch))

    modeled = [option for option in out["options"] if not str(option).startswith("#hdr:")]
    providers = {str(option).split(":", 1)[0] for option in modeled}

    assert providers == set(MODEL_PRESETS)
