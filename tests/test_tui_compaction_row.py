"""Context compaction shows in the chat: an animated row that becomes the outcome."""

import pytest
from textual.widgets import Static

from novacode_cli import ui_events as ev


def _app():
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    return NovaApp(
        agent=_FakeAgent(), assistant_id="nova-agent", session_state=_SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="deepseek-v4.1-flash",
    )


def _rows(app) -> list[str]:
    return [str(w.render()) for w in app.query(".nova-compaction").results(Static)]


@pytest.mark.asyncio
async def test_the_row_animates_then_says_it_compacted_once():
    app = _app()
    async with app.run_test(size=(110, 40)) as pilot:
        await app._render(ev.ContextMessage(message="Context is compacting", event_type="nova_compaction_start"))
        await pilot.pause(0.4)
        assert len(_rows(app)) == 1 and "Compacting context" in _rows(app)[0]

        await app._render(ev.ContextMessage(message="Context compacted", event_type="nova_compaction_end"))
        await app._render(ev.CompactionNotice())  # the turn's own notice
        await pilot.pause(0.3)
        assert _rows(app) == ["⟳ Context compacted"], "the same row, settled, not a second line"
        assert not any("Context compacted" in str(w.render()) for w in app.query(".logline").results(Static))


@pytest.mark.asyncio
async def test_a_turn_cancelled_mid_compaction_leaves_no_spinner():
    app = _app()
    async with app.run_test(size=(110, 40)) as pilot:
        await app._render(ev.ContextMessage(message="Context is compacting", event_type="nova_compaction_start"))
        await pilot.pause(0.2)
        await app._render(ev.Cancelled())
        await pilot.pause(0.2)
        assert _rows(app) == []
