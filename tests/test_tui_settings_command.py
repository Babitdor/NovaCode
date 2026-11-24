"""`/settings` over the socket: the AGENT section as two fixed choices.

The Textual screen edits many sections inline. Two of its rows are pure choices
(reasoning effort, context budget), so those travel first; the rest stay
Textual-only rather than being guessed at from the outside.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui import port_server
from novacode_cli.ui.port_server import ServerPort
from novacode_cli.ui.screen_specs import (
    CONTEXT_BUDGETS,
    EFFORT_OPTIONS,
    budget_label,
    budget_tokens,
    settings_spec,
)


def _worker() -> SimpleNamespace:
    """A worker stand-in; `/settings` reads only configuration."""
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


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake NovaConfig: a dict behind the two accessors the port uses."""
    stored: dict[str, Any] = {"reasoning_effort": "medium", "context_budget_tokens": 200_000}

    def get(key: str, default: Any = None) -> Any:  # noqa: ANN401 — mirrors NovaConfig
        return stored.get(key, default)

    def set_value(key: str, value: Any) -> None:  # noqa: ANN401 — mirrors NovaConfig
        stored[key] = value

    fake = SimpleNamespace(get=get, set=set_value)
    monkeypatch.setattr(port_server, "_config", lambda: fake)
    return stored


def _port(answer: object = None) -> tuple[ServerPort, list[Any]]:
    """A port whose screen requests are captured."""
    asked: list[Any] = []
    sent: list[dict[str, Any]] = []

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 — the answer is the caller's
        asked.append(spec)
        return answer

    return ServerPort(sent.append, _worker(), ask_screen=ask_screen), asked


def test_the_spec_offers_both_choices() -> None:
    spec = settings_spec("low", 128_000)
    assert spec.kind == "form"
    fields = {field["key"]: field for field in spec.payload["fields"]}
    assert fields["reasoning_effort"]["options"] == list(EFFORT_OPTIONS)
    assert fields["reasoning_effort"]["value"] == "low"
    assert fields["context_budget_tokens"]["value"] == "128K · default"
    assert len(fields["context_budget_tokens"]["options"]) == len(CONTEXT_BUDGETS)


def test_an_unknown_effort_falls_back_to_provider_default() -> None:
    fields = {field["key"]: field for field in settings_spec("turbo", 0).payload["fields"]}
    assert fields["reasoning_effort"]["value"] == "default"


def test_the_effort_options_span_the_provider_dials() -> None:
    """xhigh and max exist natively on OpenAI/Claude/Grok and clamp elsewhere."""
    assert EFFORT_OPTIONS == ("default", "off", "low", "medium", "high", "xhigh", "max")


async def test_a_default_answer_clears_the_override(config: dict[str, Any]) -> None:
    """'default' must store unset, not the literal string, or ollama would
    wire a literal 'default' effort into every request."""
    port, _ = _port(answer={"reasoning_effort": "default"})
    await port.run("settings", None)
    assert config["reasoning_effort"] is None


def test_a_hand_set_budget_stays_visible() -> None:
    assert budget_label(96_000) == "96,000"
    assert budget_label(200_000) == "200K"


def test_a_budget_label_resolves_back_to_tokens() -> None:
    assert budget_tokens("128K · default") == 128_000
    assert budget_tokens("The model's whole window") == 0
    assert budget_tokens("96,000") == 96_000
    assert budget_tokens(" 200000 ") == 200_000


def test_a_non_budget_answer_is_refused() -> None:
    assert budget_tokens("lots") is None


async def test_an_answer_saves_both_preferences(config: dict[str, Any]) -> None:
    port, asked = _port(answer={"reasoning_effort": "high", "context_budget_tokens": "64K"})
    result = await port.run("/settings", None)
    assert len(asked) == 1
    assert asked[0].kind == "form"
    assert result.ok is True
    assert config["reasoning_effort"] == "high"
    assert config["context_budget_tokens"] == 64_000
    assert "high" in result.blocks[0].text
    assert "64K" in result.blocks[0].text


async def test_the_form_opens_on_the_current_values(config: dict[str, Any]) -> None:
    port, asked = _port(answer=None)
    await port.run("settings", None)
    fields = {field["key"]: field for field in asked[0].payload["fields"]}
    assert fields["reasoning_effort"]["value"] == "medium"
    assert fields["context_budget_tokens"]["value"] == "200K"
    assert config["reasoning_effort"] == "medium", "cancelling changes nothing"


async def test_an_unknown_effort_in_the_answer_keeps_the_current_one(
    config: dict[str, Any],
) -> None:
    port, _ = _port(answer={"reasoning_effort": "turbo"})
    await port.run("settings", None)
    assert config["reasoning_effort"] == "medium"


async def test_an_unreadable_budget_keeps_the_current_one(config: dict[str, Any]) -> None:
    port, _ = _port(answer={"context_budget_tokens": "lots"})
    await port.run("settings", None)
    assert config["context_budget_tokens"] == 200_000
