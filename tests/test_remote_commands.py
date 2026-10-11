"""Remote commands use normal handlers and scoped, interactive command panels."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    OptionList,
    Select,
    Static,
    Switch,
    TextArea,
    Tree,
)

from novacode_cli.remote.bridge import RemotePlatform
from novacode_cli.remote.command_ui import RemoteCommandPanel
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


def message(text=""):
    return SimpleNamespace(
        platform=RemotePlatform.TELEGRAM,
        chat_id=7,
        thread_id=42,
        sender_id="alice",
        text=text,
        reply_fn=AsyncMock(),
    )


class CommandScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        yield Static("Choose settings")
        yield Input(value="secret-token", password=True, id="password")
        yield OptionList("Skill one", "Skill two", id="skills")
        yield Button("Save", id="save")

    def on_button_pressed(self, event):
        self.dismiss(self.query_one(Input).value)


async def test_panels_show_choices_mask_passwords_and_accept_values():
    app = _app()
    msg = message()
    async with app.run_test() as pilot:
        panel = RemoteCommandPanel(app, msg)
        screen = CommandScreen()
        await app.push_screen(screen)
        await pilot.pause()
        await panel.refresh()
        body = msg.reply_fn.call_args.args[0]
        assert "Skill one" in body
        assert "Skill two" in body
        assert "secret-token" not in body
        input_index = next(
            i + 1 for i, (_w, kind, _v) in enumerate(panel.actions) if kind == "input"
        )
        assert await panel.handle(message(f"/set@nova_bot {input_index} new-secret"))
        assert screen.query_one(Input).value == "new-secret"
        button_index = next(
            i + 1 for i, (_w, kind, _v) in enumerate(panel.actions) if kind == "button"
        )
        assert await panel.handle(message(f"/choose@nova_bot {button_index}"))
        assert panel.closed
        assert app.screen is panel.original


async def test_panel_is_scoped_to_sender_and_topic_and_rejects_stale_choices():
    app = _app()
    async with app.run_test() as pilot:
        panel = RemoteCommandPanel(app, message())
        await app.push_screen(CommandScreen())
        await pilot.pause()
        await panel.refresh()
        wrong = message("/choose 4")
        wrong.sender_id = "bob"
        assert not await panel.handle(wrong)
        wrong.sender_id, wrong.thread_id = "alice", 99
        assert not await panel.handle(wrong)
        invalid = message("/choose -1")
        assert await panel.handle(invalid)
        assert "Choose a number" in invalid.reply_fn.call_args.args[0]
        await panel.handle(message("/back"))
        assert panel.closed


async def test_remote_builtin_commands_and_aliases_reach_the_tui(monkeypatch):
    app = _app()
    run = AsyncMock()
    monkeypatch.setattr(app, "_run_remote_command", run)
    for text in ("/skills", "/model", "/mcp", "/settings", "/themes", "/plan status"):
        reply, handler = await app._remote_slash(text)
        assert reply is None
        await handler()
    assert run.await_args_list[0].args == ("/skills",)
    assert run.await_args_list[4].args == ("/theme",)
    assert "/skills" in app._remote_help_text()
    assert "local-only" not in app._remote_help_text()


async def test_named_and_explicit_remote_skills_keep_arguments_and_pinned_prompt(monkeypatch):
    resolve = AsyncMock(return_value=SimpleNamespace(prompt="full", pinned_prompt="pinned"))
    monkeypatch.setattr("novacode_cli.commands.skill_invoke._try_skill_invocation", resolve)
    app = _app()
    for text in ("/review fix.py", "/skill:review fix.py", "/review@nova_bot fix.py"):
        reply, prompt = await app._remote_slash(text)
        assert reply is None
        assert prompt == "pinned"
        assert resolve.call_args.args[:2] == ("review", "fix.py")


async def test_command_waiting_for_panel_can_be_completed_remotely(monkeypatch):
    app = _app()
    msg = message("/skills")
    app._remote_msg = msg

    async def command(_text):
        await app.push_screen_wait(CommandScreen())
        app._log("Saved remotely")

    monkeypatch.setattr(app, "_run_slash", command)
    async with app.run_test() as pilot:
        worker = app.run_worker(app._run_remote_command("/skills"))
        for _ in range(40):
            await pilot.pause(0.05)
            if msg.reply_fn.await_count:
                break
        panel = app._remote_command_panel
        assert panel.current is not None
        button_index = next(
            i + 1 for i, (_w, kind, _v) in enumerate(panel.actions) if kind == "button"
        )
        await app._remote_route(message(f"/choose {button_index}"))
        await asyncio.wait_for(worker.wait(), 5)
        assert any("Saved remotely" in call.args[0] for call in msg.reply_fn.await_args_list)
        assert panel.closed


class ControlsScreen(ModalScreen):
    def compose(self):
        yield Switch(id="enabled")
        yield TextArea("old", id="editor")
        yield Select([("First", 1), ("Second", 2)], value=1)
        yield DataTable()
        yield Tree("Root")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_column("Item")
        table.add_rows([(f"Row {i}: a longer label for pagination coverage",) for i in range(150)])
        self.query_one(Tree).root.add("Branch").add_leaf("Leaf")


async def test_remote_controls_and_large_panels():
    app = _app()
    msg = message()
    async with app.run_test() as pilot:
        panel = RemoteCommandPanel(app, msg)
        screen = ControlsScreen()
        await app.push_screen(screen)
        await pilot.pause()
        await panel.refresh()

        def choice(kind, value=None):
            return next(
                i + 1
                for i, (_, k, v) in enumerate(panel.actions)
                if k == kind and (value is None or v == value)
            )

        await panel.handle(message(f"/choose {choice('toggle')}"))
        assert screen.query_one(Switch).value
        await panel.handle(message(f"/set {choice('text')} new content"))
        assert screen.query_one(TextArea).text == "new content"
        await panel.handle(message(f"/choose {choice('select', 2)}"))
        assert screen.query_one(Select).value == 2
        assert sum(k == "row" for _, k, _ in panel.actions) == 150
        await panel.handle(message(f"/choose {choice('row', 149)}"))
        assert screen.query_one(DataTable).cursor_row == 149
        await panel.handle(message(f"/choose {choice('tree')}"))
        assert any("Leaf" in str(v.label) for _, k, v in panel.actions if k == "tree")
        assert await panel.handle(message("/panel 2"))
        assert "Page 2/" in msg.reply_fn.call_args.args[0]
        await panel.close()


async def test_remote_commands_wait_for_active_turn_and_bound_queue():
    app = _app()
    queue = asyncio.Queue()
    app._turn_active = True
    first = message("/skills")
    await app._defer_remote_slash(first, queue)
    assert queue.empty()
    for _ in range(31):
        await app._defer_remote_slash(message("/model"), queue)
    overflow = message("/skills")
    await app._defer_remote_slash(overflow, queue)
    assert "full" in overflow.reply_fn.call_args.args[0]
    app._turn_active = False
    assert await asyncio.wait_for(queue.get(), 2) is first
    await asyncio.gather(*tuple(app._remote_command_waiters))
    await asyncio.sleep(0)
    assert not app._remote_command_waiters


async def test_failed_command_cleans_up_panel_and_output_context(monkeypatch):
    import pytest

    from novacode_cli.remote.command_ui import command_output

    app = _app()
    app._remote_msg = message()

    async def fail(_text):
        raise RuntimeError("failure")

    monkeypatch.setattr(app, "_run_slash", fail)
    async with app.run_test():
        with pytest.raises(RuntimeError, match="failure"):
            await app._run_remote_command("/skills")
        assert app._remote_command_panel.closed
        assert app._remote_command_panel.task is None
        assert command_output.get() is None


async def test_expired_panel_releases_waiting_command(monkeypatch):
    app = _app()
    app._remote_msg = message("/skills")

    async def command(_text):
        await app.push_screen_wait(CommandScreen())

    monkeypatch.setattr(app, "_run_slash", command)
    async with app.run_test() as pilot:
        worker = app.run_worker(app._run_remote_command("/skills"))
        for _ in range(40):
            await pilot.pause(0.05)
            panel = getattr(app, "_remote_command_panel", None)
            if panel is not None and panel.current is not None:
                break
        assert panel.current is not None
        panel.expires = 0
        await asyncio.wait_for(worker.wait(), 5)
        assert panel.closed
        assert app.screen is panel.original
