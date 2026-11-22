"""`/notifications` and `/images`: session state commands served by the port."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from novacode_cli.states.Session import Notification
from novacode_cli.ui.port_server import ServerPort


def _notification(nid: str, action_id: str | None = None) -> Notification:
    return Notification(
        id=nid,
        level="approval" if action_id else "info",
        title="Test",
        message="A test notification.",
        source="system",
        action_id=action_id,
        action_type="approve" if action_id else None,
    )


def _worker(notifications: list[Notification] | None = None) -> SimpleNamespace:
    notifications = notifications or []
    pending: dict[str, Any] = {}
    state = SimpleNamespace(
        thread_id="t1",
        session_id="s1",
        auto_approve=False,
        todos=[],
        headless_out_fd=None,
        notifications=notifications,
        _pending_approvals=pending,
        resolve_approval=lambda action_id, approve=True: _resolve(pending, action_id, approve),
        dismiss_notification=lambda nid: _dismiss(notifications, nid),
        clear_notifications=lambda: _clear(notifications),
    )
    return SimpleNamespace(
        agent=object(),
        session_state=state,
        assistant_id="nova-agent",
        model_name="test-model",
        session_manager=None,
        image_tracker=None,
        backend=None,
        token_tracker=None,
    )


def _resolve(pending: dict[str, Any], action_id: str, *, approve: bool) -> bool:
    future = pending.pop(action_id, None)
    if future is None or future.done():
        return False
    future.set_result(
        {"decisions": [{"type": "approve" if approve else "reject"}], "any_rejected": not approve}
    )
    return True


def _dismiss(notifications: list[Notification], nid: str) -> bool:
    for notification in notifications:
        if notification.id == nid:
            notification.dismissed = True
            return True
    return False


def _clear(notifications: list[Notification]) -> int:
    count = len(notifications)
    notifications.clear()
    return count


def _port(notifications: list[Notification] | None = None) -> tuple[ServerPort, list[dict]]:
    """A port over `_worker(notifications)`; `sent` collects emitted frames."""
    sent: list[dict[str, Any]] = []
    return ServerPort(sent.append, _worker(notifications)), sent


def _screen_port(answers: list[object]) -> tuple[ServerPort, list[Any], list[dict[str, Any]]]:
    """A port whose `ask_screen` returns the queued *answers* in order."""
    asked: list[Any] = []
    queue = list(answers)
    sent: list[dict[str, Any]] = []

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return queue.pop(0) if queue else None

    port = ServerPort(sent.append, _worker(), ask_screen=ask_screen)
    return port, asked, sent


async def test_notifications_list_shows_pending() -> None:
    port, _ = _port([_notification("n1"), _notification("n2")])
    result = await port.run("/notifications", None)
    assert result.ok is True
    assert "n1" in result.blocks[0].text
    assert "n2" in result.blocks[0].text


async def test_notifications_empty_state() -> None:
    port, _ = _port([])
    result = await port.run("/notifications", None)
    assert result.ok is True
    assert "No notifications." in result.blocks[0].text


async def test_dismiss_marks_a_notification() -> None:
    notifications = [_notification("n1")]
    port, _ = _port(notifications)
    result = await port.run("/notifications", "dismiss n1")
    assert result.ok is True
    assert notifications[0].dismissed is True


async def test_clear_reports_the_count() -> None:
    port, _ = _port([_notification("n1"), _notification("n2")])
    result = await port.run("/notifications", "clear")
    assert "Cleared 2" in result.blocks[0].text


class _FakeImages:
    def __init__(self, count: int = 0) -> None:
        self.count = count

    def list_images(self) -> list[dict]:
        return [
            {"id": f"image-{i}", "format": "png", "size_kb": 1.5, "placeholder": f"[image-{i}]"}
            for i in range(1, self.count + 1)
        ]

    def remove_image(self, _image_id: str) -> bool:
        if self.count == 0:
            return False
        self.count -= 1
        return True

    def clear(self) -> None:
        self.count = 0


async def test_images_lists_tracked_items() -> None:
    port, _ = _port()
    port._worker.image_tracker = _FakeImages(count=2)
    result = await port.run("/images", None)
    assert result.ok is True
    assert "image-1" in result.blocks[0].text
    assert "image-2" in result.blocks[0].text


async def test_images_remove_and_clear() -> None:
    port, _ = _port()
    tracker = _FakeImages(count=3)
    port._worker.image_tracker = tracker
    removed = await port.run("/images", "remove image-1")
    assert removed.ok is True
    assert tracker.count == 2
    cleared = await port.run("/images", "clear")
    assert "Cleared 2" in cleared.blocks[0].text


async def test_images_without_a_tracker_is_explicit() -> None:
    port, _ = _port()
    result = await port.run("/images", None)
    assert result.ok is False
    assert "not available" in result.message.lower()


async def test_research_without_a_query_shows_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.commands import research_handler

    async def fake(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        from novacode_cli.config.config import console

        console.print("Usage: /research [mode] <query> [-n <agents>]")

    monkeypatch.setattr(research_handler, "handle_research_command", fake)
    port, _ = _port()
    result = await port.run("/research", None)
    assert result.ok is True
    assert "usage" in result.blocks[0].text.lower()


async def test_research_returns_the_swarm_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.commands import research_handler

    async def fake(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        execute = _kwargs.get("execute_fn")
        if execute is not None:
            await execute("RESEARCH PROMPT: investigate")

    monkeypatch.setattr(research_handler, "handle_research_command", fake)
    port, _ = _port()
    result = await port.run("/research", "explain the scheduler")
    assert result.ok is True
    assert result.prompt == "RESEARCH PROMPT: investigate"


async def test_tasks_reports_the_empty_state() -> None:
    port, _ = _port()
    result = await port.run("/tasks", None)
    assert result.ok is True
    assert "No background tasks" in result.blocks[0].text
    spec = result.screen
    assert spec is not None
    assert spec.kind == "table"
    assert spec.payload["columns"][0]["key"] == "task"


async def test_files_reports_reads_and_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli.tracking import file_tracker as ft
    from novacode_cli.tracking.file_tracker import FileReadRecord, FileWriteRecord

    tracker = ft.SessionFileTracker()
    read = FileReadRecord(
        path="C:/w/a.py", content_hash="h", line_count=10, char_count=100, read_at="now"
    )
    tracker.files_read[read.path] = read
    tracker.read_order.append(read.path)
    tracker.total_reads = 1
    write = FileWriteRecord(
        path="C:/w/a.py", operation="edit", content_hash="h", written_at="2026-01-01T00:00:00Z"
    )
    tracker.files_written[write.path] = [write]
    tracker.write_order.append(write.path)
    tracker.total_writes = 1
    monkeypatch.setattr(ft, "get_session_tracker", lambda: tracker)
    port, _ = _port()
    result = await port.run("/files", None)
    assert result.ok is True
    text = result.blocks[0].text
    assert "1 file(s) / 1 op(s)" in text
    assert "a.py" in text


class _FakeManager:
    def __init__(self, snapshots: list[tuple[str, object]]) -> None:
        self.snapshots = snapshots
        self.restored: list[object] = []

    def list_snapshots(self, *, include_past_sessions: bool = True) -> list[tuple[str, object]]:  # noqa: ARG002
        return self.snapshots

    def restore(self, entry: object, session_id: str | None = None) -> bool:  # noqa: ARG002
        self.restored.append(entry)
        return True


class _Entry:
    def __init__(self, path: str, reason: str, timestamp: str) -> None:
        self.original_path = path
        self.reason = reason
        self.timestamp = timestamp
        self.snapshot_file = "snap"


async def test_restore_by_path_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import recovery

    entry = _Entry("C:/w/alpha.py", "edit_file", "2026-01-01T00:00:00")
    manager = _FakeManager([("s1", entry)])
    monkeypatch.setattr(recovery, "get_recovery_manager", lambda: manager)
    port, _ = _port()
    result = await port.run("/restore", "alpha")
    assert result.ok is True
    assert manager.restored == [entry]


async def test_restore_by_index_and_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import recovery

    entry = _Entry("C:/w/beta.py", "rm-command", "2026-01-01T00:00:00")
    manager = _FakeManager([("s1", entry)])
    monkeypatch.setattr(recovery, "get_recovery_manager", lambda: manager)
    port, _ = _port()
    by_index = await port.run("/restore", "1")
    assert by_index.ok is True
    miss = await port.run("/restore", "99")
    assert miss.ok is False
    assert "no snapshot at index" in miss.message.lower()


async def test_restore_picker_and_empty_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import recovery

    entry = _Entry("C:/w/gamma.py", "write_file", "2026-01-01T00:00:00")
    manager = _FakeManager([("s1", entry)])
    monkeypatch.setattr(recovery, "get_recovery_manager", lambda: manager)
    import novacode_cli.ui.port_server as ps

    monkeypatch.setattr(ps, "_restore_manager_for_test", manager, raising=False)
    port, asked, _sent = _screen_port([{"id": "0"}])
    result = await port.run("/restore", None)
    assert result.ok is True
    assert manager.restored == [entry]
    assert asked
    assert asked[0].kind == "list"

    empty = _FakeManager([])
    monkeypatch.setattr(recovery, "get_recovery_manager", lambda: empty)
    none = await port.run("/restore", None)
    assert none.ok is False
    assert "no file snapshots" in none.message.lower()
