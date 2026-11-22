"""The full settings form, `/voice`, and `/oracle-mode` over the socket.

The Textual settings screen edits toggles and fixed choices in several
sections. This module pins the two things a screenshot cannot: the form carries
every config slot with its current value, and an answer is written to the same
slots the Textual screen writes - never to a key the answer guessed at.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui import port_server, screen_specs
from novacode_cli.ui.port_server import ServerPort


class _Config:
    """A NovaConfig stand-in with the accessors the form uses."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {
            "reasoning_effort": "medium",
            "context_budget_tokens": 200_000,
            "submit_on_enter": True,
            "autocomplete_enabled": True,
            "animation_fps": 30,
            "low_resource_mode": False,
        }
        self.voice: dict[str, Any] = {
            "enabled": False,
            "mode": "push_to_talk",
            "speak_responses": False,
            "stt_provider": "faster-whisper",
            "tts_provider": "piper",
        }
        self.teams = {"enabled": False, "swarm": False}
        self.decision_pruning = False
        self.learning = True
        self.learning_auto = False
        self.auto_approve_decisions = False

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401 - mirrors NovaConfig
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:  # noqa: ANN401 - mirrors NovaConfig
        self.values[key] = value

    def get_voice_config(self) -> dict[str, Any]:
        return dict(self.voice)

    def set_voice_config(self, **updates: Any) -> None:
        self.voice.update(updates)

    def get_agent_team_settings(self) -> dict[str, bool]:
        return dict(self.teams)

    def set_agent_team_settings(self, *, enabled: bool, swarm: bool) -> None:
        self.teams = {"enabled": enabled, "swarm": enabled and swarm}

    def get_tool_verdicts_enabled(self) -> bool:
        return self.decision_pruning

    def set_tool_verdicts_enabled(self, enabled: bool) -> None:  # noqa: FBT001 - mirrors NovaConfig
        self.decision_pruning = enabled

    def get_learning_enabled(self) -> bool:
        return self.learning

    def set_learning_enabled(self, enabled: bool) -> None:  # noqa: FBT001 - mirrors NovaConfig
        self.learning = enabled

    def get_learning_auto_approve(self) -> bool:
        return self.learning_auto

    def set_learning_auto_approve(self, enabled: bool) -> None:  # noqa: FBT001 - mirrors NovaConfig
        self.learning_auto = enabled

    def get_auto_approve_decisions_enabled(self) -> bool:
        return self.auto_approve_decisions

    def set_auto_approve_decisions_enabled(self, enabled: bool) -> None:  # noqa: FBT001 - mirrors NovaConfig
        self.auto_approve_decisions = enabled


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        agent=object(),
        session_state=SimpleNamespace(
            thread_id="t1",
            session_id="s1",
            auto_approve=False,
            todos=[],
            headless_out_fd=None,
        ),
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
    )


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> _Config:
    fake = _Config()
    monkeypatch.setattr(port_server, "_config", lambda: fake)
    return fake


def _port(answer: object = None) -> tuple[ServerPort, list[Any]]:
    asked: list[Any] = []
    sent: list[dict[str, Any]] = []

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return answer

    return ServerPort(sent.append, _worker(), ask_screen=ask_screen), asked


def test_the_full_form_carries_every_section() -> None:
    fields = {
        field["key"]: field
        for field in screen_specs.preferences_spec(screen_specs.Preferences()).payload["fields"]
    }
    # AGENT, TOOL OUTPUT, LEARNING, VOICE, INPUT, PERFORMANCE, AUTOMATION, EXPERIMENTAL.
    for key in (
        "reasoning_effort",
        "context_budget_tokens",
        "decision_pruning",
        "learning_enabled",
        "learning_auto_approve",
        "voice_enabled",
        "voice_mode",
        "voice_speak_responses",
        "voice_stt_provider",
        "voice_tts_provider",
        "submit_on_enter",
        "autocomplete_enabled",
        "animation_fps",
        "low_resource_mode",
        "auto_approve_decisions_enabled",
        "agent_teams_enabled",
        "agent_teams_swarm",
    ):
        assert key in fields, key
    assert fields["agent_teams_swarm"]["toggle"] is True


async def test_saving_the_full_form_writes_every_slot(config: _Config) -> None:
    answer = {
        "reasoning_effort": "high",
        "context_budget_tokens": "64K",
        "decision_pruning": True,
        "learning_enabled": False,
        "learning_auto_approve": True,
        "voice_enabled": True,
        "voice_mode": "Always listening · Ctrl+L",
        "voice_speak_responses": True,
        "voice_stt_provider": "deepgram · Deepgram",
        "voice_tts_provider": "kokoro · Kokoro 82M (INT8 CPU)",
        "submit_on_enter": False,
        "autocomplete_enabled": False,
        "animation_fps": "60 FPS · smooth",
        "low_resource_mode": True,
        "auto_approve_decisions_enabled": True,
        "agent_teams_enabled": True,
        "agent_teams_swarm": True,
    }
    port, asked = _port(answer=answer)
    result = await port.run("/settings", None)
    assert asked[0].kind == "form"
    assert result.ok is True
    assert config.values["reasoning_effort"] == "high"
    assert config.values["context_budget_tokens"] == 64_000
    assert config.decision_pruning is True
    assert config.learning is False
    assert config.learning_auto is True
    assert config.voice["enabled"] is True
    assert config.voice["mode"] == "listen"
    assert config.voice["speak_responses"] is True
    assert config.voice["stt_provider"] == "deepgram"
    assert config.voice["tts_provider"] == "kokoro"
    assert config.values["submit_on_enter"] is False
    assert config.values["autocomplete_enabled"] is False
    assert config.values["animation_fps"] == 60
    assert config.values["low_resource_mode"] is True
    assert config.auto_approve_decisions is True
    assert config.teams == {"enabled": True, "swarm": True}


async def test_swarm_cannot_be_on_without_teams(config: _Config) -> None:
    port, _ = _port(answer={"agent_teams_enabled": False, "agent_teams_swarm": True})
    await port.run("/settings", None)
    assert config.teams == {"enabled": False, "swarm": False}


async def test_a_missing_answer_keeps_every_value(config: _Config) -> None:
    before = dict(config.values)
    port, _ = _port(answer=None)
    result = await port.run("/settings", None)
    assert config.values == before
    assert "unchanged" in result.message.lower()


def test_voice_spec_only_carries_the_voice_section() -> None:
    keys = [
        field["key"]
        for field in screen_specs.voice_spec(screen_specs.Preferences()).payload["fields"]
    ]
    assert keys == [
        "voice_enabled",
        "voice_mode",
        "voice_speak_responses",
        "voice_stt_provider",
        "voice_tts_provider",
    ]


async def test_voice_settings_are_saved(config: _Config) -> None:
    port, asked = _port(answer={"voice_enabled": True, "voice_mode": "Push to talk · Ctrl+G"})
    result = await port.run("/voice", None)
    assert asked[0].title == "Voice"
    assert config.voice["enabled"] is True
    assert config.voice["mode"] == "push_to_talk"
    assert "on" in result.blocks[0].text


async def test_a_voice_subcommand_keeps_the_shared_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[str, str | None]] = []

    async def passthrough(_self: ServerPort, name: str, args: str | None) -> Any:  # noqa: ANN401 - test double
        called.append((name, args))
        return port_server.CommandResult(ok=True, message="status")

    monkeypatch.setattr(ServerPort, "_passthrough", passthrough)
    port, asked = _port()
    await port.run("/voice", "status")
    assert called == [("voice", "status")]
    assert asked == []


async def test_oracle_mode_toggles_and_reports(config: _Config) -> None:
    port, _ = _port()
    worker_state = port._worker.session_state
    toggled = await port.run("/oracle-mode", None)
    assert worker_state.oracle_enabled is True
    assert "on" in toggled.blocks[0].text
    off = await port.run("/oracle-mode", "off")
    assert worker_state.oracle_enabled is False
    assert "off" in off.blocks[0].text
    assert config.get("reasoning_effort") == "medium", "toggling Oracle leaves settings alone"


async def test_oracle_settings_are_validated_before_saving(config: _Config) -> None:
    port, asked = _port(
        answer={
            "target": "9.5",
            "max_rounds": "4",
            "reviewers": "2",
            "scope": "Workspace changes",
            "default_enabled": True,
        }
    )
    result = await port.run("/oracle-mode", "settings")
    assert asked[0].title == "Oracle settings"
    assert result.ok is True
    saved = config.values["oracle_settings"]
    assert saved["target"] == 9.5
    assert saved["max_rounds"] == 4
    assert saved["reviewers"] == 2
    assert saved["scope"] == "workspace"
    assert saved["default_enabled"] is True


async def test_out_of_range_oracle_settings_are_refused(config: _Config) -> None:
    port, _ = _port(answer={"target": "11", "max_rounds": "4", "reviewers": "2"})
    result = await port.run("/oracle-mode", "settings")
    assert result.ok is False
    assert "oracle_settings" not in config.values


async def test_oracle_status_renders_the_live_run(config: _Config) -> None:
    port, _ = _port()
    state = port._worker.session_state
    state.oracle_settings = SimpleNamespace(target=8.0)
    state.oracle_enabled = True
    state.oracle_run = SimpleNamespace(
        status="working",
        round=1,
        settings=SimpleNamespace(target=8.0),
        error="",
        reviewers=[
            SimpleNamespace(
                slot=1,
                assignment="Correctness",
                review=SimpleNamespace(score=7.0, summary="Mostly correct."),
                error="",
                pages_read=2,
                page_count=3,
            )
        ],
    )
    result = await port.run("/oracle-mode", "status")
    assert result.ok is True
    assert result.screen is not None
    assert result.screen.kind == "detail"
    assert "Reviewer 1" in result.screen.payload["markdown"]
    assert "7/10" in result.screen.payload["markdown"]
    assert "Mostly correct." in result.screen.payload["markdown"]
