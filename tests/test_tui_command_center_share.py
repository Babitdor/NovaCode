"""`/command-center` and `/share` in the socket server's port.

`/command-center` lists saved conversations and live tabs together; choosing one
attaches to a running tab or resumes a saved session in a new tab. `/share` is
one command with two halves: a tunnel sub-command, or -- with no argument -- the
session page published as a secret gist through the GitHub CLI. Every test here
pins the decision the port makes, with no browser, terminal or network involved.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from novacode_cli.session.session_persistence import SessionManager
from novacode_cli.ui.port_server import ServerPort

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from novacode_cli.ui.port import ScreenSpec


def _port(
    *,
    answers: list[Any] | None = None,
    manager: Any = None,  # noqa: ANN401 — a fake or real session manager
    session_id: str = "s1",
    rows: list[dict[str, Any]] | None = None,
) -> tuple[ServerPort, list[Any]]:
    seen: list[Any] = []
    pending = list(answers or [])

    async def ask_screen(spec: ScreenSpec) -> Any:  # noqa: ANN401 — mirrors the hook
        seen.append(spec)
        return pending.pop(0) if pending else None

    worker = SimpleNamespace(
        agent=None,
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=manager,
        image_tracker=None,
        backend=None,
        session_state=SimpleNamespace(thread_id="t1", session_id=session_id),
    )
    port = ServerPort(
        object(),
        worker,
        ask_screen=ask_screen,
        session_rows=lambda: list(rows or []),
    )
    return port, seen


def _manager(tmp_path: Path) -> SessionManager:
    """Two saved sessions, so the picker has something to order."""
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    manager.save_session(
        session_id="saved-1",
        thread_id="th-1",
        messages=[],
        assistant_id="nova-agent",
        current_task="review the parser",
        project_root=tmp_path,
    )
    manager.save_session(
        session_id="saved-2",
        thread_id="th-2",
        messages=[],
        assistant_id="nova-agent",
        current_task="write the docs",
        project_root=tmp_path,
    )
    return manager


def _live(tmp_path: Path) -> list[dict[str, Any]]:
    return [{"id": "live-1", "name": "live work", "cwd": str(tmp_path), "status": "running"}]


# ── /command-center ──────────────────────────────────────────────────────
async def test_command_center_lists_saved_and_live_together(tmp_path: Path) -> None:
    port, seen = _port(manager=_manager(tmp_path), session_id="live-1", rows=_live(tmp_path))
    await port.run("/command-center", None)
    labels = [row["label"] for row in seen[0].payload["items"]]
    # The attached session is marked current and the running tab carries its dot.
    assert "→ ● live work" in labels
    assert any("review the parser" in label for label in labels)
    assert seen[0].payload["counts"] == [
        "All 3",
        "Needs you 0",
        "Working 1",
        "Ready 0",
        "Inactive 2",
    ]


async def test_command_center_attaches_to_a_live_tab(tmp_path: Path) -> None:
    port, _seen = _port(
        answers=[{"id": "session:live-1"}],
        manager=_manager(tmp_path),
        session_id="s1",
        rows=_live(tmp_path),
    )
    result = await port.run("/command-center", None)
    assert result.ok is True
    assert result.attach == "live-1"
    assert result.message == "Attached to live work."


async def test_command_center_resumes_a_saved_session(tmp_path: Path) -> None:
    port, _seen = _port(
        answers=[{"id": "session:saved-1"}],
        manager=_manager(tmp_path),
        session_id="s1",
    )
    result = await port.run("/command-center", None)
    assert result.ok is True
    assert result.resume == "saved-1"
    assert result.create == f"{tmp_path.name} · saved-1"


async def test_command_center_filters_by_status(tmp_path: Path) -> None:
    port, seen = _port(
        answers=[None],
        manager=_manager(tmp_path),
        session_id="live-1",
        rows=_live(tmp_path),
    )
    result = await port.run("/command-center", "working")
    assert result.message == "Cancelled."
    assert seen[0].title == "Agent command center · Working"
    assert [row["label"] for row in seen[0].payload["items"]] == ["→ ● live work"]


async def test_command_center_rejects_an_unknown_filter(tmp_path: Path) -> None:
    port, seen = _port(manager=_manager(tmp_path))
    result = await port.run("/command-center", "sleeping")
    assert result.ok is False
    assert result.message == (
        "Unknown filter “sleeping”. Try one of: all, needsyou, working, ready, inactive."
    )
    assert seen == []


async def test_command_center_with_nothing_to_show_says_so() -> None:
    port, seen = _port(manager=None, rows=[])
    result = await port.run("/command-center", None)
    assert result.ok is True
    assert result.message == "Nothing is all right now."
    assert seen == []


async def test_command_center_reports_an_unreadable_store(tmp_path: Path) -> None:
    class _Broken:
        def list_sessions(self, _limit: int) -> list[Any]:
            message = "disk is gone"
            raise OSError(message)

    port, seen = _port(
        answers=[None],
        manager=_Broken(),
        session_id="live-1",
        rows=_live(tmp_path),
    )
    await port.run("/command-center", None)
    assert seen[0].payload["note"] == (
        "Saved conversations could not be read; only live tabs are listed."
    )


async def test_command_center_leaves_the_current_session_alone(tmp_path: Path) -> None:
    port, _seen = _port(
        answers=[{"id": "session:saved-1"}],
        manager=_manager(tmp_path),
        session_id="saved-1",
    )
    result = await port.run("/command-center", None)
    assert result.ok is True
    assert result.message == "That conversation is already open here."
    assert result.attach is None
    assert result.resume is None


# ── /share ───────────────────────────────────────────────────────────────
async def test_share_without_gh_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    port, seen = _port()
    result = await port.run("/share", None)
    assert result.ok is False
    assert "needs the GitHub CLI" in result.message
    assert seen == []


async def test_share_needs_session_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gh")
    port, _seen = _port(manager=None)
    result = await port.run("/share", None)
    assert result.ok is False
    assert result.message == "Session history is off for this run."


async def test_share_with_an_empty_history_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gh")
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    port, seen = _port(manager=manager)
    result = await port.run("/share", None)
    assert result.ok is False
    assert result.message == "Nothing in this session's history yet."
    assert seen == []


async def test_share_asks_before_uploading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gh")
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    manager.history_tree("s1").sync(
        [{"type": "HumanMessage", "id": "h1", "content": "add a parser"}]
    )
    port, seen = _port(answers=[{"id": "cancel"}], manager=manager)
    result = await port.run("/share", None)
    assert seen[0].title == "Share this session?"
    assert result.message == "Cancelled."


async def test_share_uploads_an_export_page_as_a_gist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gh")
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    manager.history_tree("s1").sync(
        [{"type": "HumanMessage", "id": "h1", "content": "add a parser"}]
    )
    uploaded: list[str] = []

    async def fake_gist(_gh: str, page: str) -> tuple[str, str]:
        uploaded.append(page)
        return "https://gist.github.com/abc123", ""

    monkeypatch.setattr("novacode_cli.ui.port_server._create_gist", fake_gist)
    port, seen = _port(answers=[{"id": "upload"}], manager=manager)
    result = await port.run("/share", None)
    assert [row["id"] for row in seen[0].payload["items"]] == ["upload", "cancel"]
    assert len(uploaded) == 1
    # The gist holds the same page /export writes, not a second rendering.
    assert "<html" in uploaded[0]
    assert "add a parser" in uploaded[0]
    assert result.ok is True
    assert result.message == "● Shared: https://gistpreview.github.io/?abc123"


async def test_share_reports_a_failed_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gh")
    manager = SessionManager(sessions_dir=tmp_path / "sessions")
    manager.history_tree("s1").sync(
        [{"type": "HumanMessage", "id": "h1", "content": "add a parser"}]
    )

    async def failing_gist(_gh: str, _page: str) -> tuple[str, str]:
        return "", "HTTP 401: Bad credentials"

    monkeypatch.setattr("novacode_cli.ui.port_server._create_gist", failing_gist)
    port, _seen = _port(answers=[{"id": "upload"}], manager=manager)
    result = await port.run("/share", None)
    assert result.ok is False
    assert result.message == "/share failed: HTTP 401: Bad credentials"


async def test_share_with_an_argument_runs_the_tunnel_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_share(context: Any) -> str:  # noqa: ANN401 — mirrors the handler
        calls.append(str(context.cmd_args))
        return "● Sharing artifacts"

    monkeypatch.setattr("novacode_cli.commands.share_handler.handle_share_command", fake_share)
    port, seen = _port()
    result = await port.run("/share", "artifacts")
    assert calls == ["artifacts"]
    assert seen == []
    assert result.ok is True
