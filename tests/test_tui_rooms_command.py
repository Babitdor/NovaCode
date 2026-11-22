"""`/rooms` read/write room actions over `RoomStore`.

The store is a fake that records calls; no project database is touched. Every
menu action maps to one store method and returns to the picker.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from novacode_cli.ui.port_server import ServerPort


class _Store:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[tuple[str, str, str]] = []
        self.created: list[str] = []
        self.updated: list[tuple[str, str, str]] = []
        self.changed: list[tuple[str, int, str, str]] = []
        self.reacted: list[tuple[str, int, str, str]] = []

    def rows(self, _sql: str, _args: tuple = ()) -> list[dict[str, Any]]:
        return [
            {"id": "general", "name": "#general", "topic": "Project chat"},
            {"id": "dm:nova", "name": "Nova", "topic": ""},
        ]

    def history(self, room: str) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "author": "owner",
                "text": f"Hello {room}",
                "created_at": "2026-01-01 10:00",
                "reactions": [{"emoji": "👍"}, {"emoji": "👍"}],
            }
        ]

    def board(self, room: str, _agent: str) -> dict[str, Any]:
        return {
            "tasks": [{"id": "t1", "status": "active", "goal": f"Ship {room}"}],
            "notes": [{"author": "nova", "kind": "note", "body": "Remember the tests."}],
        }

    def message(self, room: str, author: str, text: str) -> int:
        self.sent.append((room, author, text))
        return 1

    def change_message(self, room: str, key: int, action: str, text: str = "") -> None:
        self.changed.append((room, key, action, text))

    def react(self, room: str, key: int, author: str, emoji: str) -> None:
        self.reacted.append((room, key, author, emoji))

    def create_channel(self, name: str) -> str:
        self.created.append(name)
        return name

    def update_channel(self, room: str, name: str, topic: str) -> None:
        self.updated.append((room, name, topic))

    def close(self) -> None:
        self.closed = True


def _worker() -> SimpleNamespace:
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


def _port(answers: list[object]) -> tuple[ServerPort, list[Any], _Store]:
    asked: list[Any] = []
    queue = list(answers)
    store = _Store()

    async def ask_screen(spec: Any) -> Any:  # noqa: ANN401 - the answer is the caller's
        asked.append(spec)
        return queue.pop(0) if queue else None

    port = ServerPort(lambda _frame: None, _worker(), ask_screen=ask_screen)
    port._rooms_store_instance = store
    return port, asked, store


async def test_rooms_lists_new_channels_and_close() -> None:
    port, asked, _ = _port([None])
    result = await port.run("/rooms", None)
    assert result.ok is True
    items = asked[0].payload["items"]
    assert [item["id"] for item in items] == ["__new__", "general", "dm:nova", "__close__"]


async def test_read_opens_the_transcript() -> None:
    port, asked, _ = _port([{"id": "general"}, {"id": "read"}, None, {"id": "__close__"}])
    await port.run("/rooms", None)
    details = [spec for spec in asked if spec.kind == "detail"]
    assert details
    assert "Hello general" in details[0].payload["markdown"]
    assert "👍 x2" in details[0].payload["markdown"]


async def test_send_persists_and_reopens_the_transcript() -> None:
    port, asked, store = _port(
        [
            {"id": "general"},
            {"id": "send"},
            {"text": "Status?"},
            None,
            {"id": "__close__"},
        ]
    )
    await port.run("/rooms", None)
    assert store.sent == [("general", "owner", "Status?")]
    assert any(spec.kind == "detail" for spec in asked)


async def test_board_renders_tasks_and_notes() -> None:
    port, asked, _ = _port([{"id": "general"}, {"id": "board"}, None, {"id": "__close__"}])
    await port.run("/rooms", None)
    details = [spec for spec in asked if spec.kind == "detail"]
    assert details
    assert "Ship general" in details[0].payload["markdown"]
    assert "Remember the tests." in details[0].payload["markdown"]


async def test_settings_saves_name_and_topic() -> None:
    port, _, store = _port(
        [
            {"id": "general"},
            {"id": "settings"},
            {"name": "renamed", "topic": "New topic"},
            {"id": "__close__"},
        ]
    )
    await port.run("/rooms", None)
    assert store.updated == [("general", "renamed", "New topic")]


async def test_new_channel_creates_it() -> None:
    port, _, store = _port([{"id": "__new__"}, {"name": "ops"}, {"id": "__close__"}])
    await port.run("/rooms", None)
    assert store.created == ["ops"]


async def test_close_releases_the_store() -> None:
    port, _, store = _port([None])
    await port.run("/rooms", None)
    await port.close()
    assert store.closed is True


async def test_subcommands_refuse_explicitly() -> None:
    port, asked, _ = _port([])
    result = await port.run("/rooms", "send hello")
    assert result.ok is False
    assert "not ported" in result.message.lower()
    assert asked == []


async def test_message_edit_calls_change_message() -> None:
    port, _, store = _port(
        [
            {"id": "general"},
            {"id": "messages"},
            {"id": "1"},
            {"id": "edit"},
            {"text": "edited"},
            None,
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    await port.run("/rooms", None)
    assert store.changed == [("general", 1, "edit", "edited")]


async def test_message_react_and_pin_call_the_store() -> None:
    port, _, store = _port(
        [
            {"id": "general"},
            {"id": "messages"},
            {"id": "1"},
            {"id": "react"},
            {"emoji": "🎉"},
            {"id": "1"},
            {"id": "pin"},
            None,
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    await port.run("/rooms", None)
    assert store.reacted == [("general", 1, "owner", "🎉")]
    assert store.changed == [("general", 1, "pin", "")]


async def test_message_delete_requires_confirmation() -> None:
    port, _, store = _port(
        [
            {"id": "general"},
            {"id": "messages"},
            {"id": "1"},
            {"id": "delete"},
            True,
            None,
            {"id": "back"},
            {"id": "__close__"},
        ]
    )
    await port.run("/rooms", None)
    assert store.changed == [("general", 1, "delete", "")]
