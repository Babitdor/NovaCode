"""One session's transcript widget plus the conversation state that goes with it.

``NovaApp`` keeps ~30 attributes describing the *current* conversation — the
streaming message being built, open tool cards, the todo widget, deferred input,
context-warning flags, the token tracker. With a single session those can live on
the app; with several they must belong to whichever session is on screen.

Rather than thread a pane argument through thousands of lines of ``app.py``, the
active pane's state simply *is* the app's attributes, and switching tabs swaps
the whole bundle out and the next one in. That works because rendering never
interleaves: a spawned session runs in its own OS process, so a hidden pane
buffers incoming events instead of drawing them (see ``NovaApp._deliver``).

    # ponytail: attribute-bundle swap + buffer-while-hidden. Ceiling: a hidden
    # pane's transcript is stale until you switch to it. Upgrade path if that
    # ever matters: move _render onto SessionPane with per-pane widget refs.

The authoritative membership of :data:`STATEFUL_ATTRS` is "everything ``/clear``
and ``/resume`` reset", plus the per-conversation objects (agent, backend,
trackers) — those handlers are the existing definition of conversation state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from textual.containers import VerticalScroll

# Attributes that belong to a conversation rather than to the app. Missing ones
# are tolerated (``getattr`` default) because several are created lazily.
STATEFUL_ATTRS: tuple[str, ...] = (
    # streaming / transcript
    "_seen",
    "_live_buf",
    "_reasoning_buf",
    "_stream_msg",
    "_reason_msg",
    "_current_assistant_id",
    "_stream_flush_scheduled",
    "_accumulated_reply",
    # tool + subagent cards
    "_tool_components",
    "_last_tool",
    "_tool_group",
    "_tool_group_body",
    "_tool_group_entries",
    "_tool_group_lines",
    "_tool_group_last_idx",
    "_tool_group_log_lines",
    "_tool_group_refresh_scheduled",
    "_tool_group_running",
    "_subagent_widgets",
    "_subagent_count",
    "_subagent_tool_to_task",
    "_todos",
    "_todos_agent",
    "_todos_collapsed",
    # turn / activity
    "_turn_active",
    "_turn_start",
    "_activity",
    "_detach_cancelling",
    # queued input
    "_live_steers",
    "_deferred_prompts",
    "_deferred_commands",
    "_pending_job_notes",
    # context management
    "_ctx_warned",
    "_auto_compact",
    "_compacted_last_turn",
    # per-conversation objects
    "token_tracker",
    "image_tracker",
    "agent",
    "backend",
    "model_name",
    # Each conversation can run a different provider/model. Without this the
    # provider stayed app-global while model_name was per-pane, so switching
    # tabs and saving recorded the OTHER session's provider — silently
    # corrupting which model each session restores to.
    "_model_provider",
    "session_state",
    "assistant_id",
    "_restored_messages",
    "_home_banner",
)

_MISSING = object()


def fresh_state(
    *,
    session_state: Any = None,
    assistant_id: str | None = None,
    model_name: str | None = None,
    model_provider: str | None = None,
) -> dict[str, Any]:
    """A blank conversation bundle for a brand-new pane.

    A pane created with no state inherits whatever is currently on the app when
    it is switched to — including the previous pane's live widget references.
    ``_render`` reuses ``_stream_msg`` when it is not None, so a spawned session's
    reply streamed into the ROOT pane's (now hidden) widget: the agent answered
    and the tab stayed black. Every new pane therefore starts from these
    defaults, which mirror ``NovaApp.__init__``.

    Widget-holding fields are None/empty on purpose: they must be re-created
    inside the new pane's own scroll region.
    """
    return {
        # streaming / transcript
        "_seen": set(),
        "_live_buf": "",
        "_reasoning_buf": "",
        "_stream_msg": None,
        "_reason_msg": None,
        "_current_assistant_id": None,
        "_stream_flush_scheduled": False,
        "_accumulated_reply": "",
        # tool + subagent cards
        "_tool_components": {},
        "_last_tool": None,
        "_tool_group": None,
        "_tool_group_body": None,
        "_tool_group_entries": [],
        "_tool_group_lines": {},
        "_tool_group_last_idx": None,
        "_tool_group_log_lines": 0,
        "_tool_group_refresh_scheduled": False,
        "_tool_group_running": None,
        "_subagent_widgets": {},
        "_subagent_count": 0,
        "_subagent_tool_to_task": {},
        "_todos": [],
        "_todos_agent": None,
        "_todos_collapsed": False,
        # turn / activity
        "_turn_active": False,
        "_turn_start": 0.0,
        "_activity": "ready",
        "_detach_cancelling": False,
        # queued input
        "_live_steers": [],
        "_deferred_prompts": [],
        "_deferred_commands": [],
        "_pending_job_notes": [],
        # context management (the child tracks its own; the parent shows none)
        "_ctx_warned": False,
        "_auto_compact": True,
        "_compacted_last_turn": False,
        # per-conversation objects
        "token_tracker": None,
        "image_tracker": None,
        "agent": None,
        "backend": None,
        "model_name": model_name,
        "_model_provider": model_provider,
        "session_state": session_state,
        "assistant_id": assistant_id,
        "_restored_messages": [],
        "_home_banner": None,
    }


@dataclass(eq=False)
class SessionPane:
    """A session tab: its scroll region, status, and saved conversation state."""

    sid: str
    title: str
    scroll: Any
    """The pane's ``VerticalScroll``; the active pane's is what ``_transcript()`` returns."""
    kind: str = "root"
    """``root`` for the in-process session, ``child`` for a spawned one."""
    status: str = "idle"
    """idle | running | needs-approval | starting | crashed | exited"""
    child: Any = None
    """The :class:`~novacode_cli.sessions.supervisor.ChildSession`, if spawned."""
    buffer: deque = field(default_factory=lambda: deque(maxlen=2000))
    """Events that arrived while hidden, replayed on switch."""
    unread: int = 0
    pending_interrupt: dict | None = None
    """An approval this pane is blocked on, presented when the user switches here."""
    worktree: Any = None
    branch: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    remote_turns: deque = field(default_factory=deque)
    """Remote (Telegram/Discord) prompts sent to this child, oldest first. The
    head receives the child's events and is answered at its ``turn_done``."""

    def save_from(self, app: Any) -> None:
        """Capture the app's conversation attributes into this pane."""
        for name in STATEFUL_ATTRS:
            self.state[name] = getattr(app, name, _MISSING)

    def load_into(self, app: Any) -> None:
        """Restore this pane's conversation attributes onto the app.

        A pane that has never been active (freshly spawned) has no saved state,
        so nothing is written and the app keeps whatever it had — callers set up
        a new pane's state explicitly before switching to it.
        """
        for name, value in self.state.items():
            if value is not _MISSING:
                setattr(app, name, value)

    @property
    def has_state(self) -> bool:
        return bool(self.state)
