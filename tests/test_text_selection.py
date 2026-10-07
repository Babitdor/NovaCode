"""Rendered transcript text selects like document text.

Dragging inside a Markdown reply or a command's output must select exactly the
characters under the drag. It used to select from the top of the widget, because
Rich-rendered lines carried no per-character offsets for Textual to map the
mouse position onto.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "tests")

REPLY = "alpha line one\n\nbravo line two\n\ncharlie line three\n\ndelta line four"


def _app():
    import test_tui_app as T

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.ui.ui_elements import TokenTracker

    return NovaApp(
        agent=T._FakeAgent(), assistant_id="nova-agent", session_state=T._SS(), backend=None,
        token_tracker=TokenTracker(), image_tracker=None, model_name="m", session_manager=None,
    )


async def _drag(pilot, widget, start: tuple[int, int], end: tuple[int, int]) -> str:
    await pilot.mouse_down(widget, offset=start)
    await pilot.hover(widget, offset=end)
    await pilot.mouse_up(widget, offset=end)
    await pilot.pause()
    return pilot.app.screen.get_selected_text() or ""


def test_part_of_a_markdown_reply_can_be_selected():
    from novacode_cli.tui.widgets import CachedMarkdown as Markdown

    async def drive() -> tuple[str, str]:
        app = _app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            msg = await app._add_message(app._agent_label("Nova"), "nova", Markdown(REPLY))
            for _ in range(4):
                await pilot.pause()
            body = msg.query_one(".body")
            # Markdown renders the four paragraphs on rows 0, 2, 4, 6.
            within = await _drag(pilot, body, (6, 2), (9, 2))
            # The selection must be visible: a highlight behind exactly the
            # selected cells, with the text still readable on top of it.
            painted = {seg.text: seg.style for seg in body.render_line(2)}
            highlight = painted["line"].bgcolor
            assert highlight is not None and highlight != painted["bravo "].bgcolor, painted
            assert painted["line"].color != highlight, "selected text must not vanish into the highlight"
            app.screen.clear_selection()
            across = await _drag(pilot, body, (6, 2), (6, 4))
            return within, across

    within, across = asyncio.run(drive())
    assert within == "line", within
    assert across == "line two\n\ncharlie", across


def test_lines_of_command_output_can_be_selected():
    from novacode_cli.tui.widgets import OutputLog

    cmd = f'"{sys.executable}" -c "print(chr(10).join(\'row-%02d value\' % i for i in range(8)))"'

    async def drive() -> str:
        app = _app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await app._run_bash("!" + cmd)
            await app.workers.wait_for_complete()
            for _ in range(6):
                await pilot.pause()
            log = app.query_one(".bash-inline").query_one(OutputLog)
            # Output sits after the 5-cell gutter: "row-02 value" starts at x=5.
            return await _drag(pilot, log, (5, 2), (10, 3))

    assert asyncio.run(drive()) == "row-02 value\n     row-03", "exactly the dragged span"
