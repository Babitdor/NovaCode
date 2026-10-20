"""``/council <task>`` in the transcript: one live card, then plan cards with actions.

The planner streams events (``council_planner.run_planning_council``). Instead of
a line per event, :class:`CouncilCard` keeps the whole run in one panel that
updates in place: a phase stepper, every agent's progress, and the counts that
matter. When the plans land, :func:`plan_cards` renders one card each and
:class:`CouncilActions` puts approve / view / revise / reject one click away; the
typed ``/council`` verbs keep working alongside them.

Motion, all driven by wall time so it reads the same at any frame rate: the
active phase shimmers, a progress bar glides toward each new value, an agent's
tick pops when it lands, the border breathes while the council works and
cross-fades to green (or red) at the end, and the plan cards fade in one after
another with their score bars filling. Low-resource mode draws the final state
with no motion.
"""

from __future__ import annotations

import math
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
from novacode_cli.tui.animations import animate_entrance

if TYPE_CHECKING:
    from novacode_cli.tui.palette import FooterPalette

#: Vote-split entropy above which the council "split", and where it was "leaning".
SPLIT, LEANING = 0.9, 0.5

#: Motion timings, in seconds.
POP = 0.7  # an agent's tick flashing in
BREATHE = 2.4  # one border breath while the council works
SETTLE = 0.6  # the border's cross-fade to its final colour
STAGGER = 0.14  # between plan cards fading in
FILL = 0.8  # a score bar filling
#: Cells in the overall progress bar.
BAR_WIDTH = 24

#: (phase, label) in the order the council runs them.
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
    #: When each (agent, phase) tick landed, for its pop.
    landed: dict[tuple[str, str], float] = field(default_factory=dict)

    def _land(self, agent_id: str, phase: str) -> None:
        self.done.setdefault(phase, set()).add(agent_id)
        self.landed.setdefault((agent_id, phase), time.monotonic())

    def apply(self, name: str, payload: dict[str, Any]) -> None:
        """Fold one planner event into the state."""
        if name == "council.started":
            self.agents = list(payload.get("agents") or [])
        elif name == "council.phase.started":
            self.phase = payload.get("phase", "")
        elif name == "agent.completed":
            self._land(payload["agent_id"], payload.get("phase", ""))
        elif name == "proposal.critiqued":
            self._land(payload["critic_id"], CRITIQUING)
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

    def fraction(self) -> float:
        """How far through the four phases the council is, 0 to 1."""
        if self.outcome == "done":
            return 1.0
        index = self.phase_index()
        if index < 0:
            return 0.0
        agents = max(1, len(self.agents))
        phase = PHASES[index][0]
        if phase == VOTING:
            within = self.ballots / agents
        elif phase == JUDGING:
            within = 0.5  # one call with no progress of its own
        else:
            settled = len(self.done.get(phase, set()))
            settled += sum(1 for p, _ in self.failed.values() if p == phase)
            within = settled / agents
        return (index + min(1.0, within)) / len(PHASES)

    def phase_index(self) -> int:
        """Position of the current phase (past the end once finished)."""
        if self.outcome == "done":
            return len(PHASES)
        return next((i for i, (p, _) in enumerate(PHASES) if p == self.phase), -1)


def _ease(t: float) -> float:
    """Ease-out cubic on 0..1."""
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _cell(  # noqa: PLR0911 — one return per visual state
    progress: CouncilProgress,
    agent_id: str,
    phase: str,
    spin: str,
    pal: FooterPalette,
    now: float | None = None,
) -> Text:
    """One agent's status in one phase: done, failed, working or not reached.

    With ``now``, a tick that landed under :data:`POP` seconds ago flashes in.
    """
    position = progress.phase_index()
    index = next(i for i, (p, _) in enumerate(PHASES) if p == phase)
    failed = progress.failed.get(agent_id)
    if failed and failed[0] == phase:
        return Text("✖", style=pal.error)
    if agent_id in progress.done.get(phase, set()):
        age = now - progress.landed.get((agent_id, phase), 0.0) if now is not None else POP
        if age < POP / 3:
            return Text(" ✓ ", style=f"bold reverse {pal.success}")
        if age < POP:
            fade = motion._blend(pal.text, pal.success, age / POP)
            return Text("✓", style=f"bold {fade}")
        return Text("✓", style=pal.success)
    if failed or index > position or progress.outcome:
        return Text("·", style=pal.faint)
    if index < position:  # an earlier phase finished without word from this agent
        return Text("·", style=pal.faint)
    return Text(spin, style=pal.accent)


def _bar(fraction: float, pal: FooterPalette) -> Text:
    """The overall progress bar: block cells (thin box lines read as a divider)."""
    cells = max(0.0, min(1.0, fraction)) * BAR_WIDTH
    full = int(cells)
    bar = Text("█" * full, style=pal.accent)
    if full < BAR_WIDTH:
        half = cells - full >= 0.5  # noqa: PLR2004
        bar.append("▌" if half else "░", style=pal.accent if half else pal.faint)
        bar.append("░" * (BAR_WIDTH - full - 1), style=pal.faint)
    bar.append(f"  {round(fraction * 100):>3}%", style=pal.muted)
    return bar


def _border(progress: CouncilProgress, pal: FooterPalette, now: float | None) -> str:
    """Accent breathing toward faint while running; a cross-fade to the outcome."""
    final = {"done": pal.success, "failed": pal.error}.get(progress.outcome)
    if now is None:
        return final or pal.accent
    if final is not None:
        settled = (now - (progress.finished_at or now)) / SETTLE
        return motion._blend(pal.accent, final, _ease(settled))
    breath = 0.5 + 0.5 * math.sin(2 * math.pi * (now - progress.started) / BREATHE)
    return motion._blend(pal.accent, pal.faint, 0.45 * breath)


def render_progress(  # noqa: PLR0912 — one branch per stepper state
    progress: CouncilProgress,
    pal: FooterPalette,
    tick: int,
    *,
    now: float | None = None,
    bar: float | None = None,
) -> Panel:
    """The live card: phase stepper, per-agent progress and running counts.

    ``now`` turns the motion on (shimmer, pops, breathing border); ``bar`` is the
    eased value for the progress bar, else its exact target.
    """
    spin = motion.spinner(tick)
    position = progress.phase_index()

    stepper = Text()
    for i, (_, label) in enumerate(PHASES):
        if i:
            stepper.append("  ─  ", style=pal.faint)
        if i < position:
            stepper.append(f"✓ {label}", style=pal.success)
        elif i == position and not progress.outcome:
            stepper.append(f"{spin} ", style=f"bold {pal.accent}")
            if now is None:
                stepper.append(label, style=f"bold {pal.accent}")
            else:
                stepper.append_text(motion.shimmer(label, tick, pal, base=pal.accent))
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
            _cell(progress, agent["id"], BRAINSTORMING, spin, pal, now),
            _cell(progress, agent["id"], CRITIQUING, spin, pal, now),
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
        _bar(progress.fraction() if bar is None else bar, pal),
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

    border = _border(progress, pal, now)
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


def _score_bar(score: float, pal: FooterPalette, fill: float = 1.0) -> Text:
    """The 0-10 judge score as a bar; ``fill`` animates it (and its number) up."""
    shown = score * _ease(fill)
    filled = max(0, min(10, round(shown)))
    return Text.assemble(
        ("█" * filled, pal.success if score >= 7 else pal.warning),  # noqa: PLR2004
        ("░" * (10 - filled), pal.faint),
        (f" {shown:.1f}/10", pal.muted),
    )


def plan_cards(run: Any, pal: FooterPalette) -> list[Panel]:  # noqa: ANN401
    """One card per selected plan, best first."""
    cards = [plan_card(run, plan, pal) for plan in run.ranked_plans()]
    return [card for card in cards if card is not None]


def plan_card(run: Any, plan: Any, pal: FooterPalette, fill: float = 1.0) -> Panel | None:  # noqa: ANN401
    """One selected plan as a card; ``fill`` is the score bar's animation."""
    proposal = run.proposal(plan.proposal_id)
    if proposal is None:
        return None
    firsts = (run.tally or {}).get("first_choice") or {}
    best = plan.rank == 1
    body: list[Any] = [Text(proposal.title, style=f"bold {pal.text}")]
    if proposal.summary:
        body.append(Text(proposal.summary, style=pal.muted))
    meta = _score_bar(plan.score, pal, fill)
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
        note = plan.reasoning.splitlines()[0][:200]
        body.append(Text(f"judge  {note}", style=f"italic {pal.muted}"))
    head = "Recommended" if best else "Alternative"
    return Panel(
        Group(*body),
        title=Text.assemble(
            (f" #{plan.rank} ", f"bold reverse {pal.success if best else pal.muted}"),
            (f" {head}", pal.success if best else pal.muted),
        ),
        title_align="left",
        border_style=pal.success if best else pal.faint,
        padding=(0, 1),
    )


def _motion_on(app: Any) -> bool:  # noqa: ANN401
    """Motion everywhere except low-resource mode."""
    return not getattr(app, "_low_resource_mode", False)


def _frame(app: Any) -> float:  # noqa: ANN401
    """Seconds per frame: the user's animation rate, capped at 30 for a card."""
    return 1 / min(30, int(getattr(app, "_animation_fps", 30) or 30))


def _fade_in_after(widget: Static | Horizontal, delay: float) -> None:
    """Hide now, fade in after ``delay`` (a staggered entrance)."""
    widget.styles.opacity = 0.0
    widget.set_timer(max(0.01, delay), lambda: animate_entrance(widget, "fade"))


class PlanCard(Static):
    """One plan card that fades in after ``delay`` and fills its score bar."""

    DEFAULT_CSS = "PlanCard { height: auto; margin: 1 0 0 0; }"

    def __init__(self, run: Any, plan: Any, delay: float = 0.0) -> None:  # noqa: ANN401, D107
        super().__init__("")
        self._run, self._plan, self._delay = run, plan, delay
        self._shown_at: float | None = None

    def on_mount(self) -> None:
        """Static in low-resource mode; otherwise fade in, then fill."""
        if not _motion_on(self.app):
            self._paint(1.0)
            return
        self._paint(0.0)
        _fade_in_after(self, self._delay)
        self._timer = self.set_interval(_frame(self.app), self._step)

    def _step(self) -> None:
        # Timed from the first frame, not the mount: mounting a batch of cards
        # can stall the UI long enough to finish a mount-timed fill unseen.
        now = time.monotonic()
        if self._shown_at is None:
            self._shown_at = now + self._delay
        fill = (now - self._shown_at) / FILL
        if fill >= 1:
            self._timer.stop()
        self._paint(fill)

    def _paint(self, fill: float) -> None:
        pal = getattr(self.app, "_palette", None)
        if pal is not None:
            card = plan_card(self._run, self._plan, pal, max(0.0, min(1.0, fill)))
            if card is not None:
                self.update(card)


class CouncilCard(Static):
    """The live progress card; ticks its spinner and clock until the run ends."""

    DEFAULT_CSS = "CouncilCard { margin: 1 0 0 0; height: auto; }"

    def __init__(self, prompt: str) -> None:  # noqa: D107
        super().__init__("")
        self.progress = CouncilProgress(prompt=prompt)
        self._bar = 0.0
        self._last = time.monotonic()
        self._motion = False

    def on_mount(self) -> None:
        """Fade in, then animate until the council has finished and settled."""
        self._motion = _motion_on(self.app)
        if self._motion:
            animate_entrance(self, "slide")
        # Low-resource mode still ticks (the spinner and clock), just slowly.
        self._timer = self.set_interval(_frame(self.app) if self._motion else 0.5, self._frame_tick)
        self.repaint()

    # Not `_animate`: Textual sets `self._animate = None` on every widget, which
    # shadowed the method and left the timer calling nothing between events.
    def _frame_tick(self) -> None:
        now = time.monotonic()
        # Glide toward the target rather than jumping; frame-rate independent.
        target = self.progress.fraction()
        self._bar += (target - self._bar) * min(1.0, (now - self._last) * 6)
        self._last = now
        done = self.progress.finished_at
        if done is not None and now - done > SETTLE and abs(target - self._bar) < 0.005:  # noqa: PLR2004
            self._bar = target
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
        if pal is None:
            return
        now = time.monotonic()
        tick = int(now * motion.CLOCK_FPS)
        if self._motion:
            self.update(render_progress(self.progress, pal, tick, now=now, bar=self._bar))
        else:
            self.update(render_progress(self.progress, pal, tick))


class CouncilActions(Horizontal):
    """Approve / view / revise / reject for one run, as buttons."""

    DEFAULT_CSS = """
    CouncilActions { height: auto; margin: 0 0 1 0; }
    CouncilActions Button { min-width: 10; margin: 0 1 0 0; }
    """

    def __init__(self, run_id: str, count: int, delay: float = 0.0) -> None:  # noqa: D107
        super().__init__()
        self._run_id = run_id
        self._count = count
        self._delay = delay

    def on_mount(self) -> None:
        """Arrive after the plan cards it acts on."""
        if _motion_on(self.app):
            _fade_in_after(self, self._delay)

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
        decided = Static(Text(f"  {note}", style="dim"), classes="council-decided")
        self.mount(decided)
        if _motion_on(self.app):
            animate_entrance(decided, "fade")
