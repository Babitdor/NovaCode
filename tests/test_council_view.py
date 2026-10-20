"""/council in the transcript: the live card, plan cards and the action buttons."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from novacode_cli import council_planner as cp
from novacode_cli.tui.council_view import CouncilProgress, plan_cards, render_progress
from novacode_cli.tui.palette import palette_for
from novacode_cli.tui.widgets import DEFAULT_THEME


def _text(renderable) -> str:
    # Into a buffer: inside a running app stdout may not encode box drawing.
    console = Console(width=100, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()


def _palette():
    from textual.app import App

    app = App()
    return palette_for(app.get_theme(DEFAULT_THEME) or app.current_theme)


AGENTS = [
    {"id": "architect", "name": "The Architect", "avatar": "A", "color": "#7aa2f7"},
    {"id": "skeptic", "name": "The Skeptic", "avatar": "S", "color": "#e0af68"},
]


def test_progress_folds_events_into_phases_agents_and_counts():
    p = CouncilProgress(prompt="add jobs")
    p.apply("council.started", {"agents": AGENTS})
    p.apply("council.phase.started", {"phase": cp.BRAINSTORMING})
    p.apply("agent.completed", {"agent_id": "architect", "phase": cp.BRAINSTORMING})
    p.apply(
        "agent.failed",
        {
            "agent_id": "skeptic",
            "name": "The Skeptic",
            "phase": cp.BRAINSTORMING,
            "reason": "timeout",
        },
    )
    p.apply("proposal.created", {"proposal_id": "proposal-01", "title": "A"})
    p.apply("council.phase.started", {"phase": cp.VOTING})
    p.apply("vote.submitted", {"cast": 1})
    assert p.phase_index() == 2
    assert p.done[cp.BRAINSTORMING] == {"architect"}
    assert p.failed["skeptic"] == (cp.BRAINSTORMING, "timeout")
    assert (p.proposals, p.ballots) == (1, 1)
    card = _text(render_progress(p, _palette(), tick=0))
    assert "The Architect" in card
    assert "The Skeptic dropped out of brainstorming: timeout" in card
    assert "1/2 ballots" in card
    p.apply("plans.selected", {})
    assert p.outcome == "done"
    assert "plans ready" in _text(render_progress(p, _palette(), tick=0))


def test_plan_cards_rank_score_and_trade_offs():
    run = cp.CouncilRun(id="r", prompt="t")
    run.proposals = [
        cp.Proposal(
            id="proposal-01",
            agent_id="a",
            title="Queue it",
            summary="Use a job queue",
            tradeoffs={"pros": ["durable"], "cons": ["a new dependency"]},
            files_likely_affected=["a.py", "b.py", "c.py", "d.py"],
        ),
        cp.Proposal(id="proposal-02", agent_id="b", title="Thread it", summary="A thread"),
    ]
    run.judgment = cp.Judgment(
        selected_plans=[
            cp.SelectedPlan("proposal-01", 1, 8.6, "Correct and testable."),
            cp.SelectedPlan("proposal-02", 2, 6.1),
        ]
    )
    run.tally = {"first_choice": {"proposal-01": 4}}
    first, second = (_text(c) for c in plan_cards(run, _palette()))
    assert "#1" in first
    assert "Recommended" in first
    assert "8.6/10" in first
    assert "4 first-place votes" in first
    assert "+ durable" in first
    assert "a.py, b.py, c.py (+1)" in first
    assert "Correct and testable." in first
    assert "Alternative" in second


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from novacode_cli.config import config
    from novacode_cli.tui.app import NovaApp

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / ".nova")
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setattr(NovaApp, "_eager_voice_warmup", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated")
@pytest.mark.parametrize(
    ("button", "expected"),
    [("council-approve-2", ("approve", "2")), ("council-reject", ("reject", ""))],
)
async def test_action_buttons_run_the_council_verbs_once(monkeypatch, button, expected):
    from textual.widgets import Button

    from novacode_cli.tui.council_view import CouncilActions
    from tests.test_tui_sessions import _app

    app = _app()
    calls = []

    async def subcommand(sub: str, rest: str) -> None:
        calls.append((sub, rest))

    monkeypatch.setattr(app, "_council_subcommand", subcommand)
    async with app.run_test(size=(120, 40)) as pilot:
        bar = CouncilActions("council-x", 3)
        await app._transcript().mount(bar)
        await pilot.pause()
        bar.query_one(f"#{button}", Button).press()
        await pilot.pause()
        assert calls == [expected]
        assert app._council_run_id == "council-x"
        assert all(b.disabled for b in bar.query(Button)), "a decided run stays clickable"


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated")
async def test_revise_puts_the_command_in_the_composer():
    from textual.widgets import Button

    from novacode_cli.tui.council_view import CouncilActions
    from novacode_cli.tui.widgets import PromptInput
    from tests.test_tui_sessions import _app

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        bar = CouncilActions("council-x", 2)
        await app._transcript().mount(bar)
        await pilot.pause()
        bar.query_one("#council-revise", Button).press()
        await pilot.pause()
        assert app.query_one("#prompt", PromptInput).text == "/council revise "


@pytest.mark.asyncio
async def test_each_agent_reports_as_soon_as_its_plan_lands():
    from tests.test_council_planner import _FakeModel

    events = [e async for e in cp.run_planning_council("t", _FakeModel(fail_agents={"skeptic"}))]
    names = [e["event"] for e in events]
    first_proposal = names.index("proposal.created")
    landed = [
        e for e in events[:first_proposal] if e["event"] in ("agent.completed", "agent.failed")
    ]
    assert len(landed) == len(cp.PLANNING_PERSONAS)
    assert {e["payload"]["agent_id"] for e in landed if e["event"] == "agent.failed"} == {"skeptic"}


def test_progress_fraction_walks_the_four_phases():
    p = CouncilProgress(prompt="t")
    p.apply("council.started", {"agents": AGENTS})
    assert p.fraction() == 0.0
    p.apply("council.phase.started", {"phase": cp.BRAINSTORMING})
    p.apply("agent.completed", {"agent_id": "architect", "phase": cp.BRAINSTORMING})
    assert p.fraction() == pytest.approx(0.125)  # half of the first quarter
    p.apply("council.phase.started", {"phase": cp.VOTING})
    p.apply("vote.submitted", {"cast": 2})
    assert p.fraction() == pytest.approx(0.75)
    p.apply("plans.selected", {})
    assert p.fraction() == 1.0


def test_motion_pops_a_fresh_tick_and_settles_the_border():
    import time

    from novacode_cli.tui import council_view as cv

    pal = _palette()
    p = CouncilProgress(prompt="t")
    p.apply("council.started", {"agents": AGENTS})
    p.apply("council.phase.started", {"phase": cp.BRAINSTORMING})
    p.apply("agent.completed", {"agent_id": "architect", "phase": cp.BRAINSTORMING})
    now = time.monotonic()
    fresh = cv._cell(p, "architect", cp.BRAINSTORMING, "*", pal, now)
    assert "reverse" in str(fresh.style), "a tick that just landed should flash"
    settled = cv._cell(p, "architect", cp.BRAINSTORMING, "*", pal, now + cv.POP + 0.1)
    assert "reverse" not in str(settled.style)
    assert "%" in _text(render_progress(p, pal, 0, now=now, bar=0.1))
    p.finish("done")
    assert cv._border(p, pal, p.finished_at + cv.SETTLE + 0.1) == cv.motion._blend(
        pal.accent, pal.success, 1.0
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated")
async def test_low_resource_mode_shows_final_cards_without_motion():
    from novacode_cli.tui.council_view import CouncilActions, PlanCard
    from tests.test_tui_sessions import _app

    run = cp.CouncilRun(id="r", prompt="t")
    run.proposals = [cp.Proposal(id="p1", agent_id="a", title="T", summary="S")]
    run.judgment = cp.Judgment(selected_plans=[cp.SelectedPlan("p1", 1, 8.0)])
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        app._low_resource_mode = True
        card = PlanCard(run, run.ranked_plans()[0], delay=0.5)
        bar = CouncilActions("r", 1, delay=0.5)
        await app._transcript().mount(card, bar)
        await pilot.pause()
        assert "8.0/10" in _text(card.content)
        assert card.styles.opacity == 1.0
        assert bar.styles.opacity == 1.0


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated")
async def test_the_card_keeps_moving_between_planner_events():
    """Its timer must call a real method: Textual sets ``_animate = None`` on widgets."""
    from novacode_cli.tui.council_view import CouncilCard
    from tests.test_tui_sessions import _app

    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        card = CouncilCard("t")
        await app._transcript().mount(card)
        card.apply("council.phase.started", {"phase": cp.BRAINSTORMING})
        before = _text(card.content)
        card.progress.started -= 61  # a minute in, with no events since
        await pilot.pause(0.2)
        assert callable(card._timer._callback)
        assert "1:01" in _text(card.content), "the clock only moved on events"
        assert before != _text(card.content)
