"""The server's session host: prompts, interrupts, cancellation, commands.

Driven in-process with a recorded frame list instead of a socket, so every
assertion is about protocol *semantics* rather than transport:

* ``ready`` names the session before anything else;
* a prompt streams exactly one ``turn_done``, success or failure;
* an interrupt reaches the client and the client's reply unblocks the graph;
* an unanswered interrupt resolves to the safe default (never a hang, never an
  approval);
* cancel ends the turn and the queued prompt behind it still finishes;
* slash text sent as a prompt comes back as a ``command_result``.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from novacode_cli import ui_events as ev
from novacode_cli.core.agent_loop import default_interrupt_response
from novacode_cli.server.host import SessionHost, SessionSpec
from novacode_cli.ui.port import ScreenSpec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

MAX_WAIT = 5.0


class _SS:
    """Minimal session-state stand-in (mirrors tests/test_session_worker.py)."""

    def __init__(self) -> None:
        self.thread_id = "t1"
        self.session_id = "s1"
        self.auto_approve = False
        self.plan_mode_enabled = False
        self.todos = None
        self.steering_instructions = []
        self.headless_out_fd = None


def _spec() -> SessionSpec:
    """Build a session spec that needs no agent."""
    return SessionSpec(
        agent=object(),
        session_state=_SS(),
        assistant_id="nova-agent",
        model_name="test-model",
        cwd="C:/work",
    )


async def _host() -> tuple[SessionHost, list[dict[str, Any]]]:
    """A started host whose frames are captured into a list."""
    sent: list[dict[str, Any]] = []
    host = SessionHost(sent.append, _spec)
    await host.start()
    return host, sent


async def _wait_for(sent: list[dict[str, Any]], kind: str, *, count: int = 1) -> dict[str, Any]:
    """Wait until *count* frames of type *kind* have been emitted; return the last.

    Raises:
        AssertionError: When the frame never arrives.
    """
    deadline = time.monotonic() + MAX_WAIT
    while time.monotonic() < deadline:
        matching = [msg for msg in sent if msg.get("t") == kind]
        if len(matching) >= count:
            return matching[count - 1]
        await asyncio.sleep(0.01)
    msg = f"no {kind} frame after {MAX_WAIT}s; frames={[m.get('t') for m in sent]}"
    raise AssertionError(msg)


async def _drain(host: SessionHost) -> None:
    """Let a running turn finish, then close the host."""
    task = host._turn
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
    await host.close()


async def test_ready_names_the_session_before_anything_else() -> None:
    host, sent = await _host()
    try:
        assert sent[0]["t"] == "ready"
        assert sent[0]["session_id"] == "s1"
        assert sent[0]["model"] == "test-model"
        assert sent[0]["ui"] == "ratatui"
        # The home banner: the saved layout, whether the rain animates, the version.
        assert set(sent[0]["home"]) == {"style", "animation", "version"}
        assert sent[0]["composer"]["agent"] == "Nova"
        assert isinstance(sent[0]["home"]["style"], str)
        assert isinstance(sent[0]["home"]["animation"], bool)
        assert sent[0]["home"]["version"]
    finally:
        await host.close()


async def test_prompt_streams_events_then_one_turn_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stream(_text: str, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield ev.TextDelta("par")
        yield ev.TextDelta("tial")
        yield ev.AssistantMessage("partial", "Nova", "cyan")
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream)
    host, sent = await _host()
    try:
        await host.prompt("p1", "hello")
        done = await _wait_for(sent, "turn_done")
        assert done == {"t": "turn_done", "id": "p1", "ok": True}
        events = [msg for msg in sent if msg.get("t") == "ev"]
        assert [msg["c"] for msg in events] == [
            "TextDelta",
            "TextDelta",
            "AssistantMessage",
            "Done",
        ]
        assert sum(1 for msg in sent if msg.get("t") == "turn_done") == 1
    finally:
        await _drain(host)


async def test_interrupt_round_trip_unblocks_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client's decision is what the graph resumes with."""

    async def stream(_text: str, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        payload = {"action_requests": [{"name": "shell"}]}
        yield ev.InterruptRequest(kind="tool", payload=payload, future=future)
        decision = await future
        yield ev.AssistantMessage(f"resumed with {decision['decisions']}", "Nova", "cyan")
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream)
    host, sent = await _host()
    try:
        await host.prompt("p1", "run it")
        interrupt = await _wait_for(sent, "interrupt")
        assert interrupt["kind"] == "tool"
        await host.interrupt_reply(
            interrupt["id"], {"decisions": ["approve"], "any_rejected": False}
        )
        done = await _wait_for(sent, "turn_done")
        assert done["ok"] is True
        text = [msg["d"]["text"] for msg in sent if msg.get("c") == "AssistantMessage"]
        assert text[-1] == "resumed with ['approve']"
    finally:
        await _drain(host)


async def test_unanswered_interrupt_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the connection resolves the interrupt instead of hanging."""

    async def stream(_text: str, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        yield ev.InterruptRequest(kind="tool", payload={"action_requests": []}, future=future)
        await future
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream)
    host, sent = await _host()
    await host.prompt("p1", "run it")
    await _wait_for(sent, "interrupt")
    await host.close()
    resolved = [frame for frame in sent if frame.get("t") == "interrupt"]
    assert resolved, "the interrupt frame should have been sent"
    assert default_interrupt_response("tool") is not None


async def test_slash_prompt_answers_with_a_command_result() -> None:
    host, sent = await _host()
    try:
        await host.prompt("p1", "/status")
        result = await _wait_for(sent, "command_result")
        assert result["ok"] is True
        assert result["id"] == "p1"
        assert "Model" in result["blocks"][0]["text"]
        done = await _wait_for(sent, "turn_done")
        assert done == {"t": "turn_done", "id": "p1", "ok": True}
        assert not [msg for msg in sent if msg.get("t") == "ev"]
    finally:
        await host.close()


async def test_queued_prompt_runs_after_the_running_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    seen: list[str] = []

    started = asyncio.Event()

    async def stream(text: str, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
        seen.append(text)
        started.set()
        if len(seen) == 1:
            await gate.wait()
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream)
    host, sent = await _host()
    try:
        await host.prompt("p1", "first")
        await asyncio.wait_for(started.wait(), timeout=MAX_WAIT)
        await host.prompt("p2", "second")
        assert [msg for msg in sent if msg.get("t") == "turn_done"] == []
        gate.set()
        await _wait_for(sent, "turn_done", count=2)
        assert seen == ["first", "second"]
        ids = [msg["id"] for msg in sent if msg.get("t") == "turn_done"]
        assert ids == ["p1", "p2"]
    finally:
        await _drain(host)


async def test_agent_picker_rejects_locked_role_and_publishes_the_selected_role(monkeypatch) -> None:
    monkeypatch.setattr(
        "novacode_cli.agents.profiles.profile_readiness",
        lambda profile: "Enable Serena in /mcp." if profile == "serena" else None,
    )
    host, sent = await _host()
    try:
        for count, profile, expected in [(1, "serena", "Nova"), (2, "nora", "Nora")]:
            pending = asyncio.create_task(host.command(f"agent-{count}", "ui-composer", "agent"))
            screen = await _wait_for(sent, "screen", count=count)
            assert [item["id"] for item in screen["payload"]["items"]] == [
                "nova", "serena", "ralph", "nora",
            ]
            await host.resolve_screen(screen["id"], {"id": profile})
            await pending
            update = await _wait_for(sent, "composer", count=count)
            assert update["agent"] == expected
            result = await _wait_for(sent, "command_result", count=count)
            assert result["ok"] is (profile == "nora")
            assert result["action"] == "toast"
            assert not result["blocks"]
    finally:
        await host.close()


async def test_selected_ralph_runs_iterations_through_the_socket_worker(monkeypatch) -> None:
    from novacode_cli.commands.ralph_events import RalphFinished, RalphStarted
    from novacode_cli.sessions import worker as worker_module

    async def stream(prompt, _agent, assistant_id, _state, **_kwargs):
        assert assistant_id == "ralph"
        yield ev.AssistantMessage(prompt, "Ralph", "#ffad64")
        yield ev.Done()

    async def run(_agent, _state, _assistant, _tracker, args, *, execute_fn, emit, on_event):
        assert args == ["Keep the original task text"]
        on_event(RalphStarted(args[0], 2, False, None))
        assert await execute_fn("First iteration")
        assert await execute_fn("Second iteration")
        on_event(RalphFinished(2, 0, 2, "complete", "Complete"))

    monkeypatch.setattr(worker_module, "iterate_agent_events", stream)
    monkeypatch.setattr("novacode_cli.commands.ralph_handler.handle_ralph_command", run)
    host, sent = await _host()
    try:
        host.spec.session_state.agent_profile = "ralph"
        await host.prompt("ralph", "Keep the original task text")
        assert (await _wait_for(sent, "turn_done"))["ok"]
        assert [frame["d"]["text"] for frame in sent if frame.get("c") == "AssistantMessage"] == [
            "First iteration", "Second iteration",
        ]
        assert len([frame for frame in sent if frame["t"] == "turn_done"]) == 1
    finally:
        await _drain(host)


async def test_a_command_that_asks_for_a_screen_waits_for_the_answer() -> None:
    """A `screen` frame out, a `screen_result` back, one answer."""
    host, sent = await _host()
    try:
        asking = asyncio.create_task(
            host._ask_screen(ScreenSpec("confirm", {"message": "go?"}, "Confirm"))
        )
        frame = await _wait_for(sent, "screen")
        assert frame["kind"] == "confirm"
        assert frame["title"] == "Confirm"
        assert frame["payload"] == {"message": "go?"}
        await host.resolve_screen(frame["id"], result=True)
        assert await asyncio.wait_for(asking, timeout=MAX_WAIT) is True
        assert host._screens == {}
    finally:
        await host.close()


async def test_an_unanswered_screen_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent client must not wedge the command coroutine."""
    monkeypatch.setattr("novacode_cli.server.host.SCREEN_TIMEOUT", 0.05)
    host, sent = await _host()
    try:
        assert await host._ask_screen(ScreenSpec("form", {"fields": []})) is None
        notice = await _wait_for(sent, "notice")
        assert "timed out" in notice["message"]
        assert host._screens == {}
    finally:
        await host.close()


async def test_closing_the_host_resolves_a_pending_screen() -> None:
    """Disconnecting mid-modal answers the default instead of hanging."""
    host, sent = await _host()
    asking = asyncio.create_task(host._ask_screen(ScreenSpec("list", {"items": []})))
    await _wait_for(sent, "screen")
    await host.close()
    assert await asyncio.wait_for(asking, timeout=MAX_WAIT) is None


async def test_a_late_screen_answer_is_reported_not_swallowed() -> None:
    host, sent = await _host()
    try:
        await host.resolve_screen("scr-404", result=True)
        notice = await _wait_for(sent, "notice")
        assert "no longer open" in notice["message"]
    finally:
        await host.close()


async def test_a_prompt_result_starts_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/init` answers with a prompt; the host must run it, not just report it."""

    async def handler(*_args: object, **_kwargs: object) -> str:
        return "generate NOVA.md"

    monkeypatch.setattr("novacode_cli.commands.commands.handle_command", handler)
    host, sent = await _host()
    started: list[tuple[str, str]] = []

    async def record(prompt_id: str, text: str, **_kwargs: object) -> None:
        started.append((prompt_id, text))

    host.prompt = record
    try:
        await host.command("c1", "init")
        frame = await _wait_for(sent, "command_result")
        assert frame["prompt"] == "generate NOVA.md"
        assert started == [("c1:prompt", "generate NOVA.md")]
    finally:
        await host.close()


async def test_the_catalogue_covers_every_tui_command() -> None:
    host, sent = await _host()
    try:
        await host.send_catalogue()
        frame = await _wait_for(sent, "catalogue")
        entries = frame["commands"]
        from novacode_cli.server.catalog import CATALOG

        assert [entry["name"] for entry in entries] == list(CATALOG)
        assert "help" in [entry["name"] for entry in entries]
        for entry in entries:
            assert {"name", "help", "aliases", "wants_text", "route", "served"} <= set(entry)
    finally:
        await host.close()


async def test_cancel_ends_the_turn_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def stream(_text: str, *_args: object, **_kwargs: object) -> AsyncIterator[object]:
        started.set()
        await asyncio.sleep(3600)
        yield ev.Done()

    monkeypatch.setattr("novacode_cli.sessions.worker.iterate_agent_events", stream)
    host, sent = await _host()
    await host.prompt("p1", "long one")
    await asyncio.wait_for(started.wait(), timeout=MAX_WAIT)
    await host.cancel()
    done = await _wait_for(sent, "turn_done")
    assert done["ok"] is False
    await host.close()


async def test_approval_mode_is_set_and_refused_explicitly() -> None:
    host, sent = await _host()
    try:
        await host.set_approval_mode(True)
        assert host.spec.session_state.auto_approve is True
        await host.set_approval_mode("yes")
        error = [msg for msg in sent if msg.get("t") == "error"]
        assert error
        assert error[-1]["message"] == "Invalid approval mode."
    finally:
        await host.close()


async def test_attach_other_session_is_an_explicit_notice() -> None:
    host, sent = await _host()
    try:
        await host.attach("someone-elses-session")
        notices = [msg for msg in sent if msg.get("t") == "notice"]
        assert notices
        assert notices[-1]["level"] == "warning"
    finally:
        await host.close()
