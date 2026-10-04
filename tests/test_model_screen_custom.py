"""`/model`: pick a provider, type a model id the lists do not offer.

The provider used to be whichever one the highlighted list row belonged to, so a
custom model meant scrolling to a row of the right provider first, and a
provider that listed no models could not be picked at all.
"""

from __future__ import annotations

import asyncio
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Select, Static

# Imported up front: on Windows the config module wraps sys.stdout at import,
# which fails once a running Textual app has replaced it with its print capture.
import novacode_cli.config.config  # noqa: E402, F401
import novacode_cli.config.model_manager  # noqa: E402, F401


def _run(actions, *, models=None, current=("ollama", "gemma4:31b-cloud")):
    """Open the picker with fake data, run *actions* on it, return its result."""
    from novacode_cli.tui.screens import ModelScreen

    from novacode_cli.config.provider_auth import ProviderAuthState, ProviderAuthStatus

    statuses = {
        name: ProviderAuthStatus(name=name, state=ProviderAuthState.NOT_REQUIRED)
        for name in ("ollama", "openai", "openrouter", "anthropic")
    }
    models = models if models is not None else {"ollama": ["gemma4:31b-cloud", "llama3"], "openai": ["gpt-5-mini"]}
    out: dict = {}

    class Host(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    async def drive() -> None:
        app = Host()
        async with app.run_test(size=(120, 44)) as pilot:
            screen = ModelScreen(*current)
            screen._load = lambda: screen._apply(statuses, models, [])  # no credential store, no network
            app.push_screen(screen, callback=lambda r: out.setdefault("result", r))
            for _ in range(4):
                await pilot.pause()
            await actions(pilot, screen)
            for _ in range(4):
                await pilot.pause()

    asyncio.run(drive())
    return out.get("result")


def test_a_custom_model_goes_to_the_provider_chosen_in_the_dropdown():
    async def actions(pilot, screen):  # noqa: ANN001, ANN202
        select = screen.query_one("#model-provider", Select)
        assert select.value == "ollama", "starts on the current provider"
        select.value = "openrouter"  # lists no models here: could not be picked before
        await pilot.pause()
        screen.query_one("#model", Input).value = "meta/llama-9-experimental"
        screen._submit()

    result = _run(actions)
    assert (result["provider"], result["model"]) == ("openrouter", "meta/llama-9-experimental")
    assert result["kind"] == "model" and result["role"] == "main"


def test_a_typed_provider_prefix_wins_and_model_colons_survive():
    async def with_prefix(pilot, screen):  # noqa: ANN001, ANN202
        screen.query_one("#model", Input).value = "anthropic:claude-next"
        screen._submit()

    result = _run(with_prefix)
    assert (result["provider"], result["model"]) == ("anthropic", "claude-next")

    async def model_with_colon(pilot, screen):  # noqa: ANN001, ANN202
        screen.query_one("#model", Input).value = "qwen3:14b-custom"  # not a provider prefix
        screen._submit()

    result = _run(model_with_colon)
    assert (result["provider"], result["model"]) == ("ollama", "qwen3:14b-custom")


def test_browsing_the_list_moves_the_dropdown_with_it():
    seen: dict = {}

    async def actions(pilot, screen):  # noqa: ANN001, ANN202
        options = screen.query_one("#model-options", OptionList)
        index = next(i for i, pick in screen._targets.items() if pick.provider == "openai")
        options.highlighted = index
        for _ in range(3):
            await pilot.pause()
        seen["provider"] = screen.query_one("#model-provider", Select).value
        screen.dismiss(None)

    _run(actions)
    assert seen["provider"] == "openai"
