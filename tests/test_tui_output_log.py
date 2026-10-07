"""Bounded output, hidden writes, reflow, and paint coalescing regressions."""

from rich.text import Text
from textual.app import App

from novacode_cli.tui.widgets import OutputLog


def test_output_retains_tail_with_both_limits_and_literal_text():
    output = OutputLog(max_lines=3, max_chars=24, markup=False)
    for i in range(10000):
        output.write(f"row-{i}")
    assert [line.text for line in output.lines] == ["row-9998", "row-9999"]
    assert output._output_chars <= 24
    output.write("[red]literal[/red]")
    assert output.lines[-1].text == "[red]literal[/red]"
    output.write(Text("X" * 1000, style="red"))
    assert output.lines[-1].text == "X" * 23
    assert output._output_lines[-1].style == "red"
    output.clear()
    assert output.lines == [] and output._output_chars == 0


async def test_hidden_output_survives_and_reflows_with_one_pending_paint():
    class OutputApp(App):
        def compose(self):
            yield OutputLog(id="output", max_lines=50)

    app = OutputApp()
    async with app.run_test(size=(80, 24)) as pilot:
        output = app.query_one(OutputLog)
        output.styles.display = "none"
        output.write("hidden result " * 10)
        timer = output._paint_timer
        output.write("next result")
        assert output._paint_timer is timer
        assert "hidden result" in output.lines[0].text
        await pilot.pause(0.1)
        output.styles.display = "block"
        await pilot.pause()
        original_height = output._output_body.size.height
        await pilot.resize_terminal(30, 24)
        await pilot.pause()
        assert output._output_body.size.height > original_height
        assert "hidden result" in str(output._output_body.content)
        assert output._paint_timer is None
        output.write("clear before the paint")
        timer = output._paint_timer
        output.clear()
        assert output._paint_timer is None and output.lines == []
        assert timer is not None


async def test_long_log_only_renders_visible_rows_and_releases_removed_content(monkeypatch):
    from novacode_cli.tui.output_body import OutputBody

    rendered = []
    original = OutputBody.render_line

    def render_line(body, y):
        rendered.append(y)
        return original(body, y)

    monkeypatch.setattr(OutputBody, "render_line", render_line)

    class OutputApp(App):
        def compose(self):
            yield OutputLog(id="output", max_lines=2000)

    app = OutputApp()
    async with app.run_test(size=(80, 24)) as pilot:
        output = app.query_one(OutputLog)
        output.write(Text(("abcdefgh " * 10 + "\n") * 1800, style="red"))
        await pilot.pause(0.1)
        body = output._output_body
        assert len(body._rows) > 1800
        assert len(set(rendered)) < 100, "offscreen output must not be materialized"
        for offset in range(0, 600, 40):
            output.scroll_to(y=offset, animate=False)
            await pilot.pause()
        assert len(body._styles_cache._cache) <= body._MAX_CACHED_ROWS
        await output.remove()
        await pilot.pause()
        assert output._output_chars == 0
        assert not output._output_lines and not body._lines and not body._rows
        assert not body._styles_cache._cache
        for _ in range(100):
            output.write("late background output " * 100)
        await pilot.pause()
        assert not output._output_lines
        assert output._paint_timer is None


async def test_output_wrap_styles_and_selection_match_displayed_rows():
    from textual.geometry import Offset
    from textual.selection import Selection

    class OutputApp(App):
        def compose(self):
            yield OutputLog(id="wrapped", markup=False)
            yield OutputLog(id="unwrapped", wrap=False)

    app = OutputApp()
    async with app.run_test(size=(40, 24)) as pilot:
        text = Text("alpha\t123 " + "猫 🦉 café " * 12 + "\nlast row", style="red")
        wrapped = app.query_one("#wrapped", OutputLog)
        wrapped.write(text)
        unwrapped = app.query_one("#unwrapped", OutputLog)
        unwrapped.write(text)
        await pilot.pause(0.1)
        body = wrapped._output_body
        expected = text.wrap(app.console, body.size.width)
        actual = [body.render_line(y).text.rstrip() for y in range(len(body._rows))]
        assert actual == [row.plain.rstrip() for row in expected]
        strip = body.render_line(0)
        assert any(segment.style.color.name == "red" for segment in strip if segment.style.color)
        selected = body.get_selection(Selection(Offset(0, 0), Offset(4, 1)))
        assert selected == (actual[0] + "\n" + actual[1][:4], "\n")
        assert len(unwrapped._output_body._rows) == 2


async def test_hidden_log_does_not_prepare_render_rows_until_revealed():
    class OutputApp(App):
        def compose(self):
            output = OutputLog(id="output", max_lines=2000)
            output.display = False
            yield output

    app = OutputApp()
    async with app.run_test() as pilot:
        output = app.query_one(OutputLog)
        for _ in range(100):
            output.write("hidden result " * 30)
        await pilot.pause(0.1)
        assert not output._output_body._rows
        output.display = True
        await pilot.pause()
        assert output._output_body._rows
