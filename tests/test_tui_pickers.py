"""Tests for standalone pre-TUI Textual pickers (e.g. --resume)."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False


def _sessions():
    now = datetime.now(UTC).isoformat()
    return [
        SimpleNamespace(
            session_id="abcd1234ef",
            project_root="b:/proj/nova",
            model_name="deepseek-v4",
            message_count=63,
            last_active=now,
            current_task="wire steering",
            task_status="active",
        ),
        SimpleNamespace(
            session_id="99887766aa",
            project_root=None,
            model_name="gpt",
            message_count=4,
            last_active=now,
            current_task=None,
            task_status="complete",
        ),
    ]


async def _drive_pick(select_index, key):
    from novacode_cli.tui.pickers import SessionPickerApp

    sessions = _sessions()
    app = SessionPickerApp(sessions)
    async with app.run_test() as pilot:
        ol = app.query_one("#sessions")
        assert ol.option_count == len(sessions), ol.option_count
        ol.focus()
        ol.highlighted = select_index
        await pilot.press(key)
    return app.return_value


def test_picker_returns_selected_session():
    if not _HAS_TEXTUAL:
        return
    res = asyncio.run(_drive_pick(1, "enter"))
    assert res == "99887766aa", res


def test_picker_cancel_returns_none():
    if not _HAS_TEXTUAL:
        return
    res = asyncio.run(_drive_pick(0, "escape"))
    assert res is None, res


async def _drive_onboarding(check, *, provider="anthropic", value="sk-test-123", then_save_anyway=False):
    """Run first-time setup with the provider check replaced by *check*."""
    from textual.widgets import Button, Input, Select

    from novacode_cli import onboarding_check
    from novacode_cli.tui.pickers import OnboardingApp

    captured: dict = {}
    seen: dict = {}
    real = onboarding_check.check_provider
    onboarding_check.check_provider = check  # no network in a test
    app = OnboardingApp()
    # Don't touch the real keyring/config — capture what would be persisted.
    app._persist = staticmethod(
        lambda provider, key, opt, model="": captured.update(
            provider=provider, key=key, opt=opt, model=model
        )
    )
    try:
        async with app.run_test() as pilot:
            prov = app.query_one("#provider", Select)
            key = app.query_one("#provider-key", Input)
            anyway = app.query_one("#save-anyway", Button)
            assert str(prov.value) == "ollama"
            assert key.password is False  # host field, not secret
            assert anyway.display is False, "offered only after a check that did not pass"

            prov.value = provider
            await pilot.pause()
            if provider != "ollama":
                assert key.password is True
                app.query_one("#finish").press()
                await pilot.pause()
                assert "required" in str(app.query_one("#status").render()).lower()

            key.value = value
            app.query_one("#finish").press()
            for _ in range(40):
                await pilot.pause(0.05)
                if app.return_value is not None or anyway.display:
                    break
            seen["status"] = str(app.query_one("#status").render())
            seen["offered"] = bool(anyway.display)
            if then_save_anyway and anyway.display:
                anyway.press()
                for _ in range(40):
                    await pilot.pause(0.05)
                    if app.return_value is not None:
                        break
    finally:
        onboarding_check.check_provider = real
    return app.return_value, captured, seen


def test_onboarding_validates_and_persists():
    if not _HAS_TEXTUAL:
        return
    from novacode_cli.onboarding_check import Check

    result, captured, _ = asyncio.run(_drive_onboarding(lambda p, v: Check(True, "API key accepted.")))
    assert result is True, result
    assert captured.get("provider") == "anthropic"
    assert captured.get("key") == "sk-test-123"
    assert captured.get("model"), "setup must name the model it starts on"


def test_onboarding_does_not_save_a_rejected_key():
    """A wrong key used to end in "setup complete" and fail on the first prompt."""
    if not _HAS_TEXTUAL:
        return
    from novacode_cli.onboarding_check import Check

    rejected = lambda p, v: Check(False, "anthropic rejected that API key (HTTP 401).")  # noqa: E731
    result, captured, seen = asyncio.run(_drive_onboarding(rejected))
    assert result is None and captured == {}, "nothing may be saved, and the screen stays open"
    assert "rejected" in seen["status"] and seen["offered"]


def test_onboarding_can_save_anyway_when_the_check_cannot_pass():
    """Offline, or setting up before the server is started: the user decides."""
    if not _HAS_TEXTUAL:
        return
    from novacode_cli.onboarding_check import Check

    offline = lambda p, v: Check(None, "Could not reach the provider to check the key.")  # noqa: E731
    result, captured, seen = asyncio.run(_drive_onboarding(offline, then_save_anyway=True))
    assert seen["offered"] and result is True
    assert captured.get("key") == "sk-test-123"


def test_onboarding_starts_ollama_on_a_model_that_is_installed():
    if not _HAS_TEXTUAL:
        return
    from novacode_cli.onboarding_check import Check

    found = lambda p, v: Check(True, "Ollama is running with 2 model(s).", ["llama3.3:70b", "qwen3:14b"])  # noqa: E731
    result, captured, _ = asyncio.run(_drive_onboarding(found, provider="ollama", value="http://localhost:11434"))
    assert result is True
    assert captured["model"] == "llama3.3:70b", "not a default the machine does not have"


def test_the_provider_checks_read_the_answer_correctly(monkeypatch):
    """One GET each; 401 is a wrong key, a dead socket is a dead server."""
    import requests

    from novacode_cli import onboarding_check as oc

    class _Resp:
        def __init__(self, status, payload=None):
            self.status_code, self._payload = status, payload or {}

        def json(self):
            return self._payload

    calls: list = []

    def get(url, headers=None, timeout=None):  # noqa: ANN001, ANN202
        calls.append((url, headers or {}))
        return answer(url)

    monkeypatch.setattr(oc.requests, "get", get)

    answer = lambda url: _Resp(401)  # noqa: E731
    assert oc.check_key("openai", "sk-bad").ok is False
    answer = lambda url: _Resp(200)  # noqa: E731
    assert oc.check_key("anthropic", "sk-good").ok is True
    assert calls[-1][1].get("x-api-key") == "sk-good"
    oc.check_key("google", "g-key")
    assert "g-key" not in calls[-1][0], "a key must not travel in the URL"
    assert oc.check_key("nvidia", "nvapi-x").ok is True and len(calls) == 3, "no request: its list is public"

    answer = lambda url: _Resp(200, {"models": [{"name": "llama3.3:70b"}]})  # noqa: E731
    found = oc.check_ollama("http://localhost:11434/")
    assert found.ok is True and found.models == ["llama3.3:70b"]
    answer = lambda url: _Resp(200, {"models": []})  # noqa: E731
    assert oc.check_ollama("").ok is False, "running with no models cannot answer a prompt"

    def dead(url, headers=None, timeout=None):  # noqa: ANN001, ANN202
        raise requests.ConnectionError

    monkeypatch.setattr(oc.requests, "get", dead)
    assert oc.check_ollama("http://127.0.0.1:9").ok is False
    assert oc.check_key("openai", "sk-x").ok is None, "offline is not the same as a wrong key"
