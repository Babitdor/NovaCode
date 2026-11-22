"""`/import` and `/compare` over the socket: one bounded context, no Textual.

The import machinery moved to the session layer (`novacode_cli/session/imported_context.py`)
so the port can reach it; these tests pin that the port writes the same single
reference message the terminal app writes, and that `/compare` returns a prompt
for the host to run rather than running a turn itself.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from novacode_cli.server.catalog import get
from novacode_cli.session.adapters import HarnessMessage, ImportedSession
from novacode_cli.session.imported_context import MESSAGE_ID, ImportedContext, estimated_tokens
from novacode_cli.ui.port_server import ServerPort


class _Agent:
    """Records the state writes a command makes."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def aupdate_state(
        self, config: object, *, values: dict, as_node: str, **options: object
    ) -> None:
        self.writes.append(
            {"config": config, "values": values, "as_node": as_node, "options": options}
        )


class _Manager:
    """Just the session directory `/import` stores beside."""

    def __init__(self, root: Path) -> None:
        self.sessions_dir = root


def _context(provider: str = "codex", selector: str = "abc") -> ImportedContext:
    return ImportedContext(
        ImportedSession(
            provider,
            selector,
            "/project",
            [
                HarnessMessage("user", "Fix auth"),
                HarnessMessage("tool", "tests pass", tool_name="Bash"),
            ],
        )
    )


def _port(
    *, manager: _Manager | None = None, tracker: object | None = None, state: object | None = None
) -> tuple[ServerPort, _Agent, list[dict]]:
    emitted: list[dict] = []
    agent = _Agent()
    session = state or SimpleNamespace(thread_id="t1", session_id="s1", token_tracker=tracker)
    worker = SimpleNamespace(
        agent=agent,
        assistant_id="nova-agent",
        model_name="test-model",
        backend=None,
        image_tracker=None,
        session_manager=manager,
        session_state=session,
    )
    return ServerPort(emitted.append, worker), agent, emitted


@pytest.mark.asyncio
async def test_import_writes_one_reference_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import novacode_cli.session.imported_context as module

    monkeypatch.setattr(module, "load_import", lambda *_a, **_k: _context())
    port, agent, _emitted = _port(manager=_Manager(tmp_path))
    result = await port.run("/import", "codex abc")
    assert result.ok is True
    assert result.message.startswith("Imported 2 messages from codex.")
    assert "Imported Context · codex" in result.blocks[1].text
    assert "2 messages" in result.blocks[1].text
    (write,) = agent.writes
    (message,) = write["values"]["messages"]
    assert message.id == MESSAGE_ID
    assert "Bash" in message.content
    assert write["as_node"] == "model"
    assert write["config"] == {"configurable": {"thread_id": "t1"}}
    stored = tmp_path / "s1" / "imported-context.json"
    assert stored.is_file()
    assert ImportedContext.load(tmp_path, "s1") is not None


@pytest.mark.asyncio
async def test_import_needs_a_live_conversation(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.session.imported_context as module

    monkeypatch.setattr(module, "load_import", lambda *_a, **_k: _context())
    port, agent, _emitted = _port(state=SimpleNamespace(thread_id=None, session_id="s1"))
    result = await port.run("/import", "codex abc")
    assert result.ok is False
    assert result.message == "Session import needs a live conversation."
    assert agent.writes == []


@pytest.mark.asyncio
async def test_import_reports_the_usage_line(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.session.imported_context as module

    def refuse(*_args: object, **_kwargs: object) -> ImportedContext:
        message = (
            "Use /import codex|claude|nova <session ID or 'file path'> "
            "[--mode compact|full|relevant], or --last."
        )
        raise ValueError(message)

    monkeypatch.setattr(module, "load_import", refuse)
    port, _agent, _emitted = _port()
    result = await port.run("/import", "")
    assert result.ok is False
    assert result.message.startswith("Session import: Use /import codex|claude|nova")


@pytest.mark.asyncio
async def test_import_keeps_the_reference_when_storing_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import novacode_cli.session.imported_context as module

    monkeypatch.setattr(module, "load_import", lambda *_a, **_k: _context())
    monkeypatch.setattr(
        module.ImportedContext, "save", lambda *_a, **_k: _raise(OSError("disk is full"))
    )
    port, agent, _emitted = _port(manager=_Manager(tmp_path))
    result = await port.run("/import", "codex abc")
    assert result.ok is False
    assert result.message.startswith("Imported the context, but storing it failed: disk is full")
    assert len(agent.writes) == 1


def _raise(error: Exception) -> None:
    raise error


@pytest.mark.asyncio
async def test_import_reports_a_graph_write_failure(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.session.imported_context as module

    monkeypatch.setattr(module, "load_import", lambda *_a, **_k: _context())
    port, agent, _emitted = _port()

    async def refuse(*_args: object, **_kwargs: object) -> None:
        message = "checkpoint store is locked"
        raise RuntimeError(message)

    agent.aupdate_state = refuse
    result = await port.run("/import", "codex abc")
    assert result.ok is False
    assert result.message.startswith("Could not write the imported context:")


@pytest.mark.asyncio
async def test_compare_returns_the_prompt_the_host_runs(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.session.imported_context as module

    def load(
        provider: str, selector: str = "", *_args: object, **_kwargs: object
    ) -> ImportedContext:
        return _context(provider, selector)

    monkeypatch.setattr(module, "load_import", load)
    port, agent, _emitted = _port()
    result = await port.run("/compare", "claude:a codex:b")
    assert agent.writes == []
    assert result.prompt is not None
    assert result.prompt.startswith("Compare these historical sessions:")
    assert "claude:a" in result.prompt
    assert "codex:b" in result.prompt
    assert estimated_tokens(result.prompt) <= 12000
    assert "claude" in result.message
    assert "codex" in result.message


@pytest.mark.asyncio
async def test_compare_rejects_a_single_selector():
    port, _agent, _emitted = _port()
    result = await port.run("/compare", "claude:a")
    assert result.ok is False
    assert result.message == "Use /compare claude:<id> codex:<id>."
    assert result.prompt is None


@pytest.mark.asyncio
async def test_compare_reports_the_unreadable_side(monkeypatch: pytest.MonkeyPatch):
    import novacode_cli.session.imported_context as module

    def load(provider: str, *_args: object, **_kwargs: object) -> ImportedContext:
        if provider == "codex":
            message = "Unknown session provider 'codex'. Available: claude"
            raise ValueError(message)
        return _context(provider, "a")

    monkeypatch.setattr(module, "load_import", load)
    port, _agent, _emitted = _port()
    result = await port.run("/compare", "claude:a codex:b")
    assert result.ok is False
    assert result.message.startswith("Session import: Unknown session provider 'codex'")
    assert result.prompt is None


def test_import_and_compare_are_served():
    for name in ("import", "compare"):
        spec = get(name)
        assert spec is not None
        assert spec.served is True
        assert spec.route == "passthrough"


@pytest.mark.asyncio
async def test_context_imported_reports_what_was_imported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import novacode_cli.session.imported_context as module

    monkeypatch.setattr(module, "load_import", lambda *_a, **_k: _context())
    port, _agent, _emitted = _port(manager=_Manager(tmp_path))
    await port.run("/import", "codex abc")
    result = await port.run("/context", "imported")
    assert result.ok is True
    assert result.message.startswith("Imported Context · codex · compact")
    assert "2 messages" in result.blocks[0].text


@pytest.mark.asyncio
async def test_context_imported_switches_mode_without_reloading_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import novacode_cli.session.imported_context as module

    monkeypatch.setattr(module, "load_import", lambda *_a, **_k: _context())
    port, agent, _emitted = _port(manager=_Manager(tmp_path))
    await port.run("/import", "codex abc")
    message_id = agent.writes[-1]["values"]["messages"][0].id
    result = await port.run("/context", "imported relevant auth")
    assert result.ok is True
    assert result.message == "Imported context · relevant"
    assert agent.writes[-1]["values"]["messages"][0].id == message_id
    stored = ImportedContext.load(tmp_path, "s1")
    assert stored is not None
    assert stored.mode == "relevant"
    assert stored.query == "auth"


@pytest.mark.asyncio
async def test_context_imported_refuses_an_unknown_mode(tmp_path: Path):
    import novacode_cli.session.imported_context as module

    port, agent, _emitted = _port(manager=_Manager(tmp_path))
    _context().save(tmp_path, "s1")
    result = await port.run("/context", "imported sideways")
    assert result.ok is False
    assert result.message.startswith("Session import: Imported context mode must be")
    assert agent.writes == []


@pytest.mark.asyncio
async def test_context_imported_says_when_nothing_was_imported(tmp_path: Path):
    port, _agent, _emitted = _port(manager=_Manager(tmp_path))
    result = await port.run("/context", "imported")
    assert result.ok is False
    assert result.message == "This Nova session has no imported context. Use /import first."
