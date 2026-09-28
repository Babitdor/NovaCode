"""The ``/model`` picker: provider-grouped rows, indicators, filter, selection.

Readiness is rendered per provider, and a provider that cannot be called is not
a dead end — selecting one of its models asks the app to run ``/auth`` for it
rather than letting model construction fail later with an SDK-level message.
"""

from __future__ import annotations

import pytest

from novacode_cli.config.provider_auth import (
    ProviderAuthSource,
    ProviderAuthState,
    ProviderAuthStatus,
)

try:
    import textual  # noqa: F401

    _HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    _HAS_TEXTUAL = False

pytestmark = pytest.mark.skipif(not _HAS_TEXTUAL, reason="textual is not installed")

FAKE_MODELS: dict[str, list[str]] = {
    "openai": ["gpt-5-mini", "gpt-4o"],
    "anthropic": ["claude-sonnet-5", "claude-opus-5"],
    "ollama": ["llama3.3:70b"],
    "google": ["gemini-3-pro"],
    "openrouter": ["anthropic/claude-3.5-sonnet"],
    "opencode": ["glm-5.3"],
    "nvidia": ["deepseek-ai/deepseek-v4-pro-0813"],
}


def _status(
    name: str, state: ProviderAuthState, source: ProviderAuthSource = ProviderAuthSource.NONE
) -> ProviderAuthStatus:
    return ProviderAuthStatus(name=name, state=state, source=source)


FAKE_STATUSES: dict[str, ProviderAuthStatus] = {
    "openai": _status("openai", ProviderAuthState.MISSING),
    "anthropic": _status("anthropic", ProviderAuthState.CONFIGURED, ProviderAuthSource.STORED),
    "ollama": _status("ollama", ProviderAuthState.NOT_REQUIRED),
    "google": _status("google", ProviderAuthState.MISSING),
    "openrouter": _status("openrouter", ProviderAuthState.MISSING),
    "opencode": _status("opencode", ProviderAuthState.MISSING),
    "nvidia": _status("nvidia", ProviderAuthState.MISSING),
    # Voice providers are credentials too, and the picker lists them beside the
    # chat providers — one configured, one not, so both paths are exercised.
    "elevenlabs": _status("elevenlabs", ProviderAuthState.CONFIGURED, ProviderAuthSource.STORED),
    "deepgram": _status("deepgram", ProviderAuthState.MISSING),
}

FAKE_VOICE: dict = {
    "stt_provider": "faster-whisper",
    "tts_provider": "piper",
    "stt_model": "base",
    "providers": {
        "faster-whisper": {"model": "base"},
        "piper": {"voice": "en_US-lessac-medium"},
    },
}


class _FakeConfig:
    """NovaConfig stand-in carrying canned recents and voice settings."""

    recent: list[str] = []
    voice: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_recent_models(self) -> list[str]:
        return list(type(self).recent)

    def get_voice_config(self) -> dict:
        return dict(type(self).voice or FAKE_VOICE)


@pytest.fixture(autouse=True)
def _stubbed(monkeypatch):
    """Serve canned statuses, models and recents; never touch the real ones."""
    import novacode_cli.config.model_catalog as catalog
    from novacode_cli.config import nova_config, provider_auth

    monkeypatch.setattr(
        provider_auth, "get_all_auth_statuses", lambda **kwargs: dict(FAKE_STATUSES)
    )
    monkeypatch.setattr(catalog, "get_all_models", lambda **kwargs: dict(FAKE_MODELS))
    _FakeConfig.recent = []
    _FakeConfig.voice = {}
    monkeypatch.setattr(nova_config, "NovaConfig", _FakeConfig)


async def _open(pilot, app, **kwargs):
    """Push a ModelScreen and wait for its background load to land."""
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import ModelScreen

    screen = ModelScreen(**kwargs)
    app.push_screen(screen)
    for _ in range(120):
        await pilot.pause()
        try:
            if screen.query_one("#model-options", OptionList).option_count:
                return screen
        except Exception:  # noqa: BLE001 — not mounted yet
            pass
    raise AssertionError("model list never populated")


def _options(screen):
    from textual.widgets import OptionList

    return screen.query_one("#model-options", OptionList).options


def _model_ids(screen) -> list[str]:
    """Ids of the selectable CHAT rows (voice rows are asserted separately)."""
    voice = {pick.spec for pick in screen._targets.values() if pick.kind == "voice"}
    return [
        option.id
        for option in _options(screen)
        if option.id and not str(option.id).startswith("#hdr:") and option.id not in voice
    ]


def _voice_ids(screen) -> list[str]:
    """Ids of the selectable VOICE rows, in painted order."""
    return [pick.spec for pick in screen._targets.values() if pick.kind == "voice"]


def _voice_row(screen, spec: str):
    """The ``_Pick`` behind a voice row id."""
    return next(pick for pick in screen._targets.values() if pick.spec == spec)


def _all_ids(screen) -> list[str]:
    return [option.id for option in _options(screen)]


def _texts(screen) -> list[str]:
    return [str(option.prompt) for option in _options(screen)]


def _host():
    from textual.app import App

    class _Host(App):
        def compose(self):
            return []

    return _Host()


async def test_rows_are_grouped_under_provider_headers():
    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        ids = _all_ids(screen)
        headers = [i for i in ids if i and str(i).startswith("#hdr:")]

        # One header per chat provider, plus the two speech section labels.
        for provider in FAKE_MODELS:
            assert f"#hdr:{provider}" in headers
        assert "#hdr:Speech to text" in headers
        assert "#hdr:Text to speech" in headers
        # Every voice provider also gets its own header, which is what carries
        # its credential indicator. Without one a missing key is invisible until
        # the row is already selected.
        from novacode_cli.audio.providers import STT_PROVIDERS, TTS_PROVIDERS

        for provider, meta in (STT_PROVIDERS | TTS_PROVIDERS).items():
            if meta.get("options"):
                assert f"#hdr:{provider}" in headers, f"{provider} has no header"
        # Every model row is prefixed with its provider, which is what the
        # selection returns and what the app switches on.
        assert set(_model_ids(screen)) == {
            f"{provider}:{model}" for provider, models in FAKE_MODELS.items() for model in models
        }


async def test_headers_carry_the_credential_indicator():
    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        by_id = dict(zip([o.id for o in _options(screen)], _texts(screen), strict=True))

        assert "no key" in by_id["#hdr:openai"]
        # A configured provider needs no annotation, and Ollama needs no key at
        # all: saying "no key" there would be wrong.
        assert "no key" not in by_id["#hdr:anthropic"]
        assert "no key required" in by_id["#hdr:ollama"]


async def test_typing_filters_the_visible_models():
    from textual.widgets import Input

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        screen.query_one("#model-filter", Input).value = "sonnet"
        await pilot.pause()

        assert set(_model_ids(screen)) == {
            "anthropic:claude-sonnet-5",
            "openrouter:anthropic/claude-3.5-sonnet",
        }


async def test_a_filter_with_no_matches_says_so_instead_of_going_blank():
    from textual.widgets import Input

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        screen.query_one("#model-filter", Input).value = "zzzz-no-such-model"
        await pilot.pause()

        assert not _model_ids(screen)
        assert any("No models match" in text for text in _texts(screen))


async def test_choosing_a_model_dismisses_with_it():
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import ModelScreen

    app = _host()
    got: list[dict | None] = []

    async with app.run_test(size=(120, 40)) as pilot:
        screen = ModelScreen()
        app.push_screen(screen, callback=got.append)
        for _ in range(120):
            await pilot.pause()
            if screen.query_one("#model-options", OptionList).option_count:
                break

        option_list = screen.query_one("#model-options", OptionList)
        for index, option in enumerate(option_list.options):
            if option.id == "anthropic:claude-opus-5":
                option_list.highlighted = index
                break
        await pilot.press("enter")
        await pilot.pause()

    assert got == [{"kind": "model", "provider": "anthropic", "model": "claude-opus-5"}]


async def test_a_provider_without_a_key_asks_to_authenticate():
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import ModelScreen

    app = _host()
    got: list[dict | None] = []

    async with app.run_test(size=(120, 40)) as pilot:
        screen = ModelScreen()
        app.push_screen(screen, callback=got.append)
        for _ in range(120):
            await pilot.pause()
            if screen.query_one("#model-options", OptionList).option_count:
                break

        option_list = screen.query_one("#model-options", OptionList)
        for index, option in enumerate(option_list.options):
            if option.id == "openai:gpt-4o":
                option_list.highlighted = index
                break
        await pilot.press("enter")
        await pilot.pause()

    # Not an error to swallow: the app routes this into /auth for the provider.
    assert got == [{"provider": "openai", "needs_auth": True, "kind": "model"}]


async def test_a_typed_model_id_is_used_for_the_selected_provider():
    from textual.widgets import Input, OptionList

    from novacode_cli.tui.screens import ModelScreen

    app = _host()
    got: list[dict | None] = []

    async with app.run_test(size=(120, 40)) as pilot:
        screen = ModelScreen()
        app.push_screen(screen, callback=got.append)
        for _ in range(120):
            await pilot.pause()
            if screen.query_one("#model-options", OptionList).option_count:
                break

        screen.query_one("#model-filter", Input).value = "claude-opus-5"
        await pilot.pause()
        screen.query_one("#model", Input).value = "claude-opus-6-preview"
        await pilot.press("enter")
        await pilot.pause()

    # The curated lists cannot name a model the provider has not published yet,
    # so a free-typed id goes through for the provider under the cursor.
    assert got == [{"kind": "model", "provider": "anthropic", "model": "claude-opus-6-preview"}]


async def test_ctrl_r_restricts_the_list_to_the_curated_subset():
    from novacode_cli.config.model_manager import MODEL_PRESETS

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)
        assert set(_model_ids(screen)) == {f"{p}:{m}" for p, ms in FAKE_MODELS.items() for m in ms}

        await pilot.press("ctrl+r")

        curated = {
            f"{provider}:{model}"
            for provider, preset in MODEL_PRESETS.items()
            for model in preset["models"]
        }
        assert set(_model_ids(screen)) == curated
        # A curated id the stubbed catalog never returned is back in view.
        assert "google:gemini-1.5-flash" in _model_ids(screen)
        # Ctrl+R means "the curated CHAT lists", so the speech sections go away
        # rather than sitting under a list that deliberately excludes them.
        assert not _voice_ids(screen)
        assert "#hdr:Speech to text" not in _all_ids(screen)


async def test_voice_headers_carry_the_credential_indicator():
    """The key state has to be visible before a row is chosen."""
    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        by_id = dict(zip(_all_ids(screen), _texts(screen), strict=True))

        assert "no key" in by_id["#hdr:deepgram"]
        # Configured in FAKE_STATUSES, so no annotation.
        assert "no key" not in by_id["#hdr:elevenlabs"]
        # Local providers need no key at all.
        assert "no key" not in by_id["#hdr:piper"]


async def test_a_voice_row_shows_the_value_it_will_store():
    """An ElevenLabs row is a voice ID; the label alone would hide that."""
    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        by_id = dict(zip(_all_ids(screen), _texts(screen), strict=True))

        assert "21m00Tcm4TlvDq8ikWAM" in by_id["elevenlabs:21m00Tcm4TlvDq8ikWAM"]
        # A label that already contains the value is not repeated.
        piper_row = by_id["piper:en_US-lessac-medium"]
        assert piper_row.count("en_US-lessac-medium") == 1


# ── Voice rows ───────────────────────────────────────────────────────────────
# The picker carries a second axis besides chat models. These pin the parts that
# differ: what a voice row selects, and that it never reaches MODEL_PRESETS.


async def test_voice_sections_list_every_speech_provider():
    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        ids = set(_voice_ids(screen))
        by_spec = {spec: _voice_row(screen, spec) for spec in ids}

        # The repo default for each provider is offered, under the right axis.
        assert by_spec["faster-whisper:base"].space == "stt"
        assert by_spec["faster-whisper:base"].field == "model"
        assert by_spec["deepgram:nova-2"].space == "stt"
        assert by_spec["piper:en_US-lessac-medium"].space == "tts"
        assert by_spec["piper:en_US-lessac-medium"].field == "voice"
        # ElevenLabs stores a voice ID under its own key, not `voice`.
        eleven = _voice_row(screen, "elevenlabs:21m00Tcm4TlvDq8ikWAM")
        assert eleven.space == "tts"
        assert eleven.field == "voice_id"
        # Local, keyless providers are listed too — they are still a choice.
        assert "parakeet:parakeet-tdt-0.6b-v2" in ids


async def test_choosing_a_voice_row_dismisses_with_its_axis_and_field():
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import ModelScreen

    app = _host()
    got: list[dict | None] = []

    async with app.run_test(size=(120, 40)) as pilot:
        screen = ModelScreen()
        app.push_screen(screen, callback=got.append)
        for _ in range(120):
            await pilot.pause()
            if screen.query_one("#model-options", OptionList).option_count:
                break

        option_list = screen.query_one("#model-options", OptionList)
        for index, option in enumerate(option_list.options):
            if option.id == "piper:en_US-amy-medium":
                option_list.highlighted = index
                break
        await pilot.press("enter")
        await pilot.pause()

    # `kind` is what keeps a voice pick from being handed to MODEL_PRESETS,
    # which has no entry for a voice provider.
    assert got == [
        {
            "kind": "voice",
            "provider": "piper",
            "model": "en_US-amy-medium",
            "space": "tts",
            "field": "voice",
        }
    ]


async def test_a_voice_provider_without_a_key_asks_to_authenticate():
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import ModelScreen

    app = _host()
    got: list[dict | None] = []

    async with app.run_test(size=(120, 40)) as pilot:
        screen = ModelScreen()
        app.push_screen(screen, callback=got.append)
        for _ in range(120):
            await pilot.pause()
            if screen.query_one("#model-options", OptionList).option_count:
                break

        option_list = screen.query_one("#model-options", OptionList)
        for index, option in enumerate(option_list.options):
            if option.id == "deepgram:nova-2":
                option_list.highlighted = index
                break
        await pilot.press("enter")
        await pilot.pause()

    assert got == [{"provider": "deepgram", "needs_auth": True, "kind": "voice"}]


async def test_a_filter_finds_the_voice_section_by_provider_name():
    """Typing a provider name must find it — a user rarely knows a voice id."""
    from textual.widgets import Input

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        screen.query_one("#model-filter", Input).value = "eleven"
        await pilot.pause()

        assert set(_voice_ids(screen)) == {"elevenlabs:21m00Tcm4TlvDq8ikWAM"}
        assert not _model_ids(screen)


@pytest.mark.parametrize("needle", ["voice", "speech"])
async def test_a_filter_finds_the_voice_section_by_axis_word(needle: str):
    """An axis word must reach the speech rows.

    They are painted below every chat model, and a full list is a few hundred of
    those, which buries the two voice sections far below the fold. Typed into the
    filter, "voice" and "speech" are how a user gets there.

    The filter is an ordered-subsequence match, so a chat id can still match by
    accident (measured against the real catalog: none for "voice", one for
    "speech"). What matters is that every speech row survives and the chat list
    collapses, which is what this asserts.
    """
    from textual.widgets import Input

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)
        every_voice_row = set(_voice_ids(screen))
        assert every_voice_row

        screen.query_one("#model-filter", Input).value = needle
        await pilot.pause()

        assert set(_voice_ids(screen)) == every_voice_row
        assert len(_model_ids(screen)) <= 2


async def test_the_info_line_says_where_the_voice_rows_are():
    """A count alone does not help when the rows sit below the fold."""
    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        count = len(_voice_ids(screen))
        plain = screen._info_text().plain

        assert count
        assert f"{count} voice option(s)" in plain
        assert "at the end" in plain
        assert "type “voice”" in plain


async def test_a_typed_voice_id_is_used_for_the_highlighted_voice_provider():
    from textual.widgets import Input, OptionList

    from novacode_cli.tui.screens import ModelScreen

    app = _host()
    got: list[dict | None] = []

    async with app.run_test(size=(120, 40)) as pilot:
        screen = ModelScreen()
        app.push_screen(screen, callback=got.append)
        for _ in range(120):
            await pilot.pause()
            if screen.query_one("#model-options", OptionList).option_count:
                break

        option_list = screen.query_one("#model-options", OptionList)
        for index, option in enumerate(option_list.options):
            if option.id == "elevenlabs:21m00Tcm4TlvDq8ikWAM":
                option_list.highlighted = index
                break
        # A voice the registry does not list, so the free-text field is the only
        # way to reach it.
        screen.query_one("#model", Input).value = "EXAVITQu4vr4xnSDxMaL"
        await pilot.press("enter")
        await pilot.pause()

    assert got == [
        {
            "kind": "voice",
            "provider": "elevenlabs",
            "model": "EXAVITQu4vr4xnSDxMaL",
            "space": "tts",
            "field": "voice_id",
        }
    ]


async def test_the_active_voice_is_marked():
    """The configured voice shows as current, which is how a user sees the axis."""
    _FakeConfig.voice = {
        "stt_provider": "deepgram",
        "tts_provider": "elevenlabs",
        "providers": {
            "deepgram": {"model": "nova-3"},
            "elevenlabs": {"voice_id": "21m00Tcm4TlvDq8ikWAM"},
        },
    }

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        by_id = dict(zip(_all_ids(screen), _texts(screen), strict=True))

        assert "(current)" in by_id["deepgram:nova-3"]
        assert "(current)" not in by_id["deepgram:nova-2"]
        assert "(current)" in by_id["elevenlabs:21m00Tcm4TlvDq8ikWAM"]
        assert "(current)" not in by_id["piper:en_US-lessac-medium"]


async def test_recent_picks_are_pinned_on_top_and_not_repeated():
    _FakeConfig.recent = ["anthropic:claude-opus-5", "opencode:glm-5.3"]

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app)

        ids = [option.id for option in _options(screen)]
        assert ids[0] == "#hdr:Recent"
        assert ids[1] == "anthropic:claude-opus-5"

        assert ids.count("anthropic:claude-opus-5") == 1
        assert ids.count("opencode:glm-5.3") == 1


async def test_the_model_in_use_is_marked():
    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(
            pilot, app, current_provider="anthropic", current_model="claude-opus-5"
        )

        by_id = dict(zip([o.id for o in _options(screen)], _texts(screen), strict=True))

        assert "(current)" in by_id["anthropic:claude-opus-5"]
        assert "(current)" not in by_id["anthropic:claude-sonnet-5"]


async def test_the_cursor_lands_on_the_providers_rows_when_no_model_matches():
    """After `/auth` for a provider, reopening must land on that provider.

    The caller passes the provider it just authenticated, not a model, so there
    is no exact row to find — landing on the first row of the list would look
    like the picker ignored the choice that reopened it.
    """
    from textual.widgets import OptionList

    app = _host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _open(pilot, app, current_provider="opencode", current_model="gone")

        option_list = screen.query_one("#model-options", OptionList)
        highlighted = option_list.get_option_at_index(option_list.highlighted).id

        assert str(highlighted).startswith("opencode:")
