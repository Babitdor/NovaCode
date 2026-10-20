"""``/council <task>`` in the transcript: one live card, then plan cards with actions.

The planner streams events (``council_planner.run_planning_council``). Instead of
a line per event, :class:`CouncilCard` keeps the whole run in one panel that
updates in place: a phase stepper, every agent's progress, and the counts that
matter. When the plans land, :func:`plan_cards` renders one card each and
:class:`CouncilActions` puts approve / view / revise / reject one click away; the
typed ``/council`` verbs keep working alongside them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.containers import Horizontal
from textual.widgets import Button, Static

from novacode_cli.council_planner import BRAINSTORMING, CRITIQUING, JUDGING, VOTING
from novacode_cli.tui import motion

if TYPE_CHECKING:
    from novacode_cli.tui.palette import FooterPalette

#: (phase, label) in the order the council runs them.
#: Vote-split entropy above which the council "split", and where it was "leaning".
SPLIT, LEANING = 0.9, 0.5

PHASES = ((BRAINSTORMING, "Plan"), (CRITIQUING, "Critique"), (VOTING, "Vote"), (JUDGING, "Judge"))


@dataclass
class CouncilProgress:
    """Everything the card shows, folded from the planner's events."""

    prompt: str
    agents: list[dict[str, str]] = field(default_factory=list)
    phase: str = ""
    started: float = field(default_factory=time.monotonic)
    done: dict[str, set[str]] = field(default_factory=dict)  # phase -> agent ids
    failed: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (phase, why)
    proposals: int = 0
    critiques: int = 0
    ballots: int = 0
    entropy: float | None = None
    outcome: str = ""  # "", "done" or "failed"
    message: str = ""
    finished_at: float | None = None

    def apply(self, name: str, payload: dict[str, Any]) -> None:
        """Fold one planner event into the state."""
        if name == "council.started":
            self.agents = list(payload.get("agents") or [])
        elif name == "council.phase.started":
            self.phase = payload.get("phase", "")
        elif name == "agent.completed":
            self.done.setdefault(payload.get("phase", ""), set()).add(payload["agent_id"])
        elif name == "proposal.critiqued":
            self.done.setdefault(CRITIQUING, set()).add(payload["critic_id"])
            self.critiques += 1
        elif name == "agent.failed":
            why = payload.get("reason") or "no reply"
            self.failed[payload["agent_id"]] = (payload.get("phase", ""), why)
        elif name == "proposal.created":
            self.proposals += 1
        elif name == "vote.submitted":
            self.ballots = int(payload.get("cast", self.ballots + 1))
        elif name == "vote.tallied":
            self.entropy = float((payload.get("tally") or {}).get("entropy", 0.0))
        elif name == "plans.selected":
            self.finish("done")
        elif name == "council.failed":
            self.finish("failed", payload.get("reason", ""))

    def finish(self, outcome: str, message: str = "") -> None:
        """Stop the clock with a final outcome."""
        self.outcome, self.message = outcome, message
        self.finished_at = time.monotonic()

    def elapsed(self) -> str:
        """``m:ss`` since the council convened."""
        seconds = int((self.finished_at or time.monotonic()) - self.started)
        return f"{seconds // 60}:{seconds % 60:02d}"

    def phase_index(self) -> int:
        """Position of the current phase (past the end once finished)."""
        if self.outcome == "done":
            return len(PHASES)
        return next((i for i, (p, _) in enumerate(PHASES) if p == self.phase), -1)


def _cell(
    progress: CouncilProgress, agent_id: str, phase: str, spin: str, pal: FooterPalette
) -> Text:
    """One agent's status in one phase: done, failed, working or not reached."""
    position = progress.phase_index()
    index = next(i for i, (p, _) in enumerate(PHASES) if p == phase)
    failed = progress.failed.get(agent_id)
    if failed and failed[0] == phase:
        return Text("✖", style=pal.error)
    if agent_id in progress.done.get(phase, set()):
        return Text("✓", style=pal.success)
    if failed or index > position or progress.outcome:
        return Text("·", style=pal.faint)
    if index < position:  # an earlier phase finished without word from this agent
        return Text("·", style=pal.faint)
    return Text(spin, style=pal.accent)


def render_progress(progress: CouncilProgress, pal: FooterPalette, tick: int) -> Panel:
    """The live card: phase stepper, per-agent progress and running counts."""
    spin = motion.spinner(tick)
    position = progress.phase_index()

    stepper = Text()
    for i, (_, label) in enumerate(PHASES):
        if i:
            stepper.append("  ─  ", style=pal.faint)
        if i < position:
            stepper.append(f"✓ {label}", style=pal.success)
        elif i == position and not progress.outcome:
            stepper.append(f"{spin} {label}", style=f"bold {pal.accent}")
        elif progress.outcome == "failed" and i == position:
            stepper.append(f"✖ {label}", style=pal.error)
        else:
            stepper.append(f"○ {label}", style=pal.faint)
    if position == len(PHASES) - 1 and not progress.outcome:
        stepper.append("   weighing plans against the rubric…", style=pal.muted)

    agents = Table.grid(padding=(0, 2))
    agents.add_column(no_wrap=True)
    agents.add_column(justify="center")
    agents.add_column(justify="center")
    agents.add_row(Text(""), Text("plan", style=pal.muted), Text("critique", style=pal.muted))
    for agent in progress.agents:
        name = Text(f"{agent.get('avatar', '')} ", style="")
        name.append(agent.get("name", agent.get("id", "")), style=agent.get("color") or pal.text)
        agents.add_row(
            name,
            _cell(progress, agent["id"], BRAINSTORMING, spin, pal),
            _cell(progress, agent["id"], CRITIQUING, spin, pal),
        )

    counts = Text(style=pal.muted)
    counts.append(f"{progress.proposals} plans")
    counts.append(f" · {progress.critiques} critiques")
    voters = len(progress.agents) or progress.ballots
    counts.append(f" · {progress.ballots}/{voters} ballots")
    if progress.entropy is not None:
        mood = (
            "split"
            if progress.entropy >= SPLIT
            else "leaning"
            if progress.entropy >= LEANING
            else "agreed"
        )
        counts.append(f" · council {mood} ({progress.entropy:.2f})")

    body: list[Any] = [
        Text(progress.prompt.splitlines()[0][:160], style=pal.text),
        Text(),
        stepper,
        Text(),
        agents,
    ]
    for agent_id, (phase, why) in progress.failed.items():
        name = next(
            (a.get("name", agent_id) for a in progress.agents if a.get("id") == agent_id), agent_id
        )
        body.append(Text(f"✖ {name} dropped out of {phase}: {why[:120]}", style=pal.warning))
    body += [Text(), counts]
    if progress.outcome == "failed" and progress.message:
        body.append(Text(progress.message, style=pal.error))

    border = {"done": pal.success, "failed": pal.error}.get(progress.outcome, pal.accent)
    status = {"done": "plans ready", "failed": "stopped"}.get(
        progress.outcome, "planning, not editing"
    )
    return Panel(
        Group(*body),
        title=Text.assemble(("◈ Council", f"bold {pal.accent}"), (f"  {status}", pal.muted)),
        title_align="left",
        subtitle=Text(progress.elapsed(), style=pal.muted),
        subtitle_align="right",
        border_style=border,
        padding=(0, 1),
    )


def _score_bar(score: float, pal: FooterPalette) -> Text:
    filled = max(0, min(10, round(score)))
    return Text.assemble(
        ("█" * filled, pal.success if score >= 7 else pal.warning),  # noqa: PLR2004
        ("░" * (10 - filled), pal.faint),
        (f" {score:.1f}/10", pal.muted),
    )


def plan_cards(run: Any, pal: FooterPalette) -> list[Panel]:  # noqa: ANN401
    """One card per selected plan, best first."""
    cards = []
    firsts = (run.tally or {}).get("first_choice") or {}
    for plan in run.ranked_plans():
        proposal = run.proposal(plan.proposal_id)
        if proposal is None:
            continue
        best = plan.rank == 1
        body: list[Any] = [Text(proposal.title, style=f"bold {pal.text}")]
        if proposal.summary:
            body.append(Text(proposal.summary, style=pal.muted))
        meta = _score_bar(plan.score, pal)
        votes = firsts.get(plan.proposal_id, 0)
        meta.append(f"   {votes} first-place vote{'s' if votes != 1 else ''}", style=pal.muted)
        steps = len(proposal.implementation_steps)
        if steps:
            meta.append(f" · {steps} steps", style=pal.muted)
        body += [Text(), meta]
        for pro in proposal.tradeoffs.get("pros", [])[:2]:
            body.append(Text(f"+ {pro}", style=pal.success))
        for con in proposal.tradeoffs.get("cons", [])[:2]:
            body.append(Text(f"- {con}", style=pal.warning))
        files = proposal.files_likely_affected
        if files:
            more = f" (+{len(files) - 3})" if len(files) > 3 else ""  # noqa: PLR2004
            body.append(Text(f"files  {', '.join(files[:3])}{more}", style=pal.muted))
        if plan.reasoning:
            body.append(
                Text(f"judge  {plan.reasoning.splitlines()[0][:200]}", style=f"italic {pal.muted}")
            )
        head = "Recommended" if best else "Alternative"
        cards.append(
            Panel(
                Group(*body),
                title=Text.assemble(
                    (f" #{plan.rank} ", f"bold reverse {pal.success if best else pal.muted}"),
                    (f" {head}", pal.success if best else pal.muted),
                ),
                title_align="left",
                border_style=pal.success if best else pal.faint,
                padding=(0, 1),
            )
        )
    return cards


class CouncilCard(Static):
    """The live progress card; ticks its spinner and clock until the run ends."""

    DEFAULT_CSS = "CouncilCard { margin: 1 0 0 0; height: auto; }"

    def __init__(self, prompt: str) -> None:  # noqa: D107
        super().__init__("")
        self.progress = CouncilProgress(prompt=prompt)
        self._tick = 0

    def on_mount(self) -> None:
        """Paint now, then animate until the council finishes."""
        self._timer = self.set_interval(1 / 8, self._animate)
        self.repaint()

    def _animate(self) -> None:
        self._tick += 1
        if self.progress.outcome:
            self._timer.stop()
        self.repaint()

    def apply(self, name: str, payload: dict[str, Any]) -> None:
        """Fold an event in and repaint."""
        self.progress.apply(name, payload)
        self.repaint()

    def finish(self, outcome: str, message: str = "") -> None:
        """End the run from outside (an exception, a missing model)."""
        self.progress.finish(outcome, message)
        self.repaint()

    def repaint(self) -> None:
        """Render with the app's current theme palette."""
        pal = getattr(self.app, "_palette", None)
        if pal is not None:
            self.update(render_progress(self.progress, pal, self._tick * 6))


class CouncilActions(Horizontal):
    """Approve / view / revise / reject for one run, as buttons."""

    DEFAULT_CSS = """
    CouncilActions { height: auto; margin: 0 0 1 0; }
    CouncilActions Button { min-width: 10; margin: 0 1 0 0; }
    """

    def __init__(self, run_id: str, count: int) -> None:  # noqa: D107
        super().__init__()
        self._run_id = run_id
        self._count = count

    def compose(self) -> Any:  # noqa: ANN401
        """One approve button per plan, then the verbs that need no number."""
        for n in range(1, self._count + 1):
            yield Button(
                f"Approve #{n}",
                id=f"council-approve-{n}",
                variant="success" if n == 1 else "default",
            )
        yield Button("View plan…", id="council-view")
        yield Button("Revise…", id="council-revise")
        yield Button("Reject", id="council-reject", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route to the same handlers the typed ``/council`` verbs use."""
        event.stop()
        app: Any = self.app
        app._council_run_id = self._run_id
        button = event.button.id or ""
        if button.startswith("council-approve-"):
            self.disable(f"Approved plan #{button.rsplit('-', 1)[1]}")
            app.run_worker(app._council_subcommand("approve", button.rsplit("-", 1)[1]))
        elif button == "council-view":
            app.run_worker(self._view())
        elif button == "council-revise":
            composer = app.query_one("#prompt")
            composer.text = "/council revise "
            composer.focus()
        elif button == "council-reject":
            self.disable("Rejected")
            app.run_worker(app._council_subcommand("reject", ""))

    async def _view(self) -> None:
        from novacode_cli.tui.screens import PickScreen

        app: Any = self.app
        labels = [f"Plan #{n}" for n in range(1, self._count + 1)]
        pick = await app.push_screen_wait(PickScreen("Show which plan in full?", labels))
        if 0 <= pick < self._count:
            await app._council_subcommand("view", str(pick + 1))

    def disable(self, note: str) -> None:
        """Freeze the bar once the run has been decided."""
        for button in self.query(Button):
            button.disabled = True
        self.mount(Static(Text(f"  {note}", style="dim"), classes="council-decided"))
