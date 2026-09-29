"""A reply's markdown colours come from the theme, in both directions.

Rich resolves its markdown elements through the console's theme (``markdown.h2``,
``markdown.code``, ...) and its own defaults are ANSI names: ``underline magenta``
for h2, ``bold cyan on black`` for inline code. ANSI is not themeable, so headings
arrived as whatever the terminal paints for magenta — on tokyo-night that read as
red — and no ``/theme`` moved them.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novacode_cli.tui.app import NovaApp

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

#: Style words that name a fixed terminal colour, i.e. exactly what must not
#: appear: the value has to be a theme-derived hex instead.
ANSI_COLOUR_NAMES = frozenset(
    {
        "black",
        "red",
        "green",
        "yellow",
        "blue",
        "magenta",
        "cyan",
        "white",
        "bright_black",
        "bright_red",
        "bright_green",
        "bright_yellow",
        "bright_blue",
        "bright_magenta",
        "bright_cyan",
        "bright_white",
    }
)


def test_no_markdown_element_is_pinned_to_an_ansi_colour():
    """Every colour-carrying element must resolve to the theme, not the terminal."""
    from novacode_cli.tui.palette import markdown_styles, palette_for
    from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT

    for theme in (NOVA_TOKYO_NIGHT, NOVA_MATRIX):
        styles = markdown_styles(palette_for(theme))
        assert styles, "no markdown styles at all"
        for element, style in styles.items():
            words = set(style.split()) - {"on", "bold", "italic", "underline", "dim", "strike"}
            offending = {word for word in words if word in ANSI_COLOUR_NAMES}
            assert not offending, f"{element} names a fixed colour {offending}: {style!r}"


def test_markdown_colours_differ_between_themes():
    """The whole point: a different theme is a different set of colours."""
    from novacode_cli.tui.palette import markdown_styles, palette_for
    from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT

    tokyo = markdown_styles(palette_for(NOVA_TOKYO_NIGHT))
    matrix = markdown_styles(palette_for(NOVA_MATRIX))

    assert set(tokyo) == set(matrix), "both themes must cover the same elements"
    for element in ("markdown.h1", "markdown.h2", "markdown.h3", "markdown.code"):
        assert tokyo[element] != matrix[element], f"{element} is theme-blind"
        assert palette_for(NOVA_TOKYO_NIGHT).primary in tokyo[element]
        assert palette_for(NOVA_MATRIX).primary in matrix[element]


def _heading_colour(app: NovaApp, needle: str) -> str:
    """The painted foreground of the transcript row carrying *needle*, as hex."""

    def hexed(colour: object) -> str:
        triplet = getattr(colour, "triplet", None)
        if triplet is None:
            return str(colour)
        return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"

    for strip in app.screen._compositor.render_strips():
        if needle in strip.text:
            for segment in getattr(strip, "_segments", []):
                if needle in segment.text:
                    return hexed(getattr(segment.style, "color", None))
    return "not painted"


async def _drive_reply_headings() -> dict:
    from rich.markdown import Markdown as RichMarkdown
    from rich.text import Text

    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.palette import palette_for
    from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
    )
    out: dict = {}
    async with app.run_test(size=(120, 40)) as pilot:
        app.theme = "tokyo-night"
        message = await app._add_message(
            Text("Nova", style="green"),
            "nova",
            RichMarkdown("## An H2 heading\n\nBody prose.\n"),
        )
        # The card fades in; a faded colour is a dimmed one, so let it settle.
        for _ in range(240):
            await pilot.pause()
            if float(message.styles.opacity) >= 1.0:
                break

        out["before"] = _heading_colour(app, "An H2 heading")
        out["tokyo_expected"] = palette_for(NOVA_TOKYO_NIGHT).primary

        # Switch the theme; the reply already on screen must follow it.
        app.theme = "matrix"
        for _ in range(120):
            await pilot.pause()
        out["after"] = _heading_colour(app, "An H2 heading")
        out["matrix_expected"] = palette_for(NOVA_MATRIX).primary
    return out


def test_an_existing_reply_recolours_when_the_theme_changes():
    """Headings must move with ``/theme``, including on a message already rendered."""
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_reply_headings())

    assert out["before"] != "not painted", "the heading never reached the screen"
    assert out["before"] == out["tokyo_expected"], (
        f"the heading is {out['before']}, not the theme's primary "
        f"{out['tokyo_expected']} (is it still ANSI magenta?)"
    )
    assert out["after"] == out["matrix_expected"], (
        f"after switching themes the heading is {out['after']}, not {out['matrix_expected']}"
    )
