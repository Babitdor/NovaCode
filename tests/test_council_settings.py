"""Council configuration, model routing and private editor drafts."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Never
from unittest.mock import AsyncMock

import pytest
from textual.app import App
from textual.widgets import Button, Input, OptionList, Select, TextArea

from novacode_cli import council
from novacode_cli.council_settings import (
    default_settings,
    parse_settings,
    personality_templates,
    resolve_models,
)
from novacode_cli.tui.council_settings_screen import CouncilSettingsScreen


def test_defaults_and_roundtrip():
    settings = default_settings()
    assert len(settings.personas()) == 6
    assert len(settings.personas(debate=True)) == 5
    assert parse_settings(settings.to_dict()) == settings
    assert len(personality_templates()) == 11
    model = object()
    assert all(value is model for value in resolve_models(settings, model).values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "two\nlines"),
        ("system", " "),
        ("provider", "unknown"),
        ("model", "missing-provider"),
        ("id", []),
    ],
)
def test_invalid_settings_are_rejected(field: str, value: object):
    raw = default_settings().to_dict()
    raw["members"][0][field] = value
    with pytest.raises(ValueError, match=r"Council|council|Select|Every|Unknown"):
        parse_settings(raw)


def test_names_are_unique_case_insensitively():
    raw = default_settings().to_dict()
    raw["members"][0]["name"] = raw["members"][1]["name"].upper()
    with pytest.raises(ValueError, match="distinct"):
        parse_settings(raw)


def test_overrides_are_cached_without_changing_default():
    settings = default_settings()
    members = tuple(replace(m, provider="openai", model="custom") for m in settings.members)
    settings = replace(settings, members=members)
    calls = []
    override, default = object(), object()

    def build(provider: str, model: str) -> object:
        calls.append((provider, model))
        return override

    models = resolve_models(settings, default, build=build)
    assert calls == [("openai", "custom")]
    assert models["judge"] is default
    assert all(models[m.id] is override for m in members)
    assert set(resolve_models(settings, default, build=build, debate=True)) == {
        p.id for p in settings.personas(debate=True)
    }


def test_invalid_override_never_silently_falls_back():
    settings = default_settings()
    settings = replace(settings, judge=replace(settings.judge, provider="openai", model="bad"))

    def fail(*_args: str) -> Never:
        message = "provider construction failed"
        raise RuntimeError(message)

    with pytest.raises(ValueError, match="The Judge"):
        resolve_models(settings, object(), build=fail)


@pytest.mark.asyncio
@pytest.mark.parametrize("save", [True, False])
async def test_command_persists_only_save_and_preserves_main_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    save: bool,
):
    from novacode_cli.config import nova_config
    from novacode_cli.council_settings import load_settings
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(
        nova_config.Settings,
        "from_environment",
        lambda: SimpleNamespace(
            user_deepagents_dir=tmp_path,
        ),
    )
    config = nova_config.NovaConfig()
    config.set("model", {"provider": "openai", "model": "keep-main-model"})
    draft = default_settings().to_dict()
    draft["members"][0]["name"] = "Saved architect"
    logs = []
    app = SimpleNamespace(
        push_screen_wait=AsyncMock(return_value=draft if save else None),
        _log=lambda message: logs.append(str(message)),
    )
    await NovaApp._council_settings(app)
    assert nova_config.NovaConfig().get("model") == {
        "provider": "openai",
        "model": "keep-main-model",
    }
    assert load_settings().members[0].name == (
        "Saved architect" if save else default_settings().members[0].name
    )
    assert bool(logs) == save


@pytest.mark.asyncio
async def test_planner_uses_member_models_and_judge_without_approving(tmp_path: Path):
    from novacode_cli import council_planner as cp
    from tests.test_council_planner import _FakeModel

    settings = default_settings()
    models = {m.id: _FakeModel() for m in settings.members}
    judge, fallback = _FakeModel(), _FakeModel()
    run = cp.CouncilRun(id="settings-routing", prompt="Add a queue")
    events = [
        event
        async for event in cp.run_planning_council(
            run.prompt,
            fallback,
            members=settings.personas(),
            member_models=models,
            judge_model=judge,
            judge_perspective="Prioritize reproducibility",
            run=run,
            store=cp.CouncilArtifactStore(tmp_path),
        )
    ]
    assert events
    assert all(len(model.seen) == 3 for model in models.values())
    assert len(judge.seen) == 1
    assert "Prioritize reproducibility" in judge.seen[0][0]
    assert not fallback.seen
    assert run.status == cp.AWAITING_APPROVAL
    assert run.approved_proposal_id is None


@pytest.mark.asyncio
async def test_web_uses_overrides_for_answers_and_ballots(monkeypatch: pytest.MonkeyPatch):
    settings = default_settings()
    models = {p.id: object() for p in settings.personas(debate=True)}
    calls = []

    async def stream(
        persona: council.Persona,
        _conversation: str,
        model: object,
    ) -> AsyncIterator[tuple[str, str]]:
        calls.append(("answer", persona.id, model, persona.system))
        yield "delta", "An answer"

    async def ballot(
        persona: council.Persona,
        members: list[council.Persona],
        _answers: dict[str, str],
        _topic: str,
        model: object,
    ) -> tuple[council.Persona, str, str]:
        calls.append(("vote", persona.id, model, persona.system))
        return persona, next(p.name for p in members if p.id != persona.id), "reason"

    monkeypatch.setattr(council, "_stream_persona_turn", stream)
    monkeypatch.setattr(council, "_cast_ballot", ballot)
    events = [
        e
        async for e in council.run_council(
            "topic",
            object(),
            settings.personas(debate=True),
            member_models=models,
        )
    ]
    assert events[-1]["type"] == "done"
    assert len(calls) == 10
    assert all(model is models[seat] for _, seat, model, _ in calls)


@pytest.mark.asyncio
async def test_settings_command_does_not_start_a_council():
    from tests.test_council_tui import _Stub

    app = _Stub()
    app._council_settings = AsyncMock()
    await app._run_council("/council settings")
    app._council_settings.assert_awaited_once()
    assert not app.workers
    assert not app.chat_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_editor_preserves_models_templates_and_keeps_buttons_visible(size: tuple[int, int]):
    settings = default_settings()
    settings = replace(
        settings,
        members=(
            replace(settings.members[0], provider="openai", model="custom-model"),
            *settings.members[1:],
        ),
    )
    app = App()
    result = []
    async with app.run_test(size=size) as pilot:
        screen = CouncilSettingsScreen(settings)
        app.push_screen(screen, result.append)
        await pilot.pause()
        assert screen.query_one("#council-model", Input).value == "custom-model"
        for selector in ("#council-reset", "#council-cancel", "#council-save"):
            region = screen.query_one(selector, Button).region
            assert region.right <= size[0]
            assert region.bottom <= size[1]
            assert region.width >= 10
            assert region.height == 3
        screen.query_one("#council-name", Input).value = "Custom Architect"
        seats = screen.query_one("#council-members", OptionList)
        seats.highlighted = 1
        await pilot.pause()
        seats.highlighted = 0
        await pilot.pause()
        assert screen.query_one("#council-name", Input).value == "Custom Architect"
        assert screen.query_one("#council-model", Input).value == "custom-model"
        screen.query_one("#council-template", Select).value = "security"
        screen.query_one("#council-template-apply", Button).press()
        await pilot.pause()
        assert "trust boundaries" in screen.query_one("#council-system", TextArea).text
        assert screen.query_one("#council-model", Input).value == "custom-model"
        screen.query_one("#council-save", Button).press()
        await pilot.pause()
    saved = parse_settings(result[0])
    assert saved.members[0].name == "The Security Reviewer"
    assert saved.members[0].model == "custom-model"
    assert settings.members[0].name != saved.members[0].name


@pytest.mark.asyncio
async def test_editor_cancel_discards_changes_and_invalid_save_stays_open():
    settings = default_settings()
    app, result = App(), []
    async with app.run_test() as pilot:
        screen = CouncilSettingsScreen(settings)
        app.push_screen(screen, result.append)
        await pilot.pause()
        screen.query_one("#council-name", Input).value = ""
        screen.query_one("#council-save", Button).press()
        await pilot.pause()
        assert app.screen is screen
        assert not result
        await pilot.press("escape")
        await pilot.pause()
    assert result == [None]
    assert settings == default_settings()


@pytest.mark.asyncio
async def test_settings_opens_from_real_app_command_worker(monkeypatch: pytest.MonkeyPatch):
    from novacode_cli import council_settings
    from tests.test_agents_screen import _app

    monkeypatch.setattr(council_settings, "load_settings", default_settings)
    app = _app()
    async with app.run_test(size=(80, 24)) as pilot:
        worker = app.run_worker(app._run_slash("/council settings"))
        for _ in range(4):
            await pilot.pause(0.05)
        assert isinstance(app.screen, CouncilSettingsScreen)
        for selector in ("#council-save", "#council-cancel"):
            button = app.screen.query_one(selector, Button)
            assert button.region.bottom <= 24
            assert button.region.right <= 80
        await pilot.press("escape")
        await worker.wait()


def test_web_settings_offer_draft_recovery_for_invalid_saved_settings(monkeypatch):
    from novacode_cli import council_web_settings as web
    from novacode_cli.council_settings import default_settings

    def invalid():
        raise ValueError('Duplicate names')
    monkeypatch.setattr(web, 'load_settings', invalid)
    catalog = web.settings_catalog()
    assert catalog['settings'] == default_settings().to_dict()
    assert 'Duplicate names' in catalog['warning']
