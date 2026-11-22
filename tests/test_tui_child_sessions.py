"""Child sessions: the adapter between `SessionSupervisor` and the socket host.

No real process is started here. The supervisor is a fake that records what the
adapter asked it to do, so the frame fan-out, command surface, registry
lifecycle and ownership checks are all exercised at reducer speed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from novacode_cli.server import children, frames
from novacode_cli.server.children import ChildHost, ChildSessionManager
from novacode_cli.server.sessions import SessionRegistry

if TYPE_CHECKING:
    from pathlib import Path


class _FakeSupervisor:
    """Records child calls; no subprocess, no pipes."""

    def __init__(self) -> None:
        self.spawned: list[dict[str, Any]] = []
        self.prompts: list[tuple[str, str, Any, bool]] = []
        self.closed: list[str] = []
        self.cancelled: list[str] = []
        self.replies: list[tuple[str, str, Any]] = []
        self.approvals: list[tuple[str, bool]] = []

    async def spawn(self, **kwargs: Any) -> Any:  # noqa: ANN401 - mirrors the supervisor
        self.spawned.append(kwargs)
        return SimpleNamespace(
            session_id=kwargs["session_id"],
            name=kwargs["name"],
            worktree=kwargs["worktree"],
            status="idle",
        )

    async def send_prompt(
        self,
        session_id: str,
        text: str,
        *,
        images: Any = None,  # noqa: ANN401 - the supervisor's image objects
        auto_approve: Any = None,  # noqa: ANN401 - tri-state
        prompt_id: str | None = None,
        steer: bool = False,
    ) -> str:
        _ = auto_approve
        self.prompts.append((session_id, text, images, steer))
        return prompt_id or "p1"

    async def reply_interrupt(self, session_id: str, interrupt_id: str, result: Any) -> None:  # noqa: ANN401
        self.replies.append((session_id, interrupt_id, result))

    async def set_auto_approve(self, session_id: str, *, enabled: bool) -> bool:
        self.approvals.append((session_id, enabled))
        return True

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    async def close(self, session_id: str) -> Any:  # noqa: ANN401 - returns ChildSession
        self.closed.append(session_id)
        return None


@pytest.fixture
def wired() -> tuple[ChildHost, _FakeSupervisor, list[dict[str, Any]], SessionRegistry]:
    supervisor = _FakeSupervisor()
    registry = SessionRegistry()
    child = SimpleNamespace(
        session_id="s-1", name="docs", worktree="C:/work/docs", status="starting"
    )
    gone: list[str] = []
    host = ChildHost(child, supervisor, registry, on_gone=gone.append)  # type: ignore[arg-type]
    sent: list[dict[str, Any]] = []
    host.add_sink(sent.append)
    return host, supervisor, sent, registry


def test_ready_sets_status_and_is_not_forwarded(
    wired: tuple[ChildHost, _FakeSupervisor, list[dict[str, Any]], SessionRegistry],
) -> None:
    host, _, sent, _ = wired
    host.feed({"t": "ready", "model": "child-model", "cwd": "C:/work/docs"})
    assert sent == [], "the sessions row already carried the handshake"
    assert host.row() == {
        "id": "s-1",
        "name": "docs",
        "cwd": "C:/work/docs",
        "status": "idle",
        "model": "child-model",
    }


def test_events_and_interrupts_are_forwarded_and_change_status(
    wired: tuple[ChildHost, _FakeSupervisor, list[dict[str, Any]], SessionRegistry],
) -> None:
    host, _, sent, _ = wired
    host.feed({"t": "ev", "c": "TextDelta", "d": {"text": "hi"}})
    assert host.row()["status"] == "running"
    host.feed({"t": "interrupt", "id": "i1", "kind": "tool", "payload": {}})
    assert host.row()["status"] == "needs-approval"
    host.feed({"t": "turn_done", "id": "p1", "ok": True})
    assert host.row()["status"] == "idle"
    assert [frame["t"] for frame in sent] == ["ev", "interrupt", "turn_done"]
    assert frames.decode_line(frames.encode_line(sent[0])) is not None


def test_an_exit_notifies_the_manager_and_the_client(
    wired: tuple[ChildHost, _FakeSupervisor, list[dict[str, Any]], SessionRegistry],
) -> None:
    host, _, sent, _ = wired
    gone: list[str] = []
    host._on_gone = gone.append
    host.feed({"t": "exited", "code": 3, "crashed": True})
    assert gone == ["s-1"]
    assert host.row()["status"] == "crashed"
    assert sent[-1]["t"] == "notice"
    assert sent[-1]["level"] == "error"


async def test_prompts_images_and_approvals_reach_the_supervisor(
    wired: tuple[ChildHost, _FakeSupervisor, list[dict[str, Any]], SessionRegistry],
) -> None:
    host, supervisor, _, _ = wired
    await host.prompt("p9", "run it", images=[{"base64_data": "AAA", "format": "png"}])
    await host.interrupt_reply("i1", {"decisions": ["approve"]})
    await host.set_approval_mode(True)
    assert supervisor.prompts[0][0:2] == ("s-1", "run it")
    assert supervisor.prompts[0][2][0].base64_data == "AAA"
    assert supervisor.replies == [("s-1", "i1", {"decisions": ["approve"]})]
    assert supervisor.approvals == [("s-1", True)]


async def test_local_commands_answer_and_others_refuse(
    wired: tuple[ChildHost, _FakeSupervisor, list[dict[str, Any]], SessionRegistry],
) -> None:
    host, supervisor, sent, _ = wired
    await host.command("c1", "stop", None)
    assert supervisor.cancelled == ["s-1"]
    assert sent[-1]["t"] == "command_result"

    await host.command("c2", "status", None)
    assert "Session   s-1" in sent[-1]["blocks"][0]["text"]

    await host.command("c3", "clear", None)
    assert sent[-1]["action"] == "clear"

    await host.command("c4", "rooms", None)
    assert sent[-1]["ok"] is False
    assert "not ported" in sent[-1]["message"]


async def test_create_registers_and_close_unregisters() -> None:
    supervisor = _FakeSupervisor()
    registry = SessionRegistry()
    manager = ChildSessionManager(registry, "C:/work")
    manager._supervisor = supervisor

    host = await manager.create("docs")
    assert supervisor.spawned[0]["worktree"].as_posix() == "C:/work"
    assert supervisor.spawned[0]["name"] == "docs"
    assert registry.get(host.session_id) is host
    assert manager._hosts[host.session_id] is host

    assert await manager.close(host.session_id) is True
    assert registry.get(host.session_id) is None
    assert supervisor.closed == [host.session_id]
    assert await manager.close("missing") is False


async def test_child_events_reach_the_registered_host() -> None:
    supervisor = _FakeSupervisor()
    registry = SessionRegistry()
    manager = ChildSessionManager(registry, "C:/work")
    manager._supervisor = supervisor

    host = ChildHost(
        SimpleNamespace(session_id="s-2", name="docs", worktree="C:/work", status="idle"),
        supervisor,  # type: ignore[arg-type]
        registry,
    )
    manager._hosts["s-2"] = host
    sent: list[dict[str, Any]] = []
    host.add_sink(sent.append)

    await manager._on_message("s-2", {"t": "ev", "c": "TextDelta", "d": {"text": "x"}})
    assert sent == [{"t": "ev", "c": "TextDelta", "d": {"text": "x"}}]


def test_the_module_does_not_import_textual() -> None:
    from pathlib import Path

    source = Path(children.__file__).read_text(encoding="utf-8")
    assert "novacode_cli.tui" not in source


async def test_create_can_resume_a_saved_session_in_its_own_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saved = tmp_path / "saved-project"
    saved.mkdir()

    class _Manager:
        def load_session_meta(self, session_id: str) -> Any:  # noqa: ANN401 - test double
            return SimpleNamespace(session_id=session_id, project_root=str(saved))

    monkeypatch.setattr("novacode_cli.session.session_persistence.SessionManager", _Manager)
    supervisor = _FakeSupervisor()
    manager = ChildSessionManager(SessionRegistry(), "C:/work")
    manager._supervisor = supervisor

    host = await manager.create("docs", resume_id="saved-1")
    spawned = supervisor.spawned[0]
    assert spawned["resume_id"] == "saved-1"
    # A resumed session's tools belong to the folder it was saved in.
    assert spawned["worktree"] == saved
    assert host.row()["cwd"] == str(saved)


async def test_create_resumes_in_the_base_folder_when_the_meta_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Manager:
        def load_session_meta(self, _session_id: str) -> Any:  # noqa: ANN401 - test double
            message = "meta is gone"
            raise OSError(message)

    monkeypatch.setattr("novacode_cli.session.session_persistence.SessionManager", _Manager)
    supervisor = _FakeSupervisor()
    manager = ChildSessionManager(SessionRegistry(), "C:/work")
    manager._supervisor = supervisor

    await manager.create("docs", resume_id="saved-2")
    assert supervisor.spawned[0]["worktree"].as_posix() == "C:/work"
    assert supervisor.spawned[0]["resume_id"] == "saved-2"
