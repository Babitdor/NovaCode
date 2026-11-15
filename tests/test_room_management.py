"""Owner message/channel controls fence workers and preserve unrelated conversations."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Input, Static, TextArea

from novacode_cli.rooms import cli
from novacode_cli.rooms.runtime import RoomRuntime
from novacode_cli.rooms.store import RoomStore
from novacode_cli.tui.room_settings import RoomSettingsModal
from novacode_cli.tui.rooms import RoomsScreen
from novacode_cli.tui.widgets import PromptInput


def test_channel_rename_keeps_identity_history_drafts_and_sessions(tmp_path: Path):
    store = RoomStore(tmp_path)
    key = store.create_channel("development")
    message = store.message(key, "owner", "Keep this history")
    store.view("viewer", key, "Keep this draft")
    job = store.enqueue(key, "claude", "Review", "runtime")
    row = store.rows("SELECT * FROM jobs WHERE id=?", (job,))[0]
    store.session(row, "native-session")
    store.update_channel(key, "engineering", "Build **carefully**")
    assert store.room(key)["name"] == "#engineering"
    assert store.room(key)["topic"] == "Build **carefully**"
    assert store.history(key)[0]["id"] == message
    assert store.view("viewer", key)["draft"] == "Keep this draft"
    assert store.session(row) == "native-session"
    # The old display name can be reused without changing the original channel's identity.
    fresh = store.create_channel("development")
    assert fresh != key and store.room(fresh)["name"] == "#development"
    with pytest.raises(ValueError, match="already exists"):
        store.update_channel(fresh, "engineering")
    with pytest.raises(ValueError, match="DMs"):
        store.update_channel("dm:codex", "forged")
    for name, topic in (("../escape", ""), ("ok", "x" * 1025)):
        with pytest.raises(ValueError):
            store.update_channel(key, name, topic)
    assert store.room(key)["name"] == "#engineering"
    store.close()
    reopened = RoomStore(tmp_path)
    assert reopened.room(key)["topic"] == "Build **carefully**"
    reopened.close()


def test_clear_thread_is_scoped_and_preserves_starter_other_threads_and_tasks(tmp_path: Path):
    store = RoomStore(tmp_path)
    root = store.message("general", "owner", "Starter")
    other = store.message("general", "owner", "Other starter")
    reply = store.message("general", "claude", "Thread reply", thread_id=root)
    keep = store.message("general", "codex", "Other thread reply", thread_id=other)
    quote = store.message("general", "owner", "Quote", reply_to=reply)
    store.react("general", reply, "owner", "👍")
    store.view("viewer", "general", "Unsent text")
    first = store.enqueue("general", "claude", "Request", "runtime", thread_id=root)
    store.finish(first, "paused")
    second = store.enqueue("general", "codex", "Request", "runtime", thread_id=other)
    rows = {r["id"]: r for r in store.rows("SELECT * FROM jobs")}
    store.session(rows[first], "first-native")
    store.session(rows[second], "second-native")
    note = store.publish("general", "claude", "finding", {"text": "Keep note"})
    assert store.clear_messages("general", root) == 1
    assert {r["id"] for r in store.history("general")} == {root, other, keep, quote}
    assert store.message_record("general", quote)["reply_to"] is None
    assert store.rows("SELECT * FROM reactions") == []
    assert store.session(rows[first]) is None
    assert store.session(rows[second]) == "second-native"
    assert store.rows("SELECT status FROM jobs WHERE id=?", (first,))[0]["status"] == "cancelled"
    assert store.rows("SELECT id FROM notes")[0]["id"] == note
    assert store.view("viewer", "general")["draft"] == "Unsent text"
    store.finish(second, "finished")
    assert store.clear_messages("general") == 4
    assert store.history("general") == []
    assert store.rows("SELECT id FROM notes")
    store.close()


@pytest.mark.parametrize("status", ["queued", "working", "cancelling"])
def test_busy_channel_cannot_be_cleared_or_deleted_from_another_store(tmp_path: Path, status: str):
    first, second = RoomStore(tmp_path), RoomStore(tmp_path)
    room = first.create_channel("busy")
    key = first.message(room, "owner", "Keep this")
    job = second.enqueue(room, "claude", "Run", "other-runtime")
    second.finish(job, status)
    for action in (first.clear_messages, first.delete_channel):
        with pytest.raises(ValueError, match="Cancel or pause"):
            action(room)
        assert first.history(room)[0]["id"] == key
    second.finish(job, "finished")
    first.delete_channel(room)
    assert second.claim(job) is None  # A scheduler may still hold the old queued ID.
    second.finish(job, "paused")  # Late cleanup must not resurrect deleted records.
    first.close()
    second.close()


def test_delete_channel_cleans_associations_but_keeps_files_and_protected_rooms(tmp_path: Path):
    store = RoomStore(tmp_path)
    room = store.create_channel("temporary")
    message = store.message(room, "claude", "Temporary")
    store.react(room, message, "owner", "👀")
    job = store.enqueue(room, "claude", "Request", "runtime")
    row = store.rows("SELECT * FROM jobs WHERE id=?", (job,))[0]
    store.session(row, "native")
    store.finish(job, "finished")
    store.view("viewer", room, "draft")
    store.publish(room, "claude", "finding", {"text": "Temporary note"})
    task = store.propose(room, "Preserve files", ["claude"])
    worktree = tmp_path / "kept-worktree"
    worktree.mkdir()
    marker = worktree / "important.txt"
    marker.write_text("Owner work", encoding="utf-8")
    with store.transaction():
        task["worktree"] = str(worktree)
        store.put_task(task)
    private = store.message("dm:codex", "codex", "Private history")
    for protected in ("general", "dm:nova", "dm:claude", "dm:codex"):
        with pytest.raises(ValueError, match="cannot be deleted"):
            store.delete_channel(protected)
    store.delete_channel(room)
    for table in ("messages", "jobs", "tasks", "notes", "views", "conversations"):
        assert store.rows(f"SELECT * FROM {table} WHERE room=?", (room,)) == []
    assert store.rows("SELECT * FROM reactions WHERE message=?", (message,)) == []
    assert store.history("dm:codex")[0]["id"] == private
    assert marker.read_text(encoding="utf-8") == "Owner work"
    with pytest.raises(ValueError, match="unauthorized"):
        store.view("viewer", room, "Cannot restore deleted room")
    store.close()
    reopened = RoomStore(tmp_path)
    assert not reopened.rows("SELECT id FROM rooms WHERE id=?", (room,))
    reopened.close()


def test_mark_all_read_preserves_drafts_and_other_viewers(tmp_path: Path):
    store = RoomStore(tmp_path)
    public = store.message("general", "claude", "Public")
    private = store.message("dm:codex", "codex", "Private")
    store.view("me", "general", "Unsent draft")
    store.view("other", "general", "Other draft")
    store.mark_read("me")
    assert store.view("me", "general")["seen"] == public
    assert store.view("me", "dm:codex")["seen"] == private
    assert store.view("me", "general")["draft"] == "Unsent draft"
    assert store.view("other", "general")["seen"] == 0
    store.close()


def test_failed_channel_delete_rolls_back_all_associated_records(tmp_path: Path):
    store = RoomStore(tmp_path)
    room = store.create_channel("rollback")
    key = store.message(room, "owner", "Keep message")
    store.react(room, key, "owner", "👍")
    store.publish(room, "claude", "finding", {"text": "Keep note"})
    store.db.execute(
        "CREATE TRIGGER reject_delete BEFORE DELETE ON notes "
        "BEGIN SELECT RAISE(ABORT,'note is busy'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="note is busy"):
        store.delete_channel(room)
    assert store.room(room)["id"] == room
    assert store.history(room)[0]["text"] == "Keep message"
    assert store.history(room)[0]["reactions"]
    assert store.rows("SELECT id FROM notes WHERE room=?", (room,))
    store.close()


@pytest.mark.asyncio
async def test_clear_and_delete_wait_until_worker_cancellation_cleanup_finishes(
    tmp_path, monkeypatch
):
    started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fake(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    monkeypatch.setattr(cli, "run", fake)
    runtime = RoomRuntime(RoomStore(tmp_path))
    room = runtime.store.create_channel("running")
    await runtime.open()
    try:
        await runtime.send(room, "@claude work")
        await asyncio.wait_for(started.wait(), 3)
        job = runtime.store.rows("SELECT id FROM jobs")[0]["id"]
        cancelling = asyncio.create_task(runtime.cancel(job))
        await asyncio.wait_for(cleanup.wait(), 3)
        assert runtime.store.rows("SELECT status FROM jobs")[0]["status"] == "cancelling"
        for action in ("clear_messages", "delete_channel"):
            with pytest.raises(ValueError, match="Cancel or pause"):
                await runtime.db(action, room)
        release.set()
        await cancelling
        await runtime.db("delete_channel", room)
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.timeout(40)
async def test_channel_menu_rename_clear_cancel_and_delete(tmp_path: Path):
    store = RoomStore(tmp_path)
    room = store.create_channel("testing")
    key = store.message(room, "claude", "A **message**")
    store.close()
    app = App()
    async with app.run_test(size=(140, 42)) as pilot:
        screen = RoomsScreen(tmp_path)
        await app.push_screen(screen)
        await screen.switch(room)
        await pilot.pause()
        composer = screen.query_one("#room-composer", PromptInput)
        composer.value = "Unsent draft"
        assert await pilot.click("#room-menu")
        await pilot.pause()
        assert isinstance(app.screen, RoomSettingsModal)
        app.screen.query_one("#channel-name", Input).value = "quality"
        app.screen.query_one("#channel-topic", TextArea).text = "Checks and findings"
        await pilot.pause()
        assert await pilot.click("#channel-save")
        await app.workers.wait_for_complete()
        # A timer refresh can overlap the save; assert against the next completed poll.
        while screen.refreshing:
            await pilot.pause()
        await screen.poll()
        await pilot.pause()
        assert screen.runtime.store.room(room)["name"] == "#quality"
        assert str(screen.query_one("#room-heading", Static).content) == "#quality"
        assert composer.value == "Unsent draft"
        copied = []
        app.copy_to_clipboard = copied.append
        copy_button = screen.query_one(f"#copy-{key}", Button)
        copy_button.scroll_visible(animate=False)
        await pilot.pause()
        background = copy_button.styles.background
        await pilot.hover(copy_button)
        await pilot.pause()
        assert copy_button.styles.background == background
        await pilot.click(f"#copy-{key}")
        await app.workers.wait_for_complete()
        assert copied == ["A **message**"]
        for approved in (False, True):
            await pilot.click("#room-menu")
            await pilot.pause()
            await pilot.click("#channel-clear")
            await pilot.pause()
            await pilot.press("y" if approved else "n")
            await app.workers.wait_for_complete()
            assert bool(screen.runtime.store.history(room)) is not approved
        assert not screen.message_widgets
        assert composer.value == "Unsent draft"
        await pilot.click("#room-menu")
        await pilot.pause()
        await pilot.click("#channel-delete")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        assert screen.room == "general"
        assert not screen.runtime.store.rows("SELECT * FROM rooms WHERE id=?", (room,))
        await screen.action_sessions()


@pytest.mark.asyncio
async def test_poll_recovers_from_external_channel_deletion_and_removes_cleared_messages(tmp_path):
    store = RoomStore(tmp_path)
    room = store.create_channel("shared")
    store.message(room, "owner", "Clear me")
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen = RoomsScreen(tmp_path)
        await app.push_screen(screen)
        await screen.switch(room)
        await pilot.pause()
        assert screen.message_widgets
        await asyncio.to_thread(store.clear_messages, room)
        await screen.poll()
        assert not screen.message_widgets
        await asyncio.to_thread(store.delete_channel, room)
        await screen.poll()
        assert screen.room == "general"
        assert not str(screen.query_one("#room-status", Static).content).startswith("Unknown")
        await screen.action_sessions()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("room", ["general", "dm:claude"])
async def test_protected_room_menu_is_keyboard_accessible_on_narrow_terminals(tmp_path, room):
    store = RoomStore(tmp_path)
    record = store.room(room)
    store.close()
    choices = []
    app = App()
    async with app.run_test(size=(55, 20)) as pilot:
        menu = RoomSettingsModal(record)
        await app.push_screen(menu, choices.append)
        await pilot.pause()
        assert menu.query_one("#channel-delete", Button).disabled
        assert menu.query_one("#channel-save", Button).disabled is room.startswith("dm:")
        clear = menu.query_one("#channel-clear", Button)
        clear.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert choices[0]["action"] == "clear"
