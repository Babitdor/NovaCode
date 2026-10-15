"""Focus or hover on the chat must not restyle every message in it (multi-second freezes)."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from novacode_cli.tui.widgets import TranscriptScroll


class _Chat(App):
    CSS = "Static.note.big { color: red; }"

    def compose(self) -> ComposeResult:
        with TranscriptScroll(id="transcript"):
            for n in range(300):
                yield Static(f"message {n}", classes="note")


@pytest.mark.asyncio
async def test_focus_and_hover_restyle_the_container_alone(monkeypatch):
    app = _Chat()
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(TranscriptScroll)
        transcript.update_node_styles()  # first call styles everything once
        restyled: list[int] = []
        original = app.stylesheet.update_nodes

        def counting(nodes, animate=False):
            nodes = list(nodes)
            restyled.append(len(nodes))
            return original(nodes, animate=animate)

        monkeypatch.setattr(app.stylesheet, "update_nodes", counting)
        transcript.focus()
        await pilot.pause()
        transcript.update_node_styles()  # what a hover change calls
        assert restyled and max(restyled) == 1, restyled

        restyled.clear()
        transcript.add_class("wide")  # a class rules could key on: full restyle
        assert max(restyled) > 300
