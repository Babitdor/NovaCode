"""`/council settings` over the socket: the seat list, per-seat forms, validation.

The Textual editor is one tabbed modal. The socket vocabulary has no tabs, so the
server edits the same draft through `list → form → list`, and only saves after
the production `parse_settings` accepts every seat.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli import council_settings
from novacode_cli.ui import port_server
from novacode_cli.ui.port_server import ServerPort


class _Config:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401 - mirrors NovaConfig
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:  # noqa: ANN401 - mirrors NovaConfig
        self.values[key] = value


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        agent=object(),
        session_state=SimpleNamespace(
            thread_id="t1", session_id="s1", auto_approve=False, todos=[], headless_out_fd=None
        ),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )


def _port(answers: list[object]) -> tuple[ServerPort, list[Any]]:
    asked: list[Any] = []
    queue = list(answers)

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return queue.pop(0) if queue else None

    return ServerPort(lambda _frame: None, _worker(), ask_screen=ask_screen), asked


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> _Config:
    fake = _Config()
    monkeypatch.setattr(port_server, "_config", lambda: fake)
    monkeypatch.setattr(council_settings, "load_settings", council_settings.default_settings)
    return fake


async def test_editing_one_seat_then_saving_persists_it(config: _Config) -> None:
    defaults = council_settings.default_settings()
    port, asked = _port(
        [
            {"id": "architect"},
            {
                "name": "The Architect (mine)",
                "provider": "Session model (inherit)",
                "model": "",
                "template": "Keep current",
                "system": "Think in load-bearing constraints.",
            },
            {"id": "__save__"},
        ]
    )
    result = await port.run("/council", "settings")
    assert result.ok is True
    assert asked[0].kind == "list"
    assert asked[1].kind == "form"
    assert asked[2].kind == "list"
    saved = config.values["council"]
    architect = next(seat for seat in saved["members"] if seat["id"] == "architect")
    assert architect["name"] == "The Architect (mine)"
    assert architect["system"] == "Think in load-bearing constraints."
    assert architect["provider"] == ""
    assert len(saved["members"]) == len(defaults.members)


async def test_a_personality_template_replaces_the_system_prompt(config: _Config) -> None:
    port, _ = _port(
        [
            {"id": "architect"},
            {
                "name": "The Architect",
                "provider": "Session model (inherit)",
                "model": "",
                "template": "The Security Reviewer",
                "system": "ignored because a template was chosen",
            },
            {"id": "__save__"},
        ]
    )
    result = await port.run("/council", "settings")
    assert result.ok is True
    saved = config.values["council"]
    architect = next(seat for seat in saved["members"] if seat["id"] == "architect")
    assert "security reviewer" in architect["system"].lower()
    assert "ignored" not in architect["system"]


async def test_reset_restores_defaults_before_saving(config: _Config) -> None:
    port, _ = _port(
        [
            {"id": "architect"},
            {
                "name": "Changed",
                "provider": "Session model (inherit)",
                "model": "",
                "template": "Keep current",
                "system": "changed",
            },
            {"id": "__reset__"},
            {"id": "__save__"},
        ]
    )
    result = await port.run("/council", "settings")
    assert result.ok is True
    saved = config.values["council"]
    defaults = council_settings.default_settings().to_dict()
    assert saved == defaults


async def test_a_provider_without_a_model_is_refused(config: _Config) -> None:
    port, _ = _port(
        [
            {"id": "architect"},
            {
                "name": "The Architect",
                "provider": "openai · OpenAI",
                "model": "",
                "template": "Keep current",
                "system": "x",
            },
            {"id": "__save__"},
        ]
    )
    result = await port.run("/council", "settings")
    assert result.ok is False
    assert "provider and model" in result.message.lower()
    assert "council" not in config.values


async def test_cancelling_every_screen_saves_nothing(config: _Config) -> None:
    port, _ = _port([None])
    result = await port.run("/council", "settings")
    assert result.ok is True
    assert "unchanged" in result.message.lower()
    assert "council" not in config.values


async def test_bare_and_revision_refuse_explicitly() -> None:
    port, asked = _port([])
    for args in ("", "revise make it smaller"):
        result = await port.run("/council", args)
        assert result.ok is False, args
        assert "not ported" in result.message.lower()
    assert asked == []
