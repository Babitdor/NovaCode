"""Markdown reply panels and prompt attachment use explicit, pane-owned actions."""

from unittest.mock import AsyncMock

from rich.markdown import Markdown
from textual.widgets import Collapsible

from novacode_cli import ui_events as ev
from tests.test_cross_session_lifecycle import wait_until
from tests.test_cross_session_tui import setup
from tests.test_tui_sessions import _app, isolated_session_config  # noqa: F401


async def reply_panel(
    app, monkeypatch, response="## Login\n\n**Use tokens**\n\n```python\na = 1\n```"
):
    pane, send, controller = await setup(app, monkeypatch)
    controller.refresh()
    message = controller.broker.send("root", "b", "Explain login")
    await controller.notice(message)
    notice = controller.notices[("root", message.id)]
    assert notice.attach_button.disabled
    controller.broker.start(message.id)
    controller.broker.complete(message.id, response)
    await controller.notice(message)
    return pane, controller, message, notice


async def test_reply_is_markdown_and_collapses_without_switching_tabs(monkeypatch):
    app = _app()
    async with app.run_test() as pilot:
        pane, controller, message, notice = await reply_panel(app, monkeypatch)
        assert isinstance(notice, Collapsible)
        assert notice.collapsed
        assert isinstance(notice.body.content, Markdown)
        await pilot.pause()
        notice.query_one("CollapsibleTitle").scroll_visible(animate=False, force=True)
        await pilot.pause()
        assert await pilot.click(notice.query_one("CollapsibleTitle"))
        await pilot.pause()
        assert not notice.collapsed
        assert app._active_pane is app._root_pane
        await controller.notice(message)
        assert not notice.collapsed, "status refresh must preserve the user's expansion"
        await pilot.pause()
        notice.query_one("CollapsibleTitle").scroll_visible(animate=False, force=True)
        await pilot.pause()
        assert await pilot.click(notice.query_one("CollapsibleTitle"))
        await pilot.pause()
        assert notice.collapsed
        await controller.close()


async def test_attach_preserves_draft_markdown_and_running_turn_without_sending(monkeypatch):
    app = _app()
    async with app.run_test() as pilot:
        pane, controller, message, notice = await reply_panel(app, monkeypatch)
        prompt = app.query_one("#prompt")
        prompt.value = "Use this answer to update my code.  "
        original = prompt.value
        dispatch = AsyncMock()
        monkeypatch.setattr(app, "_dispatch", dispatch)
        app._turn_active = True
        notice.collapsed = False
        await pilot.pause()
        assert not notice.attach_button.disabled
        notice.attach_button.scroll_visible(animate=False, force=True)
        await pilot.pause()
        assert await pilot.click(notice.attach_button)
        await pilot.pause()
        expected = (
            original + f"\n\n[Cross-session reply {message.id} from backend]\n{message.response}"
        )
        assert prompt.value == expected
        assert prompt.has_focus
        assert app._turn_active and app._active_pane is app._root_pane
        dispatch.assert_not_called()
        notice.attach_to_prompt()
        assert prompt.value == expected, "repeat clicks must not duplicate the attachment"
        await controller.close()


async def test_hidden_card_cannot_attach_to_another_tabs_draft(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, controller, message, notice = await reply_panel(app, monkeypatch)
        await app._switch_to(pane)
        prompt = app.query_one("#prompt")
        prompt.value = "Destination draft"
        notice.attach_to_prompt()
        assert prompt.value == "Destination draft"
        own_notice = controller.notices[(pane.sid, message.id)]
        own_notice.attach_to_prompt()
        assert prompt.value.endswith(message.response)
        assert app._active_pane is pane
        await controller.close()


async def test_request_without_response_cannot_attach_or_change_draft(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        controller.refresh()
        message = controller.broker.send("root", "b", "Question")
        await controller.notice(message)
        notice = controller.notices[("root", message.id)]
        prompt = app.query_one("#prompt")
        prompt.value = "My draft"
        assert notice.attach_button.disabled
        notice.attach_to_prompt()
        assert prompt.value == "My draft"
        await controller.close()


async def test_open_button_navigates_only_when_explicitly_pressed(monkeypatch):
    app = _app()
    async with app.run_test() as pilot:
        pane, controller, message, notice = await reply_panel(app, monkeypatch)
        notice.collapsed = False
        await pilot.pause()
        notice.open_button.scroll_visible(animate=False, force=True)
        await pilot.pause()
        assert await pilot.click(notice.open_button)
        await wait_until(lambda: app._active_pane is pane)
        await controller.close()


async def test_child_linked_reply_uses_latest_answer_not_concatenated_commentary(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)
        result = await controller.handle("root", "send", {"target": "b", "text": "Q"})
        await wait_until(lambda: bool(controller.child_turns))
        controller.child_answer("b", "I'll investigate", "p-peer")
        controller.child_answer("b", "## Final answer\n\nUse tokens.", "p-peer")
        controller.child_done("b", "p-peer", True)
        await wait_until(lambda: controller.broker.messages[result["id"]].status == "completed")
        assert controller.broker.messages[result["id"]].response == "## Final answer\n\nUse tokens."
        await controller.close()


async def test_main_linked_reply_excludes_intermediate_and_subagent_answers(monkeypatch):
    app = _app()
    async with app.run_test():
        pane, send, controller = await setup(app, monkeypatch)

        async def stream(*args, **kwargs):
            yield ev.AssistantMessage(
                text="I'll investigate", agent_name="Nova", agent_color="cyan"
            )
            yield ev.AssistantMessage(text="## Answer", agent_name="Nova", agent_color="cyan")
            yield ev.AssistantMessage(
                text="Subagent output", agent_name="helper", agent_color="cyan", is_subagent=True
            )

        monkeypatch.setattr("novacode_cli.agent_stream.run_agent_stream", stream)
        monkeypatch.setattr(app, "_save_session", AsyncMock())
        result = await controller.handle("b", "send", {"target": "root", "text": "Q"})
        await wait_until(lambda: controller.broker.messages[result["id"]].status == "completed")
        assert controller.broker.messages[result["id"]].response == "## Answer"
        await controller.close()
