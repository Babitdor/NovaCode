"""`/claude-plan`, `/codex-plan`, `/claude-review`, `/codex-review` in the port.

The external CLI explores the repository in its own process; Nova receives only
its text, then acts on it. These tests pin that handoff: the plan or report is
shown as blocks and the prompt carrying it back starts a normal turn, so the
conversation keeps one model holding the thread.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import novacode_cli.commands as planner_package
from novacode_cli.server.catalog import CATALOG
from novacode_cli.ui.port_server import ServerPort

if TYPE_CHECKING:
    import pytest


class _Worker:
    """The bits of a session worker the planner commands touch."""

    def __init__(self) -> None:
        self.agent = object()
        self.assistant_id = "nova-agent"
        self.model_name = "test-model"
        self.session_manager = None
        self.image_tracker = None
        self.backend = None
        self.session_state = SimpleNamespace(thread_id="t1", session_id="s1")


def _port(*, sent: list[dict[str, Any]] | None = None) -> ServerPort:
    """A port that records the frames it emits.

    Args:
        sent: Where emitted frames go; a fresh list when omitted.

    Returns:
        The port under test.
    """
    sink: list[dict[str, Any]] = [] if sent is None else sent
    return ServerPort(sink.append, _Worker())


def _planner(**overrides: Any) -> SimpleNamespace:
    """Stand-in for `claude_plan`/`codex_plan`."""
    attrs: dict[str, Any] = {
        "NAME": "Claude Code",
        "COMMAND": "/claude-plan",
        "DOCS_URL": "https://example.invalid",
        "REVIEW_ARGS": {"instructions": "review it", "tool_args": ["Read"]},
        "executable": lambda: "claude",
        "run_turn": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _install(monkeypatch: pytest.MonkeyPatch, name: str, planner: SimpleNamespace) -> None:
    """Point a command name at a stand-in module.

    `raising=False` because the real module is imported on first use, so the
    package attribute may not exist yet on a cold run.
    """
    monkeypatch.setattr(planner_package, name, planner, raising=False)


def _turn(**overrides: Any) -> SimpleNamespace:
    """A finished planning turn."""
    attrs: dict[str, Any] = {"text": "", "plan": None, "error": None, "session_id": None}
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


async def test_a_plan_is_shown_and_handed_to_nova(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def run_turn(task: str, _cwd: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        seen["task"] = task
        seen["kwargs"] = kwargs
        return _turn(plan="1. add a parser\n2. add tests")

    _install(monkeypatch, "claude_plan", _planner(run_turn=run_turn))

    sent: list[dict[str, Any]] = []
    result = await _port(sent=sent)._claude_plan_command("teach it to parse x.py")

    assert result.ok is True
    assert result.message == "◆ Claude Code produced a plan."
    assert [block.kind for block in result.blocks] == ["heading", "code"]
    assert "add a parser" in result.blocks[1].text
    assert result.prompt is not None
    assert "add a parser" in result.prompt
    assert "Implement it" in result.prompt
    assert seen["task"] == "teach it to parse x.py"
    assert seen["kwargs"] == {}
    assert sent[0]["blocks"][0]["text"] == "◆ Claude Code is planning…"


async def test_a_missing_cli_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "codex_plan", _planner(NAME="Codex", executable=lambda: None))

    result = await _port()._codex_plan_command("anything")

    assert result.ok is False
    assert "Codex is not installed" in result.message


async def test_a_failed_turn_reports_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_turn(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
        return _turn(error="not logged in")

    _install(monkeypatch, "claude_plan", _planner(run_turn=run_turn))

    result = await _port()._claude_plan_command("plan it")

    assert result.ok is False
    assert result.message == "Claude Code stopped: not logged in"


async def test_an_empty_turn_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_turn(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
        return _turn()

    _install(monkeypatch, "claude_plan", _planner(run_turn=run_turn))

    result = await _port()._claude_plan_command("plan it")

    assert result.ok is False
    assert "returned nothing" in result.message


async def test_a_review_passes_the_read_only_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def run_turn(task: str, _cwd: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        seen["task"] = task
        seen["kwargs"] = kwargs
        return _turn(text="finding: the loop leaks")

    _install(monkeypatch, "codex_plan", _planner(NAME="Codex", run_turn=run_turn))

    result = await _port()._codex_review_command("the parser")

    assert result.ok is True
    assert result.message == "◆ Codex reviewed the changes."
    assert "leaks" in result.blocks[1].text
    assert result.prompt is not None
    assert "Check each finding" in result.prompt
    assert seen["kwargs"] == {"instructions": "review it", "tool_args": ["Read"]}
    assert seen["task"] == "the parser"


async def test_an_empty_review_asks_for_the_whole_change(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def run_turn(task: str, _cwd: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
        seen["task"] = task
        return _turn(text="all good")

    _install(monkeypatch, "claude_plan", _planner(run_turn=run_turn))

    await _port()._claude_review_command("")

    assert seen["task"] == "Review the changes in this repository"


def test_the_catalogue_serves_all_four() -> None:
    for name in ("claude-plan", "codex-plan", "claude-review", "codex-review"):
        assert CATALOG[name].served is True, name
