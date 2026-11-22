"""`/ralph`, `/trello` and `/create` over the socket: long jobs behind a task.

Each of these commands either loops forever (the kanban board) or runs for a long
time (Ralph's autonomous loop), so the port starts them as tasks it owns and
answers the command immediately. What the Textual UI painted into a live card
arrives here as transcript blocks; `close()` stops whatever is still running.
"""

import asyncio
from types import SimpleNamespace

import pytest

from novacode_cli.server.catalog import get
from novacode_cli.ui.port import Block
from novacode_cli.ui.port_server import ServerPort


class _Board:
    """A stand-in Trello server that records what the watcher was given."""

    def __init__(self, *, fail: OSError | None = None) -> None:
        self.url = "http://127.0.0.1:7777"
        self.is_running = False
        self.remote: tuple[object, str] | None = None
        self._fail = fail

    async def start(self) -> None:
        if self._fail is not None:
            raise self._fail
        self.is_running = True

    def bind_remote(self, connect, board_id: str) -> None:  # noqa: ANN001
        self.remote = (connect, board_id)


def _port(*, state: object | None = None) -> tuple[ServerPort, list[dict]]:
    emitted: list[dict] = []
    worker = SimpleNamespace(
        agent=object(),
        assistant_id="nova-agent",
        model_name="test-model",
        backend=None,
        image_tracker=None,
        session_manager=None,
        session_state=state or SimpleNamespace(thread_id="t1", session_id="s1"),
    )
    return ServerPort(emitted.append, worker), emitted


def _connect(_state: object) -> None:
    """Stand in for the Telegram bridge, which needs a network."""


def _texts(emitted: list[dict]) -> list[str]:
    return [
        block["text"]
        for frame in emitted
        for block in frame.get("blocks", [])
        if block.get("kind") == "text"
    ]


@pytest.mark.asyncio
async def test_ralph_starts_a_task_and_streams_its_lines(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.ralph_handler as handler

    seen: list[str | None] = []

    async def fake(
        _agent,  # noqa: ANN001
        _state,  # noqa: ANN001
        _assistant_id,  # noqa: ANN001
        _tracker,  # noqa: ANN001
        args,  # noqa: ANN001
        *,
        emit,  # noqa: ANN001
        **_kwargs: object,
    ) -> None:
        seen.append(args)
        emit("[bold]step 1[/bold] wrote the parser")
        emit("")
        emit("step 2 ran the tests")

    monkeypatch.setattr(handler, "handle_ralph_command", fake)
    port, emitted = _port()
    result = await port.run("/ralph", "ship the parser")
    assert result.ok is True
    assert result.message.startswith("● Ralph started")
    assert port._ralph_task is not None
    await port._ralph_task
    assert seen == ["ship the parser"]
    texts = _texts(emitted)
    assert "step 1 wrote the parser" in texts
    assert "step 2 ran the tests" in texts


@pytest.mark.asyncio
async def test_ralph_refuses_a_second_run(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.ralph_handler as handler

    release = asyncio.Event()

    async def slow(*_args: object, **_kwargs: object) -> None:
        await release.wait()

    monkeypatch.setattr(handler, "handle_ralph_command", slow)
    port, _emitted = _port()
    await port.run("/ralph", "one")
    second = await port.run("/ralph", "two")
    assert second.message == "Ralph is already running. /ralph --status shows where it is."
    release.set()
    await port._ralph_task


@pytest.mark.asyncio
async def test_ralph_status_reports_where_it_is(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.ralph_handler as handler

    async def status(_state, *, emit, **_kwargs: object) -> None:  # noqa: ANN001
        emit("[cyan]Ralph · round 3 of 40[/cyan]")
        emit("last: tests green")

    monkeypatch.setattr(handler, "handle_ralph_status", status)
    port, _emitted = _port()
    result = await port.run("/ralph", "--status")
    assert result.message == "Ralph · round 3 of 40"
    assert "last: tests green" in result.blocks[0].text


@pytest.mark.asyncio
async def test_ralph_needs_a_live_session():
    port, _emitted = _port()
    port._worker.session_state = None
    result = await port.run("/ralph", "anything")
    assert result.ok is False
    assert result.message == "/ralph needs a live session."
    assert port._ralph_task is None


@pytest.mark.asyncio
async def test_trello_start_announces_the_board_and_watches(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.trello_handler as handler
    import novacode_cli.commands.trello_server as server_module

    board = _Board()
    monkeypatch.setattr(server_module, "TrelloServer", lambda: board)
    watched: list[tuple] = []

    async def watch_loop(*args: object) -> None:
        watched.append(args)
        log = args[-1]
        log("[bold]picked up[/bold] a card", "cyan")

    monkeypatch.setattr(handler, "trello_watch_loop", watch_loop)
    monkeypatch.setattr("novacode_cli.commands.trello_remote.connect_telegram", _connect)
    port, emitted = _port()
    result = await port.run("/trello", None)
    assert result.ok is True
    assert board.url in result.message
    assert board.remote is not None
    assert port._worker.session_state.trello_server is board
    await port._trello_task
    (call,) = watched
    assert call[0] is board
    assert call[3] is port._worker.session_state
    assert call[5].__name__ == "execute_task"
    assert "picked up a card" in _texts(emitted)


@pytest.mark.asyncio
async def test_trello_reports_an_occupied_port(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.trello_server as server_module

    monkeypatch.setattr(server_module, "TrelloServer", lambda: _Board(fail=OSError("in use")))
    port, _emitted = _port()
    result = await port.run("/trello", None)
    assert result.ok is False
    assert result.message == "Could not start the kanban board: in use"
    assert port._trello_task is None


@pytest.mark.asyncio
async def test_trello_status_and_stop_print_through_the_handler(
    monkeypatch: pytest.MonkeyPatch,
):
    import novacode_cli.commands.trello_handler as handler

    monkeypatch.setattr(
        handler,
        "_handle_status",
        lambda _state: print("board idle on :7777"),  # noqa: T201
    )
    monkeypatch.setattr(
        handler,
        "_handle_stop",
        lambda _state: print("board stopped"),  # noqa: T201
    )
    port, _emitted = _port()
    status = await port.run("/trello", "status")
    assert status.message == "board idle on :7777"
    stopped = await port.run("/trello", "stop")
    assert [block.text for block in stopped.blocks] == ["board stopped"]


@pytest.mark.asyncio
async def test_trello_needs_a_live_session():
    port, _emitted = _port()
    port._worker.session_state = None
    result = await port.run("/trello", None)
    assert result.message == "/trello needs a live session."


@pytest.mark.asyncio
async def test_create_starts_the_browser_through_its_handler(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.create_handler as handler

    async def fake(_state, _args):  # noqa: ANN001, ANN202
        print("Skills UI at http://127.0.0.1:9000")  # noqa: T201

    monkeypatch.setattr(handler, "handle_create_command", fake)
    port, _emitted = _port()
    result = await port.run("/create", None)
    assert result.ok is True
    assert result.message == "Skills UI at http://127.0.0.1:9000"


@pytest.mark.asyncio
async def test_create_reports_a_failure(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.create_handler as handler

    async def fake(_state, _args):  # noqa: ANN001, ANN202
        message = "port 9000 is taken"
        raise OSError(message)

    monkeypatch.setattr(handler, "handle_create_command", fake)
    port, _emitted = _port()
    result = await port.run("/create", None)
    assert result.ok is False
    assert result.message == "/create failed: port 9000 is taken"


@pytest.mark.asyncio
async def test_close_stops_the_tasks_the_port_started(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.commands.ralph_handler as handler
    import novacode_cli.commands.trello_handler as trello
    import novacode_cli.commands.trello_server as server_module

    async def forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(handler, "handle_ralph_command", forever)
    monkeypatch.setattr(trello, "trello_watch_loop", forever)
    monkeypatch.setattr(server_module, "TrelloServer", _Board)
    monkeypatch.setattr("novacode_cli.commands.trello_remote.connect_telegram", _connect)
    port, _emitted = _port()
    await port.run("/ralph", "go")
    await port.run("/trello", None)
    ralph_task, trello_task = port._ralph_task, port._trello_task
    await asyncio.sleep(0)
    await port.close()
    assert ralph_task.cancelled()
    assert trello_task.cancelled()
    assert port._ralph_task is None
    assert port._trello_task is None


def test_the_background_trio_is_served():
    for name in ("ralph", "trello", "create"):
        spec = get(name)
        assert spec is not None
        assert spec.served is True
        assert spec.route == "passthrough"


def test_blocks_are_plain_text():
    assert Block("text", "plain").kind == "text"
