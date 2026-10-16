"""Home dashboard panels and local, non-blocking system telemetry."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Static

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from novacode_cli.tui.palette import FooterPalette

_KIB = 1024
_WORDMARK_MIN_WIDTH = 50
_BANNER_DOUBLE_WIDTH = 100

HOME_STYLES = (
    ("Dashboard · navigation and system panels", "dashboard"),
    ("Classic · Matrix banner and chat", "classic"),
    ("Centered · wordmark and focused composer", "centered"),
    ("Banner · bold wordmark and open conversation", "banner"),
    ("Minimal · compact conversation and controls", "minimal"),
)


def home_style(value: object, *, legacy_dashboard: object = True) -> str:
    """Resolve a saved style, retaining the previous Dashboard/Classic choice."""
    if isinstance(value, str) and value in {key for _, key in HOME_STYLES}:
        return value
    return "classic" if legacy_dashboard is False else "dashboard"


@dataclass(frozen=True)
class HomeIdentity:
    """Actual session facts displayed by the alternative home layouts."""

    version: str
    model: str
    provider: str
    workspace: str
    session_id: str
    context_percent: float | None


@dataclass(frozen=True)
class HomePresentation:
    """The themed text shared by the home layout widgets."""

    art: Text
    details: Text
    tip: Text
    metadata: Text


def home_presentation(
    style: str,
    width: int,
    identity: HomeIdentity,
    palette: FooterPalette,
) -> HomePresentation:
    """Apply Nova's branding to each layout without inventing session metrics."""
    from novacode_cli.brand import WORDMARK_LINES, compact_mark, wordmark

    logo = wordmark() if width >= _WORDMARK_MIN_WIDTH else compact_mark()
    details = f"Nova Code  v{identity.version}"
    if style == "banner" and width >= _BANNER_DOUBLE_WIDTH:
        logo = "\n".join("".join(char * 2 for char in line) for line in WORDMARK_LINES)
    art = Text(logo, style=f"bold {palette.text if style == 'centered' else palette.accent}")
    if style == "minimal":
        facts = [details, f"model  {identity.model}", f"cwd    {identity.workspace}"]
        art = Text(no_wrap=True, overflow="ellipsis")
        for index, line in enumerate(WORDMARK_LINES):
            art.append(line[:10] + "   ", style=palette.text)
            art.append((facts[index] if index < len(facts) else "") + "\n", style=palette.muted)
        details = (
            f"Started session {identity.session_id[:12]}" if identity.session_id else "New session"
        )
    tip = (
        "Build. Think. Refine. Together."
        if style == "centered"
        else (
            "Tip  /context shows what occupies the context window.\n"
            "Type to begin   / commands   @ files   /help"
        )
    )
    metadata = Text("Nova", style=palette.accent)
    metadata.append(f"  ·  {identity.provider}  ·  {identity.model}", style=palette.text)
    if identity.context_percent is not None:
        metadata.append(f"  ·  ctx {identity.context_percent:.0f}%", style=palette.muted)
    return HomePresentation(
        art,
        Text(details, style=palette.muted),
        Text(tip, style=palette.muted),
        metadata,
    )


@dataclass(frozen=True)
class SystemSnapshot:
    """A single sample of host resource usage and network throughput."""

    cpu: float
    memory: float
    received_per_second: float | None
    sent_per_second: float | None


class SystemSampler:
    """Read local counters; the first sample establishes the network baseline."""

    def __init__(self) -> None:
        """Start without a prior network-counter sample."""
        self._previous: tuple[float, int, int] | None = None

    def sample(self) -> SystemSnapshot | None:
        """Return current usage, or no data if host counters are unavailable."""
        try:
            import psutil

            cpu = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory().percent
            counters = psutil.net_io_counters()
            now = time.monotonic()
            current = (now, counters.bytes_recv, counters.bytes_sent)
            previous = self._previous
            self._previous = current
            if previous is None or now <= previous[0]:
                return SystemSnapshot(cpu, memory, None, None)
            elapsed = now - previous[0]
            received = max(0, current[1] - previous[1]) / elapsed
            sent = max(0, current[2] - previous[2]) / elapsed
            return SystemSnapshot(cpu, memory, received, sent)
        except (ImportError, OSError, AttributeError):
            return None


def _rate(value: float | None) -> str:
    """Format bytes per second without implying a reading before the baseline."""
    if value is None:
        return "sampling"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < _KIB or unit == "GB/s":
            return f"{value:.0f} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"
        value /= _KIB
    return "0 B/s"


def _meter(label: str, percentage: float, accent: str, muted: str) -> Text:
    """Keep the value readable when a terminal or sensor reports an edge value."""
    value = max(0.0, min(100.0, percentage))
    filled = round(value / 10)
    line = Text(f"{label:<4} ")
    line.append("━" * filled, style=f"bold {accent}".strip())
    line.append("─" * (10 - filled), style=muted)
    line.append(f" {value:>3.0f}%")
    return line


def system_text(snapshot: SystemSnapshot | None, *, accent: str = "", muted: str = "dim") -> Text:
    """Render actual host readings, including both network directions."""
    body = Text("⌁  SYSTEM\n", style=f"bold {accent}" if accent else "bold")
    if snapshot is None:
        body.append("Host counters unavailable", style=muted)
        return body
    body.append_text(_meter("CPU", snapshot.cpu, accent, muted))
    body.append("\n")
    body.append_text(_meter("RAM", snapshot.memory, accent, muted))
    body.append("\n")
    body.append(f"↓ {_rate(snapshot.received_per_second)}  ↑ {_rate(snapshot.sent_per_second)}")
    return body


class HomeNavItem(Static, can_focus=True):
    """A keyboard and mouse accessible, left-aligned sidebar tab."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "activate", "Open", show=False)]

    class Activated(Message):
        """A sidebar tab was selected."""

        def __init__(self, item: HomeNavItem) -> None:
            """Keep the selected item for the app's command router."""
            super().__init__()
            self.item = item

    def action_activate(self) -> None:
        """Open this tab through Nova's shared command path."""
        self.post_message(self.Activated(self))

    def on_click(self, event: events.Click) -> None:
        """Treat pointer activation the same as Enter."""
        self.action_activate()
        event.stop()


class HomeSidebar(VerticalScroll):
    """Navigation to existing Nova commands and the current workspace."""

    def compose(self) -> ComposeResult:
        """Build navigation links and workspace information."""
        yield Static("✧  NOVA", classes="home-card-title")
        for label, identifier in (
            ("◉  Chat", "chat"),
            ("▣  Codebase", "codebase"),
            ("⌁  Run tests", "run"),
            ("⚒  Tools", "tools"),
            ("♧  Agents", "agents"),
            ("⬡  Artifacts", "artifacts"),
            ("⚙  Settings", "settings"),
        ):
            yield HomeNavItem(label, id=f"home-nav-{identifier}", classes="home-nav")
        yield Static("PROJECT", classes="home-card-title home-section")
        yield Static("", id="home-workspace", classes="home-side-detail")
        yield Static("BRANCH", classes="home-card-title home-section")
        yield Static("", id="home-branch", classes="home-side-detail")


class HomeRightRail(VerticalScroll):
    """Live host and session facts shown beside the empty conversation."""

    def compose(self) -> ComposeResult:
        """Build the live system and session cards."""
        yield Static(system_text(None), id="home-system", classes="home-card")
        yield Static("", id="home-model", classes="home-card")
        yield Static("", id="home-tasks", classes="home-card")
        yield Static("", id="home-context", classes="home-card")


class HomeActions(Horizontal):
    """Quick prompts that put editable text into the main composer."""

    def compose(self) -> ComposeResult:
        """Build four editable task starters."""
        for label, identifier in (
            ("✦  Explain", "explain"),
            ("⑂  Refactor", "refactor"),
            ("⬡  Build", "build"),
            ("⚙  Debug", "debug"),
        ):
            yield Button(label, id=f"home-action-{identifier}", classes="home-action")
