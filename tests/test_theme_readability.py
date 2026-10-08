"""Inline code remains readable in restored replies and every shipped palette."""

import pytest
from rich.console import Console
from rich.style import Style
from textual.theme import BUILTIN_THEMES

from novacode_cli.tui.palette import _hex, apply_markdown_theme, markdown_styles, palette_for
from novacode_cli.tui.themes import NOVA_EXTRA_THEMES
from novacode_cli.tui.widgets import NOVA_MATRIX, NOVA_TOKYO_NIGHT, CachedMarkdown
from tests.test_tui_app import _disable_live_update_checks  # noqa: F401


def _luminance(color):
    values = color.get_truecolor()
    channels = [v / 3294.6 if v <= 10 else ((v / 255 + 0.055) / 1.055) ** 2.4 for v in values]
    return sum(v * weight for v, weight in zip(channels, (0.2126, 0.7152, 0.0722)))


@pytest.mark.parametrize(
    "theme",
    [*BUILTIN_THEMES.values(), NOVA_TOKYO_NIGHT, NOVA_MATRIX, *NOVA_EXTRA_THEMES],
    ids=lambda t: t.name,
)
def test_code_is_readable_and_transcript_content_survives(theme):
    console = Console(width=100, color_system="truecolor")
    apply_markdown_theme(console, theme)
    reply = CachedMarkdown(
        "File `src/auth.py` uses `/api/tags`.\n\n| File | Purpose |\n| --- | --- |\n| `config.py` | Setup |"
    )
    segments = list(console.render(reply))
    text = "".join(s.text for s in segments)
    for needle in ("src/auth.py", "/api/tags", "config.py"):
        assert needle in text
        chip = next(s for s in segments if needle in s.text)
        assert chip.style.color and chip.style.bgcolor
        a, b = sorted((_luminance(chip.style.color), _luminance(chip.style.bgcolor)))
        assert (b + 0.05) / (a + 0.05) >= 4.5


def test_css_colors_and_alpha_are_resolved_without_gray_fallback():
    assert _hex("ansi_blue") == "#000080"
    assert _hex("ansi_default", "#ffffff") == "#ffffff"
    assert _hex("rgb(10,20,30)") == "#0a141e"
    assert _hex("rgb(10, 20, 30)") == "#0a141e"
    assert _hex("rgba(255,255,255,0.5)", background="#000000") == "#7f7f7f"
    assert _hex("#ffffff 50%", background="#000000") == "#7f7f7f"


def test_cached_reply_recolors_at_same_width_after_theme_switch():
    console = Console(width=100, color_system="truecolor")
    reply = CachedMarkdown("Use `auth.py`\n\n## Findings")
    apply_markdown_theme(console, NOVA_TOKYO_NIGHT)
    before = list(console.render(reply))
    first_cache = reply._cache_segments
    assert list(console.render(reply)) == before
    assert reply._cache_segments is first_cache
    apply_markdown_theme(console, NOVA_MATRIX)
    after = list(console.render(reply))
    assert before != after
    expected = Style.parse(markdown_styles(palette_for(NOVA_MATRIX))["markdown.code"])
    assert next(s for s in after if "auth.py" in s.text).style.color == expected.color


def test_nova_registers_five_distinct_extra_themes(monkeypatch):
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(NovaConfig, "__init__", lambda self: None)
    monkeypatch.setattr(NovaConfig, "get", lambda self, key: None)
    app = NovaApp.__new__(NovaApp)
    registered = []
    monkeypatch.setattr(NovaApp, "register_theme", lambda self, theme: registered.append(theme))
    monkeypatch.setattr(NovaApp, "theme", "tokyo-night")
    app._apply_saved_theme()
    assert len(NOVA_EXTRA_THEMES) == 5
    assert all(theme in registered for theme in NOVA_EXTRA_THEMES)
    assert len({theme.background for theme in NOVA_EXTRA_THEMES}) == 5


@pytest.mark.asyncio
async def test_extra_themes_apply_in_the_live_tui_and_persist(tmp_path):
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.tui.app import NovaApp
    from novacode_cli.tui.workspace_approval import WorkspaceApprovalApp
    from novacode_cli.ui.ui_elements import TokenTracker
    from tests.test_tui_app import _SS, _FakeAgent

    NovaConfig().set("theme", "nova-paper")
    app = NovaApp(
        agent=_FakeAgent(),
        assistant_id="nova-agent",
        session_state=_SS(),
        backend=None,
        token_tracker=TokenTracker(),
        image_tracker=None,
        model_name="m",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.theme == "nova-paper"
        for theme in NOVA_EXTRA_THEMES:
            app.theme = theme.name
            await pilot.pause()
            assert app.screen.styles.background.hex.lower() == theme.background
    approval = WorkspaceApprovalApp(tmp_path)
    assert approval.theme == "nova-paper"
