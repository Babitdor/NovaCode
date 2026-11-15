"""Discord-style room interactions preserve persistence and execution boundaries."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Input, Static

from novacode_cli.rooms import cli
from novacode_cli.rooms.runtime import RoomRuntime
from novacode_cli.rooms.store import RoomStore
from novacode_cli.tui.room_message import RoomMessage
from novacode_cli.tui.rooms import RoomsScreen
from novacode_cli.tui.widgets import CachedMarkdown, PromptInput


def test_existing_rooms_migrate_without_losing_messages_or_drafts(tmp_path: Path):
    directory = tmp_path / ".nova" / "workspace"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "rooms.sqlite3") as db:
        db.executescript("""
            CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
                author TEXT NOT NULL, text TEXT NOT NULL, job TEXT);
            INSERT INTO messages(room,author,text) VALUES('general','owner','Original message');
            CREATE TABLE views(viewer TEXT, room TEXT, draft TEXT DEFAULT '', seen INTEGER DEFAULT 0,
                PRIMARY KEY(viewer,room));
            INSERT INTO views VALUES('owner','general','Unsent draft',1);
        """)
    for _ in range(2):
        store = RoomStore(tmp_path)
        assert store.history("general")[0]["text"] == "Original message"
        assert store.view("owner", "general")["draft"] == "Unsent draft"
        assert store.view("owner", "general")["seen"] == 1
        store.clear_draft("owner", "general", "Older draft")
        assert store.view("owner", "general")["draft"] == "Unsent draft"
        store.close()


def test_replies_threads_reactions_edits_and_deletion_are_room_scoped(tmp_path: Path):
    store = RoomStore(tmp_path)
    root = store.message("general", "owner", "A **proposal**")
    other = store.message("dm:codex", "codex", "Private material")
    reply = store.message("general", "claude", "Looks good", reply_to=root)
    thread = store.message("general", "owner", "Details", thread_id=root)
    assert store.message_record("general", reply)["reply_to"] == root
    assert store.message_record("general", root)["created_at"].endswith("Z")
    for reference in ({"reply_to": other}, {"thread_id": other}, {"thread_id": thread}):
        with pytest.raises(ValueError):
            store.message("general", "owner", "Invalid reference", **reference)
    with pytest.raises(ValueError, match="unauthorized"):
        store.react("dm:codex", other, "claude", "👍")
    with pytest.raises(ValueError, match="supported"):
        store.react("general", root, "owner", "bad")
    store.react("general", root, "owner", "👍")
    store.react("general", root, "claude", "👍")
    store.react("general", root, "owner", "👍")
    assert store.history("general")[0]["reactions"] == [
        {"message": root, "author": "claude", "emoji": "👍"},
    ]
    with pytest.raises(ValueError, match="own messages"):
        store.change_message("general", reply, "edit", "Forged text")
    store.change_message("general", root, "edit", "Updated proposal")
    store.change_message("general", root, "pin")
    assert store.message_record("general", root)["pinned"]
    assert store.message_record("general", root)["edited_at"]
    store.change_message("general", root, "delete")
    deleted = store.history("general")[0]
    assert deleted["deleted"] and not deleted["text"] and not deleted["reactions"]
    # Existing threads survive deletion of their starter.
    store.message("general", "owner", "Still discussing", thread_id=root)
    assert store.context("general", "claude")["messages"][0]["id"] == reply
    assert [m["text"] for m in store.context("general", "claude", root)["messages"]] == [
        "Details",
        "Still discussing",
    ]
    store.close()


@pytest.mark.asyncio
async def test_agent_thread_replies_and_native_sessions_stay_in_the_thread(tmp_path, monkeypatch):
    observed = []

    async def fake(agent, prompt, cwd, session, **kwargs):
        observed.append((json.loads(prompt), session))
        return cli.Turn(text="## Findings\n\n**Verified**", session=f"native-{len(observed)}")

    monkeypatch.setattr(cli, "run", fake)
    store = RoomStore(tmp_path)
    root = store.message("general", "owner", "Thread starter")
    store.message("general", "owner", "Outside thread")
    runtime = RoomRuntime(store)
    await runtime.open()
    try:
        await runtime.send("general", "@claude inspect this", thread_id=root)
        for _ in range(100):
            jobs = await runtime.db("rows", "SELECT status FROM jobs")
            if jobs and all(job["status"] == "finished" for job in jobs):
                break
            await asyncio.sleep(0.03)
        else:
            pytest.fail("Thread agent did not finish")
        history = store.history("general")
        assert history[-1]["thread_id"] == root
        assert history[-1]["text"].startswith("## Findings")
        assert "Outside thread" not in str(observed[0][0]["shared_messages"])
        await runtime.send("general", "@claude inspect channel")
        for _ in range(100):
            jobs = await runtime.db("rows", "SELECT status FROM jobs")
            if all(job["status"] == "finished" for job in jobs):
                break
            await asyncio.sleep(0.03)
        assert len(observed) == 2
        assert observed[0][1] is None and observed[1][1] is None
        assert store.history("general")[-1]["thread_id"] is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_room_markdown_search_pins_threads_drafts_and_multiline(tmp_path, monkeypatch):
    async def no_call(*args, **kwargs):
        pytest.fail("Browsing or editing must not run an agent")

    monkeypatch.setattr(cli, "run", no_call)
    store = RoomStore(tmp_path)
    root = store.message("general", "claude", "## Plan\n\n**Build** this:\n\n```python\nx = 1\n```")
    own = store.message("general", "owner", "Original wording")
    store.message("general", "claude", "Thread-only material", thread_id=root)
    store.close()
    app = App()
    async with app.run_test(size=(140, 42)) as pilot:
        screen = RoomsScreen(tmp_path, "viewer")
        await app.push_screen(screen)
        await pilot.pause()
        assert not str(screen.query_one("#room-status", Static).render())
        widget = screen.query_one(f"#message-{root}", RoomMessage)
        assert isinstance(widget.query_one(".room-message-body", Static).content, CachedMarkdown)
        assert screen.query_one("#room-members").display
        assert not screen.message_widgets[3].display
        composer = screen.query_one("#room-composer", PromptInput)
        composer.value = "Unsent channel draft"
        await pilot.pause()
        await screen.message_action(f"thread-{root}")
        await pilot.pause()
        assert screen.message_widgets[3].display
        composer.value = "Thread draft"
        composer.cursor_position = len(composer.value)
        await pilot.press("shift+enter")
        assert composer.value.endswith("\n")
        await screen.switch("general")
        assert composer.value == "Unsent channel draft"
        await screen.message_action(f"thread-{root}")
        assert composer.value == "Thread draft\n"
        await screen.switch("general")
        await screen.message_action(f"edit-{own}")
        composer.value = "Edited **wording**"
        await screen.send()
        assert composer.value == "Unsent channel draft"
        assert screen.runtime.store.message_record("general", own)["text"] == "Edited **wording**"
        await screen.message_action(f"pin-{root}")
        await screen.message_action(f"react0-{root}")
        assert str(screen.query_one(f"#react0-{root}", Button).label) == "👍 1"
        screen.pins_only = True
        screen.filter_messages()
        assert screen.message_widgets[root].display
        assert not screen.message_widgets[own].display
        screen.pins_only = False
        screen.query_one("#room-search", Input).value = "wording"
        await pilot.pause()
        assert screen.message_widgets[own].display
        assert not screen.message_widgets[root].display
        assert composer.value == "Unsent channel draft"
        await pilot.resize_terminal(55, 20)
        assert not screen.query_one("#room-members").display
        assert not screen.query_one("#room-sidebar").display
        await screen.action_sessions()


@pytest.mark.asyncio
async def test_poll_and_channel_switch_cannot_mix_transcripts(tmp_path, monkeypatch):
    store = RoomStore(tmp_path)
    store.message("general", "claude", "Public transcript")
    store.message("dm:codex", "codex", "Private transcript")
    store.close()
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen = RoomsScreen(tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        entered, release = asyncio.Event(), asyncio.Event()
        original = screen.runtime.db

        async def delayed(method, *args, **kwargs):
            if method == "history" and args[0] == "general":
                entered.set()
                await release.wait()
            return await original(method, *args, **kwargs)

        monkeypatch.setattr(screen.runtime, "db", delayed)
        polling = asyncio.create_task(screen.poll())
        await asyncio.wait_for(entered.wait(), 3)
        switching = asyncio.create_task(screen.switch("dm:codex"))
        release.set()
        await asyncio.gather(polling, switching)
        await pilot.pause()
        assert screen.room == "dm:codex"
        assert all(w.record["room"] == "dm:codex" for w in screen.message_widgets.values())
        assert len(screen.message_widgets) == 1
        await screen.action_sessions()


@pytest.mark.asyncio
async def test_keyboard_send_replies_and_delete_confirmation(tmp_path, monkeypatch):
    async def fake(*args, **kwargs):
        return cli.Turn(text="**Acknowledged**")

    monkeypatch.setattr(cli, "run", fake)
    store = RoomStore(tmp_path)
    root = store.message("general", "claude", "Please review")
    store.close()
    app = App()
    async with app.run_test(size=(140, 42)) as pilot:
        screen = RoomsScreen(tmp_path)
        await app.push_screen(screen)
        await pilot.pause()
        await screen.message_action(f"reply-{root}")
        composer = screen.query_one("#room-composer", PromptInput)
        composer.value = "@claude Reviewed"
        composer.cursor_position = len(composer.value)
        await pilot.press("shift+enter")
        await pilot.press("o", "k", "enter")
        for _ in range(30):
            messages = await screen.runtime.db("history", "general")
            if len(messages) == 3:
                break
            await pilot.pause()
        own = next(m for m in messages if m["author"] == "owner")
        assert own["text"] == "@claude Reviewed\nok"
        assert own["reply_to"] == root
        assert not composer.value
        await screen.poll()
        delete = screen.query_one(f"#delete-{own['id']}", Button)
        delete.scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click(delete)
        await pilot.pause()
        await pilot.press("n")
        await app.workers.wait_for_complete()
        assert not screen.runtime.store.message_record("general", own["id"])["deleted"]
        delete.scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click(delete)
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        assert next(m for m in screen.runtime.store.history("general") if m["id"] == own["id"])[
            "deleted"
        ]
        await screen.action_sessions()
