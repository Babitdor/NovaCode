"""A mouse move restyles the widgets whose hover changed, not their subtrees."""

from __future__ import annotations

import pytest

from novacode_cli import ui_events as ev
from tests.test_tui_compaction_row import _app


async def _cards(app, pilot, n=6):
    for k in range(n):
        await app._render(ev.ToolCall(name="read_file", display_str=f"read_file(f{k}.py)", icon="", call_id=f"c{k}"))
        await app._render(ev.ToolResult(call_id=f"c{k}", preview="ok", full_output="x"))
        await app._add_message(app._agent_label("Nova"), "nova", f"answer {k}")
    await pilot.pause(0.3)
    return list(app._transcript().query("CollapsibleTitle"))


@pytest.mark.asyncio
async def test_hover_still_styles_but_restyles_one_widget_at_a_time(monkeypatch):
    app = _app()
    async with app.run_test(size=(120, 60)) as pilot:
        titles = await _cards(app, pilot)
        assert titles, "the tool cards are in the chat"
        sizes: list[int] = []
        real = app.stylesheet.update_nodes

        def spy(nodes, animate=False):
            nodes = list(nodes)
            sizes.append(len(nodes))
            return real(nodes, animate=animate)

        monkeypatch.setattr(app.stylesheet, "update_nodes", spy)
        card = titles[-1].parent  # what a pointer over the card would hover
        textual_restyle = type(app).__mro__[1].update_styles

        def styles(node):
            return [dict(n.styles.get_rules()) for n in node.walk_children(with_self=True)]

        # What a mouse move onto the card, then off it, calls (the chat is
        # behind the home view in this harness, so a pointer cannot reach it).
        for target in (card, None):
            app._set_mouse_over(target, target)
            assert sizes and max(sizes) == 1, sizes
            fast = styles(card)
            textual_restyle(app, card)  # Textual's own full-subtree restyle
            assert styles(card) == fast, "restyling the card alone gave the same styles"
            sizes.clear()


@pytest.mark.asyncio
async def test_a_descendant_hover_rule_brings_back_the_full_restyle():
    app = _app()
    async with app.run_test(size=(120, 60)) as pilot:
        await _cards(app, pilot, n=1)
        assert not app._has_descendant_hover_rule()
        app.stylesheet.add_source("Collapsible:hover Static { text-style: bold; }")
        app.stylesheet.parse()
        assert app._has_descendant_hover_rule()
