"""A reply's markdown colours come from the theme, in both directions.

Rich resolves its markdown elements through the console's theme (``markdown.h2``,
``markdown.code``, ...) and its own defaults are ANSI names: ``underline magenta``
for h2, ``bold cyan on black`` for inline code. ANSI is not themeable, so headings
arrived as whatever the terminal paints for magenta — on tokyo-night that read as
red — and no ``/theme`` moved them.
"""

from __future__ import annotations

import asyncio
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401
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


def _painted_colour(app: NovaApp, needle: str) -> str:
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
    from novacode_cli.tui.widgets import CachedMarkdown as RichMarkdown
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
            RichMarkdown("## An H2 heading\n\nBody prose with `src/auth.py`.\n"),
        )
        # The card fades in; a faded colour is a dimmed one, so let it settle.
        for _ in range(240):
            await pilot.pause()
            if float(message.styles.opacity) >= 1.0:
                break

        out["before"] = _painted_colour(app, "An H2 heading")
        out["code_before"] = _painted_colour(app, "src/auth.py")
        out["tokyo_expected"] = palette_for(NOVA_TOKYO_NIGHT).primary

        # Switch the theme; the reply already on screen must follow it.
        app.theme = "matrix"
        for _ in range(120):
            await pilot.pause()
        out["after"] = _painted_colour(app, "An H2 heading")
        out["code_after"] = _painted_colour(app, "src/auth.py")
        out["matrix_expected"] = palette_for(NOVA_MATRIX).primary
    return out


def test_an_existing_reply_recolours_when_the_theme_changes():
    """Headings must move with ``/theme``, including on a message already rendered."""
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_reply_headings())
    assert out["code_before"] == out["tokyo_expected"]
    assert out["code_after"] == out["matrix_expected"]

    assert out["before"] != "not painted", "the heading never reached the screen"
    assert out["before"] == out["tokyo_expected"], (
        f"the heading is {out['before']}, not the theme's primary "
        f"{out['tokyo_expected']} (is it still ANSI magenta?)"
    )
    assert out["after"] == out["matrix_expected"], (
        f"after switching themes the heading is {out['after']}, not {out['matrix_expected']}"
    )


# ── Speaker labels ───────────────────────────────────────────────────────────


def test_no_speaker_label_is_pinned_to_an_ansi_colour():
    """The role headers must resolve to a theme hex, never a terminal colour.

    ``You`` was ``bold cyan`` and the agent was ``green`` at every call site. ANSI
    names are not themeable, so ``/theme`` could not move them even once.
    """
    from novacode_cli.tui.palette import (
        agent_label_style,
        palette_for,
        user_label_style,
    )
    from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT

    for theme in (NOVA_TOKYO_NIGHT, NOVA_MATRIX):
        palette = palette_for(theme)
        styles = [
            user_label_style(palette),
            agent_label_style(palette, "Nova"),
            # The main agent must ignore a legacy colour passed in by the loop.
            agent_label_style(palette, "Nova Agent", "#10b981"),
            # A named subagent keeps its own colour, which is not theme-derived.
            agent_label_style(palette, "Ralph", "#ff007f"),
        ]
        for style in styles:
            words = set(style.split()) - {"bold", "italic", "dim", "underline"}
            assert not (words & ANSI_COLOUR_NAMES), f"label pinned to ANSI: {style!r}"
            assert len(words) == 1, f"label style names no single colour: {style!r}"
        # The three distinct speakers must not collapse onto one colour: 'You' takes the
        # theme's primary, the agent its accent, a subagent its registered identity colour.
        assert len({user_label_style(palette), styles[1], styles[3]}) == 3, (
            f"speakers are not distinguishable under {theme.name}"
        )


def test_speaker_label_colours_differ_between_themes():
    """A different theme must yield different label colours, for both speakers."""
    from novacode_cli.tui.palette import (
        agent_label_style,
        palette_for,
        user_label_style,
    )
    from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT

    tokyo, matrix = palette_for(NOVA_TOKYO_NIGHT), palette_for(NOVA_MATRIX)
    assert user_label_style(tokyo) != user_label_style(matrix), "'You' is theme-blind"
    assert agent_label_style(tokyo, "Nova") != agent_label_style(matrix, "Nova"), (
        "agent is theme-blind"
    )


def test_a_subagent_keeps_its_identity_colour():
    """A named subagent's registered colour is identity, not decoration.

    This is the one case that must NOT track the theme: ralph is pink so it reads
    as a different speaker from the main agent. The main agent, which has no
    identity colour, is what follows the theme.
    """
    from novacode_cli.tui.palette import agent_label_style, palette_for
    from novacode_cli.tui.widgets import NOVA_TOKYO_NIGHT

    palette = palette_for(NOVA_TOKYO_NIGHT)
    assert agent_label_style(palette, "Ralph", "#ff007f") == "bold #ff007f"
    # Both spellings of the main agent ignore whatever the loop passed in, which
    # is still a legacy hardcoded green.
    themed = agent_label_style(palette, "Nova")
    assert themed == agent_label_style(palette, "Nova Agent")
    assert "#10b981" not in themed
    # An ANSI name is not an identity colour either: it is not themeable.
    assert agent_label_style(palette, "Ralph", "green") == themed


def test_main_agent_label_is_recognised_under_every_spelling():
    """``Nova`` and ``Nova Agent`` are the same speaker.

    ``get_agent_display_name`` title-cases the assistant id, so the streaming path
    writes "Nova Agent" while replay and the banner write "Nova". Missing the
    second spelling left the main agent pinned to a colour on the streaming path.
    """
    from novacode_cli.tui.palette import is_main_agent_label

    assert is_main_agent_label("Nova")
    assert is_main_agent_label("Nova Agent")
    assert is_main_agent_label("  nova  ")
    assert not is_main_agent_label("Ralph")
    assert not is_main_agent_label("Nova Adept")


async def _drive_speaker_labels() -> dict:
    """Mount a transcript, then switch themes with it already on screen."""
    from rich.markdown import Markdown
    from rich.text import Text

    import novacode_cli.ui_events as ev
    from langchain_core.messages import AIMessage, HumanMessage
    from novacode_cli.config.config import set_agent_color
    from novacode_cli.tui.app import ChatMessage, NovaApp
    from novacode_cli.tui.palette import palette_for
    from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    set_agent_color("ralph", "#ff007f")
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
        for _ in range(120):
            await pilot.pause()

        # Drive the real call sites rather than hand-built labels: a test that
        # constructs its own ``Text("You", style="bold cyan")`` and then asserts
        # the label is themed passes even when every real call site is still ANSI.
        # _replay_history is a @work worker, so call the underlying coroutine: it only
        # mounts widgets, so it carries no agent stream into teardown.
        app._restored_messages = [
            HumanMessage(content="hello there"),
            AIMessage(content="reply"),
        ]
        await app._replay_history.__wrapped__(app)

        # The streaming + commit path, which is where the per-agent colour arrives.
        await app._render(ev.TextDelta("sub says hi"))
        await app._render(
            ev.AssistantMessage(text="sub reply", agent_name="Ralph", agent_color="#ff007f")
        )
        for _ in range(200):
            await pilot.pause()

        tokyo = palette_for(NOVA_TOKYO_NIGHT)
        matrix = palette_for(NOVA_MATRIX)
        for needle in ("You", "Nova", "Ralph"):
            out[f"tokyo_{needle}"] = _painted_colour(app, needle)
            out[f"after_{needle}"] = None
        out["tokyo_user_expected"] = tokyo.user_label
        out["tokyo_agent_expected"] = tokyo.agent_label
        out["matrix_user_expected"] = matrix.user_label
        out["matrix_agent_expected"] = matrix.agent_label

        # The transcript is already on screen: this is the case that used to
        # leave every header on the previous theme's colours.
        app.theme = "matrix"
        for _ in range(200):
            await pilot.pause()
        for needle in ("You", "Nova", "Ralph"):
            out[f"after_{needle}"] = _painted_colour(app, needle)

        out["headers"] = [str(m._header.style) for m in app.query(ChatMessage)]
    return out


def test_speaker_labels_follow_the_theme_including_a_live_switch():
    """``You`` and the agent must recolour with ``/theme``, already-rendered ones too.

    Every assertion reads the composited screen, not a declared style, because a
    Rich ``Text`` colour beats CSS and can disagree with it.
    """
    if not _HAS_TEXTUAL:
        return
    out = asyncio.run(_drive_speaker_labels())

    assert out["tokyo_You"] == out["tokyo_user_expected"], (
        f"'You' painted {out['tokyo_You']}, not the theme's "
        f"{out['tokyo_user_expected']} (is it still ANSI cyan?)"
    )
    assert out["tokyo_Nova"] == out["tokyo_agent_expected"], (
        f"agent painted {out['tokyo_Nova']}, not {out['tokyo_agent_expected']} "
        f"(is it still ANSI green?)"
    )
    assert out["after_You"] == out["matrix_user_expected"], (
        f"after /theme the existing 'You' label is {out['after_You']}, not "
        f"{out['matrix_user_expected']} (only new messages were recoloured?)"
    )
    assert out["after_Nova"] == out["matrix_agent_expected"], (
        f"after /theme the existing agent label is {out['after_Nova']}, not "
        f"{out['matrix_agent_expected']}"
    )
    # A subagent's identity colour is not theme-derived and must survive /theme.
    assert out["after_Ralph"] == "#ff007f", (
        f"the subagent label was recoloured to {out['after_Ralph']}; its identity "
        f"colour must be preserved across a theme switch"
    )
