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
