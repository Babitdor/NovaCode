"""The command seam: ``UIPort`` / ``ServerPort`` and the parity ledger.

A command must never "succeed" by doing nothing. These tests pin three things:

* the ported set (``/help``, ``/status``, ``/model``, ``/clear``, ``/quit``) answers
  with real blocks or an explicit client action;
* every other command answers ``ok=false`` and names itself, using the exact
  sentence the protocol documents;
* ``rust/LEDGER.md`` lists every module that could regress silently — the TUI's
  and the command handlers' — so "not ported yet" is auditable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novacode_cli.ui.port import (
    NOT_PORTED_MESSAGE,
    Block,
    CommandResult,
    ScreenSpec,
    blocks_from_text,
    strip_ansi,
)
from novacode_cli.ui.port_server import ServerPort, canonical

ROOT = Path(__file__).resolve().parents[1]


class _SS:
    """Minimal session-state stand-in."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.todos: list[dict[str, Any]] = []
        self.steering_instructions: list[Any] = []
        self.headless_out_fd = None


def _worker(**over: Any) -> SimpleNamespace:
    """A worker stand-in with the attributes the port reads."""
    base = {
        "agent": object(),
        "session_state": _SS(),
        "assistant_id": "nova-agent",
        "model_name": "test-model",
        "session_manager": None,
        "image_tracker": None,
        "backend": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _port(worker: SimpleNamespace | None = None) -> tuple[ServerPort, list[dict[str, Any]]]:
    """A port whose frames are captured into a list."""
    sent: list[dict[str, Any]] = []
    return ServerPort(sent.append, worker or _worker()), sent


async def test_status_reports_the_live_session() -> None:
    port, _ = _port()
    result = await port.run("status", None)
    assert result.ok is True
    text = result.blocks[0].text
    assert "Model     test-model" in text
    assert "Session   s1" in text
    assert "Approval  manual" in text


async def test_status_uses_the_token_tracker_when_present() -> None:
    state = _SS()
    state.token_tracker = SimpleNamespace(last_total_tokens=1234, context_window_size=200000)
    port, _ = _port(_worker(session_state=state))
    result = await port.run("/status", None)
    assert "1,234 / 200,000" in result.blocks[0].text


async def test_clear_and_quit_are_client_actions() -> None:
    port, _ = _port()
    clear = await port.run("clear", None)
    assert clear.ok is True
    assert clear.action == "clear"
    quit_result = await port.run("quit", None)
    assert quit_result.action == "quit"
    # `/exit` and `/q` are the same action, and the slash is optional.
    assert (await port.run("/exit", None)).action == "quit"
    assert (await port.run("q", None)).action == "quit"


async def test_help_runs_the_existing_handler_and_captures_its_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(*_args: object, **_kwargs: object) -> bool:
        from novacode_cli.config.config import console

        console.print("Available commands: /help /status /model")
        return True

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    port, _ = _port()
    result = await port.run("help", None)
    assert result.ok is True
    assert "Available commands" in "\n".join(block.text for block in result.blocks)


async def test_a_handler_that_answers_with_a_prompt_sets_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `/init`, `/goal` and `/skill:x` answer by returning a prompt instead of
    # printing; the Textual UI streams it into a turn, so the port reports it as
    # a prompt rather than as a one-line summary.
    async def handler(*args: object, **_kwargs: object) -> str:
        assert args[0] == "/model provider:model-x"
        return "Switched to provider:model-x"

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    port, _ = _port()
    result = await port.run("model", "provider:model-x")
    assert result.ok is True
    assert result.prompt == "Switched to provider:model-x"
    assert result.message == ""


async def test_a_print_only_handler_still_reaches_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Handlers are supposed to print through the Rich console; one that calls
    # `print` must not paint over the client's terminal instead.
    async def handler(*_args: object, **_kwargs: object) -> bool:
        print("plain print output")  # noqa: T201 - the behaviour under test

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    port, _ = _port()
    result = await port.run("help", None)
    text = "\n".join(block.text for block in result.blocks)
    assert "plain print output" in text


async def test_sandbox_identity_reaches_the_shared_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def handler(_text: str, *_args: object, **kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    sent: list[dict[str, Any]] = []
    port = ServerPort(sent.append, _worker(), sandbox_id="sb1", sandbox_type="docker")
    await port.run("help", None)
    assert seen["sandbox_id"] == "sb1"
    assert seen["sandbox_type"] == "docker"


async def test_a_command_the_shared_layer_declines_is_not_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `handle_command` answers `False` when nothing matched (plugins missing, a
    # feature flag off). Reporting that as success would be a silent no-op.
    async def handler(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    port, _ = _port()
    result = await port.run("help", None)
    assert result.ok is False
    assert result.message == NOT_PORTED_MESSAGE.format(name="/help")


async def test_a_failing_handler_becomes_an_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(*_args: object, **_kwargs: object) -> bool:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    port, _ = _port()
    result = await port.run("help", None)
    assert result.ok is False
    assert "failed" in result.message


async def test_every_catalogued_command_is_served() -> None:
    # The waiting list is empty: nothing may fall through to `not_ported`.
    port, _ = _port()
    for name in ("loop", "remote", "claude-plan", "codex-review"):
        result = await port.run(f"{name} --status" if name == "ralph" else name, None)
        assert result.message != NOT_PORTED_MESSAGE.format(name=f"/{name}"), name


@pytest.mark.parametrize("name", ["learning", "skills"])
async def test_served_passthrough_commands_reach_the_shared_handler(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def handler(text: str, *_args: object, **_kwargs: object) -> bool:
        seen.append(text)
        return True

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    port, _ = _port()
    result = await port.run(name, None)
    assert result.ok is True
    assert seen == [f"/{name}"]


async def test_context_returns_a_table_screen_without_blocking() -> None:
    """`/context` is a report: the spec travels with the result, nothing waits."""
    state = _SS()
    state.token_tracker = SimpleNamespace(
        model_name="test-model",
        has_api_data=False,
        session_total_tokens=1555,
        last_output=321,
        get_breakdown=lambda: SimpleNamespace(
            context_window_size=200_000,
            total_tokens=1_000,
            system_prompt_tokens=400,
            user_memory_tokens=100,
            project_memory_tokens=0,
            tool_definitions_tokens=200,
            user_message_tokens=100,
            assistant_message_tokens=100,
            tool_result_tokens=100,
            mcp_tool_tokens=0,
            mcp_prompt_tokens=0,
            mcp_tool_count=0,
            user_message_count=2,
            assistant_message_count=2,
            tool_call_count=1,
        ),
    )
    port, sent = _port(_worker(session_state=state))
    result = await port.run("/context", None)
    assert result.ok is True
    assert "test-model" in result.blocks[0].text
    assert result.screen is not None
    assert result.screen.kind == "table"
    rows = result.screen.payload["rows"]
    assert [row["category"] for row in rows][:2] == ["System + memory", "Tool definitions"]
    assert rows[0]["share"] == "50.0%"
    # No modal round trip: the port never asked, so nothing can time out.
    assert not [frame for frame in sent if frame.get("t") == "screen"]


async def test_context_without_a_tracker_reports_the_empty_state() -> None:
    port, _ = _port()
    result = await port.run("context", None)
    assert result.ok is True
    assert result.blocks[0].text == "No token usage captured yet."
    assert result.screen is not None
    assert result.screen.payload["rows"] == []


async def test_ask_returns_the_clients_answer() -> None:
    async def asker(spec: ScreenSpec) -> Any:  # noqa: ANN401 - a modal owns its shape
        assert spec.kind == "confirm"
        return True

    port = ServerPort([].append, _worker(), ask_screen=asker)
    assert await port.ask(ScreenSpec("confirm", {"message": "go?"})) is True


async def test_ask_without_a_host_reports_the_missing_screen() -> None:
    port, sent = _port()
    answer = await port.ask(ScreenSpec("confirm", {"message": "go?"}))
    assert answer is None
    assert any(frame["t"] == "notice" for frame in sent)


async def test_an_unknown_command_is_reported_as_unported() -> None:
    port, _ = _port()
    result = await port.run("definitely-not-a-command", None)
    assert result.ok is False
    assert "definitely-not-a-command" in result.message


async def test_client_actions_and_aliases_are_canonical() -> None:
    assert canonical("/model") == "model"
    assert canonical("MODEL gpt") == "model"
    assert canonical("exit") == "quit"
    assert canonical("q") == "quit"


def test_ansi_is_stripped_and_boxed_output_becomes_code() -> None:
    raw = "\x1b[31mred\x1b[0m plain\n┌────┐\n│ ok │\n└────┘\n"
    plain = strip_ansi(raw)
    assert "\x1b" not in plain
    blocks = blocks_from_text(plain)
    assert blocks[0] == Block("text", "red plain")
    assert any(block.kind == "code" for block in blocks)


def test_command_result_serializes_blocks() -> None:
    result = CommandResult(ok=True, message="m", blocks=(Block("text", "hi"),))
    assert result.as_blocks() == [{"kind": "text", "text": "hi"}]


async def test_export_asks_the_client_then_writes_the_chosen_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from novacode_cli.config import config as nova_config
    from novacode_cli.session.session_persistence import SessionManager

    monkeypatch.setattr(nova_config, "HOME_DIR", tmp_path / ".nova")
    sm = SessionManager(tmp_path)
    sm.history_tree("s1").sync(
        [
            {"type": "HumanMessage", "content": "hi", "id": "h1"},
            {"type": "AIMessage", "content": "hello", "id": "a1"},
        ]
    )
    captured: dict[str, ScreenSpec] = {}

    async def asker(spec: ScreenSpec) -> Any:  # noqa: ANN401 — a modal owns its shape
        captured["spec"] = spec
        return {"format": "json", "thinking": True, "tools": True}

    port = ServerPort([].append, _worker(session_manager=sm), ask_screen=asker)
    target = tmp_path / "out.json"
    result = await port.run("export", str(target))

    assert result.ok is True
    assert captured["spec"].kind == "form"
    fields = {field["key"]: field for field in captured["spec"].payload["fields"]}
    assert fields["format"]["options"] == ["markdown", "json"]
    assert fields["thinking"]["toggle"] is True and fields["tools"]["toggle"] is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["format"] == "nova-session-export/1"
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]


async def test_export_without_a_client_writes_the_html_default(tmp_path: Path) -> None:
    from novacode_cli.session.session_persistence import SessionManager

    sm = SessionManager(tmp_path)
    sm.history_tree("s1").sync([{"type": "HumanMessage", "content": "hi", "id": "h1"}])
    port, _ = _port(_worker(session_manager=sm))

    result = await port.run("export", str(tmp_path / "out"))

    assert result.ok is True
    text = (tmp_path / "out.html").read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")


async def test_export_cancelled_when_the_client_declines(tmp_path: Path) -> None:
    from novacode_cli.session.session_persistence import SessionManager

    sm = SessionManager(tmp_path)
    sm.history_tree("s1").sync([{"type": "HumanMessage", "content": "hi", "id": "h1"}])

    async def asker(spec: ScreenSpec) -> Any:  # noqa: ANN401 — a modal owns its shape
        return None

    port = ServerPort([].append, _worker(session_manager=sm), ask_screen=asker)
    result = await port.run("export", str(tmp_path / "out.json"))

    assert result.ok is True
    assert result.message == "Export cancelled."
    assert not list(tmp_path.glob("out*"))


def test_ledger_lists_every_command_handler_module() -> None:
    """Every module under ``commands/`` must be auditable in the parity ledger."""
    ledger = (ROOT / "rust" / "LEDGER.md").read_text(encoding="utf-8")
    modules = sorted(path.name for path in (ROOT / "novacode_cli" / "commands").glob("*.py"))
    missing = [name for name in modules if name != "__init__.py" and name not in ledger]
    assert missing == []


@pytest.mark.timeout(180)
def test_recorded_fixture_regenerates_without_a_diff(tmp_path: Path) -> None:
    """The parity corpus must be script-generated, never hand-edited."""
    out = tmp_path / "core_chat.jsonl"
    completed = subprocess.run(  # noqa: S603 — fixed script, current interpreter
        [sys.executable, str(ROOT / "scripts" / "record_tui_session.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    committed = (ROOT / "rust" / "nova-tui" / "tests" / "fixtures" / "core_chat.jsonl").read_bytes()
    assert out.read_bytes() == committed, "re-run scripts/record_tui_session.py"


def test_ledger_lists_every_tui_module() -> None:
    """Every Textual module must appear in the ledger before it can be deleted."""
    ledger = (ROOT / "rust" / "LEDGER.md").read_text(encoding="utf-8")
    modules = sorted(path.name for path in (ROOT / "novacode_cli" / "tui").glob("*.py"))
    missing = [name for name in modules if name not in ledger]
    assert missing == []
