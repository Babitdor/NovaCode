"""`/council` command routing, and the approval gate at the TUI layer.

The planner enforces its own invariants (tests/test_council_planner.py). What
this file pins is the wiring around it: that `/council` still opens the web UI
it has always opened, that a task starts a planning run instead, and — the one
that matters — that no path from a slash command reaches the coding agent
without an approval the planner accepted.

The real methods are bound to a stub rather than booting a NovaApp: the routing
is plain logic, and a full Textual app in the loop buys nothing here but flakes.
"""

from __future__ import annotations

import pytest

try:
    from novacode_cli.tui.app import NovaApp

    _HAS_TUI = True
except ImportError:  # pragma: no cover
    _HAS_TUI = False

from novacode_cli import council_planner as cp

pytestmark = pytest.mark.skipif(_HAS_TUI is False, reason="textual not installed")


class _Stub:
    """Records what the handler did instead of touching a terminal or a model."""

    def __init__(self) -> None:
        self.logged: list[str] = []
        self.chat_calls: list[str] = []
        self.prompts: list[str] = []
        self.workers: list[object] = []

    def _log(self, renderable: object, **_kwargs: object) -> None:
        self.logged.append(str(renderable))

    def run_worker(self, coro) -> None:
        self.workers.append(coro)
        coro.close()  # never actually run a model in a unit test

    async def _run_chat(self, text: str) -> None:
        self.chat_calls.append(text)

    async def _stream_prompt(self, text: str, assistant_id=None) -> None:
        self.prompts.append(text)

    async def _council_plan(self, prompt: str, run=None) -> None:
        self.planned = (prompt, run)


if _HAS_TUI:
    for _name in (
        "_run_council",
        "_council_subcommand",
        "_council_store",
        "_council_target",
        "_council_render_plans",
        "_council_show_plan",
        "_council_context",
    ):
        setattr(_Stub, _name, getattr(NovaApp, _name))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep .nova/council out of the real repo
    return _Stub()


def _awaiting_run() -> cp.CouncilRun:
    run = cp.CouncilRun(id="council-20260830-000000-test", prompt="add jobs")
    run.proposals = [
        cp.Proposal(id="proposal-01", agent_id="architect", title="A", summary="sa"),
        cp.Proposal(id="proposal-02", agent_id="skeptic", title="B", summary="sb"),
    ]
    run.judgment = cp.Judgment(
        selected_plans=[
            cp.SelectedPlan("proposal-02", 1, 9.0, "best on correctness"),
            cp.SelectedPlan("proposal-01", 2, 8.0, "simpler"),
        ]
    )
    run.status = cp.AWAITING_APPROVAL
    return run


# ── Routing ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/council", "/council stop"])
async def test_bare_council_still_opens_the_web_ui(app, text):
    """Existing behaviour is preserved — this command has always done this."""
    await app._run_council(text)
    assert app.chat_calls == [text]
    assert app.workers == []


@pytest.mark.asyncio
async def test_a_task_starts_a_planning_run_not_the_web_ui(app):
    await app._run_council("/council add background task execution")
    assert app.chat_calls == []
    assert len(app.workers) == 1


@pytest.mark.asyncio
async def test_subcommands_do_not_start_a_planning_run(app):
    """'/council view' must not be mistaken for a task called 'view'."""
    for sub in ("view", "history", "reject", "cancel"):
        stub = _Stub()
        await stub._run_council(f"/council {sub}")
        assert stub.workers == [], f"'{sub}' was treated as a task to plan"
        assert stub.chat_calls == []


@pytest.mark.asyncio
async def test_history_reports_when_there_is_nothing_yet(app):
    await app._run_council("/council history")
    assert any("No council runs" in line for line in app.logged)


# ── The approval gate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approving_a_presented_plan_hands_it_to_the_coding_agent(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council approve 1")

    assert len(app.prompts) == 1
    handoff = app.prompts[0]
    assert "APPROVED" in handoff
    assert "# Implementation Plan" in handoff
    # The executor is told not to relitigate the choice.
    assert "Do not redesign it" in handoff


@pytest.mark.asyncio
async def test_a_cancelled_run_cannot_be_approved_into_execution(app):
    """The gate that stops a dead run being replayed into real work."""
    run = _awaiting_run()
    run.status = cp.CANCELLED
    app._council_store().save(run)

    await app._run_council("/council approve 1")
    assert app.prompts == [], "a cancelled run reached the coding agent"
    assert any("not awaiting approval" in line for line in app.logged)


@pytest.mark.asyncio
async def test_approving_without_a_run_reaches_no_agent(app):
    await app._run_council("/council approve 1")
    assert app.prompts == []
    assert any("No council run" in line for line in app.logged)


@pytest.mark.asyncio
async def test_approve_needs_a_number(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council approve")
    assert app.prompts == []
    assert any("Which plan?" in line for line in app.logged)


@pytest.mark.asyncio
async def test_an_out_of_range_choice_reaches_no_agent(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council approve 9")
    assert app.prompts == []
    assert any("between 1 and 2" in line for line in app.logged)


@pytest.mark.asyncio
async def test_approval_survives_a_restart(app):
    """A run approved from disk, with nothing in this session's memory."""
    app._council_store().save(_awaiting_run())
    fresh = _Stub()  # no _council_run_id — as after a restart
    await fresh._run_council("/council approve 1")
    assert len(fresh.prompts) == 1


# ── Revision and cancellation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revision_records_the_note_and_replans(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council revise use the simpler state machine")

    assert len(app.workers) == 1  # a new planning round started
    reloaded = app._council_store().latest()
    assert reloaded.revision_notes == ["use the simpler state machine"]
    assert reloaded.status == cp.INITIALIZING


@pytest.mark.asyncio
async def test_revision_without_notes_asks_instead_of_replanning(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council revise")
    assert app.workers == []
    assert any("Say what to change" in line for line in app.logged)


@pytest.mark.asyncio
async def test_reject_ends_the_session_without_implementing(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council reject")
    assert app.prompts == []
    assert app._council_store().latest().status == cp.REJECTED


# ── Presentation ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_lists_the_plans_with_how_to_act_on_them(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council view")
    joined = "\n".join(app.logged)
    assert "#1" in joined
    assert "/council approve" in joined


@pytest.mark.asyncio
async def test_view_n_renders_the_full_plan(app):
    app._council_store().save(_awaiting_run())
    await app._run_council("/council view 1")
    assert app.logged, "nothing was rendered"


def test_command_help_mentions_the_planning_verbs():
    """Autocomplete and /help both derive from this one table entry."""
    from novacode_cli.tui.app import TUI_COMMANDS

    entry = TUI_COMMANDS["council"]
    assert entry.handler == "_run_council"
    assert entry.wants_text is True
    assert "approve" in entry.help


@pytest.mark.asyncio
async def test_an_implemented_run_is_recorded_against_the_council_run(app):
    """/council history must show what was actually built, not just approved."""
    app._council_store().save(_awaiting_run())
    await app._run_council("/council approve 1")
    assert app._council_store().latest().status == cp.COMPLETED


@pytest.mark.asyncio
async def test_a_failed_implementation_does_not_leave_the_run_executing(app):
    """Otherwise history reports a dead run as still in flight, forever."""
    app._council_store().save(_awaiting_run())

    async def _boom(text, assistant_id=None):
        raise RuntimeError("agent died")

    app._stream_prompt = _boom
    with pytest.raises(RuntimeError):
        await app._run_council("/council approve 1")
    assert app._council_store().latest().status == cp.COMPLETED
