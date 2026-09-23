"""Textual chat application (Phase 1).

A scrollable transcript + input + status line. The agent runs in a Textual
worker that iterates :func:`novacode_cli.agent_stream.run_agent_stream` and
renders each :mod:`novacode_cli.ui_events` event. HITL interrupts are shown as
modal screens.

Existing ``rich`` renderers are reused by capturing their output to a ``Text``
(``_capture``), so the visual style matches the legacy UI without duplicating
rendering code.

Animations
----------
All animated effects (entrance slide/fade/zoom, pulsing borders, shimmer,
thinking dots) are defined in :mod:`novacode_cli.tui.animations` and called
from ``on_mount`` handlers via Python's ``animate()`` API.
"""

from __future__ import annotations
from novacode_cli.prompts import render_template

import asyncio
import contextlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

from rich.markdown import Markdown
from rich.markup import escape as _esc
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    ContentSwitcher,
    Input,
    OptionList,
    Static,
    RichLog,
    Tab,
    Tabs,
)
from textual.widgets.option_list import Option

from novacode_cli.tui.animations import (
    animate_entrance,
)



# Widgets and modal screens were extracted verbatim into widgets.py /
# screens.py. Re-exported here so `from novacode_cli.tui.app import X`
# keeps working for tests, main.py, and remote code.
from novacode_cli.tui.widgets import (
    DEFAULT_THEME,
    NOVA_MATRIX,
    NOVA_TOKYO_NIGHT,
    ChatMessage,
    MatrixRain,
    NovaStatusBar,
    PromptInput,
    SessionHeader,
    TranscriptScroll,
    TuiInitRenderer,
)
from novacode_cli.tui.screens import (
    AgentCreateModal,
    AgentsScreen,
    ApprovalModal,
    BackgroundTasksScreen,
    ClaudePluginsScreen,
    ConfirmModal,
    HookCreateModal,
    HooksScreen,
    InfoListScreen,
    McpCustomModal,
    McpInstallModal,
    McpScreen,
    ModelScreen,
    PickScreen,
    PlanApprovalModal,
    PluginsScreen,
    QuestionModal,
    RalphScreen,
    RememberRuleModal,
    RemoteScreen,
    ServersScreen,
    SessionsScreen,
    SkillCreateModal,
    SkillsScreen,
    ThemeScreen,
    WikiScreen,
)


# Transcript is pruned from the top once it exceeds this many widgets, down to
# _TRANSCRIPT_LOW_WATER — keeps Textual's layout/scroll/repaint fast in long
# sessions (the DOM would otherwise grow without bound).
#
# Sized from measured reflow cost, which is linear in widget count:
#     50 widgets -> 3.7 ms      250 -> 9.5 ms
#    150 widgets -> 5.6 ms      400 -> 13.8 ms
# The spinner ticks at 20 Hz (a 50 ms frame budget), so at 400 widgets a single
# layout pass ate ~28% of every frame — re-laying-out hundreds of widgets that
# are scrolled far out of view. 200 halves that to ~7 ms while still holding
# well over a screenful of scrollback (a full-height terminal shows ~40 rows).
_MAX_TRANSCRIPT_WIDGETS = 200
_TRANSCRIPT_LOW_WATER = 150

# Responsive breakpoints (terminal columns/rows). Below _NARROW_WIDTH the info
# bar sheds its widest columns and the status line drops its right-side counts;
# below the _MIN_* floor the layout can't fit and we surface a "too small" note.
_NARROW_WIDTH = 90
_MIN_WIDTH = 50
_MIN_HEIGHT = 12

# Tools whose result is a code change worth seeing in full: these keep their own
# Collapsible with a colored diff body so the user can review what the agent
# changed. Every other tool (reads, search, exec, MCP, …) condenses into the
# shared tool group. Keep in sync with the file-write tools that emit a FileOp
# record with a diff (see tracking/file_tracker.py).
_DETAILED_TOOL_NAMES = frozenset(
    {
        "write_file",
        "edit_file",
        "create_file",
        "multi_edit",
        "str_replace",
        "apply_patch",
    }
)


# Tool calls are grouped under a category heading in the condensed tool panel
# (e.g. "Explored — 3 reads"), so a long run of tools reads as a few labelled
# sections instead of one flat list. Order here drives the section order.
#
# Coverage: every tool the agent can call — the @tool functions in
# novacode_cli/tools/, deepagents' built-in filesystem/subagent/todo tools, and
# MCP tools (which arrive prefixed as "<server>_<tool>", matched by prefix
# below). A tool matching nothing falls into "Other".
_TOOL_CATEGORIES: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "Explored",
        "→",
        frozenset(
            {
                # Filesystem reads (deepagents built-ins + aliases)
                "read_file",
                "read",
                "view",
                "ls",
                "list_dir",
                "glob",
                "grep",
                "search",
                # Semantic / symbol search
                "code_search",
                "find_related_code",
                "find_file",
                "search_for_pattern",
                "get_symbols_overview",
                "find_symbol",
                "find_referencing_symbols",
                "find_implementations",
                "find_declaration",
                # Web / docs research
                "web_search",
                "fetch_url",
                "duckduckgo_search",
                "docs_search",
                "github_trending",
                "hacker_news",
                "reddit_posts",
                "twitter_search",
                "twitter_trending",
                "linkedin_jobs",
                "package_info",
                "oracle",
            }
        ),
    ),
    (
        "Edited",
        "✎",
        frozenset(
            {
                # Filesystem writes (deepagents built-ins + aliases)
                "write_file",
                "edit_file",
                "create_file",
                "multi_edit",
                "str_replace",
                "apply_patch",
                "delete",
                # Symbol-level edits
                "rename_symbol",
                "replace_symbol_body",
                "insert_after_symbol",
                "insert_before_symbol",
                "replace_content",
                "replace_in_files",
                "create_text_file",
                "safe_delete_symbol",
            }
        ),
    ),
    (
        "Ran",
        "$",
        frozenset(
            {
                "execute",
                "shell",
                "bash",
                "execute_bash",
                "run_command",
                "run_tests",
                "start_dev_server",
                "python_kernel",
                "daemon",
                "kill_process",
                "restart_task",
                "terminate_task",
            }
        ),
    ),
    (
        "Delegated",
        "⟐",
        frozenset(
            {
                "task",
                "start_async_task",
                "check_async_task",
                "update_async_task",
                "cancel_async_task",
                "list_async_tasks",
                "send_agent_message",
                "read_agent_messages",
            }
        ),
    ),
    (
        "Planned",
        "☰",
        frozenset(
            {
                "write_todos",
                "enter_plan_mode",
                "exit_plan_mode",
                "ask_user_question",
                "plan",
                "think",
                "compact_conversation",
            }
        ),
    ),
    (
        "Remembered",
        "◈",
        frozenset(
            {
                "remember",
                "recall",
                "forget",
                "write_memory",
                "read_memory",
                "list_memories",
                "create_memory_structure",
                "wiki_write",
                "wiki_read",
                "wiki_search",
                "wiki_update_index",
            }
        ),
    ),
    (
        "Produced",
        "◆",
        frozenset(
            {
                "create_artifact",
                "update_artifact",
                "list_artifacts",
                "skill_manage",
                "speak",
            }
        ),
    ),
    (
        "Tasks",
        "⚙",
        frozenset(
            {
                "get_task_status",
                "get_task_logs",
                "list_background_tasks",
            }
        ),
    ),
)

#: Category name → (glyph, member tool names), for O(1) lookup.
_TOOL_CATEGORY_INDEX: dict[str, tuple[str, frozenset[str]]] = {
    name: (glyph, members) for name, glyph, members in _TOOL_CATEGORIES
}

#: Fallback category for a tool not named in any group above.
_OTHER_CATEGORY = "Other"

#: MCP tools arrive as "<server>_<tool>" (langchain-mcp-adapters prefixes the
#: server name). These server prefixes route them into a sensible category
#: instead of "Other", so a Playwright or Serena burst reads as one section.
_MCP_SERVER_CATEGORY: tuple[tuple[str, str], ...] = (
    ("playwright", "Explored"),
    ("chrome-devtools", "Explored"),
    ("cua-driver", "Ran"),
    ("serena", "Explored"),
    ("apify", "Explored"),
    ("context7", "Explored"),
    ("langsmith", "Explored"),
)


def _tool_category(name: str) -> str:
    """The category heading a tool call belongs under (``"Other"`` if unknown)."""
    for cat_name, _glyph, members in _TOOL_CATEGORIES:
        if name in members:
            return cat_name
    # MCP tools: "<server>_<tool>" — match on the server prefix.
    lowered = name.lower()
    for prefix, cat in _MCP_SERVER_CATEGORY:
        if lowered.startswith(prefix):
            return cat
    return _OTHER_CATEGORY


def _category_glyph(cat: str) -> str:
    """The leading glyph for a category heading."""
    entry = _TOOL_CATEGORY_INDEX.get(cat)
    return entry[0] if entry else "•"


# The @mention token immediately before the cursor (start-of-line or after
# whitespace, so emails like user@host don't match). Used to drive @file/@agent
# autocomplete *anywhere* in the line, not just at the very start.
_AT_FRAGMENT_RE = re.compile(r"(?:^|(?<=\s))@([^\s@]*)$")


@dataclass(frozen=True)
class SlashCommand:
    """One TUI slash command. THE registry entry — autocomplete, dispatch, and
    /help are all derived from the TUI_COMMANDS table, so a command exists in
    exactly one place. (Previously each command lived in an autocomplete list,
    a ~50-branch elif chain, hand-written help text, and a passthrough set —
    and the four drifted.)

    handler: NovaApp method name, resolved with getattr at dispatch. Called
        with the full input text when ``wants_text``, else with no args; sync
        handlers and coroutines both work.
    """

    handler: str
    help: str
    wants_text: bool = True
    aliases: tuple[str, ...] = ()


# The table. Insertion order drives autocomplete + /help order.
# cron/webhook/prompt route through the legacy console handler via
# _passthrough_command (print-or-toggle-only commands; never read stdin or use
# a Live spinner — those would hang or garble inside Textual).
TUI_COMMANDS: dict[str, SlashCommand] = {
    "help": SlashCommand("_run_help", "show this help", wants_text=False, aliases=("?",)),
    "init": SlashCommand("_run_init", "generate NOVA.md from the codebase"),
    "model": SlashCommand("_run_model", "switch provider / model", wants_text=False),
    "sessions": SlashCommand("_run_sessions", "list / delete saved sessions", wants_text=False),
    "session": SlashCommand(
        "_run_session_command", "parallel sessions: new / list / close (ctrl+n, alt+<n>)"
    ),
    "resume": SlashCommand("_run_resume", "resume a saved session for this path (/resume <id>)"),
    "artifacts": SlashCommand("_run_artifacts", "open the artifacts list", wants_text=False),
    "tasks": SlashCommand("_run_tasks", "open the background tasks panel", wants_text=False),
    "cowork": SlashCommand(
        "_run_cowork", "launch the Nova Cowork desktop app (/cowork [task])", aliases=("desktop",)
    ),
    "mcp": SlashCommand("_run_mcp", "view / remove MCP servers", wants_text=False),
    "skills": SlashCommand("_run_skills", "list skills", wants_text=False),
    "agents": SlashCommand("_run_agents", "list subagents", wants_text=False),
    "plan": SlashCommand("_run_plan", "plan mode (status / off)"),
    "goal": SlashCommand("_run_goal", "set a persistent goal (status / clear)"),
    "btw": SlashCommand("_run_btw", "ask a side question without touching the main conversation"),
    "remote": SlashCommand(
        "_run_remote_screen", "manage Discord/Telegram bridges", wants_text=False
    ),
    "compact": SlashCommand("_run_compact", "summarize conversation to free context"),
    "save": SlashCommand("_run_save", "save the session now", wants_text=False),
    "copy": SlashCommand("_run_copy", "copy last response (or whole chat) — or click a message"),
    "plugins": SlashCommand("_run_plugins", "install / manage plugins and marketplaces"),
    "middleware": SlashCommand(
        "_run_middleware", "list active middleware (/reload-plugins to reload)", wants_text=False
    ),
    "reload-plugins": SlashCommand(
        "_run_reload_plugins",
        "reload plugin registrations",
        wants_text=False,
        aliases=("reload_plugins",),
    ),
    "steer": SlashCommand("_run_steer", "add/list/clear steering instructions"),
    "notifications": SlashCommand(
        "_run_notifications", "review/pending approvals (dismiss|approve <id> · clear)"
    ),
    "cron": SlashCommand("_passthrough_command", "manage scheduled (heartbeat) tasks"),
    "webhook": SlashCommand("_passthrough_command", "manage the webhook ingress server"),
    "prompt": SlashCommand("_passthrough_command", "manage evolving system-prompt templates"),
    "refine": SlashCommand(
        "_passthrough_command", "refinement audit trail (/refine history|rollback <id>)"
    ),
    "voice": SlashCommand("_run_voice", "local voice I/O settings (STT / VAD / TTS)"),
    "research": SlashCommand("_run_research", "launch a multi-agent research swarm"),
    "dream": SlashCommand("_run_dream", "reflect over memories to surface ideas", wants_text=False),
    "evolution": SlashCommand(
        "_run_evolution", "view skills unlocked / levelled up by complex tasks", wants_text=False
    ),
    "reindex": SlashCommand(
        "_run_reindex", "rebuild the semantic code-search index", wants_text=False
    ),
    "images": SlashCommand("_run_images", "list/remove/clear conversation images"),
    "files": SlashCommand("_run_files", "session file read/write summary", wants_text=False),
    "tests": SlashCommand("_run_tests", "run project tests (auto-detect or /tests <cmd>)"),
    "servers": SlashCommand("_run_servers", "manage dev servers", wants_text=False),
    "kill": SlashCommand("_run_kill", "kill processes"),
    "restore": SlashCommand("_run_restore", "restore a file from the snapshot trash"),
    "hooks": SlashCommand("_run_hooks", "list/enable/disable/remove hooks"),
    "browser-use": SlashCommand(
        "_run_browser_use", "AI browser automation, results analyzed by the agent"
    ),
    "ralph": SlashCommand("_run_ralph_screen", "autonomous looping mode (/ralph <task>)"),
    "trello": SlashCommand("_run_trello", "kanban task board in the browser"),
    "create": SlashCommand("_run_create", "Skills & Agents web UI"),
    "council": SlashCommand(
        "_run_council",
        "plan a task with the council (view / approve N / revise / history)",
    ),
    "clear": SlashCommand("_run_clear", "clear the transcript", wants_text=False),
    "tokens": SlashCommand("_run_token_view", "show token / context usage", wants_text=False),
    "context": SlashCommand("_run_token_view", "show context usage breakdown", wants_text=False),
    "cost": SlashCommand("_run_token_view", "show session token spend", wants_text=False),
    "verbose": SlashCommand("_run_verbose", "toggle internal-context display", wants_text=False),
    "trace": SlashCommand("_run_trace", "tracing status"),
    "log": SlashCommand("_run_log", "recent runs"),
    "theme": SlashCommand("_run_theme", "switch color theme", wants_text=False),
    "quit": SlashCommand("action_quit", "exit the TUI", wants_text=False),
    "exit": SlashCommand("action_quit", "exit the TUI", wants_text=False),
    # Wiki commands
    "ingest": SlashCommand("_run_ingest", "ingest a raw source into the wiki"),
    "ask": SlashCommand("_run_ask", "ask with wiki context prepended"),
    "file": SlashCommand("_run_file", "file conversation knowledge into the wiki"),
    "wiki": SlashCommand(
        "_run_wiki", "show Obsidian LLM Wiki browser (interactive)", wants_text=False
    ),
    "effort": SlashCommand("_run_effort", "set model reasoning effort"),
    "learning": SlashCommand(
        "_run_learning", "toggle Nova's autonomous learning loop (/learning on|off|status)"
    ),
}

_TUI_COMMAND_ALIASES: dict[str, str] = {
    alias: name for name, spec in TUI_COMMANDS.items() for alias in spec.aliases
}

# Autocomplete entries — derived; plugin commands append at registration time.
_TUI_SLASH_COMMANDS = [f"/{name}" for name in TUI_COMMANDS]

from novacode_cli import ui_events as ev
from novacode_cli.agent_stream import run_agent_stream
from novacode_cli.config.config import console as _rich_console
from novacode_cli.input_utils import (
    PasteTracker,
    resolve_paste_placeholders,
)


def _status_for_event(event: Any, current: str) -> str:
    """Derive a session's tab status from the events already flowing.

    Cheaper than a side-channel status protocol, and it works identically for the
    in-process root session and a spawned child.
    """
    if isinstance(event, (ev.Done, ev.Cancelled, ev.Error)):
        return "idle"
    if isinstance(event, (ev.ToolCall, ev.AssistantMessage, ev.StatusUpdate)):
        return "running"
    return current


def _capture(fn, *args, **kwargs) -> Text:
    """Render an existing ``console.print``-based helper into a ``Text``.

    Lets the TUI reuse the legacy rich renderers (tool panels, todos, file ops)
    without printing to the real terminal — capture redirects the global
    console to an in-memory buffer.
    """
    with _rich_console.capture() as cap:
        fn(*args, **kwargs)
    return Text.from_ansi(cap.get())


def _approval_details(action_requests: list[dict]) -> Text:
    """Detailed, multi-line view of the actions awaiting approval."""
    from novacode_cli.ui.ui_elements import format_tool_display

    t = Text()
    t.append("⚠ The agent wants to run:\n\n", style="yellow")
    for ar in action_requests:
        name = ar.get("name", "?")
        args = ar.get("args", {}) or {}
        try:
            disp = format_tool_display(name, args)
        except Exception:  # noqa: BLE001
            disp = name
        t.append(f"  • {disp}\n", style="bold")
        if isinstance(args, dict):
            for k, v in args.items():
                sval = str(v).replace("\n", " ")
                if len(sval) > 160:
                    sval = sval[:160] + "…"
                t.append(f"      {k}: {sval}\n", style="dim")
    return t



#: Characters of in-progress prose kept in the live (pre-commit) preview.
_LIVE_PREVIEW_CHARS = 20_000

#: Percentage thresholds at which a usage meter turns amber, then red.
_PCT_WARN = 75
_PCT_CRITICAL = 90

#: Cap on the background-agent final answer echoed back to the main agent, so a
#: verbose run cannot flood the next turn's context.
_BG_REPORT_MAX_CHARS = 2000


def _pct_color(percent: float, base: str) -> str:
    """Recolor a usage percentage green→amber→red as it approaches the cap.

    Args:
        percent: The usage percentage (0-100).
        base: The color to use below the warning threshold.

    Returns:
        A hex color string.
    """
    if percent >= _PCT_CRITICAL:
        return "#f7768e"
    if percent >= _PCT_WARN:
        return "#e0af68"
    return base


def _render_bg_event(  # noqa: PLR0912, PLR0915 — one branch per event type is the point
    event: Any,  # noqa: ANN401 — event-stream dispatch, matches ui_events.py
    write: Callable[[str], None],
    set_phase: Callable[[str], None],
    fileop_summary: Callable[[Any], str],
    pending: dict[str, str],
) -> None:
    """Render one background-agent progress event into its card.

    The background (Ctrl+B) card is the only view of a detached agent turn, so it
    must explain *what* is happening, not just name the tool: tool arguments
    (``display_str``), the reasoning trace, results, file changes and subagent
    dispatches all go into the body, while ``set_phase`` keeps a one-line live
    status in the card title (visible even when the card is collapsed).

    A tool call and its result are rendered as ONE line (``✓ read_file(x) · Read
    42 lines``) rather than two. RichLog cannot rewrite a line, so the call is
    buffered in ``pending`` and only written when its result arrives; a call with
    no result yet is flushed as-is by the caller when the turn ends.

    Args:
        event: A UI event from the agent stream.
        write: Sink for one line of Rich markup into the card body.
        set_phase: Sink for the live status line shown in the card title.
        fileop_summary: Summarizer for a file-op record (``+A / -D`` etc.).
        pending: Mutable buffer holding the in-flight tool call's rendered prefix,
            keyed by ``call_id`` (or ``""`` when the event carries no id).
    """
    from novacode_cli.ui_events import (
        AssistantMessage,
        FileOp,
        ReasoningDelta,
        StatusUpdate,
        SubagentActivity,
        TextDelta,
        ToolCall,
        ToolResult,
    )

    def _flush_pending() -> None:
        """Write any buffered tool call that never got a result."""
        for prefix in pending.values():
            write(prefix)
        pending.clear()

    if isinstance(event, AssistantMessage) and event.text:
        _flush_pending()
        # Blank line before prose so it does not run into the tool lines above.
        write("")
        for line in event.text.splitlines():
            # Escape: prose may contain [brackets] (markdown links, list markers)
            # that Rich would otherwise interpret as markup.
            write(f"[white]{_esc(line)}[/white]" if line.strip() else "")
        set_phase("responding")
    elif isinstance(event, TextDelta) and event.text:
        # Deliberately NOT written. The committed AssistantMessage carries the
        # same text, and RichLog is append-only (no replace), so rendering the
        # live preview here would duplicate every paragraph. The card title's
        # "responding" phase is the live signal instead.
        set_phase("responding")
    elif isinstance(event, ReasoningDelta) and event.text:
        # Dimmed thinking trace, so the user can see it is reasoning, not stalled.
        _flush_pending()
        # Blank line before the trace so it does not run into the tool lines above.
        write("")
        for line in event.text.splitlines():
            if line.strip():
                write(f"[dim italic]💭 {_esc(line)}[/dim italic]")
        set_phase("thinking")
    elif isinstance(event, StatusUpdate) and event.message:
        set_phase(event.message)
    elif isinstance(event, ToolCall):
        # Buffer the call; it is written together with its result so the pair
        # reads as one line. Show the tool AND its arguments (display_str).
        _flush_pending()
        pending[event.call_id or ""] = f"[cyan]{event.icon} {_esc(event.display_str)}[/cyan]"
        set_phase(f"running {event.name}…")
    elif isinstance(event, ToolResult):
        prefix = pending.pop(event.call_id or "", None)
        if prefix is None:
            # No matching call (e.g. a result for a call we never saw): stand alone.
            prefix = "[dim]·[/dim]"
        if event.is_error:
            write(f"[red]✗[/red] {prefix} [red]· {_esc(event.preview)}[/red]")
        elif event.preview:
            write(f"[green]✓[/green] {prefix} [dim]· {_esc(event.preview)}[/dim]")
        else:
            write(f"[green]✓[/green] {prefix}")
    elif isinstance(event, FileOp):
        rec = event.record
        errored = bool(getattr(rec, "error", None)) or (
            getattr(rec, "status", "") == "error"
        )
        mark = "[red]✗[/red]" if errored else "[green]✓[/green]"
        prefix = pending.pop(event.call_id or "", None)
        if prefix is not None:
            write(f"{mark} {prefix} [dim]· {_esc(fileop_summary(rec))}[/dim]")
        else:
            write(f"{mark} [dim]{_esc(fileop_summary(rec))}[/dim]")
    elif isinstance(event, SubagentActivity):
        _flush_pending()
        who = _esc(event.subagent_type or "subagent")
        if event.kind == "dispatched":
            write(f"[magenta]◇ dispatched {who}[/magenta]")
        elif event.kind == "completed":
            write(f"[magenta]◆ {who} done[/magenta]")
        elif event.message:
            write(f"[magenta]◇ {_esc(event.message)}[/magenta]")


class NovaApp(App):
    """Phase-1 Nova chat TUI."""

    # Colors come from the active Textual theme (default: tokyo-night, registered
    # in on_mount). Do NOT redefine $primary/$surface/etc. here — CSS variable
    # definitions override the theme and break /theme switching.
    CSS = """
    /* --- App chrome --- */
    Screen { background: $background; }

    /* Scrollbars are hidden cosmetically across the app (transcript, embedded
       terminal, tool/subagent logs, modals, lists). Scrolling still works via
       the wheel, keys, and drag; only the bar is not painted.
       A transparent scrollbar color makes ScrollBar.render() emit blanks while
       keeping the bar's size, so layout is untouched. Do NOT instead set
       `scrollbar-size-vertical: 0`: a zero scrollbar size on a scroll container
       breaks Textual's content-width calculation and collapses its children to
       zero width (verified: the transcript's message bodies went 69 -> 0 cols).
       These must be set on the SCROLL CONTAINERS, not on Screen: ScrollBar
       reads `self.parent.styles.scrollbar_background`, so a Screen-level rule
       never reaches the bar and it still paints (as a black column).
       The `scrollbar-gutter: stable` rules below keep the reserved column so
       content width does not shift when a scrollable region appears. */
    VerticalScroll, RichLog, OptionList, TextArea, Collapsible, SelectionList,
    Select, DataTable, Log, Markdown, Tree, DirectoryTree, TabbedContent {
        scrollbar-color: transparent;
        scrollbar-background: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-color-active: transparent;
        scrollbar-background-active: transparent;
    }

    /* Session panes. The ContentSwitcher wrapping the transcript must be fully
       transparent to layout: without these it defaults to auto sizing, the
       transcript stops filling the screen and its content width shrinks, so the
       Matrix-rain banner (sized from the TERMINAL width) no longer fits its
       container and every row wraps — which pushed the logo down a row and then
       back up as the rain shifted. Panes are styled as a group so a spawned
       session's transcript looks identical to the root one. */
    #panes { height: 1fr; width: 100%; }
    #panes > VerticalScroll { height: 1fr; width: 100%; padding: 1 2; }
    /* Hidden until a second session exists; _refresh_tabs() toggles it.
       Height MUST be explicit: the Tabs widget's inner tabs-scroll is height:1fr,
       and a 1fr child inside an `auto` parent expands to the whole screen — the
       tab bar then ate ~30 rows and crushed #panes (every session pane, root
       included) to a 1-row sliver, so nothing rendered once the bar appeared. */
    #session-tabs { display: none; height: 3; width: 100%; }
    #transcript { height: 1fr; padding: 1 2; }
    #transcript > .subagent {
        border-left: thick $accent; padding: 0 2;
        margin: 1 0; background: $surface;
    }
    #transcript > .tool { color: $warning; padding: 0 2; margin: 1 0; background: $background; }
    .toolbody { color: $text-muted; margin: 0; height: auto; }
    .terminal-log {
        /* Scale with terminal height; floor at the old fixed 5 rows so small
           windows are never worse, grow up to 16 on tall windows. */
        height: 25vh;
        min-height: 5;
        max-height: 16;
        background: $boost;
        border: round $border;
        margin: 0 0;
        padding: 0 1;
        scrollbar-gutter: stable;
    }
    #tool-group-log, #subagent-log {
        display: none;
    }
    #tool-group-log.active, #subagent-log.active {
        display: block;
    }
    #transcript > .logline {
        height: auto; padding: 0 2;
        background: $surface; margin: 1 0;
    }
    /* --- /ralph native cards (accent-bar style, like .initlog) --- */
    #transcript > .ralph-run {
        height: auto; border-left: thick $primary;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .ralph-iter {
        height: auto; border-left: thick $accent;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .ralph-iter.done { border-left: thick $success; }
    #transcript > .ralph-iter.failed { border-left: thick $error; }
    #transcript > .ralph-summary {
        height: auto; border-left: thick $success;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .ralph-status {
        height: auto; border-left: thick $secondary;
        padding: 0 2; margin: 1 0; background: $surface;
    }
    #transcript > .nova-event {
        height: auto; padding: 0 2;
        background: $surface; margin: 1 0;
        border-left: thick $accent;
    }
    /* Theme variables, not hardcoded hex: these bars must recolor with /theme
       like every other accent bar. */
    #transcript > .nova-event.nova-review-start {
        border-left: thick $primary;
    }
    #transcript > .nova-event.nova-review-complete {
        border-left: thick $success;
    }
    #transcript > .nova-event.nova-skill-refinement {
        border-left: thick $warning;
    }
    #cmdpalette {
        width: 100%;
        /* Grow the completion list on taller terminals (was a fixed 10 rows). */
        height: auto; max-height: 40vh;
        border: thick $accent; background: $panel;
        padding: 0 1;
        display: none; layer: overlay; dock: bottom;
        margin-bottom: 8;
    }
    /* --- Prompt dock: 3-row bottom section --- */
    #prompt-dock {
        dock: bottom;
        height: auto;
        background: $surface;
    }
    #prompt-hint-bar {
        height: 1;
        padding: 0 2;
        background: $surface;
        color: $text-muted;
    }
    #prompt {
        width: 1fr;
        background: $panel; color: $text;
        padding: 0 2;
        /* Grows with the text (multi-line composing, wrapped long lines)
           and stops before it eats the transcript. */
        height: auto; min-height: 3; max-height: 15;
        border: none;
        /* Smooth fade when switching into/out of a mode. */
        transition: background 300ms in_out_cubic;
    }
    #prompt:focus {
        background: $boost;
    }
    /* The > chevron prefix for the input. */
    #prompt-prefix {
        width: 3;
        height: 3;
        padding: 0 0 0 1;
        background: $panel;
        color: $accent;
    }
    #prompt-row {
        height: auto;
        background: $panel;
        padding: 0;
        border-top: solid $border 30%;
        border-bottom: solid $border 30%;
    }
    /* BASH mode — magenta, urgent. */
    #prompt.bash-mode {
        background: #2a1a2e; color: #d7c4ff;
    }
    #prompt:focus.bash-mode {
        background: #2f1e35;
    }
    #prompt-row.bash-mode {
        border-top: solid #bb9af7 50%;
        border-bottom: solid #bb9af7 50%;
    }
    #prompt-prefix.bash-mode { color: #bb9af7; background: #2a1a2e; }
    /* PLAN mode — blue, calm. */
    #prompt.plan-mode {
        background: #161f33; color: #b4c6ef;
    }
    #prompt:focus.plan-mode {
        background: #1b2540;
    }
    #prompt-row.plan-mode {
        border-top: solid #7aa2f7 50%;
        border-bottom: solid #7aa2f7 50%;
    }
    #prompt-prefix.plan-mode { color: #7aa2f7; background: #161f33; }
    #mode-badge {
        height: 1;
        padding: 0 2;
        background: $panel;
        color: $text-muted;
    }
    /* --- Info bar: workspace / branch / sandbox / model / quota --- */
    #info-bar {
        height: 2;
        padding: 0 1;
        background: $background;
    }
    /* Docked todo checklist: sits above the prompt so it stays on screen.
       Auto-height so a short list costs a few rows; max-height clamps a
       long one (real lists run 2-14 items) and scrolls inside itself. */
    /* Inside #prompt-dock (a Vertical), so it takes its own rows above the
       input instead of fighting it for the same bottom-docked rows. */
    #todo-dock {
        display: none;
        height: auto;
        max-height: 12;
        overflow-y: auto;
        padding: 0 2;
        background: $surface;
        /* No accent bar: the checklist reads as plain transcript chrome. The
           "Todos" word in the header carries the theme color instead. */
    }
    #todo-dock.active { display: block; }
    #todo-dock:hover { background: $boost; }
    /* Collapsed: just the one-line summary header. */
    #todo-dock.collapsed { max-height: 1; overflow-y: hidden; }
    #tasks-bar {
        display: none;
        height: 1;
        padding: 0 1;
        background: $background;
        color: $accent;
    }
    #tasks-bar.active { display: block; }
    /* "Jump to latest" — shown only while the transcript is scrolled away from
       the bottom, so a long scrollback never traps the user. Sits at the
       top-right of the footer, directly above the status/skills bar.
       The row is transparent and right-aligns its child; the child shrinks to
       its text (width: auto), so ONLY the words are a click target — the empty
       space to their left is not part of the button. */
    #jump-latest-row {
        display: none;
        height: 1;
        align: right middle;
        background: transparent;
    }
    #jump-latest-row.active { display: block; }
    /* Shrink to the text: the box is only as wide as the label, so the row is
       not a full-width bar. */
    #jump-latest-box {
        width: auto;
        height: 1;
        background: transparent;
    }
    #jump-latest {
        width: auto;
        height: 1;
        padding: 0 2;
        background: transparent;
        color: $accent;
    }
    #jump-latest:hover { color: $foreground; text-style: bold; }
    .info-col {
        height: 2;
        padding: 0 1;
        width: 1fr;
    }
    .info-label {
        height: 1;
        color: $text-muted;
    }
    .info-value {
        height: 1;
    }
    /* Narrow terminals: shed the widest info columns so the rest stay readable
       instead of being squeezed to a few clipped characters. Toggled by
       _apply_responsive_layout adding the `narrow` class to the screen. */
    .narrow #col-workspace, .narrow #col-sandbox { display: none; }
    .session-header {
        height: auto;
        padding: 1 2;
        background: $surface;
        align: left middle;
    }
    .session-pill {
        background: $boost;
        border: round $border;
        padding: 0 2;
        margin: 0 1;
        height: auto;
    }
    .pill-model { color: $primary; }
    .pill-sandbox { color: $success; }
    .pill-memory { color: $accent; }
    .breadcrumb { color: $text-muted; }
    Screen > .modal-backdrop { background: $surface 50%; }
    /* Every modal centers its box and dims the backdrop. */
    ModalScreen { align: center middle; background: $surface 50%; }
    /* Approval body scrolls within bounds so the choices never get clipped. */
    #modal-body-scroll { height: auto; max-height: 55%; scrollbar-gutter: stable; }
    #choices {
        height: auto; max-height: 8; margin-top: 1;
        border: round $accent; background: $boost;
    }
    #choices:focus { border: round $warning; }
    #modal-box {
        width: 80%; max-width: 110; height: auto; max-height: 90%;
        border: thick $accent; background: $surface;
        padding: 1 4; layer: overlay;
    }
    #modal-title { margin-bottom: 1; padding: 0 0; }
    #modal-body { padding: 0 0; }
    /* Long lists scroll inside the box instead of overflowing the screen. */
    #sessions, #pick-list, #infolist, #mcp-configured, #mcp-presets, #plugins, #cplugins-list, #agents-list, #skills-list, #servers-list, #hooks-list, #wiki-pages-list, #wiki-inbox-list {
        height: auto; max-height: 40%;
        padding: 0 2;
    }
    #wiki-tab-buttons {
        height: auto;
        margin-bottom: 1;
    }
    #wiki-tab-buttons Button {
        margin-right: 1;
    }
    #pages-container, #inbox-container {
        height: auto;
    }
    #pages-header, #inbox-header {
        margin-bottom: 0;
    }
    .preview-box {
        background: $boost;
        border: round $accent 50%;
        padding: 1 2;
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        max-height: 12;
        scrollbar-gutter: stable;
        overflow-y: scroll;
    }
    /* The Ollama model list sits ABOVE the inputs + Switch/Cancel buttons, so it
       gets a tighter cap and its own scroll — otherwise a long list pushes the
       buttons out of the modal and they can't be clicked. */
    #modellist {
        height: auto; max-height: 9;
        border: round $accent 50%; margin-bottom: 1;
    }
    #modal-buttons {
        height: auto; align: center middle;
        margin-top: 1; padding: 0 0;
    }
    #modal-buttons Button { margin: 0 1; }
    #modal-hint { padding: 0 1; color: $text-muted; }
    Collapsible { margin: 0; }
    Collapsible > .collapsible--title { padding: 0 1; background: $surface; }
    /* The condensed tool group reads as plain transcript, not a raised card:
       its background matches the screen's, and it must not shift color on hover
       or when focus lands inside it (Collapsible's default :focus-within tint,
       and CollapsibleTitle's own :hover/:focus backgrounds). */
    #transcript > .tool, #transcript > .tool > CollapsibleTitle {
        background: $background;
    }
    #transcript > .tool:hover, #transcript > .tool:focus-within {
        background: $background;
        background-tint: transparent;
    }
    #transcript > .tool > CollapsibleTitle:hover,
    #transcript > .tool > CollapsibleTitle:focus {
        background: $background;
        color: $warning;
    }
    .btw-card { margin: 1 0; border-left: thick $accent-muted; }
    .btw-card > .collapsible--title { color: $accent-muted; background: $surface; }
    .btw-body { padding: 0 2; color: $text-muted; }
    /* Compaction is housekeeping, not conversation: a quiet one-line card that
       expands for the stats and summary. Muted so it recedes in the transcript. */
    .compact-card { margin: 1 0; border-left: thick $success-muted; }
    .compact-card > .collapsible--title {
        color: $success-muted;
        background: $surface;
    }
    .compact-body { padding: 0 2; color: $text-muted; }
    .bgshell-card { margin: 1 0; border-left: thick $warning-muted; }
    .bgshell-card > .collapsible--title { color: $warning; background: $surface; }
    .bgshell-log {
        height: 30vh; min-height: 8; max-height: 22;
        border: none; background: $surface;
    }
    /* The agent card emits discrete progress lines, not a firehose of command
       output, so it sizes to its content instead of reserving 30vh. Capped so a
       long run cannot swallow the transcript. */
    .bgagent-card {
        margin: 1 0;
        border-left: thick $success-muted;
        background: $surface;
    }
    .bgagent-card > .collapsible--title {
        color: $success;
        background: $surface;
        text-style: bold;
    }
    .bgagent-log {
        height: auto; max-height: 14;
        border: none; background: $surface;
        padding: 0 1;
    }
    /* The Collapsible body is a Vertical that defaults to 1fr, which would
       stretch the card to the full transcript height. Size it to the log.
       (The Vertical sits inside the Collapsible's Contents wrapper.) */
    .bgagent-card Contents > Vertical { height: auto; }
    .bgagent-done > .collapsible--title { color: $success; }
    .bgagent-failed > .collapsible--title { color: $error; }
    /* No reserved scrollbar column: the bar is hidden (see the transparent
       scrollbar colors above), so keeping the gutter would waste 2 columns and
       stop the home banner's rain from reaching the right edge. */
    VerticalScroll { scrollbar-gutter: auto; }
    #remote-status-container {
        height: auto; max-height: 12;
        background: $boost;
        border: round $accent;
        padding: 0 1;
        margin-bottom: 1;
    }
    #remote-section-title {
        margin-top: 1;
        margin-bottom: 0;
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        # ctrl+c copies the current text selection if there is one, else quits.
        # Textual captures the mouse, so the terminal's native copy doesn't work
        # in the transcript — this restores select-then-copy while keeping the
        # familiar ctrl+c-to-quit when nothing is selected. ctrl+q always quits.
        ("ctrl+c", "copy_or_quit", "Copy / Quit"),
        ("ctrl+t", "toggle_terminal", "Terminal"),
        ("ctrl+b", "run_background", "Background"),
        ("ctrl+g", "voice_talk", "Talk"),
        ("ctrl+l", "voice_toggle", "Listen"),
        ("escape", "cancel_turn", "Cancel"),
        # Parallel sessions. alt+… chords are used because ctrl+a/e/k/u/w are
        # shadowed by Textual's Input editing bindings while #prompt has focus
        # (the normal state).
        ("alt+t", "toggle_todos", "Todos"),
        ("ctrl+end", "jump_latest", "Jump to latest"),
        ("ctrl+n", "new_session", "New session"),
        ("alt+right", "next_session", "Next session"),
        ("alt+left", "prev_session", "Prev session"),
        *[(f"alt+{i}", f"goto_session({i})", f"Session {i}") for i in range(1, 10)],
    ]

    def __init__(
        self,
        *,
        agent,
        assistant_id,
        session_state,
        backend,
        token_tracker,
        image_tracker,
        model_name,
        session_manager=None,
        restored_messages=None,
        sandbox_id: str | None = None,
        sandbox_type: str | None = None,
        sandbox_meta: dict | None = None,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.assistant_id = assistant_id
        self.session_state = session_state
        self.backend = backend
        self.token_tracker = token_tracker
        self.image_tracker = image_tracker
        # Collapses large pastes into [paste #N +M lines] placeholders; resolved
        # back to full text on submit. Shared helpers with the legacy input.
        self.paste_tracker = PasteTracker()
        self.model_name = model_name or "unknown"
        # Provider of the live model, recorded on save so a resume can rebuild
        # the exact model instead of falling back to the global config. Derived
        # from the same precedence chain that built the model; kept in sync by
        # the model-switch path. None means "unknown", which resume treats as
        # "nothing to restore".
        try:
            from novacode_cli.utils.model_info import get_current_provider

            self._model_provider: str | None = get_current_provider()
        except Exception:  # noqa: BLE001 — never block app construction
            self._model_provider = None
        self.session_manager = session_manager
        # Sandbox identity for session persistence (so --continue can reconnect).
        self._sandbox_id = sandbox_id
        self._sandbox_type = sandbox_type
        self._sandbox_meta = sandbox_meta
        # Prior conversation turns to replay into the transcript on resume.
        self._restored_messages = list(restored_messages or [])
        self._seen: set[str] = set()
        self._speech_lock = asyncio.Lock()
        self._live_buf = ""  # accumulating streamed answer prose
        self._reasoning_buf = ""  # accumulating reasoning trace
        self._stream_msg: ChatMessage | None = None  # in-progress Nova answer widget
        self._reason_msg: ChatMessage | None = None  # in-progress reasoning widget
        self._current_assistant_id: str | None = None
        # Streaming coalescing: deltas append to the buffers above, but the widget
        # is only repainted on a ~50ms timer (see _flush_stream) so a fast token
        # stream doesn't trigger a full re-render + scroll per token.
        self._stream_flush_scheduled = False
        # "Follow the tail" intent: True while the user wants new content to
        # auto-scroll into view. It is NOT the same as being geometrically at the
        # bottom — content growth raises max_scroll_y without the user moving, so
        # geometry alone cannot tell "the user scrolled away" from "the answer got
        # longer". Only a scroll that moves the viewport *up* clears this flag;
        # reaching the bottom (or clicking jump-to-latest) sets it again.
        self._follow_tail = True
        # Last label rendered into #jump-latest. _update_jump_latest runs on every
        # scroll event and every ~100ms streaming flush, so re-rendering the same
        # text would churn the footer layout ~10x/sec while thinking streams.
        self._jump_latest_label = ""
        # Cached singleton widget refs (resolved once in on_mount) to avoid a
        # query_one DOM walk on every delta / keystroke / status tick.
        self._w_cache: dict[str, Any] = {}
        # call_id -> (collapsible, body Static, base title) for open tool calls
        self._tool_components: dict[str, tuple[Collapsible, Static, str]] = {}
        # fallback for tool calls that arrive without an id
        self._last_tool: tuple[Collapsible, Static, str] | None = None
        # Condensed tool view: a run of consecutive tool calls collapses into a
        # single "tool group" panel (one compact line per tool) instead of one
        # Collapsible per call, so a burst of tools doesn't flood the chat.
        self._tool_group: Collapsible | None = None
        self._tool_group_body: Vertical | None = None
        self._tool_group_entries: list[dict] = []  # per-tool {base, mark, detail, error}
        self._tool_group_lines: dict[str, int] = {}  # call_id -> entry index
        self._tool_group_last_idx: int | None = None  # fallback for id-less results
        # Coalescing state for tool-group repaints (see
        # _schedule_tool_group_refresh): a burst of tool events paints once.
        self._tool_group_refresh_scheduled: bool = False
        self._tool_group_running: str | None = None
        # subagent tracking: call_id -> (collapsible, body Static, type, start_time)
        self._subagent_widgets: dict[str, tuple[Collapsible, Static, str, float]] = {}
        self._subagent_count: int = 0  # running total for display
        # Maps running subagent tool call_id -> subagent task call_id
        self._subagent_tool_to_task: dict[str, str] = {}
        self._remote_msg: Any = None  # current RemoteMessage during remote turn
        self._remote_question_future: asyncio.Future | None = None
        # Live tool output arrives on the shell's background loop thread, one
        # ~1KB chunk at a time. It is buffered here and painted in batches.
        self._tool_out_lock = threading.Lock()
        self._tool_out_pending: dict[str, list[str]] = {}
        self._tool_out_scheduled = False
        # Voice I/O (lazy: only built when first used; None when deps absent).
        self._voice_pipeline: Any = None
        self._voice_listening: bool = False
        self._voice_capturing: bool = False
        self._voice_speak_responses: bool = False  # set from config on first use
        # Tool/subagent names used during the current remote turn — collapsed
        # into the compact live status line's condensed counts.
        self._remote_activity: list[str] = []
        # The per-turn live status line (edits one compact message in place).
        self._remote_status: Any = None
        # The MatrixRain animation widget, tracked so it can be removed on clear.
        self._home_banner: Static | None = None
        # name → async (args) -> str  for slash commands contributed by plugins.
        self._plugin_commands: dict[str, Any] = {}
        # Strong refs to fire-and-forget background sends so the event loop
        # doesn't garbage-collect them mid-flight (asyncio only holds weak refs).
        self._bg_tasks: set[Any] = set()
        # One-shot guard for the Ollama CPU-offload advisory (set once the model
        # is loaded and checked, so we don't spawn `ollama ps` every turn).
        self._ollama_offload_checked = False
        self._btw_agent: Any = None  # lazy-init btw side-channel agent (web-search only)
        self._bg_job_count: int = 0  # monotonic counter for background shell jobs
        # Notes for detached (Ctrl+B) shell jobs that finished; prepended to the
        # agent's next turn so it learns of completions without an interrupt.
        self._pending_job_notes: list[str] = []
        # 1s timer that ticks the ⚙ tasks-bar runtime; only runs while tasks are
        # active (see _refresh_tasks_bar) so it never interferes when idle.
        self._tasks_timer: Any = None
        # Todo data for the docked checklist (#todo-dock). Per-pane, because
        # the dock widget is app-global: a session switch repaints from these.
        self._todos: list = []
        self._todos_agent: str | None = None
        self._todos_collapsed: bool = False
        # _init_widget and _init_steps removed — /init progress is now _log-only
        # Live per-iteration Ralph cards, keyed by iteration number, so an
        # IterationFinished event can update the card mounted at its start.
        self._ralph_iter_cards: dict[int, Static] = {}
        self._skill_names_cache: list[str] | None = None
        # Enabled-skill count shown in the status bar (total minus curation-
        # disabled), cached briefly; invalidated to None on a /skills toggle.
        self._skill_count_cache: int | None = None
        self._skill_count_ts: float = 0.0
        self._agent_names_cache: list[str] | None = None
        # Responsive layout flags, driven by _apply_responsive_layout on resize.
        self._narrow = False
        self._tiny = False
        # Live status state (animated spinner + elapsed while a turn runs).
        self._activity = "ready"
        self._turn_active = False
        # The in-flight remote-bridge turn, if any. Held so escape can cancel
        # that turn without cancelling the consumer loop that received it.
        self._remote_turn_task: asyncio.Task | None = None
        # Set while a Ctrl+B detach cancels the turn, so the CancelledError path
        # shows a "moved to background" note instead of "Cancelled."
        self._detach_cancelling = False
        self._turn_start = 0.0
        self._spinner_frame = 0
        # Input mode pulse animation (plan / bash) — see _set_input_pulse.
        self._input_pulse_mode: str | None = None
        self._input_pulse_timer: Any = None
        self._pulse_on = False
        # Per-keystroke de-churn: last (plan, bash) mode state + last palette list.
        self._last_mode_state: tuple[bool, bool, bool] | None = None
        self._last_palette: list[str] | None = None
        # Notification badge: last seen unread count (drives status refresh).
        self._last_notif_count = 0
        # Context-window management: warn once per crossing; auto-compact at critical.
        self._ctx_warned = False
        self._auto_compact = True
        # Live steering: SteeringInstructions added mid-turn, removed when it ends.
        self._live_steers: list = []
        # Deferred prompts: messages sent during an active turn that weren't
        # consumed by the steering middleware are re-dispatched as new turns.
        self._deferred_prompts: list[str] = []
        # Slash/bash commands submitted during an active turn: queued to RUN as
        # commands once the turn ends (not steered / not sent to the agent as text).
        self._deferred_commands: list[str] = []
        # Nova learning status (review cycles) shown inline in the #status line
        # beside the context %, so it never overlaps the input. _nova_status is
        # the current message (or None); the timer auto-clears it after a moment.
        self._nova_status: str | None = None
        self._nova_status_style: str = "dim"
        self._nova_indicator_timer: Any = None
        self._os_focused = True

    def _current_agent_info(self) -> tuple[str, str]:
        from novacode_cli.core.input_preparation import get_agent_display_name
        from novacode_cli.config.config import get_agent_color

        aid = self._current_assistant_id or self.assistant_id
        name = get_agent_display_name(aid)
        color = get_agent_color(aid) if name != "Nova" else "#10b981"
        return name, color

    def compose(self) -> ComposeResult:
        # One scroll region per session, swapped by a ContentSwitcher. The root
        # pane deliberately keeps id="transcript" so existing CSS, the widget
        # cache and every test that queries "#transcript" are unaffected while
        # there is only one session.
        # Hidden until a second session exists, so a single-session run looks
        # exactly as it did before.
        # Tooltips: Textual shows a widget's tooltip on hover after
        # TOOLTIP_DELAY, walking up the ancestor chain — so setting one on a
        # container covers all of its children. They document the clickable
        # footer components and the info-bar columns, which are otherwise
        # unlabelled beyond a one-word heading.
        yield Tabs(id="session-tabs").with_tooltip(
            "Session tabs — alt+←/→ to switch, ctrl+n for a new session"
        )
        with ContentSwitcher(initial="transcript", id="panes"):
            yield TranscriptScroll(id="transcript").with_tooltip(
                "Transcript — drag to select, ctrl+c to copy"
            )
        yield OptionList(id="cmdpalette")
        with Vertical(id="prompt-dock"):
            # Todos live INSIDE the prompt dock, not as a second
            # dock:bottom sibling: two bottom-docked siblings both claim the
            # same rows, so the checklist rendered on top of the input and
            # only appeared after a resize forced a reflow.
            yield Static("", id="todo-dock").with_tooltip(
                "Todo checklist — click (or alt+t) to collapse/expand"
            )
            # "Jump to latest" sits at the top-right of the footer, directly above
            # the status/skills bar. The outer row is a transparent, right-aligning
            # strip; the inner Horizontal shrinks to the text (width: auto) and the
            # Static inside it is the only click target — the empty space to the
            # left of the words is not part of the button.
            with (
                Horizontal(id="jump-latest-row"),
                Horizontal(id="jump-latest-box"),
            ):
                yield Static("", id="jump-latest").with_tooltip(
                    "Jump to the latest message — click (or ctrl+end)"
                )
            yield Static("", id="prompt-hint-bar")
            with Horizontal(id="prompt-row"):
                yield Static("> ", id="prompt-prefix")
                yield PromptInput(
                    placeholder="Type your message or @path/to/file  (shift+enter for a new line)",
                    id="prompt",
                    paste_tracker=self.paste_tracker,
                    on_large_paste=self._on_large_paste,
                ).with_tooltip(
                    "Enter to send · shift+enter for a new line · "
                    "/ for commands · @ for files/agents · ! for bash"
                )
            yield Static("", id="mode-badge").with_tooltip(
                "Active input mode (normal / bash / plan)"
            )
            # Persistent background-tasks indicator (hidden until a task runs).
            # Click it (or Ctrl+B with nothing running) to open the tasks panel.
            yield Static("", id="tasks-bar").with_tooltip(
                "Background tasks — click (or ctrl+b) to open the tasks panel"
            )
            with Horizontal(id="info-bar"):
                with Vertical(id="col-workspace", classes="info-col").with_tooltip(
                    "Workspace root — the directory Nova reads and writes"
                ):
                    yield Static("workspace (/directory)", classes="info-label")
                    yield Static("", id="info-workspace", classes="info-value")
                with Vertical(classes="info-col").with_tooltip(
                    "Current git branch of the workspace"
                ):
                    yield Static("branch", classes="info-label")
                    yield Static("", id="info-branch", classes="info-value")
                with Vertical(id="col-sandbox", classes="info-col").with_tooltip(
                    "Sandbox backend running the agent's shell commands"
                ):
                    yield Static("sandbox", classes="info-label")
                    yield Static("", id="info-sandbox", classes="info-value")
                with Vertical(classes="info-col").with_tooltip(
                    "Active model — /model to switch"
                ):
                    yield Static("/model", classes="info-label")
                    yield Static("", id="info-model", classes="info-value")
                with Vertical(classes="info-col").with_tooltip(
                    "Context window fill, then cumulative session usage — "
                    "/context for the full breakdown"
                ):
                    yield Static("context", classes="info-label")
                    yield Static("", id="info-quota", classes="info-value")
                # Persistent artifacts component — fixed in the footer, click (or
                # /artifacts) to open the list. Updates live via a registry observer.
                with Vertical(id="col-artifacts", classes="info-col").with_tooltip(
                    "Artifacts — click (or /artifacts) to open the list"
                ):
                    yield Static("artifacts", classes="info-label")
                    yield Static("", id="info-artifacts", classes="info-value")

    def _apply_saved_theme(self) -> None:
        """Register Nova's palettes and apply the persisted theme (or default)."""
        for theme in (NOVA_TOKYO_NIGHT, NOVA_MATRIX):
            try:
                self.register_theme(theme)
            except Exception:  # noqa: BLE001
                pass
        name = DEFAULT_THEME
        try:
            from novacode_cli.config.nova_config import NovaConfig

            saved = NovaConfig().get("theme")
            if saved and saved in self.available_themes:
                name = saved
        except Exception:  # noqa: BLE001
            pass
        try:
            self.theme = name
        except Exception:  # noqa: BLE001
            pass

    def on_mount(self) -> None:
        import threading

        self._thread_id = threading.get_ident()
        # Log what the loop was running whenever the UI freezes for >1s
        # (~/.nova/logs/freeze.log). The UI and the agent share this loop, so
        # the stack is the culprit, wherever it lives.
        try:
            from novacode_cli.tui.stall_watch import StallWatch

            self._stall_watch = StallWatch(asyncio.get_running_loop())
            self._stall_watch.start()
        except Exception:  # noqa: BLE001 — diagnostics must never stop the app
            self._stall_watch = None
        # Build the slash-autocomplete skill list off the loop now: built lazily
        # on the first "/" keystroke it scans ~750 SKILL.md files on the loop.
        threading.Thread(target=self._get_skill_names, name="nova-skill-names", daemon=True).start()
        # Register Nova's palette and apply the saved (or default) theme first,
        # so the whole UI renders with the right colors from the first frame.
        self._apply_saved_theme()
        # Warm the singleton-widget cache so hot paths (streaming, status ticks,
        # keystrokes) skip the query_one DOM walk. See _w().
        for _sel, _kind in (
            ("#transcript", VerticalScroll),
            ("#prompt", PromptInput),
            ("#mode-badge", Static),
            ("#cmdpalette", OptionList),
            ("#prompt-hint-bar", Static),
            ("#jump-latest-row", Horizontal),
            ("#jump-latest-box", Horizontal),
            ("#jump-latest", Static),
            ("#info-workspace", Static),
            ("#info-branch", Static),
            ("#info-sandbox", Static),
            ("#info-model", Static),
            ("#info-quota", Static),
        ):
            try:
                self._w_cache[_sel] = self.query_one(_sel, _kind)
            except NoMatches:
                pass
        self.query_one("#cmdpalette", OptionList).display = False
        self._init_root_pane()
        self._set_status("ready")
        self._update_mode_badge()
        self._refresh_hint_bar()
        self._refresh_info_bar()
        # Background tasks (Ctrl+B): observe the registry so the persistent
        # indicator + notifications update reactively; tick once a second so the
        # runtime clock advances while tasks run.
        try:
            from novacode_cli.shell.jobs import get_registry

            get_registry().add_observer(self._on_task_event_threadsafe)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_tasks_bar()
        # Persistent artifacts component: observe the registry + show initial count.
        # bind_session first, so a resumed session's artifacts are back in the
        # registry (and its count) before the component renders.
        try:
            from novacode_cli.artifacts.registry import bind_session as _bind_artifacts
            from novacode_cli.artifacts.registry import get_registry as _get_art_registry

            _restored_artifacts = _bind_artifacts(
                getattr(self.session_state, "session_id", "") or "",
                getattr(self.session_manager, "sessions_dir", None),
            )
            _get_art_registry().add_observer(self._on_artifact_event_threadsafe)
            if _restored_artifacts:
                self._log(
                    Text(
                        f"◈ restored {_restored_artifacts} artifact(s) from this session",
                        style="dim",
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        self._refresh_artifacts_component()
        # Keep the info bar live: model / branch / sandbox / quota can change
        # outside any single command (e.g. the agent runs `git checkout`), so
        # refresh on a slow timer (branch git runs off-thread, so it's cheap).
        self.set_interval(3.0, self._refresh_info_bar)
        # Load slash commands contributed by enabled plugins (TUI dispatch).
        self._load_plugin_commands()
        # Animate the live status (~5 fps) while a turn is active.
        self.set_interval(0.05, self._tick)
        self.query_one("#prompt", PromptInput).focus()
        # Show ASCII art banner on home screen
        self._show_home_banner()
        # If voice is enabled in config, pre-download models at startup
        # so the first push-to-talk or spoken reply is instant.
        self._eager_voice_warmup()
        # Replay prior conversation when resuming a session.
        self._replay_history()
        # Route remote bridge status messages into the transcript (not stdout).
        mgr = getattr(self.session_state, "_remote_bridge_manager", None)
        if mgr is not None:

            async def _status_cb(m: str) -> None:
                self._log(Text(f"🔗 Remote: {m}", style="dim"))

            try:
                mgr.set_status_callback(_status_cb)
            except Exception:  # noqa: BLE001
                pass
        # Consume remote (Discord/Telegram) messages and render them in the TUI.
        if getattr(self.session_state, "_remote_message_queue", None) is not None:
            self._remote_consumer()
        # Register tool output callback for live terminal/command execution streaming
        try:
            from novacode_cli.events import register_tool_output_callback

            register_tool_output_callback(self._on_tool_output)
        except Exception:
            pass

    def _post_to_ui(self, callback: Any, *args: Any) -> None:
        """Run ``callback(*args)`` on the UI thread WITHOUT waiting for it.

        Textual's ``call_from_thread`` blocks the calling thread until the UI has
        run the callback. The callers here are the shell's shared background
        loop and tool threads: blocking them on the UI paced every running
        command and background task to the UI's speed, and stalled them all
        whenever the UI was busy. Order is preserved (FIFO scheduling). During
        shutdown the update is dropped rather than run off the UI thread.
        """
        if getattr(self, "_thread_id", None) == threading.get_ident():
            callback(*args)
            return
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return

        async def run() -> None:
            try:
                with self._context():
                    callback(*args)
            except Exception:  # noqa: BLE001 — a UI update must never kill the caller
                logger.debug("UI update failed", exc_info=True)

        try:
            asyncio.run_coroutine_threadsafe(run(), loop)
        except RuntimeError:  # loop closed between the check and the call
            pass

    def _on_tool_output(self, call_id: str, text: str) -> None:
        """Queue a chunk of live tool output; it is painted in batches (<=20/s)."""
        with self._tool_out_lock:
            self._tool_out_pending.setdefault(call_id, []).append(text)
            if self._tool_out_scheduled:
                return
            self._tool_out_scheduled = True
        self._post_to_ui(self.set_timer, 0.05, self._flush_tool_output)

    def _flush_tool_output(self) -> None:
        with self._tool_out_lock:
            pending, self._tool_out_pending = self._tool_out_pending, {}
            self._tool_out_scheduled = False
        for call_id, parts in pending.items():
            self._write_tool_output(call_id, "".join(parts))

    def _write_tool_output(self, call_id: str, text: str) -> None:
        """Append live output to the widget showing ``call_id`` (UI thread)."""
        if call_id in self._tool_components:
            comp, body, base = self._tool_components[call_id]
            if isinstance(body, RichLog):
                body.write(text)
                body.scroll_end(animate=False)
        elif call_id in self._subagent_tool_to_task:
            subagent_cid = self._subagent_tool_to_task[call_id]
            if subagent_cid in self._subagent_widgets:
                comp, body, stype, start_time = self._subagent_widgets[subagent_cid]
                try:
                    log_widget = body.query_one("#subagent-log", RichLog)
                    if not log_widget.has_class("active"):
                        log_widget.add_class("active")
                    log_widget.write(text)
                    log_widget.scroll_end(animate=False)
                    comp._log_lines = getattr(comp, "_log_lines", 0) + text.count("\n")
                    log_widget.styles.height = min(max(comp._log_lines + 2, 5), 8)
                except Exception:
                    pass
        elif self._tool_group_body is not None:
            try:
                log_widget = self._tool_group_body.query_one("#tool-group-log", RichLog)
                if log_widget.has_class("active"):
                    log_widget.write(text)
                    log_widget.scroll_end(animate=False)
                    self._tool_group_log_lines += text.count("\n")
                    log_widget.styles.height = min(max(self._tool_group_log_lines + 2, 5), 8)
            except Exception:
                pass

    # -- OS focus handlers ----------------------------------------------------
    # Pause/resume the MatrixRain animation when the terminal window gains or
    # loses OS-focus, so the TUI never spins the CPU on an invisible animation.

    def _matrix_rain(self) -> MatrixRain | None:
        """Return the MatrixRain widget if it is mounted, else None."""
        try:
            return self.query_one("#matrix-rain", MatrixRain)
        except NoMatches:
            return None

    def on_app_blur(self) -> None:
        """Pause MatrixRain when the terminal loses OS focus."""
        self._os_focused = False
        rain = self._matrix_rain()
        if rain is not None:
            rain.pause()

    def on_app_focus(self) -> None:
        """Resume MatrixRain when the terminal regains OS focus."""
        self._os_focused = True
        rain = self._matrix_rain()
        if rain is not None:
            vp = getattr(self, "_voice_pipeline", None)
            if vp is None or not vp.tts_active:
                rain.resume()

    # -- helpers --------------------------------------------------------------
    def _w(self, selector: str, kind: Any) -> Any:
        """Return a cached singleton widget, resolving (and caching) on first use.

        Avoids a `query_one` DOM walk on every delta / keystroke / status tick.
        Raises NoMatches (like query_one) if the widget isn't mounted yet — hot
        callers that may run before mount guard with try/except.
        """
        w = self._w_cache.get(selector)
        if w is None:
            try:
                w = self.query_one(selector, kind)
            except NoMatches:
                if self.screen_stack:
                    w = self.screen_stack[0].query_one(selector, kind)
                else:
                    raise
            self._w_cache[selector] = w
        return w

    def _transcript(self) -> VerticalScroll:
        """The active session's scroll region.

        Resolved through the active pane rather than the ``_w`` cache: that cache
        keys on the selector, so after a tab switch it would keep handing back
        the previous pane's widget and content would land in the wrong session.
        """
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.scroll is not None:
            return pane.scroll
        return self._w("#transcript", VerticalScroll)

    # ── session panes ────────────────────────────────────────────────────

    def _init_root_pane(self) -> None:
        """Register the in-process session as pane 0.

        Called once at mount. With a single session this changes nothing the user
        can see: the pane simply wraps the existing ``#transcript`` widget.
        """
        from novacode_cli.tui.session_pane import SessionPane

        try:
            scroll = self.query_one("#transcript", VerticalScroll)
        except NoMatches:  # pragma: no cover - compose always yields it
            return
        pane = SessionPane(
            sid="root",
            title=(getattr(self.session_state, "session_id", "") or "main")[:8],
            scroll=scroll,
            kind="root",
        )
        self._panes: list = [pane]
        self._root_pane = pane
        self._active_pane = pane
        # Seed the (hidden) tab bar now so it is never empty when a session is
        # spawned later. See _refresh_tabs for why an empty bar is a problem.
        self._refresh_tabs()

    def _pane_for(self, sid: str):
        """The pane owning session *sid*, or None."""
        for pane in getattr(self, "_panes", []):
            if pane.sid == sid:
                return pane
        return None

    async def _deliver(self, pane, event) -> None:
        """Route one stream event to *pane*: render if visible, else buffer.

        A hidden pane must not draw, because the app's widget references
        (``_stream_msg``, ``_tool_group``, …) belong to whichever pane is
        currently swapped in — rendering into a hidden pane would scribble into
        the visible one. Transient deltas are dropped rather than buffered: the
        authoritative text arrives as ``AssistantMessage``, so replaying a
        thousand keystroke-sized fragments on switch would be pure cost.
        """
        self._feed_remote(pane, event)
        if pane is None:  # before panes exist (early mount) — render directly
            await self._render(event)
            return
        pane.status = _status_for_event(event, pane.status)
        if pane is getattr(self, "_active_pane", pane):
            await self._render(event)
        else:
            if not isinstance(event, (ev.TextDelta, ev.ReasoningDelta, ev.StatusUpdate)):
                pane.buffer.append(event)
                pane.unread += 1

    async def _switch_to(self, pane) -> None:
        """Make *pane* the visible session, swapping conversation state with it."""
        current = getattr(self, "_active_pane", None)
        if pane is current:
            return
        if current is not None:
            current.save_from(self)
        if pane.has_state:
            pane.load_into(self)

        self._active_pane = pane
        try:
            self.query_one("#panes", ContentSwitcher).current = pane.scroll.id
        except NoMatches:  # pragma: no cover - switcher always present
            pass
        # Keep the tab highlight in sync with the pane that is actually active.
        # Re-entry is harmless: the TabActivated handler no-ops when the pane is
        # already active.
        with contextlib.suppress(Exception):
            tabs = self.query_one("#session-tabs", Tabs)
            if tabs.active != pane.sid:
                tabs.active = pane.sid

        # Replay what arrived while this pane was hidden.
        while pane.buffer:
            await self._render(pane.buffer.popleft())
        pane.unread = 0

        # The todo dock is app-global but its data is per-pane, so repaint
        # it for the pane now on screen — otherwise the previous session's
        # checklist sits under this one.
        self._paint_todos(getattr(self, "_todos", None), getattr(self, "_todos_agent", None))
        self._refresh_status()
        self._update_mode_badge()
        self._scroll_end()

    async def _dispatch_to_child(self, pane, text: str) -> None:
        """Handle input while a spawned session's tab is active.

        Only session-management commands are interpreted locally; everything else
        is forwarded to the child as a prompt.

        # ponytail: other slash commands operate on self.agent, which a child
        # pane doesn't own. Forward a command channel to the child only if it
        # turns out people want /model, /compact etc. per session.
        """
        stripped = text.strip()
        low = stripped.lower()

        if low in ("/quit", "/exit", "quit", "exit", "q"):
            await self.action_quit()
            return
        if low in ("/close", "/session close"):
            await self._close_session(pane)
            return
        if low.startswith("/session"):
            await self._run_session_command(stripped)
            return
        if stripped.startswith("/") or stripped.startswith("!"):
            self._log(
                Text(
                    f"“{stripped.split()[0]}” isn't available inside a spawned session. "
                    "Use /close, or switch to the main session (alt+1).",
                    style="#e0af68",
                )
            )
            return

        await self._add_message(Text("You", style="bold cyan"), "user", Markdown(stripped))
        if await self._supervisor().send_prompt(pane.sid, stripped) is None:
            self._log(Text("✖ that session is no longer running.", style="bold #f7768e"))
        pane.status = "running"
        self._refresh_tabs()

    async def _run_session_command(self, text: str) -> None:
        """``/session new|list|close [name[: task]]``."""
        parts = text.split(maxsplit=2)
        sub = (parts[1] if len(parts) > 1 else "list").lower()
        rest = parts[2] if len(parts) > 2 else ""

        if sub == "new":
            name, _, task = rest.partition(":")
            await self.spawn_session(name.strip() or "session", task.strip())
            return

        if sub == "close":
            pane = self._pane_for(rest.strip()) if rest.strip() else self._active_pane
            if pane is None:
                self._log(Text(f"No session “{rest.strip()}”.", style="#e0af68"))
                return
            await self._close_session(pane)
            return

        block = Text()
        for i, pane in enumerate(getattr(self, "_panes", []), 1):
            glyph = self._PANE_GLYPHS.get(pane.status, "●")
            marker = "→" if pane is self._active_pane else " "
            block.append(f"{marker} {i}. {glyph} {pane.title}  [{pane.status}]")
            if pane.branch:
                block.append(f"  {pane.branch}", style="dim")
            block.append("\n")
        block.append("\n/session new <name>[: task] · /session close [name] · alt+<n>", style="dim")
        self._log(block)

    # ── tab bar ──────────────────────────────────────────────────────────

    _PANE_GLYPHS = {
        "idle": "●",
        "running": "◐",
        "needs-approval": "⚠",
        "starting": "⏳",
        "crashed": "✖",
        "exited": "○",
    }

    def _refresh_tabs(self) -> None:
        """Redraw the session tab bar; hidden while there is only one session."""
        panes = getattr(self, "_panes", [])
        try:
            tabs = self.query_one("#session-tabs", Tabs)
        except NoMatches:  # pragma: no cover - compose always yields it
            return

        # Visibility is cosmetic; the tab set is always kept in sync. The root
        # tab is therefore present (hidden) from startup, so adding the first
        # child never lands in an EMPTY bar — which would auto-activate the tab
        # being added and queue a stale TabActivated that switched the user back.
        tabs.display = len(panes) > 1

        want = []
        for i, pane in enumerate(panes, 1):
            glyph = self._PANE_GLYPHS.get(pane.status, "●")
            unread = f" +{pane.unread}" if pane.unread else ""
            want.append((pane.sid, f"{i}:{glyph} {pane.title}{unread}"))

        # Update INCREMENTALLY; never clear() and rebuild. clear()+add_tab emits
        # a TabActivated for the first tab, delivered asynchronously — so it
        # landed after the rebuild and dragged the user back to pane 1. That is
        # why a freshly spawned session seemed to "do nothing": you were silently
        # returned to the root pane and everything you typed went to the root
        # agent. Adding a tab to a non-empty bar does not change the selection.
        existing = {t.id: t for t in tabs.query(Tab)}
        wanted = {sid for sid, _ in want}

        for sid in existing:
            if sid not in wanted:
                with contextlib.suppress(Exception):
                    tabs.remove_tab(sid)

        for sid, label in want:
            tab = existing.get(sid)
            if tab is None:
                tabs.add_tab(Tab(label, id=sid))
            else:
                tab.label = label

    def on_tabs_tab_activated(self, event) -> None:
        """Clicking / keyboard-selecting a tab switches sessions.

        Ignored while the bar is being rebuilt: adding tabs emits TabActivated
        for the first one, which would otherwise drag the user back to pane 1
        every time a session is spawned or closed.
        """
        if getattr(self, "_rebuilding_tabs", False):
            return
        tab = getattr(event, "tab", None)
        pane = self._pane_for(getattr(tab, "id", "") or "")
        if pane is not None and pane is not getattr(self, "_active_pane", None):
            self.run_worker(self._switch_to(pane))

    # ── session actions ──────────────────────────────────────────────────

    def action_new_session(self) -> None:
        self.run_worker(self._prompt_new_session())

    def action_next_session(self) -> None:
        self._cycle_session(1)

    def action_prev_session(self) -> None:
        self._cycle_session(-1)

    def _cycle_session(self, delta: int) -> None:
        panes = getattr(self, "_panes", [])
        if len(panes) < 2:
            return
        try:
            idx = panes.index(self._active_pane)
        except ValueError:
            idx = 0
        self.run_worker(self._switch_to(panes[(idx + delta) % len(panes)]))

    def action_goto_session(self, number: int) -> None:
        """alt+<n>: jump straight to the nth session."""
        panes = getattr(self, "_panes", [])
        if 1 <= number <= len(panes):
            self.run_worker(self._switch_to(panes[number - 1]))

    # ── spawning child sessions ──────────────────────────────────────────

    def _supervisor(self):
        """The child-process manager, created on first use."""
        sup = getattr(self, "_session_supervisor", None)
        if sup is None:
            from novacode_cli.sessions.supervisor import SessionSupervisor

            sup = SessionSupervisor(self._on_child_message)
            self._session_supervisor = sup
        return sup

    async def _prompt_new_session(self) -> None:
        """Ctrl+N: ask for a name + task, then spawn."""
        from novacode_cli.tui.screens import QuestionModal

        answer = await self.push_screen_wait(
            QuestionModal(
                {
                    "question": (
                        "New parallel session — name it, optionally with a task:\n"
                        "  fix-parser: add retry logic to the HTTP client"
                    )
                }
            )
        )
        # QuestionModal dismisses with {"response": QuestionResponse}, and
        # QuestionResponse is a TypedDict — so the answer is response["answer"],
        # NOT an attribute. Reading it as an attribute silently stringified the
        # whole dict and named the session "{'answer'".
        raw = ""
        if isinstance(answer, dict):
            response = answer.get("response")
            if isinstance(response, dict):
                raw = str(response.get("answer") or "")
            elif isinstance(response, str):
                raw = response
        elif isinstance(answer, str):
            raw = answer
        if not raw.strip():
            return
        name, _, task = raw.partition(":")
        await self.spawn_session(name.strip() or "session", task.strip())

    async def spawn_session(self, name: str, task: str = "") -> None:
        """Create a worktree, launch a child session, and show it as a new tab."""
        import uuid

        from novacode_cli.sessions import worktree as wt
        from novacode_cli.tui.session_pane import SessionPane

        sup = self._supervisor()
        if sup.at_capacity():
            self._log(
                Text(
                    "⚠ Session limit reached — close one before starting another.",
                    style="bold #e0af68",
                )
            )
            return

        sid = f"s-{uuid.uuid4().hex[:8]}"
        self._log(Text(f"⏳ preparing worktree for “{name}”…", style="#7aa2f7"))

        try:
            info = await asyncio.to_thread(
                wt.create_worktree, name, repo=wt.repo_root(Path.cwd()), session_id=sid
            )
        except Exception as e:  # noqa: BLE001 — surface, don't crash the TUI
            self._log(Text(f"✖ could not create worktree: {e}", style="bold #f7768e"))
            return

        for warn in info.warnings:
            self._log(Text(f"⚠ {warn}", style="#e0af68"))

        # Mount the pane immediately: agent build takes seconds and the UI must
        # never block on it.
        scroll = VerticalScroll(id=f"pane-{sid}")
        await self.query_one("#panes", ContentSwitcher).mount(scroll)
        # ContentSwitcher only hides non-current children at COMPOSE time; one
        # mounted later stays visible and renders on top of the active pane. The
        # new (empty) pane then covered the running session's transcript, which
        # looked like everything had vanished. Hide it until switched to.
        scroll.display = False
        pane = SessionPane(
            sid=sid,
            title=name,
            scroll=scroll,
            kind="child",
            status="starting",
            worktree=info.path,
            branch=info.branch,
        )
        # A pane with no saved state inherits whatever is on the app when it is
        # switched to — including the previous pane's live widget refs. _render
        # reuses `_stream_msg` when it isn't None, so the child's reply streamed
        # into the ROOT pane's hidden widget and this tab stayed black. Start it
        # from a blank conversation instead.
        from novacode_cli.states.Session import SessionState
        from novacode_cli.tui.session_pane import fresh_state

        child_state = SessionState(auto_approve=False, no_splash=True)
        child_state.session_id = sid
        pane.state = fresh_state(
            session_state=child_state,
            assistant_id=self.assistant_id,
            model_name=self.model_name,
        )
        self._panes.append(pane)
        self._refresh_tabs()
        # Switch to it. Without this the new tab merely EXISTS while the root
        # session stays active, so everything typed next goes to the root agent
        # and renders in the root transcript — the spawned session just sits
        # there never receiving a prompt, which reads as "the tab does nothing".
        await self._switch_to(pane)

        try:
            child = await sup.spawn(
                session_id=sid,
                name=name,
                worktree=info.path,
                branch=info.branch,
                assistant_id=self.assistant_id or "nova-agent",
            )
        except Exception as e:  # noqa: BLE001
            pane.status = "crashed"
            self._refresh_tabs()
            self._log(Text(f"✖ could not start session: {e}", style="bold #f7768e"))
            return

        pane.child = child
        await self._open_remote_topics(pane)
        self._pending_task_for = getattr(self, "_pending_task_for", {})
        if task:
            self._pending_task_for[sid] = task
        note = Text()
        note.append(f"◆ you are now in session “{name}”\n", style="bold #9ece6a")
        note.append(f"   {info.path}", style="dim")
        if info.branch:
            note.append(f"  ·  {info.branch}", style="dim")
        note.append("\n   alt+1 returns to the main session · /close ends this one", style="dim")
        self._log(note)

    async def _on_child_message(self, sid: str, msg: dict) -> None:
        """Handle one JSONL frame from a child session."""
        from novacode_cli.sessions import protocol

        pane = self._pane_for(sid)
        if pane is None:
            return
        kind = msg.get("t")

        if kind == "ready":
            pane.status = "idle"
            # Send the task it was spawned with, now that it can accept one.
            task = getattr(self, "_pending_task_for", {}).pop(sid, None)
            if task:
                await self._supervisor().send_prompt(sid, task)

        elif kind == "ev":
            event = protocol.decode_event(msg)
            if event is not None:
                await self._deliver(pane, event)

        elif kind == "interrupt":
            pane.status = "needs-approval"
            pane.pending_interrupt = msg
            # A turn started from Telegram runs with auto-approve (as the main
            # session's remote turns do), so settle it even in a hidden tab.
            remote_tool = pane.remote_turns and str(msg.get("kind") or "tool") == "tool"
            if pane is self._active_pane or remote_tool:
                # @work: returns a Worker, not an awaitable. The handler shows
                # approval modals via push_screen_wait, which Textual only
                # permits inside a worker — child interrupts arrive on the
                # supervisor's raw stdout-reader task, so without @work the
                # push raised NoActiveWorker and every child approval failed
                # closed to a silent reject ("User rejected the tool call").
                self._handle_child_interrupt(pane)
            else:
                # Never steal the screen for a background session: flag the tab
                # and present it when the user switches there.
                with contextlib.suppress(Exception):
                    self.session_state.add_notification(
                        "warn",
                        f"{pane.title}: approval needed",
                        "A background session is waiting for a decision.",
                        "sessions",
                    )

        elif kind == "turn_done":
            pane.status = "idle"
            await self._finish_remote_turn(pane)

        elif kind == "error":
            await self._deliver(pane, ev.Error(message=str(msg.get("message") or "")))

        elif kind == "exited":
            pane.status = "crashed" if msg.get("crashed") else "exited"
            while pane.remote_turns:
                await self._finish_remote_turn(pane, error=f"✖ Session “{pane.title}” ended.")

        self._refresh_tabs()

    @work
    async def _handle_child_interrupt(self, pane) -> None:
        """Present a child's approval in this UI and send the decision back.

        Reuses ``_handle_interrupt`` verbatim, so a spawned session gets exactly
        the same modals as the local one. ``session_state`` is swapped for the
        duration so "remember for this session" and auto-approve apply to the
        CHILD, not the root conversation.
        """
        msg = pane.pending_interrupt
        if msg is None:
            return
        pane.pending_interrupt = None

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        request = ev.InterruptRequest(
            kind=str(msg.get("kind") or "tool"), payload=msg.get("payload"), future=fut
        )
        prev_state = self.session_state
        proxy = pane.state.get("session_state")
        try:
            if proxy is not None:
                self.session_state = proxy
            await self._handle_interrupt(request)
            result = await fut
        except Exception:  # noqa: BLE001 — never leave the child blocked
            result = None
        finally:
            self.session_state = prev_state

        await self._supervisor().reply_interrupt(pane.sid, str(msg.get("id")), result)
        pane.status = "running"
        self._refresh_tabs()

    async def _close_session(self, pane) -> None:
        """Close a spawned session and report what happened to its worktree."""
        from novacode_cli.sessions import worktree as wt

        if pane.kind == "root":
            self._log(Text("The main session can't be closed.", style="#e0af68"))
            return

        await self._supervisor().close(pane.sid)
        while pane.remote_turns:
            await self._finish_remote_turn(pane, error=f"✖ Session “{pane.title}” was closed.")
        await self._close_remote_topics(pane)

        # Worktree cleanup runs only after the process is gone: on Windows a
        # directory a live process holds open cannot be removed. Work is never
        # destroyed — a dirty or committed worktree is kept and reported.
        outcome = ""
        if pane.worktree is not None and pane.branch:
            with contextlib.suppress(Exception):
                repo = wt.repo_root(Path.cwd())
                if repo is not None:
                    outcome = await asyncio.to_thread(
                        wt.remove_worktree, pane.worktree, repo=repo
                    )

        if pane in self._panes:
            self._panes.remove(pane)
        with contextlib.suppress(Exception):
            await pane.scroll.remove()

        if pane is self._active_pane:
            await self._switch_to(self._root_pane)
        self._refresh_tabs()

        note = f"◆ session “{pane.title}” closed"
        if outcome:
            note += f" — worktree {outcome}"
        if pane.branch and "removed" not in outcome:
            note += f" (branch {pane.branch})"
        self._log(Text(note, style="#7aa2f7"))

    def _prune_transcript(self) -> None:
        """Cap the transcript: drop the oldest widgets once it grows too large.

        Skips the in-progress widgets we still hold references to (streaming
        answer/reasoning, open tool/subagent cards, todo, /init tracker) so a
        live turn is never disturbed.
        """
        try:
            tr = self._transcript()
        except NoMatches:
            return
        children = tr.children
        if len(children) <= _MAX_TRANSCRIPT_WIDGETS:
            return
        protected: set[int] = {
            id(w)
            for w in (
                self._stream_msg,
                self._reason_msg,
                self._tool_group,
                *(t[0] for t in self._tool_components.values()),
                *(s[0] for s in self._subagent_widgets.values()),
            )
            if w is not None
        }
        if self._last_tool is not None:
            protected.add(id(self._last_tool[0]))
        to_remove = []
        # Oldest first; stop once we're back at the low-water mark.
        target = len(children) - _TRANSCRIPT_LOW_WATER
        for w in children:
            if len(to_remove) >= target:
                break
            if id(w) not in protected:
                to_remove.append(w)
        if not to_remove:
            return
        # ONE batched removal, not N individual ones. Widget.remove() returns an
        # AwaitRemove and posts its own message; calling it in a loop queued one
        # removal per widget, and since this method is sync none were awaited.
        # Under a fast log burst the queue outran the event loop — the
        # transcript reached 800+ widgets against a 400 cap, and Textual could
        # not drain its pending messages. remove_children() does the whole batch
        # in one operation (measured: an 800-line burst 7.4 s -> 2.9 s).
        try:
            tr.remove_children(to_remove)
        except Exception:  # noqa: BLE001 — pruning must never break rendering
            pass

    def _scroll_end(self, *, force: bool = True) -> None:
        """Scroll the transcript to the newest content.

        Args:
            force: When True (the default) always jump to the bottom. Pass False
                for *automatic* scrolling (new content arriving, a streaming
                repaint) so a user who has scrolled up to read is not yanked back
                down mid-sentence.
        """
        if force or self._follow_tail:
            try:
                self._transcript().scroll_end(animate=False)
            except NoMatches:
                pass
        self._update_jump_latest()

    def _update_jump_latest(self, *, measure: bool = False) -> None:
        """Show the "jump to latest" affordance only while not following the tail.

        Driven by the ``_follow_tail`` intent flag rather than the instantaneous
        scroll geometry: content growth moves the bottom without the user moving,
        so geometry alone would flash the button on every streaming repaint.

        Args:
            measure: Recompute the "N lines below" count. Only pass True from a
                real user scroll. Automatic calls (streaming flushes, new content)
                leave the label alone: the count is measured from the bottom, so
                it would otherwise climb on every flush while the user sits still,
                re-rendering the footer ~10x/sec and reading as the scrollbar
                being pushed toward the button.
        """
        try:
            tr = self._transcript()
        except NoMatches:
            return
        try:
            # The row owns visibility (it is the full-width, transparent strip);
            # the inner Static owns the text and is the only click target.
            row = self._w("#jump-latest-row", Horizontal)
            if not self._follow_tail and measure:
                # Show how far back the user is, so the affordance is informative.
                rows = max(0, int(tr.max_scroll_y - tr.scroll_y))
                label = f"↓ Jump to latest  ({rows} lines below)"
                # Only touch the widget when the text actually changes.
                if label != self._jump_latest_label:
                    self._jump_latest_label = label
                    self._w("#jump-latest", Static).update(Text(label, style="bold"))
            row.set_class(not self._follow_tail, "active")
        except NoMatches:
            pass

    def on_transcript_scroll_at_end_changed(
        self, _event: TranscriptScroll.AtEndChanged
    ) -> None:
        """Toggle the jump-to-latest button when the transcript reaches/leaves the end."""
        self._update_jump_latest(measure=True)

    def on_transcript_scroll_scrolled(self, event: TranscriptScroll.Scrolled) -> None:
        """Track scroll intent: moving up stops following, reaching the end resumes.

        Only the active pane's scroll region counts — a background session
        scrolling must not change what the visible transcript is doing.
        """
        try:
            if event.scroll is not self._transcript():
                return
        except NoMatches:
            return
        if event.new_y < event.old_y - 1:
            # The viewport moved up: the user is reading history, so stop
            # auto-scrolling. (Content growth never moves scroll_y, so this can
            # only be a real user scroll.)
            self._follow_tail = False
        elif event.scroll.is_vertical_scroll_end or (
            event.scroll.max_scroll_y - event.new_y
        ) <= 1:
            # Back at the bottom (by wheel, key, or the jump button): resume.
            self._follow_tail = True
        # A real user scroll: recompute the distance readout.
        self._update_jump_latest(measure=True)

    def action_jump_latest(self) -> None:
        """Scroll the transcript to the newest message (ctrl+end / button click)."""
        self._follow_tail = True
        self._jump_latest_label = ""
        self._scroll_end(force=True)

    async def _mount(self, widget) -> None:
        # Any non-tool content closes the current tool group so transcript order
        # stays correct and the next tool burst starts a fresh group.
        self._close_tool_group()
        await self._transcript().mount(widget)
        self._prune_transcript()
        # Automatic scroll: new content follows the tail only if the user is
        # still following it (see _follow_tail).
        self._scroll_end(force=False)

    def _remote_send(self, text: str) -> None:
        """Send a one-off status line to the remote platform during a remote turn.

        Reserved for low-frequency notices. Per-tool / per-subagent activity goes
        to the session's live status message (see :meth:`_feed_remote`) rather
        than flooding the chat.
        """
        msg = self._remote_msg
        if msg is None:
            return
        try:
            import asyncio

            # Track the task so it isn't garbage-collected mid-send and so
            # exceptions surface rather than vanish (fire-and-forget pitfall).
            task = asyncio.create_task(msg.reply_fn(f"{text}"))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:  # noqa: BLE001
            pass

    # ── remote: one chat, several sessions ───────────────────────────────

    def _remote_sessions(self) -> dict[str, str]:
        """sid -> name of every live session ("main" is the in-process one)."""
        from novacode_cli.remote.routing import ROOT

        out = {ROOT: "main"}
        for pane in getattr(self, "_panes", []):
            if pane.kind == "child" and pane.status not in ("crashed", "exited"):
                out[pane.sid] = pane.title
        return out

    def _remote_label(self, pane) -> str:
        """The session's name for remote messages, or "" when it is the only one."""
        if len(self._remote_sessions()) < 2:
            return ""
        return "main" if pane is None or pane.kind == "root" else pane.title

    def _feed_remote(self, pane, event) -> None:
        """Feed a stream event to the remote status of the session it belongs to."""
        from novacode_cli.remote.status import feed

        try:
            if pane is None or pane.kind == "root":
                if self._remote_status is not None:
                    feed(self._remote_status, event)
                return
            if pane.remote_turns:
                turn = pane.remote_turns[0]
                if turn.status is not None:
                    feed(turn.status, event)
                if isinstance(event, ev.AssistantMessage) and (event.text or "").strip():
                    turn.answer = event.text  # the last one is the answer
        except Exception:  # noqa: BLE001 — the chat mirror must never break rendering
            pass

    async def _remote_route(self, msg: Any) -> bool:
        """Send ``msg`` to its session. True when it was handled here (another
        session, or a session command); False when it is for the main session.
        """
        from novacode_cli.remote.routing import ROOT, RemoteRouter

        router = getattr(self, "_remote_router", None)
        if router is None:
            router = self._remote_router = RemoteRouter()
        sessions = self._remote_sessions()
        text = (getattr(msg, "text", "") or "").strip()
        low = text.lower()
        route_ctx = getattr(msg, "route", None)
        if not isinstance(route_ctx, dict):
            route_ctx = {}

        async def reply(body: str) -> None:
            route_ctx["sid"] = route_ctx.get("sid") or ROOT
            with contextlib.suppress(Exception):
                await msg.reply_fn(body)

        if low in ("/sessions", "/session"):
            await reply(self._remote_sessions_text(msg))
            return True
        if low.startswith("/use"):
            name = text[4:].strip()
            sid = router.match(name, sessions) if name else None
            if sid is None:
                await reply(f"No session “{name}”. {self._remote_sessions_text(msg)}")
                return True
            router.default[msg.chat_id] = sid
            await reply(f"✓ Plain messages here now go to **{sessions[sid]}**.")
            return True
        if low.startswith("/new"):
            name, _, task = text[4:].strip().partition(":")
            if not name.strip():
                await reply("Usage: `/new <name>` or `/new <name>: <task>`")
                return True
            await reply(f"⏳ Starting session **{name.strip()}**…")
            await self.spawn_session(name.strip(), task.strip())
            return True

        route = router.resolve(msg, sessions)
        if route is None:
            await reply("This topic's session has ended. Send /sessions to see what is running.")
            return True
        msg.text = route.text
        route_ctx["sid"] = route.sid
        if route.sid == ROOT:
            return False
        pane = self._pane_for(route.sid)
        if pane is None:
            return False
        await self._remote_child_turn(pane, msg)
        return True

    def _remote_sessions_text(self, msg: Any) -> str:
        from novacode_cli.remote.routing import ROOT

        router = getattr(self, "_remote_router", None)
        chat = getattr(msg, "chat_id", None)
        current = router.default.get(chat, ROOT) if router else ROOT
        lines = ["**Sessions**"]
        for pane in getattr(self, "_panes", []):
            sid = ROOT if pane.kind == "root" else pane.sid
            name = "main" if pane.kind == "root" else pane.title
            where = " · has its own topic" if router and router.topic_of(chat, sid) else ""
            mark = " ← default here" if sid == current else ""
            lines.append(f"- **{name}** · {pane.status}{where}{mark}")
        lines.append(
            "\nTalk to one: post in its topic, reply to its message, "
            "`@name <text>`, or `/use <name>`. Start one: `/new <name>: <task>`."
        )
        return "\n".join(lines)

    async def _remote_child_turn(self, pane, msg: Any) -> None:
        """Run a remote prompt in a spawned session; answer at its turn_done."""
        from types import SimpleNamespace

        from novacode_cli.remote.status import RemoteStatusLine

        if pane.child is None or pane.status in ("crashed", "exited"):
            with contextlib.suppress(Exception):
                await msg.reply_fn(f"✖ Session “{pane.title}” is not running.")
            return
        with contextlib.suppress(Exception):
            await self._add_message(
                Text(f"📡 {msg.user_name} → {pane.title}", style="bold cyan"), "user", Text(msg.text)
            )
        proxy = pane.state.get("session_state")
        turn = SimpleNamespace(
            msg=msg,
            status=None,
            answer="",
            prev_auto=getattr(proxy, "auto_approve", False),
        )
        if getattr(msg, "edit_fn", None) is not None:
            turn.status = RemoteStatusLine(msg.edit_fn, label=self._remote_label(pane))
        pane.remote_turns.append(turn)
        if proxy is not None:
            proxy.auto_approve = True  # remote turns have no one to ask
        if await self._supervisor().send_prompt(pane.sid, msg.text) is None:
            pane.remote_turns.remove(turn)
            with contextlib.suppress(Exception):
                await msg.reply_fn(f"✖ Session “{pane.title}” is no longer running.")
            return
        if turn.status is not None:
            turn.status.start()
        if len(pane.remote_turns) > 1:
            with contextlib.suppress(Exception):
                await msg.reply_fn(
                    f"⏳ Queued for **{pane.title}** behind {len(pane.remote_turns) - 1} more."
                )
        self._remote_react("🤔", msg)
        pane.status = "running"
        self._refresh_tabs()

    async def _finish_remote_turn(self, pane, error: str | None = None) -> None:
        """Settle the oldest remote turn of a spawned session and send its answer."""
        if not pane.remote_turns:
            return
        turn = pane.remote_turns.popleft()
        if turn.status is not None:
            with contextlib.suppress(Exception):
                await turn.status.finalize()
        reply = error or turn.answer or "✅ Task completed."
        if self._remote_label(pane) and not error:
            reply = f"**[{pane.title}]** {reply}"
        with contextlib.suppress(Exception):
            await turn.msg.reply_fn(reply)
        self._remote_react("✖" if error else "✅", turn.msg)
        if not pane.remote_turns:
            proxy = pane.state.get("session_state")
            if proxy is not None:
                proxy.auto_approve = turn.prev_auto

    def _remote_telegram_bridges(self) -> list:
        from novacode_cli.remote.bridge import RemotePlatform

        mgr = getattr(self.session_state, "_remote_bridge_manager", None)
        if mgr is None:
            root = getattr(self, "_root_pane", None)
            state = root.state.get("session_state") if root is not None else None
            mgr = getattr(state, "_remote_bridge_manager", None)
        try:
            return mgr.bridges(RemotePlatform.TELEGRAM) if mgr is not None else []
        except Exception:  # noqa: BLE001
            return []

    async def _open_remote_topics(self, pane) -> None:
        """Give a new session its own topic in every Telegram forum group."""
        from novacode_cli.remote.routing import RemoteRouter

        router = getattr(self, "_remote_router", None)
        if router is None:
            router = self._remote_router = RemoteRouter()
        for bridge in self._remote_telegram_bridges():
            try:
                tid = await bridge.open_topic(f"🔀 {pane.title}")
                if tid is None:
                    continue
                router.topics[(bridge._config.chat_id, pane.sid)] = tid
                await bridge.post(
                    f"◆ Session **{pane.title}** started. Messages in this topic go to it.",
                    thread_id=tid,
                    sid=pane.sid,
                )
            except Exception:  # noqa: BLE001 — a missing topic must not block the session
                continue

    async def _close_remote_topics(self, pane) -> None:
        router = getattr(self, "_remote_router", None)
        if router is None:
            return
        gone = router.forget(pane.sid)
        for bridge in self._remote_telegram_bridges():
            for chat, tid in gone:
                if chat != bridge._config.chat_id:
                    continue
                with contextlib.suppress(Exception):
                    await bridge.post(f"○ Session **{pane.title}** closed.", thread_id=tid)
                    await bridge.close_topic(tid)

    async def _remote_steer_drain(self, queue: Any) -> None:
        """While a remote turn runs, treat further remote messages as live steers.

        Lets a remote user "add extra stuff to the previous prompt": a message
        (or ``/steer …``) arriving mid-turn is injected as a live steer the
        running agent picks up at its next step, instead of queuing a whole new
        turn behind the current one. Other slash commands get a "busy" note.
        Cancelled when the turn ends.
        """
        while True:
            try:
                m = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                # Another session's message, or a session command: not a steer.
                if await self._remote_route(m):
                    continue
                if (
                    self._remote_question_future is not None
                    and not self._remote_question_future.done()
                ):
                    react_fn = getattr(m, "react_fn", None)
                    if react_fn is not None:
                        try:
                            await react_fn("📥")
                        except Exception:  # noqa: BLE001
                            pass
                    self._remote_question_future.set_result(m)
                    continue

                text = (getattr(m, "text", "") or "").strip()
                low = text.lower()
                if low.startswith("/steer"):
                    text = text[len("/steer") :].strip()
                elif text.startswith("/"):
                    reply_fn = getattr(m, "reply_fn", None)
                    if reply_fn is not None:
                        try:
                            await reply_fn(
                                "⏳ Busy with the current task — send "
                                "`/steer <text>` (or just text) to add to it."
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                if not text:
                    continue
                self._add_live_steer(text)
                react_fn = getattr(m, "react_fn", None)
                reply_fn = getattr(m, "reply_fn", None)
                if react_fn is not None:
                    try:
                        await react_fn("↗")
                    except Exception:  # noqa: BLE001
                        pass
                elif reply_fn is not None:
                    try:
                        await reply_fn(f"↗ Added to the running task: {text}")
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                from contextlib import suppress

                with suppress(ValueError):
                    queue.task_done()
                    pass

    def _remote_react(self, emoji: str, msg: Any = None) -> None:
        """Add a reaction emoji to the remote user's message (best-effort).

        ``msg`` defaults to the active remote message, but callers can pass it
        explicitly (e.g. the error handler, which runs after ``_remote_msg`` has
        already been cleared).
        """
        msg = msg if msg is not None else self._remote_msg
        if msg is None or getattr(msg, "react_fn", None) is None:
            return
        try:
            import asyncio

            task = asyncio.create_task(msg.react_fn(emoji))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:  # noqa: BLE001
            pass

    def _log(self, renderable: Any) -> None:
        """Mount an ancillary line (errors, command output, notices)."""
        self._close_tool_group()
        self._transcript().mount(Static(renderable, classes="logline"))
        self._prune_transcript()
        # Automatic scroll: don't yank a user who is reading history.
        self._scroll_end(force=False)

    # _init step-tracker widget removed — /init progress is now shown via _log only.

    async def _add_message(self, label: Text, role_class: str, body: Any) -> ChatMessage:
        msg = ChatMessage(label, role_class)
        await self._mount(msg)
        msg.update_body(body)
        animate_entrance(msg, "slide")
        return msg

    @staticmethod
    def _message_text(msg: Any) -> str:
        """Extract displayable text from a LangChain message's content."""
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content)

    def _format_breadcrumbs(self, path: Path) -> str:
        """Convert an absolute path into a condensed breadcrumb format.

        Example: B:\\Summer Project 2026\\Nova-Code\\nova-code-cli
                 -> .../Nova-Code/nova-code-cli
        """
        parts = path.parts
        if len(parts) <= 3:
            return str(path)

        # Keep the last 2 segments and prefix with .../
        breadcrumb = "/".join(parts[-2:])
        return f".../{breadcrumb}"

    @work
    async def _replay_history(self) -> None:
        """Replay restored conversation turns into the transcript on resume.

        Renders the prior Human/AI turns *and* the tool calls they made (with
        their results) so a resumed session shows the same transcript it had
        before. The agent's own state is restored separately via the
        checkpointer / continuation prompt.
        """
        msgs = self._restored_messages
        if not msgs:
            return
        from novacode_cli.compaction import is_compaction_summary
        from novacode_cli.core.streaming import is_internal_context_text

        # Tool results arrive as separate ToolMessages; index them by call id so
        # an AIMessage's tool_calls can be paired with their output.
        results: dict[str, Any] = {}
        for m in msgs:
            if getattr(m, "type", "") == "tool":
                cid = getattr(m, "tool_call_id", None)
                if cid:
                    results[cid] = m

        # Only render real conversation turns, oldest first.
        shown = 0
        for m in msgs:
            role = getattr(m, "type", "") or ""
            text = self._message_text(m).strip()
            # /compact rewrites history into a single synthetic HumanMessage
            # holding the summary (compaction.py). Replaying it verbatim shows
            # the whole summarized context as though the USER had typed it.
            if text and (is_compaction_summary(text) or is_internal_context_text(text)):
                continue
            if role == "human":
                if not text:
                    continue
                await self._add_message(Text("You", style="bold cyan"), "user", Markdown(text))
                shown += 1
            elif role == "ai":
                # Tool calls ride on the AIMessage; replay them into the
                # condensed tool group before the (possibly empty) prose.
                for tc in getattr(m, "tool_calls", None) or []:
                    await self._replay_tool_call(tc, results)
                if text:
                    await self._add_message(Text("Nova", style="green"), "nova", Markdown(text))
                    shown += 1
        if shown:
            self._log(
                Text(
                    f"⟲ Resumed — {shown} earlier message(s) restored above",
                    style="dim",
                )
            )

    async def _replay_tool_call(self, tc: dict, results: dict[str, Any]) -> None:
        """Render one restored tool call (and its result) into the tool group.

        Mirrors the live path: a condensed line per call under its category
        heading, marked done/failed from the paired ToolMessage.
        """
        name = tc.get("name", "") or "tool"
        args = tc.get("args", {}) or {}
        call_id = tc.get("id") or None
        try:
            from novacode_cli.ui.ui_elements import format_tool_display

            base = format_tool_display(name, args)
        except Exception:  # noqa: BLE001 — a display helper must never break replay
            base = name
        await self._ensure_tool_group()
        self._add_tool_group_call(call_id, base, name)
        result = results.get(call_id) if call_id else None
        if result is None:
            # No paired result (e.g. the turn was interrupted) — leave it done
            # rather than showing a perpetual spinner.
            self._mark_tool_group_result(call_id, is_error=False, detail="")
            return
        status = getattr(result, "status", "success")
        is_error = status != "success"
        detail = self._oneline(self._message_text(result))
        self._mark_tool_group_result(call_id, is_error=is_error, detail=detail)

    def _reset_streaming(self) -> None:
        """Drop any in-progress streaming/reasoning widgets at a turn boundary."""
        for ref in (self._stream_msg, self._reason_msg):
            if ref is not None:
                try:
                    ref.remove()  # fire-and-forget
                except Exception:  # noqa: BLE001
                    pass
        self._stream_msg = None
        self._reason_msg = None
        self._live_buf = ""
        self._reasoning_buf = ""
        self._stream_flush_scheduled = False
        self._tool_components.clear()
        self._last_tool = None
        self._close_tool_group()
        self._subagent_widgets.clear()
        self._subagent_count = 0
        self._subagent_tool_to_task.clear()
        # NB: the docked checklist is deliberately NOT cleared here — this
        # runs at the start of every turn, and the point of docking is that
        # the list survives. /clear and /resume clear it explicitly.
        self._current_assistant_id = None
        self._accumulated_reply = ""

    def _flush_stream(self) -> None:
        """Repaint the in-progress stream/reasoning widgets from their buffers.

        Called on a coalescing ~100ms timer (and forced at finalize) so a fast
        token stream triggers ~10 repaints/sec instead of one per token. Updates
        both live widgets in one pass and scrolls once.
        """
        self._stream_flush_scheduled = False
        painted = False
        if self._stream_msg is not None:
            # Tail only: repainting the whole buffer every 100ms is quadratic in
            # the answer length, so a model that never stops talking pegs the UI
            # and takes the app (and the terminal state) down with it. The full
            # text is committed as markdown on AssistantMessage; the viewport is
            # pinned to the end anyway.
            self._stream_msg.update_body(Text(self._live_buf[-_LIVE_PREVIEW_CHARS:]))
            painted = True
        if self._reason_msg is not None:
            self._reason_msg.update_body(Text(self._reasoning_buf[-2000:], style="dim italic"))
            painted = True
        if painted:
            # Automatic scroll: a user who scrolled up to read must not be
            # dragged back to the bottom by the next ~100ms repaint.
            self._scroll_end(force=False)

    def _schedule_stream_flush(self) -> None:
        """Ensure a flush happens soon, coalescing bursts of deltas into one."""
        if self._stream_flush_scheduled:
            return
        self._stream_flush_scheduled = True
        self.set_timer(0.1, self._flush_stream)

    def _theme_color(self, name: str, fallback: str) -> str:
        """Resolve a theme variable to a hex string for a Rich ``Text`` style.

        Rich styles are built as plain strings, so a Textual ``$variable``
        cannot be used directly; this reads the active theme instead. Falls
        back to *fallback* if the theme (or the variable) is unavailable.
        """
        raw = None
        try:
            raw = getattr(self.app.current_theme, name, None)
        except Exception:  # noqa: BLE001 — never break rendering on a theme read
            raw = None
        if not raw:
            try:
                raw = self.app.theme_variables.get(name)
            except Exception:  # noqa: BLE001
                raw = None
        raw = (raw or fallback).strip()
        return raw.removeprefix("ansi_")

    def _render_todos(
        self, todos: list, agent_name: str | None, *, collapsed: bool = False
    ) -> Text:
        """Native todo list: status glyphs + content (no legacy panel).

        The header carries the done/total count and a collapse affordance,
        because when collapsed it is the only row on screen.
        """
        glyphs = {
            "completed": ("☑", "green"),
            "in_progress": ("▶", "yellow"),
            "pending": ("☐", "dim"),
        }
        items = todos or []
        done = sum(
            1
            for td in items
            if isinstance(td, dict) and td.get("status") == "completed"
        )
        t = Text()
        name = f"{agent_name} · Todos" if agent_name else "Todos"
        caret = "▸" if collapsed else "▾"
        # The "Todos" word carries the theme's accent color (the dock itself has
        # no accent bar), so the header still reads as themed and recolors with
        # /theme.
        t.append(f"{caret} ", style="bold")
        t.append(name, style=f"bold {self._theme_color('accent', '#bb9af7')}")
        t.append(" ", style="bold")
        t.append(
            f"{done}/{len(items)}",
            style="green" if items and done == len(items) else "dim",
        )
        t.append("  click to collapse" if not collapsed else "  click to expand", style="dim")
        t.append("\n")
        if collapsed:
            return t
        for td in todos or []:
            if isinstance(td, dict):
                content = td.get("content", "")
                status = td.get("status", "pending")
            else:
                content, status = str(td), "pending"
            glyph, color = glyphs.get(status, ("☐", "dim"))
            t.append(f"  {glyph} ", style=color)
            t.append(
                f"{content}\n",
                style="strike dim" if status == "completed" else "",
            )
        return t

    def _paint_todos(self, todos: list | None, agent_name: str | None = None) -> None:
        """Repaint the docked checklist, or hide it when there is nothing to show.

        The single place the dock is written. Called on TodoUpdate, on session
        switch (the widget is app-global but ``_todos`` is per-pane, so a switch
        must repaint or pane A's list would sit under pane B), and on resume.

        A fully-completed list is dismissed automatically: the checklist has
        served its purpose and should not keep costing rows above the prompt.
        """
        try:
            dock = self._w("#todo-dock", Static)
        except NoMatches:
            return
        items = todos or []
        # Everything checked off -> dismiss. Nothing to track any more, and a
        # stale all-green list is pure noise above the input.
        if not items or all(
            isinstance(td, dict) and td.get("status") == "completed" for td in items
        ):
            dock.remove_class("active")
            dock.update("")
            return
        collapsed = getattr(self, "_todos_collapsed", False)
        # rstrip: every row ends in a newline, which would leave a blank line
        # inside a dock sized to its content.
        text = self._render_todos(items, agent_name, collapsed=collapsed)
        text.rstrip()
        dock.update(text)
        dock.set_class(collapsed, "collapsed")
        dock.add_class("active")

    def action_toggle_todos(self) -> None:
        """Collapse/expand the todo checklist (click the dock, or alt+t)."""
        self._todos_collapsed = not getattr(self, "_todos_collapsed", False)
        self._paint_todos(
            getattr(self, "_todos", None), getattr(self, "_todos_agent", None)
        )

    def _pop_tool(self, call_id: str | None) -> "tuple[Collapsible, Static, str] | None":
        """Find (and stop tracking) the tool component for a result."""
        entry = None
        if call_id and call_id in self._tool_components:
            entry = self._tool_components.pop(call_id)
        elif self._last_tool is not None:
            entry = self._last_tool
        if entry is not None and entry is self._last_tool:
            self._last_tool = None
        return entry

    @staticmethod
    def _render_diff_text(diff: str, max_lines: int = 500, path: str | None = None) -> Text:
        """Render a unified diff natively with +/- coloring (no legacy capture).

        When ``path`` resolves to a known language, syntax token colours are
        overlaid on the diff-marker colour (foreground only), so added/removed
        lines stay visually distinct.
        """
        from novacode_cli.ui.diff_highlight import highlight_line, lexer_for_path

        lexer = lexer_for_path(path)
        t = Text()
        lines = diff.splitlines()
        for line in lines[:max_lines]:
            if line.startswith(("+++", "---")):
                t.append(line + "\n", style="dim")
            elif line.startswith("@@"):
                t.append(line + "\n", style="cyan")
            elif line.startswith(("+", "-")):
                # Use an explicit background so the add/remove signal is a
                # filled block (matching the Rich path's "white on dark_green"
                # / "white on dark_red"), not just coloured text. Syntax token
                # colours overlay the foreground only.
                bg = "dark_green" if line.startswith("+") else "dark_red"
                base = f"white on {bg}"
                marker, code = line[0], line[1:]
                t.append(marker, style=base)
                for text, style in highlight_line(code, lexer):
                    t.append(text, style=f"{style} on {bg}" if style else base)
                t.append("\n")
            else:
                for text, style in highlight_line(line, lexer):
                    t.append(text, style=style or "dim")
                t.append("\n")
        if len(lines) > max_lines:
            t.append(f"… {len(lines) - max_lines} more lines\n", style="dim italic")
        return t

    def _fileop_body(self, rec, full_output: str) -> Text:
        """Native body for a file-op component: diff for writes/edits, content for reads."""
        diff = getattr(rec, "diff", None) if rec is not None else None
        path = getattr(rec, "display_path", None) if rec is not None else None
        if diff:
            return self._render_diff_text(diff, path=path)
        after = getattr(rec, "after_content", None) if rec is not None else None
        if after:  # write with no diff — show the new content as additions
            return self._render_diff_text(
                "\n".join("+" + ln for ln in after.splitlines()), path=path
            )
        out = (
            full_output
            or (getattr(rec, "read_output", None) if rec is not None else None)
            or "(no output)"
        )
        if len(out) > 6000:
            out = out[:6000] + "\n… (truncated)"
        return Text(out)

    def _fileop_summary(self, rec) -> str:
        """A concise '+A / -D' (or 'Read N lines') summary for a file-op title."""
        if rec is None:
            return ""
        tn = getattr(rec, "tool_name", "")
        m = getattr(rec, "metrics", None)
        added = getattr(m, "lines_added", 0) or 0
        removed = getattr(m, "lines_removed", 0) or 0
        if tn == "read_file":
            # Reads populate `lines_read` (file_ops.py), never `lines_written`;
            # reading the wrong field made every read summarize as bare "Read".
            n = getattr(m, "lines_read", 0) or 0
            return f"Read {n} lines" if n else "Read"
        return f"+{added} / -{removed}"

    # -- condensed tool group -------------------------------------------------

    @staticmethod
    def _oneline(s: str, limit: int = 110) -> str:
        """Collapse whitespace/newlines and truncate to a single short line."""
        s = " ".join((s or "").split())
        return s if len(s) <= limit else s[: limit - 1] + "…"

    async def _ensure_tool_group(self) -> None:
        """Create + mount the condensed tool-group panel if one isn't open."""
        if self._tool_group is not None:
            return
        self._tool_group_entries = []
        self._tool_group_lines = {}
        self._tool_group_last_idx = None
        self._tool_group_log_lines = 0
        body = Vertical(classes="toolbody")
        comp = Collapsible(body, title="⚙ tool calls", collapsed=True)
        comp.add_class("tool")
        animate_entrance(comp, "zoom")
        self._tool_group = comp
        self._tool_group_body = body
        # Mount directly (not via _mount) so we don't immediately close the group
        # we're creating.
        await self._transcript().mount(comp)
        await body.mount(Static("", id="tool-group-list"))
        await body.mount(
            RichLog(id="tool-group-log", classes="terminal-log", highlight=True, markup=True)
        )
        self._prune_transcript()
        self._scroll_end()

    def _close_tool_group(self) -> None:
        """Detach the current tool group so the next burst starts fresh."""
        # Paint any coalesced state BEFORE detaching: a pending timer would
        # otherwise fire after _tool_group is None and drop the final result,
        # leaving the last call showing as still-running.
        if self._tool_group_refresh_scheduled:
            self._tool_group_refresh_scheduled = False
            self._refresh_tool_group(running=self._tool_group_running)
        self._tool_group_running = None
        self._tool_group = None
        self._tool_group_body = None
        self._tool_group_entries = []
        self._tool_group_lines = {}
        self._tool_group_last_idx = None
        self._tool_group_log_lines = 0

    @staticmethod
    def _render_tool_line(entry: dict) -> Text:
        """One compact line for a single tool call in the group body."""
        mark = entry["mark"]
        err = entry["error"]
        mark_style = "red" if err else ("green" if mark == "✓" else "yellow")
        body_style = "red" if err else "dim"
        t = Text()
        t.append(f"{mark} ", style=mark_style)
        t.append(entry["base"], style=body_style)
        if entry["detail"]:
            t.append(f"  — {entry['detail']}", style=body_style)
        return t

    def _schedule_tool_group_refresh(self, *, running: str | None = None) -> None:
        """Coalesce a burst of tool events into one repaint (~100ms), like
        :meth:`_schedule_stream_flush` does for token deltas.

        A tool-heavy turn fires two events per call (start + result), each of
        which repainted the group immediately. Ten quick calls meant twenty
        repaints of the same widget within a few hundred ms, and a repaint at
        120 entries costs ~4 ms. Deferring collapses that to one paint per
        window while the entries themselves stay updated synchronously, so no
        state is lost — only redundant paints.
        """
        # Keep the most recent running label: the last event in the window is
        # the one whose state the paint should show.
        self._tool_group_running = running
        if self._tool_group_refresh_scheduled:
            return
        self._tool_group_refresh_scheduled = True
        self.set_timer(0.1, self._flush_tool_group_refresh)

    def _flush_tool_group_refresh(self) -> None:
        """Paint the coalesced tool-group state (see _schedule_tool_group_refresh)."""
        self._tool_group_refresh_scheduled = False
        self._refresh_tool_group(running=self._tool_group_running)

    def _refresh_tool_group(self, *, running: str | None = None) -> None:
        """Repaint the group body + title from the current entries.

        Entries are grouped under a category heading (``Explored``, ``Edited``,
        ``Ran``, …) with a count, so a long run of tools reads as a few labelled
        sections rather than one flat list. Categories keep the order they first
        appear in, so the panel mirrors the order the agent actually worked.

        Each line is rendered once and cached on its entry: this runs on EVERY
        tool call and every tool result, and re-rendering all ~100 lines each
        time made tool events O(n) — measured at 4.4 ms per call at 20 calls
        rising to 10.1 ms at 120, the dominant per-event cost on a tool-heavy
        turn. Only the entry that actually changed is re-rendered; the cache is
        dropped by the two mutators (_add_tool_group_call /
        _mark_tool_group_result), so it cannot go stale.
        """
        if self._tool_group is None or self._tool_group_body is None:
            return
        entries = self._tool_group_entries[-100:]

        # Bucket by category, preserving first-seen order.
        sections: dict[str, list[dict]] = {}
        for entry in entries:
            sections.setdefault(entry.get("category") or _OTHER_CATEGORY, []).append(entry)

        body = Text()
        first_section = True
        for cat, items in sections.items():
            if not first_section:
                body.append("\n")
            first_section = False
            # Heading: "→ Explored — 3 reads"
            body.append(f"{_category_glyph(cat)} ", style="bold #7aa2f7")
            body.append(cat, style="bold #7aa2f7")
            body.append(f" — {len(items)} {self._category_noun(cat, len(items))}\n", style="dim")
            for entry in items:
                line = entry.get("_line")
                if line is None:
                    line = self._render_tool_line(entry)
                    entry["_line"] = line
                body.append_text(line)
                body.append("\n")
        # Drop the trailing newline from the last line.
        if body.plain.endswith("\n"):
            body = body[:-1]

        try:
            self._tool_group_body.query_one("#tool-group-list", Static).update(body)
        except Exception:
            pass
        n = len(self._tool_group_entries)
        title = f"⚙ {n} tool call" + ("" if n == 1 else "s")
        if running:
            title += f"  · running {running}…"
        self._tool_group.title = title

    @staticmethod
    def _category_noun(cat: str, count: int) -> str:
        """A pluralised noun for a category heading (``3 reads``, ``1 edit``)."""
        nouns = {
            "Explored": ("read", "reads"),
            "Edited": ("edit", "edits"),
            "Ran": ("command", "commands"),
            "Delegated": ("task", "tasks"),
            "Planned": ("step", "steps"),
            "Remembered": ("memory", "memories"),
            "Produced": ("artifact", "artifacts"),
            "Tasks": ("check", "checks"),
            _OTHER_CATEGORY: ("call", "calls"),
        }
        singular, plural = nouns.get(cat, ("call", "calls"))
        return singular if count == 1 else plural

    def _add_tool_group_call(self, call_id: str | None, base: str, name: str) -> None:
        """Append a 'running' line for a new tool call."""
        entry = {
            "base": self._oneline(base),
            "mark": "⏳",
            "detail": "",
            "error": False,
            "category": _tool_category(name),
        }
        idx = len(self._tool_group_entries)
        self._tool_group_entries.append(entry)
        if call_id:
            self._tool_group_lines[call_id] = idx
        self._tool_group_last_idx = idx

        # If running a shell command execution, activate and clear the live log widget
        if name in {
            "shell",
            "bash",
            "execute",
            "execute_bash",
            "run_command",
            "run_tests",
            "start_dev_server",
        }:
            self._tool_group.collapsed = False
            try:
                log_widget = self._tool_group_body.query_one("#tool-group-log", RichLog)
                log_widget.clear()
                log_widget.add_class("active")
                log_widget.write(f"$ {base}\n")
                self._tool_group_log_lines = 1 + base.count("\n")
                log_widget.styles.height = min(max(self._tool_group_log_lines + 2, 5), 8)
            except Exception:
                pass

        self._schedule_tool_group_refresh(running=name)

    def _mark_tool_group_result(self, call_id: str | None, *, is_error: bool, detail: str) -> None:
        """Finalize the matching tool line with its status + a short result."""
        idx: int | None = None
        if call_id is not None and call_id in self._tool_group_lines:
            idx = self._tool_group_lines[call_id]
        elif self._tool_group_last_idx is not None:
            idx = self._tool_group_last_idx
        if idx is None or idx >= len(self._tool_group_entries):
            # No open group line (group already closed) — compact fallback line.
            if detail:
                self._log(
                    Text(
                        f"  ⎿  {self._oneline(detail)}",
                        style="red" if is_error else "dim",
                    )
                )
            return
        entry = self._tool_group_entries[idx]
        entry["mark"] = "✗" if is_error else "✓"
        entry["error"] = is_error
        entry["detail"] = self._oneline(detail)
        entry["_line"] = None  # fields changed → drop the cached render
        # Surface failures: pop the group open so the error isn't hidden — and
        # paint NOW rather than on the coalescing timer, or the group would
        # expand to show content that is still up to 100 ms stale.
        if is_error and self._tool_group is not None:
            self._tool_group.collapsed = False
            self._tool_group_refresh_scheduled = False
            self._refresh_tool_group()
        else:
            self._schedule_tool_group_refresh()

    def _finalize_tool(
        self, call_id: str | None, preview: str, full_output: str, *, is_error: bool
    ) -> None:
        entry = self._pop_tool(call_id)
        if entry is None:
            self._log(Text(f"  ⎿  {preview}", style="red" if is_error else "dim"))
            return
        comp, body, base = entry
        mark = "✗" if is_error else "✓"
        comp.title = f"{base}  {mark} {_esc(preview)}"
        out = full_output or "(no output)"
        if len(out) > 6000:
            out = out[:6000] + "\n… (truncated)"
        if isinstance(body, RichLog):
            body.clear()
            body.write(Text(out, style="red" if is_error else ""))
            body.scroll_end(animate=False)
        else:
            body.update(Text(out, style="red" if is_error else ""))
        # Animate border to settled state

        final_color = "#f7768e" if is_error else "#73daca"  # error / success
        try:
            comp.styles.animate("border_left", f"thick {final_color}", duration=0.35)
        except Exception:  # noqa: BLE001
            pass

    async def _handle_subagent(self, e: ev.SubagentActivity) -> None:
        """Render subagent dispatch and completion with collapsible widgets."""
        import time

        cid = e.call_id or ""
        color = e.color or "#bb9af7"

        if e.kind == "dispatched" and cid:
            self._subagent_count += 1
            label = f"⟐ {e.subagent_type or 'subagent'}"
            title = Text.assemble(
                (label, f"bold {color}"),
                (f"  · #{self._subagent_count} dispatched", "dim"),
            )
            # Create a Vertical container as the body
            body = Vertical(classes="toolbody")
            # Start expanded (collapsed=False) so dispatching subagents show live progress
            comp = Collapsible(body, title=title, collapsed=False)  # type: ignore
            comp.add_class("subagent")
            await self._mount(comp)
            animate_entrance(comp, "fade")

            # Mount a Static for status text, a Static for subagent-list, and a RichLog for the progress log
            status_text = Text(e.detail or "", style="dim") if e.detail else Text("")
            await body.mount(Static(status_text, id="subagent-status"))
            await body.mount(Static("", id="subagent-list"))
            await body.mount(
                RichLog(id="subagent-log", classes="terminal-log", highlight=True, markup=True)
            )

            # Initialize dynamic height tracking and entry lists
            comp._log_lines = 0
            comp._log_entries = []
            comp._tool_lines = {}
            try:
                log_widget = body.query_one("#subagent-log", RichLog)
                log_widget.styles.height = 5
            except Exception:
                pass

            self._subagent_widgets[cid] = (
                comp,
                body,
                e.subagent_type or "subagent",
                time.time(),
            )

        elif e.kind == "completed":
            # Try matching by call_id first, then fallback to subagent_type
            entry = None
            matched_cid = cid
            if cid and cid in self._subagent_widgets:
                entry = self._subagent_widgets.pop(cid)
            else:
                # Fallback: find first matching by subagent type
                for key, val in list(self._subagent_widgets.items()):
                    if val[2] == (e.subagent_type or ""):
                        entry = self._subagent_widgets.pop(key)
                        matched_cid = key
                        break

            if entry is not None:
                comp, body, stype, start_time = entry
                elapsed = time.time() - start_time
                dur = (
                    f"{elapsed:.1f}s"
                    if elapsed < 60
                    else f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                )
                icon = e.message or f"{e.subagent_type}"
                count = len(self._subagent_widgets)
                remaining = f" · {count} active" if count > 0 else ""
                comp.title = f"{_esc(str(icon))}  ({dur}){remaining}"
                if e.detail:
                    try:
                        body.query_one("#subagent-status", Static).update(
                            Text(e.detail, style="dim")
                        )
                    except Exception:
                        pass
                else:
                    try:
                        body.query_one("#subagent-status", Static).update(Text(""))
                    except Exception:
                        pass
                comp.collapsed = True
                # Clean up tool calls mapping for this subagent
                self._subagent_tool_to_task = {
                    k: v for k, v in self._subagent_tool_to_task.items() if v != matched_cid
                }
                # Subagent completion is already reflected in the digest's task
                # count, so no separate remote message is sent here.
            else:
                # No matching widget — log as a simple line
                dur_part = ""
                if e.detail:
                    dur_part = f" — {e.detail}"
                self._log(Text(f"{e.message}{dur_part}", style=color))

        elif e.kind in ("status", "tool_start", "tool_result") and e.message:
            if e.kind == "tool_start" and e.detail and cid:
                self._subagent_tool_to_task[e.detail] = cid
            elif e.kind == "tool_result" and e.detail:
                self._subagent_tool_to_task.pop(e.detail, None)

            if cid and cid in self._subagent_widgets:
                comp, body, stype, start_time = self._subagent_widgets[cid]
                log_entries = getattr(comp, "_log_entries", [])
                tool_lines = getattr(comp, "_tool_lines", {})

                if e.kind == "tool_start":
                    if e.detail and e.detail in tool_lines:
                        idx = tool_lines[e.detail]
                        entry = log_entries[idx]
                        entry["display"] = e.message
                    else:
                        entry = {
                            "type": "tool",
                            "display": e.message,
                            "mark": "⏳",
                            "detail": "",
                            "error": False,
                        }
                        idx = len(log_entries)
                        log_entries.append(entry)
                        if e.detail:
                            tool_lines[e.detail] = idx
                    comp._log_entries = log_entries
                    comp._tool_lines = tool_lines
                    self._refresh_subagent_list(cid)
                elif e.kind == "tool_result":
                    if e.detail and e.detail in tool_lines:
                        idx = tool_lines[e.detail]
                        entry = log_entries[idx]
                        is_error = e.color == "#f7768e"
                        entry["mark"] = "✗" if is_error else "✓"
                        entry["detail"] = e.message
                        entry["error"] = is_error
                    comp._log_entries = log_entries
                    comp._tool_lines = tool_lines
                    self._refresh_subagent_list(cid)
                else:  # status
                    log_entries.append(
                        {
                            "type": "status",
                            "display": e.message,
                        }
                    )
                    comp._log_entries = log_entries
                    self._refresh_subagent_list(cid)
            else:
                self._log(Text(f"  ⟐ {e.message}", style=color))

    def _refresh_subagent_list(self, cid: str) -> None:
        """Redraw the subagent Static list based on its current entries."""
        if cid not in self._subagent_widgets:
            return
        comp, body, stype, start_time = self._subagent_widgets[cid]
        try:
            list_widget = body.query_one("#subagent-list", Static)
            log_entries = getattr(comp, "_log_entries", [])
            lines = []
            for entry in log_entries:
                if entry.get("type") == "status":
                    lines.append(f"⟐ {entry['display']}")
                else:
                    mark = entry["mark"]
                    display = entry["display"]
                    detail = entry["detail"]
                    error = entry["error"]
                    color = "#f7768e" if error else ("#73daca" if mark == "✓" else "#bb9af7")

                    line = f"[{color}]{mark}[/{color}] {display}"
                    if detail:
                        clean_detail = detail
                        if clean_detail in ("✓", "✗"):
                            clean_detail = ""
                        if clean_detail.startswith("✓ ") or clean_detail.startswith("✗ "):
                            clean_detail = clean_detail[2:]
                        if clean_detail:
                            line += f" [dim]· {clean_detail}[/dim]"
                    lines.append(f"⟐ {line}")
            list_widget.update("\n".join(lines))
        except Exception:
            pass

    async def _remove_reasoning(self) -> None:
        """Finalize the reasoning trace: collapse it and keep it in the transcript.

        The trace used to be deleted at the end of the turn, so the model's
        thinking vanished the moment the answer arrived. It is now retained as a
        collapsed one-line card the user can expand, which keeps the transcript
        honest about what the model did without letting a long trace dominate it.
        """
        if self._reason_msg is not None:
            try:
                # Commit the full trace (the live view only showed a tail) and
                # fold it to its header.
                self._reason_msg.update_body(
                    Text(self._reasoning_buf, style="dim italic")
                )
                self._reason_msg.set_collapsed(collapsed=True)
            except Exception:  # noqa: BLE001 — finalizing must never break the turn
                pass
            self._reason_msg = None
        self._reasoning_buf = ""

    _SPINNER = "▖▘▙▚▛▜▝▞▟"

    def _set_status(self, activity: str) -> None:
        self._activity = activity
        self._refresh_status()

    def _set_nova_indicator(
        self, text: str, *, style: str = "dim", auto_clear: float | None = None
    ) -> None:
        """Show the Nova learning status (review cycle) inline in the status line.

        The status renders beside the context % (see :meth:`_refresh_status`) so
        it never overlaps the input box. Empty ``text`` clears it.

        Args:
            text: Message to display. Empty string clears the status.
            style: Rich style for the text.
            auto_clear: If set, clear the status after this many seconds.
        """
        # Cancel any pending auto-clear so a new message isn't wiped early.
        if self._nova_indicator_timer is not None:
            try:
                self._nova_indicator_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._nova_indicator_timer = None

        self._nova_status = text or None
        self._nova_status_style = style
        self._refresh_status()

        if text and auto_clear is not None:
            self._nova_indicator_timer = self.set_timer(
                auto_clear, lambda: self._set_nova_indicator("")
            )

    @staticmethod
    def _ctx_gauge(percent: float, width: int = 10) -> str:
        """A unicode fill gauge like ▕█▉░░░░░░░▏ for *percent* across *width* cells."""
        percent = max(0.0, min(100.0, percent))
        filled = percent / 100.0 * width
        full = int(filled)
        eighths = " ▏▎▍▌▋▊▉█"
        cells = ["█"] * full
        rem = round((filled - full) * 8)
        if full < width and rem > 0:
            cells.append(eighths[min(8, rem)])
        cells += ["░"] * (width - len(cells))
        return "▕" + "".join(cells[:width]) + "▏"

    def _refresh_status(self) -> None:
        line = Text()

        # Activity segment — animated spinner + elapsed while live, else a ● dot.
        # Rebuilt every frame (cheap); it is the only part that changes at 20fps.
        if self._turn_active:
            frame = self._SPINNER[self._spinner_frame % len(self._SPINNER)]
            elapsed = time.monotonic() - self._turn_start
            line.append(f"{frame} ", style="bold #bb9af7")
            line.append(str(self._activity), style="#c0caf5")
            line.append(f"  {elapsed:0.1f}s", style="dim")
        else:
            line.append("● ", style="#9ece6a")
            line.append(str(self._activity), style="#9ece6a")

        # Heavy tail (ctx gauge, bridge, notifs, counts) changes slowly. Rebuild
        # it at most ~4x/sec so the per-frame spinner update stays cheap; _tick
        # drops the cache to None when a notif/bridge change must show at once.
        now = time.monotonic()
        ttl = 0.25  # rebuild the heavy tail at most ~4x/sec
        last = getattr(self, "_status_tail_ts", 0.0)
        if getattr(self, "_status_tail", None) is None or now - last > ttl:
            self._status_tail = self._build_status_tail()
            self._status_tail_ts = now
        line.append_text(self._status_tail)

        try:
            self._w("#prompt-hint-bar", Static).update(line)
        except NoMatches:
            pass

    def _build_status_tail(self) -> Text:
        """Slow-changing status segments, cached on a short TTL by _refresh_status.

        Split out so the 20fps spinner refresh doesn't rebuild the ctx gauge,
        bridge scan, notification counts, and skill/file counts every frame.
        """
        line = Text()

        def _divider() -> None:
            line.append("  │  ", style="#3b4261")

        # Context segment — a filling gauge that recolors green→amber→red.
        if self.token_tracker is not None:
            try:
                bd = self.token_tracker.get_breakdown()
            except Exception:  # noqa: BLE001
                bd = None
            if bd is not None:
                p = bd.usage_percentage
                if p >= 90:
                    ctx_color = "#f7768e"
                elif p >= 75:
                    ctx_color = "#e0af68"
                else:
                    ctx_color = "#9ece6a"
                _divider()
                line.append("ctx ", style="dim")
                line.append(self._ctx_gauge(p), style=ctx_color)
                line.append(f" {p:.0f}%", style=f"bold {ctx_color}")

        # Nova learning status (review cycle).
        if self._nova_status:
            _divider()
            line.append(self._nova_status, style=self._nova_status_style)

        # Remote bridge indicator — shows 📡 + platform abbreviations when active.
        try:
            mgr = getattr(self.session_state, "_remote_bridge_manager", None)
            if mgr is not None:
                _active = [
                    b for b in mgr.active_bridges if b.get("status") in ("running", "connecting...")
                ]
                if _active:
                    _divider()
                    line.append("📡 ", style="bold #7dcfff")
                    _labels = []
                    for _b in _active:
                        _plat = str(_b.get("platform", "")).lower()
                        _label = "tg" if _plat == "telegram" else _plat[:3]
                        _status = _b.get("status", "")
                        _style = "dim #7dcfff" if _status == "connecting..." else "#7dcfff"
                        _labels.append((_label, _style))
                    for _i, (_lbl, _sty) in enumerate(_labels):
                        if _i:
                            line.append(" · ", style="dim #7dcfff")
                        line.append(_lbl, style=_sty)
        except Exception:  # noqa: BLE001
            pass

        notif = self._unread_count()
        if notif:
            line.append("   🔔 ", style="bold #e0af68")
            line.append(str(notif), style="bold #e0af68")

        pending = self._pending_approval_count()
        if pending:
            line.append("   ⚡", style="bold yellow")
            line.append(str(pending), style="bold yellow")

        # Right-align skill/file counts. Skills shows the *enabled* set so it
        # reflects /skills toggles, not the full installed list. Dropped on
        # narrow terminals where there's no room for them.
        skill_count = 0 if self._narrow else self._cached_enabled_skill_count()
        file_count = 0 if self._narrow else self._cached_agent_md_count()

        # Build the right-side info string
        right_parts: list[str] = []
        if file_count:
            right_parts.append(f"{file_count} NOVA.md file{'s' if file_count != 1 else ''}")
        if skill_count:
            right_parts.append(f"{skill_count} skill{'s' if skill_count != 1 else ''}")

        if right_parts:
            # Pad to push the right info to the far right
            right_text = " · ".join(right_parts)
            # Use a large gap to simulate right alignment
            line.append("  ", style="dim")
            # We'll just append it; true right-align isn't possible in Text, but
            # the CSS already handles this if we update the hint bar carefully.
            line.append(right_text, style="dim")

        return line

    def _unread_count(self) -> int:
        """Unread notification count (0 on any error)."""
        try:
            return self.session_state.unread_notification_count()
        except Exception:  # noqa: BLE001
            return 0

    def _pending_approval_count(self) -> int:
        """Pending approval count (0 on any error)."""
        try:
            return self.session_state.pending_approval_count()
        except Exception:  # noqa: BLE001
            return 0

    def _refresh_hint_bar(self) -> None:
        """Populate the hint bar above the input (delegates to _refresh_status)."""
        self._refresh_status()

    def _refresh_info_bar(self) -> None:
        """Refresh the info-bar columns below the input.

        Workspace / sandbox / model / quota update synchronously; the git branch
        is read off-thread (``_refresh_branch_worker``) so a slow repo can't stall
        the UI. Safe to call repeatedly — used at mount and on a refresh timer, so
        a model switch, branch change, or sandbox change shows up live.
        """
        from novacode_cli.config.config import settings

        self._set_info("#info-workspace", Text(str(settings.get_workspace_root()), style="bold"))

        sandbox_type = getattr(self.session_state, "_sandbox_type", None)
        if sandbox_type:
            sandbox_text = Text(str(sandbox_type), style="bold yellow")
        else:
            sandbox_text = Text("no sandbox", style="#e0af68")
        self._set_info("#info-sandbox", sandbox_text)

        self._set_info("#info-model", Text(str(self.model_name or "—"), style="bold #7aa2f7"))
        self._refresh_quota()
        self._refresh_branch_worker()

    def _set_info(self, selector: str, renderable: Text) -> None:
        """Update an info-bar Static, ignoring it if not mounted yet."""
        try:
            self._w(selector, Static).update(renderable)
        except NoMatches:
            pass

    @work(thread=True, exclusive=True, group="infobar")
    def _refresh_branch_worker(self) -> None:
        """Read the current git branch off the event loop and update the info bar."""
        import subprocess
        from novacode_cli.config.config import settings

        branch = "—"
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                cwd=str(settings.get_workspace_root()),
            )
            if result.returncode == 0:
                branch = result.stdout.strip() or "—"
        except Exception:  # noqa: BLE001
            branch = "—"
        self.call_from_thread(self._set_info, "#info-branch", Text(branch, style="bold #bb9af7"))

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        """Compact token count: 950 → '950', 12_300 → '12k', 1_240_000 → '1.2M'."""
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}k"
        return str(n)

    def _refresh_quota(self) -> None:
        """Context + session-usage refresh (called from _tick during active turns).

        Shows two numbers, because they answer different questions and move
        independently:

        * **ctx** — how full the context window is right now. Bounded, and it
          DROPS when /compact summarizes the conversation.
        * **session** — cumulative input+output over every turn ÷ budget.
          Monotonic: it tracks total spend, so /compact deliberately leaves it
          alone (see ``TokenTracker.reset``).

        Showing only the cumulative number made /compact look like a no-op, so
        both are rendered side by side.
        """
        parts: list[tuple[str, str]] = []

        # Context-window fill (bounded; drops on compaction).
        try:
            bd = self.token_tracker.get_breakdown() if self.token_tracker else None
        except Exception:  # noqa: BLE001 — a display read must never break the bar
            bd = None
        if bd is not None and getattr(bd, "context_window_size", 0):
            ctx_pct = bd.usage_percentage
            parts.append((f"ctx {ctx_pct:.0f}%", f"bold {_pct_color(ctx_pct, '#9ece6a')}"))

        # Cumulative session usage (monotonic; survives compaction).
        tot = getattr(self.token_tracker, "session_total_tokens", 0)
        if tot:
            pct = getattr(self.token_tracker, "session_pct", 0.0)
            budget = getattr(self.token_tracker, "session_token_budget", 0)
            parts.append(
                (f"{pct:.0f}% of {self._fmt_tokens(budget)}", f"bold {_pct_color(pct, '#7aa2f7')}")
            )

        if not parts:
            usage_text = Text("—", style="dim")
        else:
            usage_text = Text()
            for i, (label, style) in enumerate(parts):
                if i:
                    usage_text.append(" · ", style="dim")
                usage_text.append(label, style=style)
        try:
            self._w("#info-quota", Static).update(usage_text)
        except NoMatches:
            pass

    def _tick(self) -> None:
        refresh = False
        tail_dirty = False
        if self._turn_active:
            self._spinner_frame += 1
            refresh = True
        # Surface notifications raised by background tasks within ~200ms.
        cur = self._unread_count()
        if cur != self._last_notif_count:
            self._last_notif_count = cur
            refresh = True
            tail_dirty = True
        # Remote bridge liveness — refresh when the active bridge count changes
        # (bridge connects, disconnects, or watchdog restarts it).
        try:
            _mgr = getattr(self.session_state, "_remote_bridge_manager", None)
            _bridge_count = (
                len(
                    [
                        b
                        for b in _mgr.active_bridges
                        if b.get("status") in ("running", "connecting...")
                    ]
                )
                if _mgr is not None
                else 0
            )
        except Exception:  # noqa: BLE001
            _bridge_count = 0
        if _bridge_count != getattr(self, "_last_bridge_count", -1):
            self._last_bridge_count = _bridge_count
            refresh = True
            tail_dirty = True
        # A notif/bridge change must show at once — drop the throttled tail cache
        # so _refresh_status rebuilds it this frame instead of up to 0.25s later.
        if tail_dirty:
            self._status_tail = None
        if refresh:
            self._refresh_status()

    # -- input ----------------------------------------------------------------
    def _update_mode_badge(self, input_value: str = "") -> None:
        """Show a mode badge and restyle/animate the input for plan/bash modes.

        Bash takes visual precedence over plan when both apply (you can be in
        plan mode and still type a ``!command``). Styling is driven by CSS
        classes (``bash-mode`` / ``plan-mode``) plus a per-mode pulse so each
        mode has a distinct look *and* a distinct animation.
        """
        plan = getattr(self.session_state, "plan_mode_enabled", False)
        bash = input_value.startswith("!")
        goal = getattr(self.session_state, "active_goal", None)
        # Skip the badge/class/pulse work entirely when the mode is unchanged —
        # this runs on every keystroke, so the common case (mode didn't change)
        # must be a cheap no-op.
        if self._last_mode_state == (plan, bash, bool(goal)):
            return
        self._last_mode_state = (plan, bash, bool(goal))
        try:
            badge = self._w("#mode-badge", Static)
            prompt = self._w("#prompt", PromptInput)
        except NoMatches:
            return

        if plan and bash:
            t = Text()
            t.append("  ⏸ PLAN  ", style="bold #7aa2f7")
            t.append("$ BASH — runs in your shell", style="bold #bb9af7")
            badge.update(t)
            badge.display = True
        elif plan:
            badge.update(Text("  ⏸ PLAN MODE — proposing, not editing", style="bold #7aa2f7"))
            badge.display = True
        elif bash:
            badge.update(Text("  $ BASH — runs in your shell", style="bold #bb9af7"))
            badge.display = True
        elif goal:
            short = goal if len(goal) <= 60 else goal[:57] + "…"
            badge.update(Text(f"  🎯 GOAL — {short}", style="bold #e0af68"))
            badge.display = True
        else:
            badge.update("")
            badge.display = False

        # Drive the input look from CSS classes (bash wins over plan visually).
        prompt.set_class(bash, "bash-mode")
        prompt.set_class(plan and not bash, "plan-mode")

        # Also style the > prefix chevron and the prompt row to match.
        try:
            prefix = self.query_one("#prompt-prefix", Static)
            prefix.set_class(bash, "bash-mode")
            prefix.set_class(plan and not bash, "plan-mode")
            prefix.update("$ " if bash else "> ")
        except NoMatches:
            pass
        try:
            row = self.query_one("#prompt-row", Horizontal)
            row.set_class(bash, "bash-mode")
            row.set_class(plan and not bash, "plan-mode")
        except NoMatches:
            pass

        # Distinct animation per mode.
        self._set_input_pulse("bash" if bash else ("plan" if plan else None))

    def _set_input_pulse(self, mode: str | None) -> None:
        """Animate the input's tint with a per-mode pulse (no-op if unchanged).

        - bash: quick, urgent magenta pulse
        - plan: slow, calm blue "breathing"
        - None: stop and clear the tint
        """
        if mode == self._input_pulse_mode:
            return
        self._input_pulse_mode = mode

        if self._input_pulse_timer is not None:
            self._input_pulse_timer.stop()
            self._input_pulse_timer = None

        try:
            prompt = self.query_one("#prompt", PromptInput)
        except Exception:  # noqa: BLE001
            return

        if mode is None:
            # Smoothly fade the tint away.
            prompt.styles.animate("tint", value=Color(0, 0, 0, 0.0), duration=0.3)
            return

        if mode == "bash":
            glow = Color.parse("#bb9af7")
            period = 0.55  # fast, alert
            peak = 0.22
        else:  # plan
            glow = Color.parse("#7aa2f7")
            period = 1.1  # slow, calm
            peak = 0.16

        self._pulse_on = False

        def _tick() -> None:
            self._pulse_on = not self._pulse_on
            alpha = peak if self._pulse_on else 0.02
            try:
                prompt.styles.animate("tint", value=glow.with_alpha(alpha), duration=period * 0.85)
            except Exception:  # noqa: BLE001
                pass

        _tick()  # kick off immediately
        self._input_pulse_timer = self.set_interval(period, _tick)

    # -- autocomplete dropdown ------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        self._update_palette(event.value, event.input.cursor_position)
        self._update_mode_badge(event.value)

    def on_text_area_changed(self, event: Any) -> None:
        """The prompt is a TextArea, which posts Changed instead of
        Input.Changed. Feed the palette/mode badge the same way."""
        area = getattr(event, "text_area", None)
        if area is None or getattr(area, "id", None) != "prompt":
            return
        text = area.text
        # Palette completion is line-oriented; give it the offset within
        # the cursor's own line so "@" detection behaves as before.
        try:
            row, col = area.cursor_location
            line = text.split("\n")[row]
        except Exception:  # noqa: BLE001
            line, col = text, len(text)
        self._update_palette(line, col)
        self._update_mode_badge(text)

    def _active_at_fragment(self, value: str, cursor: int) -> tuple[int, str] | None:
        """The ``@token`` ending at the cursor, anywhere in the line.

        Returns ``(start_index_of_@, fragment_after_@)`` or ``None``. The token
        must be at the start of the line or preceded by whitespace, so emails
        (``user@host``) and mid-word ``@`` don't trigger completion.
        """
        m = _AT_FRAGMENT_RE.search(value[: max(0, cursor)])
        if not m:
            return None
        return m.start(), m.group(1)

    def _palette_candidates(self, value: str, cursor: int | None = None) -> list[str]:
        """Completion candidates for the current input, by trigger context.

        ``@`` mentions are matched at the **cursor token anywhere** in the line;
        ``/`` commands remain line-level (they only make sense at the start).
        """
        if cursor is None:
            cursor = len(value)

        # @<agent>/<file> — completes the @token under the cursor, ANYWHERE.
        frag = self._active_at_fragment(value, cursor)
        if frag is not None:
            return self._at_candidates(frag[1])

        # Slash contexts are line-level: a space means the token is complete,
        # unless it is a command like `/ingest` or `/file` that takes arguments.
        if " " in value or not value:
            if value.startswith("/ingest "):
                prefix = value[len("/ingest ") :].strip()
                try:
                    from novacode_cli.wiki.ingest import IngestEngine
                    from pathlib import Path

                    engine = IngestEngine()
                    sources = engine.list_raw_sources()
                    matches = []
                    for s in sources:
                        if (
                            not prefix
                            or prefix.lower() in s.lower()
                            or prefix.lower() in Path(s).name.lower()
                        ):
                            matches.append(s)
                    return [f"/ingest {s}" for s in matches[:50]]
                except Exception:
                    return []
            elif value.startswith("/file "):
                prefix = value[len("/file ") :].strip()
                categories = [
                    "technologies/",
                    "frameworks/",
                    "patterns/",
                    "projects/",
                    "comparisons/",
                ]
                matches = [
                    c for c in categories if not prefix or c.lower().startswith(prefix.lower())
                ]
                return [f"/file {c}" for c in matches]
            return []
        v = value.lower()
        # Legacy /skill:<name> form — still works, kept for back-compat.
        if value.startswith("/skill:"):
            return [
                f"/skill:{n}"
                for n in self._get_skill_names()
                if f"/skill:{n}".lower().startswith(v)
            ]
        # /<command> or bare /<skill-name>. Skills are invocable directly as
        # /<name> (resolved in _run_slash via _run_skill), so surface them here
        # alongside the built-in commands; a skill sharing a command's name
        # shouldn't appear twice.
        if value.startswith("/"):
            cmds = [c for c in _TUI_SLASH_COMMANDS if c.startswith(v)]
            seen = set(cmds)
            skill_cmds = [
                f"/{n}"
                for n in self._get_skill_names()
                if f"/{n}".lower().startswith(v) and f"/{n}" not in seen
            ]
            return cmds + skill_cmds
        return []

    def _at_candidates(self, fragment: str) -> list[str]:  # noqa: PLR0915 (budgeted BFS walk)
        """Build @agent + @file completions for an ``@`` fragment (no leading @)."""
        at_prefix = f"@{fragment}".lower()
        candidates: list[str] = []
        # Agent completions
        for n in self._get_agent_names():
            if f"@{n}".lower().startswith(at_prefix):
                candidates.append(f"@{n}")
        # File completions — recursive match across the whole project tree
        if True:
            prefix = fragment
            max_results = 50
            try:
                from novacode_cli.config.config import settings
                cwd = settings.get_workspace_root()
                # Skip common non-source directories to keep rglob fast
                _SKIP_DIRS = frozenset(
                    {
                        ".git",
                        ".nova",
                        ".venv",
                        ".env",
                        "node_modules",
                        "__pycache__",
                        ".pytest_cache",
                        "build",
                        "dist",
                        ".ruff_cache",
                        ".mypy_cache",
                    }
                )

                # Cap entries scanned so a rare/no-match prefix can't walk the
                # whole repo. _update_palette runs this in a thread its exclusive
                # worker can't actually interrupt, so an unbounded walk keeps
                # churning (and contends the GIL) after every keystroke.
                max_scan = 4000
                scanned = 0

                def _walk(start: Path, prefix: str, cwd: Path, seen: set[str]) -> None:
                    """Match files starting with prefix, breadth-first under start.

                    BFS (not recursion) so shallow files — the ones usually wanted
                    — are matched first and the scan budget caps deep exploration;
                    a DFS budget could be exhausted descending one huge subtree
                    before ever reaching a root-level match.
                    """
                    nonlocal scanned
                    from collections import deque

                    queue = deque([start])
                    while queue and scanned < max_scan and len(seen) < max_results:
                        for child in queue.popleft().iterdir():
                            if scanned >= max_scan or len(seen) >= max_results:
                                break
                            scanned += 1
                            is_dir = child.is_dir()
                            # Skip hidden / noise dirs (don't descend into them)
                            if is_dir and (child.name.startswith(".") or child.name in _SKIP_DIRS):
                                continue
                            if child.name.lower().startswith(prefix.lower()):
                                rel = child.relative_to(cwd).as_posix()
                                tag = f"@{rel}"
                                if is_dir:
                                    tag += "/"
                                if tag not in seen:
                                    seen.add(tag)
                                    candidates.append(tag)
                            if is_dir:
                                queue.append(child)

                seen: set[str] = set()
                if "/" in prefix:
                    dir_part, _, file_part = prefix.rpartition("/")
                    search_dir = (cwd / dir_part).resolve()
                    if search_dir.is_dir():
                        for p in search_dir.iterdir():
                            name = p.name
                            if name.lower().startswith(file_part.lower()):
                                rel = p.relative_to(cwd).as_posix()
                                tag = f"@{rel}"
                                if p.is_dir():
                                    tag += "/"
                                if tag not in seen:
                                    seen.add(tag)
                                    candidates.append(tag)
                            if len(seen) >= max_results:
                                break
                else:
                    _walk(cwd, prefix, cwd, seen)
            except Exception:
                pass
            return candidates

    @work(group="palette", exclusive=True)
    async def _update_palette(self, value: str, cursor: int | None = None) -> None:
        if cursor is None:
            cursor = len(value)

        # Small debounce for fast typing.
        await asyncio.sleep(0.05)

        # Run the potentially heavy candidate search (which walks the filesystem)
        # in a background thread to keep the main TUI loop responsive.
        matches = await asyncio.to_thread(self._palette_candidates, value, cursor)

        # No-op (don't show) when the only match already equals the current
        # token — the @fragment under the cursor, or the whole line for slashes.
        frag = self._active_at_fragment(value, cursor)
        current_token = f"@{frag[1]}" if frag is not None else value
        show = bool(matches) and not (
            len(matches) == 1 and matches[0].lower() == current_token.lower()
        )
        if not show:
            self._hide_palette()
            return
        # No-op when the candidate list is identical to what's already shown —
        # this runs on every keystroke, and rebuilding the OptionList (clear +
        # re-add) every time is the bulk of typing lag in completion contexts.
        if matches == self._last_palette:
            return
        self._last_palette = list(matches)
        try:
            palette = self._w("#cmdpalette", OptionList)
        except NoMatches:
            return
        palette.clear_options()
        for c in matches:
            palette.add_option(Option(c))
        palette.display = True
        try:
            palette.highlighted = 0
        except Exception:  # noqa: BLE001
            pass

    def _hide_palette(self) -> None:
        self._last_palette = None
        try:
            palette = self._w("#cmdpalette", OptionList)
        except NoMatches:
            return
        palette.clear_options()
        palette.display = False

    def _accept_palette(self, command: str) -> None:
        inp = self.query_one("#prompt", PromptInput)
        value = inp.value
        cursor = inp.cursor_position
        frag = self._active_at_fragment(value, cursor)
        if frag is not None and command.startswith("@"):
            # Replace only the @token under the cursor, preserving the rest of
            # the line. Directories (trailing "/") get no space so the user can
            # keep typing the path; everything else gets a trailing space.
            start, _ = frag
            trailing = "" if command.endswith("/") else " "
            inp.value = value[:start] + command + trailing + value[cursor:]
            inp.cursor_position = start + len(command) + len(trailing)
        else:
            # Slash command (line-level): replace the whole line.
            inp.value = f"{command} "
            inp.cursor_position = len(inp.value)
        self._hide_palette()
        inp.focus()

    def on_key(self, event) -> None:
        # Runs on EVERY keystroke — use the cached ref and bail immediately when
        # the palette is hidden (the common case) to avoid a DOM walk per key.
        try:
            palette = self._w("#cmdpalette", OptionList)
        except NoMatches:
            return
        if not palette.display:
            return
        if event.key == "down":
            palette.action_cursor_down()
        elif event.key == "up":
            palette.action_cursor_up()
        elif event.key == "escape":
            self._hide_palette()
            event.stop()
            event.prevent_default()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Mouse-click accept from the command palette (other lists handle their own).
        if event.option_list.id == "cmdpalette":
            self._accept_palette(str(event.option.prompt))

    def _on_large_paste(self, placeholder: str, char_count: int) -> None:
        """Notify the transcript that a large paste was collapsed."""
        self._log(Text(f"{placeholder} ({char_count:,} chars)", style="dim"))

    def on_prompt_input_submitted(self, event: Any) -> None:
        """The prompt (a TextArea) posts its own Submitted on enter."""
        self.on_input_submitted(event)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Only react to the main prompt (modals have their own inputs).
        if event.input.id != "prompt":
            return
        # If the palette is open, Enter accepts the highlighted command.
        palette = self.query_one("#cmdpalette", OptionList)
        if palette.display and palette.option_count and palette.highlighted is not None:
            opt = palette.get_option_at_index(palette.highlighted)
            self._accept_palette(str(opt.prompt))
            return
        # The input box holds compact [paste #N +M lines] placeholders for large
        # pastes (so the box stays readable while composing). On submit we expand
        # them back to the full text, which is what both the agent receives AND
        # what the chat shows — the sent message is displayed in full.
        text = resolve_paste_placeholders(event.value, self.paste_tracker).strip()
        # Strip deceptive/invisible Unicode (BiDi overrides, zero-width chars) a
        # paste may smuggle in — a prompt-injection vector. Warn + sanitize.
        text = self._sanitize_user_text(text)
        if not text:
            return
        event.input.value = ""
        self._hide_palette()
        # Submitting is an explicit act: resume following the tail so the user
        # always sees their own message, even if they had scrolled up to read.
        self._follow_tail = True
        # While the agent is working, a submitted prompt steers the current run
        # (injected for its next step) instead of cancelling it or starting a new
        # turn. Esc still cancels. Empty turns route normally.
        if self._turn_active:
            # A slash command or !bash isn't a message to the agent — queue it to
            # RUN as a command when the turn ends, rather than steering the agent
            # with its literal text.
            stripped = text.lstrip()
            if stripped.startswith("/") or stripped.startswith("!"):
                self._deferred_commands.append(text)
                self._log(
                    Text(f"↳ Queued command (runs after this turn): {text}", style="dim")
                )
            else:
                self._add_live_steer(text)
            return
        self._dispatch(text)

    def _sanitize_user_text(self, text: str) -> str:
        """Strip deceptive/invisible Unicode from user input (warn + sanitize).

        Hidden BiDi/zero-width characters in a prompt (often pasted) can hide
        instructions from the human while still reaching the model. We remove
        them and surface a TUI notice. Best-effort — never block input on error.
        """
        try:
            from novacode_cli.security.unicode_security import (
                detect_dangerous_unicode,
                strip_dangerous_unicode,
                summarize_issues,
            )

            issues = detect_dangerous_unicode(text)
            if not issues:
                return text
            cleaned = strip_dangerous_unicode(text)
            self._log(
                Text(
                    f"🛡 Removed {len(issues)} hidden Unicode char(s) from input "
                    f"({summarize_issues(issues)})",
                    style="yellow",
                )
            )
            return cleaned
        except Exception:  # noqa: BLE001
            return text

    def _add_live_steer(self, text: str) -> None:
        """Inject a transient steering instruction into the in-flight turn.

        The agent's SteeringMiddleware reads ``session_state.steering_instructions``
        on every model call, so appending here makes the running agent pick this
        up at its next step. The instruction is removed when the turn ends
        (one-turn lifetime) so it doesn't leak into later turns.
        """
        from novacode_cli.bootstrap.steering import SteeringInstruction

        if getattr(self.session_state, "steering_instructions", None) is None:
            self.session_state.steering_instructions = []
        si = SteeringInstruction(label="steer", instruction=text)
        self.session_state.steering_instructions.append(si)
        self._live_steers.append(si)
        self._log(
            Text(
                f"↗ Steering (applies on the next step): {text}",
                style="italic #7aa2f7",
            )
        )

    def action_cancel_turn(self) -> None:
        # Kill any in-flight foreground shell/execute subprocess FIRST. It runs in
        # a detached thread+loop that worker cancellation can't reach, so without
        # this a hung command keeps running (and freezes/crashes the UI) until its
        # internal timeout. The middleware's read loop polls this and terminates.
        try:
            from novacode_cli.shell.jobs import request_kill

            if request_kill():
                self._set_status("killing command…")
        except Exception:  # noqa: BLE001
            pass

        # A spawned session runs in its own process — cancel THAT, not our workers.
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "child":
            self.run_worker(self._supervisor().cancel(pane.sid))
            self._set_status("cancelling…")
            return

        # A turn started from a remote bridge runs inside the "remote" consumer
        # worker, not the "turn" group, so the group cancel below never reached
        # it — escape did nothing for a Telegram/Discord-triggered turn. Cancel
        # the tracked task instead of the group: the group cancel would take the
        # message loop with it and detach the bridge.
        remote_turn = getattr(self, "_remote_turn_task", None)
        if remote_turn is not None and not remote_turn.done():
            remote_turn.cancel()

        # Cancel only the turn (and speech), NOT every worker: cancel_all() would
        # also kill the supervisor's child-output readers and the remote consumer,
        # silently detaching every background session.
        self.workers.cancel_group(self, "turn")
        self.workers.cancel_group(self, "voice_tts")
        self._set_status("cancelling…")

    # ── Voice I/O ─────────────────────────────────────────────────────────

    def _ensure_voice_pipeline(self) -> bool:
        """Build the voice pipeline if needed; return whether voice is usable."""
        from novacode_cli import audio

        if not audio.is_voice_available():
            return False
        if self._voice_pipeline is None:
            from novacode_cli.config.nova_config import NovaConfig

            cfg = NovaConfig().get_voice_config()
            if getattr(self.session_state, "_voice_pipeline", None) is not None:
                self._voice_pipeline = self.session_state._voice_pipeline
            else:
                from novacode_cli.audio.pipeline import VoicePipeline

                self._voice_pipeline = VoicePipeline(
                    stt_provider=cfg.get("stt_provider", "faster-whisper"),
                    tts_provider=cfg.get("tts_provider", "piper"),
                    provider_configs=cfg.get("providers", {}),
                    stt_model=cfg.get("stt_model", "base"),
                    stt_device=cfg.get("stt_device", "auto"),
                    tts_voice=cfg.get("tts_voice", "en_US-lessac-medium"),
                )
            self._voice_speak_responses = bool(cfg["speak_responses"])
            # Pre-load the heavy models in the background so the first PTT /
            # spoken reply doesn't pay the load cost inline.
            self._voice_warmup()
        return True

    @work(group="voice_warmup", exclusive=True)
    async def _voice_warmup(self) -> None:
        """Background pre-load of the voice models (best-effort, never crashes).

        If any model still needs downloading, surface a status-line indicator
        while warmup runs so launching Nova doesn't look frozen.
        """
        if self._voice_pipeline is None:
            return
        from contextlib import suppress

        pending: list[str] = []
        with suppress(Exception):
            pending = self._voice_pipeline.downloads_pending()
        if pending:
            self._set_nova_indicator(
                f"⬇ downloading voice models… ({', '.join(pending)})",
                style="dim cyan",
            )
        # warmup() already swallows per-model errors; this guards the call itself.
        with suppress(Exception):
            await self._voice_pipeline.warmup()
        if pending:
            self._set_nova_indicator("✓ voice models ready", style="dim green", auto_clear=3.0)

    def _eager_voice_warmup(self) -> None:
        """Pre-load STT/TTS/VAD models at startup whenever voice will be used.

        Mirrors main.py's boot-banner preload: `enabled` (always-listening),
        `speak_responses` (Nova talks), or push-to-talk all use voice, so warm
        the models now instead of paying the load inline on the first PTT/reply.
        """
        from novacode_cli.config.nova_config import NovaConfig

        cfg = NovaConfig().get_voice_config()
        voice_wanted = bool(
            cfg.get("enabled") or cfg.get("speak_responses") or cfg.get("mode") == "push_to_talk"
        )
        if voice_wanted:
            self._ensure_voice_pipeline()

    def _voice_unavailable_notice(self) -> None:
        self._set_nova_indicator(
            "🎤 voice not installed — see /voice status",
            style="yellow",
            auto_clear=4.0,
        )

    def _submit_voice_text(self, text: str) -> None:
        """Route a transcript like typed input: steer an active turn, else dispatch."""
        text = self._sanitize_user_text(text)
        if not text:
            return
        if self._turn_active:
            self._add_live_steer(text)
        else:
            self._dispatch(text)

    async def action_voice_talk(self) -> None:
        """ctrl+g: capture one spoken utterance (VAD-endpointed) and submit it."""
        if not self._ensure_voice_pipeline():
            self._voice_unavailable_notice()
            return
        if self._voice_capturing:
            return  # debounce double-press
        self.workers.cancel_group(self, "voice_tts")
        self._voice_capturing = True
        self._set_nova_indicator("🎤 listening…", style="bold cyan")
        transcript: str | None = None
        try:
            transcript = await self._voice_pipeline.capture_utterance()
        except Exception as _mic_err:  # noqa: BLE001 — a mic/STT error must never crash the TUI
            msg = str(_mic_err)
            short = msg[:80].replace("\n", " ")
            self._set_nova_indicator(f"🎤 mic error: {short}", style="red", auto_clear=4.0)
            self._log(
                Text(
                    f"[🎤 Mic error] {msg[:200]}",
                    style="red",
                )
            )
            return
        finally:
            self._voice_capturing = False
        if transcript:
            self._set_nova_indicator("")
            self._submit_voice_text(transcript)
        else:
            self._set_nova_indicator("🎤 (nothing heard)", style="dim", auto_clear=2.0)

    async def action_voice_toggle(self) -> None:
        """ctrl+shift+v: toggle hands-free always-listening mode."""
        if not self._ensure_voice_pipeline():
            self._voice_unavailable_notice()
            return
        if self._voice_listening:
            self._voice_listening = False
            self.workers.cancel_group(self, "voice")
            self.workers.cancel_group(self, "voice_tts")
            self._set_nova_indicator("🎤 voice off", style="dim", auto_clear=2.0)
            return
        self._voice_listening = True
        self._set_nova_indicator("🎤 listening (hands-free)…", style="bold cyan")
        self._voice_listen_loop()

    @work(group="voice", exclusive=True)
    async def _voice_listen_loop(self) -> None:
        """Continuously capture utterances and submit each; pauses during TTS."""
        if not self._ensure_voice_pipeline():
            return
        try:
            await self._voice_pipeline.listen_loop(
                self._submit_voice_text,
                should_stop=lambda: not self._voice_listening,
            )
        except Exception as _listen_err:  # noqa: BLE001 — never let the audio loop crash the TUI
            self._voice_listening = False
            msg = str(_listen_err)
            short = msg[:80].replace("\n", " ")
            self._set_nova_indicator(f"🎤 listen error: {short}", style="red", auto_clear=4.0)
            self._log(
                Text(
                    f"[🎤 Listen error] {msg[:200]}",
                    style="red",
                )
            )

    @work(group="voice_tts")
    async def _speak_reply(self, text: str) -> None:
        """Speak a natural 1-2 sentence summary of an assistant reply via TTS.

        No-op unless voice has been activated this session and ``speak_responses``
        is on. The reply is condensed into a short spoken summary (out-of-band
        LLM call, fail-open); short replies are spoken as-is.
        """
        pipeline = self._voice_pipeline
        if pipeline is None or not self._voice_speak_responses:
            return
        from novacode_cli.audio.summarize import summarize_for_speech

        prose = await summarize_for_speech(text)
        if not prose:
            return

        # Re-check self._voice_pipeline in case it was reset during the await
        pipeline = self._voice_pipeline
        if pipeline is None:
            return

        # Check if the voice model needs to be downloaded before speaking
        if getattr(pipeline, "tts_needs_download", False):
            self._set_nova_indicator("🔊 downloading voice…", style="dim cyan")
            # Force the pipeline warmup (downloads the voice model)
            try:
                await pipeline.warmup()
            except Exception as w_err:  # noqa: BLE001
                self._log(Text(f"[🔊 TTS error] Voice download failed: {w_err}", style="red"))
                self._set_nova_indicator("🔊 tts error", style="red", auto_clear=3.0)
                return

        # Re-check again after warmup just in case
        pipeline = self._voice_pipeline
        if pipeline is None:
            return

        self._set_nova_indicator("🔊 speaking…", style="dim cyan")
        rain = self._matrix_rain()
        if rain is not None:
            rain.pause()
        spoke = True
        try:
            try:
                async with self._speech_lock:
                    await pipeline.speak(prose)
            except Exception as tts_err:  # noqa: BLE001 — TTS failure must never crash the TUI
                spoke = False
                msg = str(tts_err)
                self._log(
                    Text(
                        f"[🔊 TTS error] {msg}",
                        style="red",
                    )
                )
        finally:
            if self._os_focused:
                rain = self._matrix_rain()
                if rain is not None:
                    rain.resume()
        if not spoke:
            self._set_nova_indicator("🔊 tts error", style="red", auto_clear=3.0)
        elif self._voice_listening:
            self._set_nova_indicator("🎤 listening (hands-free)…", style="bold cyan")
        else:
            self._set_nova_indicator("")

    async def action_toggle_terminal(self) -> None:
        """ctrl+t: open a new inline interactive terminal widget in the chat transcript."""
        await self._run_bash("!")

    async def action_run_background(self) -> None:
        """ctrl+b: run the current input in the background without blocking the terminal.

        Mirrors Claude Code's Ctrl+B behaviour:
        - ``!<cmd>`` → background subprocess (output streams into a card).
        - Any other text → background agent turn (full agent, fresh thread_id,
          auto-approved tools so it never blocks waiting for user input).
        """
        try:
            prompt_widget = self._w("#prompt", PromptInput)
        except NoMatches:
            return
        raw = prompt_widget.value.strip()
        if not raw:
            # Context-sensitive Ctrl+B with an empty prompt:
            #  • a command is running  → detach IT to the background (the
            #    registry "started" event logs the task id + updates the ⚙ bar).
            #  • nothing running       → open the Background Tasks panel.
            from novacode_cli.shell.jobs import request_detach

            if request_detach():
                # End the agent's turn so it goes IDLE and the user can chat
                # again immediately (spec: "Main TUI immediately becomes
                # available"). The command keeps running on the background loop —
                # which is a plain daemon thread, NOT a Textual worker, so
                # cancelling the turn group leaves it untouched. The turn's
                # CancelledError handler resets _turn_active via its finally.
                self._detach_cancelling = True
                self._set_status("backgrounding command…")
                self.workers.cancel_group(self, "turn")
                return
            self._open_tasks_panel()
            return
        prompt_widget.value = ""
        self._update_mode_badge()
        self._bg_job_count += 1
        job_id = self._bg_job_count
        if raw.startswith("!"):
            cmd = raw[1:].strip()
            if not cmd:
                self._log(Text("ctrl+b: empty command after !", style="dim"))
                return
            self._bg_shell_worker(cmd, job_id)
        else:
            self._bg_agent_worker(raw, job_id)

    # ── Background tasks (persistent indicator + panel) ──────────────────
    def _refresh_tasks_bar(self) -> None:
        """Rebuild the ``⚙`` indicator: background shell jobs (●) + running async
        subagents (◇), with live runtime + count. Runs on the 1s timer (live
        clock) and on every registry event."""
        active = []
        fmt_runtime = lambda s: f"{int(s)}s"  # noqa: E731 — fallback if jobs import fails
        try:
            from novacode_cli.shell.jobs import fmt_runtime as _fr, get_registry

            fmt_runtime = _fr
            active = get_registry().active()
        except Exception:  # noqa: BLE001
            pass
        watcher = getattr(self, "_async_watcher", None)
        agents = watcher.running_tasks() if watcher is not None else []
        try:
            bar = self._w("#tasks-bar", Static)
        except NoMatches:
            return
        if not active and not agents:
            bar.remove_class("active")
            bar.update("")
            # Nothing running → stop the runtime ticker so it never interferes
            # with the rest of the UI when idle.
            if self._tasks_timer is not None:
                try:
                    self._tasks_timer.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._tasks_timer = None
            return
        # Something running → ensure the 1s runtime ticker is running.
        if self._tasks_timer is None:
            try:
                self._tasks_timer = self.set_interval(1.0, self._refresh_tasks_bar)
            except Exception:  # noqa: BLE001
                self._tasks_timer = None
        t = Text()
        total = len(active) + len(agents)
        if total == 1 and active:
            job = active[0]
            t.append("⚙ Background  ", style="bold #7aa2f7")
            t.append("● ", style="cyan")
            t.append(job.command[:48], style="#c0caf5")
            t.append(f"  ⏱ {fmt_runtime(job.runtime())}", style="dim")
        elif total == 1 and agents:
            a = agents[0]
            t.append("◇ Async agent  ", style="bold #bb9af7")
            t.append("● ", style="#bb9af7")
            t.append(str(a["agent_name"])[:36], style="#c0caf5")
            t.append(f"  ⏱ {fmt_runtime(a['runtime'])}", style="dim")
        else:
            t.append(f"⚙ Tasks ({total})  ", style="bold #7aa2f7")
            segs = []
            for job in active[:2]:
                segs.append(f"● {job.command.split()[0][:14]} {fmt_runtime(job.runtime())}")
            for a in agents[:2]:
                segs.append(f"◇ {str(a['agent_name'])[:14]} {fmt_runtime(a['runtime'])}")
            t.append("  ·  ".join(segs), style="#c0caf5")
            shown = min(len(active), 2) + min(len(agents), 2)
            if total > shown:
                t.append(f"  +{total - shown} more", style="dim")
        bar.update(t)
        bar.add_class("active")

    def _on_task_event_threadsafe(self, event: str, job: Any) -> None:
        """Registry observer — fires on the background loop thread. Marshal to the
        UI thread (Textual widgets aren't thread-safe)."""
        # "output" fires on every log chunk; the indicator shows only
        # command/runtime/count (runtime advances via the 1s timer), so ignore it
        # here to avoid flooding the UI thread. The panel reads logs directly.
        if event == "output":
            return
        self._post_to_ui(self._on_task_event, event, job)

    def _on_task_event(self, event: str, job: Any) -> None:
        self._refresh_tasks_bar()
        if event == "started" and job is not None:
            self._log(
                Text.assemble(
                    ("⚙ Running in background: ", "bold #7aa2f7"),
                    (f"{job.command[:70]}\n", "#c0caf5"),
                    (f"Task ID: {job.task_id}", "dim"),
                )
            )
            # Clarify for the agent on its next turn (a Ctrl+B detach ends the
            # current turn, so the tool call is patched as "cancelled" — this note
            # corrects that: the command is still running as a background task).
            self._pending_job_notes.append(
                f"You moved {job.task_id} to the background; it is still running "
                f"(command: {job.command}). Check it with get_task_status('{job.task_id}') "
                f"or get_task_logs('{job.task_id}')."
            )
        elif event in ("completed", "failed", "terminated") and job is not None:
            ok = event == "completed"
            glyph = "✓" if ok else "✗"
            verb = {"completed": "completed", "failed": "failed", "terminated": "terminated"}[event]
            self._log(
                Text(
                    f"{glyph} Task {verb}: {job.command[:60]} · {job.task_id}"
                    + (f" · exit {job.exit_code}" if job.exit_code is not None else ""),
                    style="green" if ok else "yellow",
                )
            )
            try:
                self.session_state.add_notification(
                    level="info",
                    title=f"Task {verb}: {job.task_id}",
                    message=f"{job.command[:100]} (exit {job.exit_code})",
                    source="shell",
                )
            except Exception:  # noqa: BLE001
                pass
            # Async monitor: a task the agent is waiting on reports back into the
            # conversation as soon as it finishes, instead of leaving a note that
            # sits until the user happens to type again.
            #
            #   resume_on_done   — Ctrl+B detached: the agent was mid-work.
            #   agent_launched   — the agent ran it with background=True.
            #
            # Both are work the agent started and cares about the result of. A
            # task the USER launched or restarted (a dev server, say) is expected
            # to keep running and only leaves a note. Either way this requires an
            # idle agent: resuming mid-turn would interleave two prompts on the
            # same thread.
            watched = getattr(job, "resume_on_done", False) or getattr(
                job, "agent_launched", False
            )
            resume = (
                event in ("completed", "failed")
                and watched
                and not self._turn_active
            )
            if resume:
                self._log(Text(f"↻ Resuming — {job.task_id} finished.", style="cyan"))
                self._continue_after_task(job)
            else:
                self._pending_job_notes.append(
                    f"Background {job.task_id} {verb} (exit {job.exit_code}). "
                    f"Command: {job.command}. Use get_task_logs('{job.task_id}') for output."
                )

    @work(exclusive=True, group="turn")
    async def _continue_after_task(self, job: Any) -> None:
        """Auto-resume the agent after a Ctrl+B-detached background task finishes,
        feeding it the result so it continues its work from where it left off."""
        tail = "\n".join(job.output.splitlines()[-40:]) or "(no output)"
        # The Ctrl+B path patches the original tool call as "cancelled", which is
        # misleading on its own — say so only when that actually happened, so an
        # agent-backgrounded task isn't told about a note it never saw.
        detached_note = (
            "If you saw a 'tool call was cancelled' note for that command "
            "earlier, it referred only to the foreground wait being detached; "
            "the command itself ran to completion.\n\n"
            if getattr(job, "resume_on_done", False)
            else ""
        )
        prompt = (
            f"[Background task finished] The command you launched in the "
            f"background — `{job.command}` ({job.task_id}) — has now finished with "
            f"exit code {job.exit_code}. {detached_note}"
            f"Output (last lines):\n{tail}\n\n"
            f"Continue with what you were doing, taking this result into account."
        )
        await self._stream_prompt(prompt)
        await self._maybe_run_approved_plan()

    @work
    async def _open_tasks_panel(self) -> None:
        """Open the Background Tasks panel; handle copy/logs results."""
        result = await self.push_screen_wait(BackgroundTasksScreen())
        if not isinstance(result, dict):
            return
        if result.get("action") == "copy":
            cmd = result.get("command", "")
            try:
                self.copy_to_clipboard(cmd)
            except Exception:  # noqa: BLE001
                pass
            self._log(Text(f"Copied command: {cmd[:70]}", style="dim"))
        elif result.get("action") == "logs":
            from novacode_cli.shell.jobs import get_registry

            job = get_registry().resolve(result.get("task_id", ""))
            if job is not None:
                tail = "\n".join(job.output.splitlines()[-40:]) or "(no output yet)"
                self._log(
                    Text.assemble(
                        (f"⚙ {job.task_id} logs ({job.status}):\n", "bold #7aa2f7"),
                        (tail, "#c0caf5"),
                    )
                )

    # ── Artifacts (persistent component) ─────────────────────────────────
    def _refresh_artifacts_component(self) -> None:
        """Update the fixed ``◈ Artifacts (N)`` footer component."""
        try:
            from novacode_cli.artifacts.registry import get_registry

            n = get_registry().count()
        except Exception:  # noqa: BLE001
            n = 0
        if n:
            t = Text("◈ ", style="#7aa2f7")
            t.append(f"Artifacts ({n})", style="bold #7aa2f7")
        else:
            t = Text("◈ Artifacts", style="dim")
        self._set_info("#info-artifacts", t)

    def _on_artifact_event_threadsafe(self, event: str, art: Any) -> None:
        """Registry observer — fires on whichever thread created/updated the
        artifact (tools run in worker threads). Marshal to the UI thread."""
        self._post_to_ui(self._on_artifact_event, event, art)

    def _on_artifact_event(self, event: str, art: Any) -> None:
        if event == "created":
            self._log(Text(f"◈ Artifact created: {art.title}", style="#7aa2f7"))
        self._refresh_artifacts_component()

    @work
    async def _open_artifacts_list(self) -> None:
        """Show the artifact list; open the chosen one in the browser."""
        from novacode_cli.artifacts.registry import get_registry
        from novacode_cli.artifacts.server import artifact_url

        arts = get_registry().list()
        if not arts:
            self._log(
                Text(
                    "No artifacts yet — ask me to create one, e.g. "
                    "\"create an artifact showing the changes you made\".",
                    style="dim",
                )
            )
            return
        options = [
            f"◈ {a.title}  ·  [{a.type}] v{a.version} · {a.status}" for a in arts
        ]
        idx = await self.push_screen_wait(
            PickScreen("Artifacts — open in browser", options, hint="↑/↓ · Enter open · Esc cancel")
        )
        if 0 <= idx < len(arts):
            import webbrowser

            url = artifact_url(arts[idx].id)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
            self._log(Text(f"◈ Opening {arts[idx].title} → {url}", style="#7aa2f7"))

    def on_click(self, event: Any) -> None:
        """Handle clicks on the persistent footer components.

        Artifacts list, background-tasks panel, the todo checklist (click
        anywhere on it to collapse/expand), and jump-to-latest.
        """
        try:
            w = getattr(event, "widget", None)
            while w is not None:
                wid = getattr(w, "id", None)
                if wid == "col-artifacts":
                    self._open_artifacts_list()
                    return
                if wid == "tasks-bar":
                    self._open_tasks_panel()
                    return
                if wid == "todo-dock":
                    self.action_toggle_todos()
                    return
                if wid == "jump-latest":
                    self.action_jump_latest()
                    return
                # A retained reasoning card folds/unfolds on click.
                if isinstance(w, ChatMessage) and w.has_class("reason"):
                    w.toggle_collapsed()
                    return
                w = getattr(w, "parent", None)
        except Exception:  # noqa: BLE001
            pass

    async def action_copy_or_quit(self) -> None:
        """ctrl+c: copy the active text selection to the clipboard, else quit.

        Lets users select text in the transcript (chat messages, tool output)
        and copy it with ctrl+c. With no selection, behaves like quit.
        """
        try:
            selected = self.screen.get_selected_text()
        except Exception:  # noqa: BLE001
            selected = None
        if selected:
            try:
                self.copy_to_clipboard(selected)
                self._log(Text(f"📋 Copied {len(selected)} chars to clipboard", style="dim"))
                # Clear the highlight now that it's copied.
                self.screen.selections = {}
                self.screen.refresh()
            except Exception:  # noqa: BLE001
                pass
            return
        await self.action_quit()

    async def action_quit(self) -> None:
        """Persist the session (so --continue works) then exit."""
        try:
            from novacode_cli.events import unregister_tool_output_callback

            unregister_tool_output_callback(self._on_tool_output)
        except Exception:
            pass
        # Shut spawned sessions down before we go: main.py ends in os._exit(),
        # which runs no finally blocks, so anything still alive here is orphaned.
        sup = getattr(self, "_session_supervisor", None)
        if sup is not None:
            with contextlib.suppress(Exception):
                await sup.close_all(timeout=5.0)
        # Same reason, and the bigger leak: MCP stdio servers are child
        # processes (a node/npx tree for playwright, a python one for serena).
        # os._exit() reaps none of them, so every Nova that exited without this
        # left its servers — and their browsers — running forever. Two Nova
        # instances leak twice as fast, which is how a 16 GB machine ends up
        # swapping.
        with contextlib.suppress(Exception):
            from novacode_cli.mcp import get_shared_mcp_middleware

            middleware = get_shared_mcp_middleware()
            pool = getattr(middleware, "_session_pool", None)
            if pool is not None:
                await asyncio.wait_for(pool.aclose(), timeout=5.0)
        await self._save_session()
        await self._consolidate_learning()
        self.exit()

    async def _consolidate_learning(self) -> None:
        """Distil this session's un-reviewed work into memory before it ends.

        Hermes reviews on a tool-call threshold, so a session that does real
        work and stops short of it (a handful of edits, then quit) learned
        nothing durable. This runs the same review once on the way out. Purely
        best-effort — exiting must never hang or fail on it.
        """
        try:
            from novacode_cli.hermes.middleware import get_active_learning_middleware

            # LangGraph compiles middleware into the graph and does not expose
            # the instances, so the module publishes the live one.
            mw = get_active_learning_middleware()
            if mw is None:
                return
            if await mw.consolidate_session():
                self._log(Text("✓ Session learnings consolidated.", style="dim"))
        except Exception:  # noqa: BLE001 — never block exit on learning
            pass

    async def _save_session(self, *, cleared: bool = False) -> None:
        """Save the conversation to disk via the session manager (best effort).

        Args:
            cleared: Mark the saved session as cleared (used by /clear) so it is
                excluded from --continue auto-resume — a cleared conversation
                won't come back, but stays on disk for the picker.
        """
        if self.session_manager is None:
            return
        try:

            config = {"configurable": {"thread_id": self.session_state.thread_id}}
            # Bound the checkpointer read so a slow/contended DB can't hang /quit.
            state = await asyncio.wait_for(self.agent.aget_state(config), timeout=5.0)
            messages = state.values.get("messages", [])
            if not messages:
                return
            from novacode_cli.config.config import settings
            todos = state.values.get("todos") or getattr(self.session_state, "todos", None)
            # save_session does several synchronous file writes — run it off the
            # event loop so /save, /clear, and quit don't freeze the UI.
            await asyncio.to_thread(
                self.session_manager.save_session,
                session_id=self.session_state.session_id,
                thread_id=self.session_state.thread_id,
                messages=messages,
                assistant_id=self.assistant_id,
                todos=todos,
                model_name=self.model_name,
                model_provider=self._model_provider,
                project_root=settings.get_workspace_root(),
                sandbox_id=self._sandbox_id,
                sandbox_type=self._sandbox_type,
                cleared=cleared,
            )
        except Exception:  # noqa: BLE001
            pass  # never block exit on a save failure

    # -- input routing --------------------------------------------------------
    @work(exclusive=True, group="turn")
    async def _dispatch(self, text: str) -> None:
        """Route input: quit / !bash / slash command / @agent / agent prompt."""
        self.workers.cancel_group(self, "voice_tts")

        # Input typed while a spawned session is on screen goes to THAT process.
        pane = getattr(self, "_active_pane", None)
        if pane is not None and pane.kind == "child":
            await self._dispatch_to_child(pane, text)
            return

        low = text.lower()
        if low in ("/quit", "/exit", "quit", "exit", "q"):
            await self.action_quit()
            return

        if text.startswith("!"):
            await self._run_bash(text)
            return

        if text.startswith("/"):
            await self._run_slash(text)
            return

        # @agent mention(s) -> delegate through the main agent's `task` tool.
        try:
            from novacode_cli.config.config import settings
            from novacode_cli.input import (
                parse_agent_mentions,
                parse_agent_mentions_multi,
            )

            mentioned_agents = parse_agent_mentions_multi(text, settings)
            agent_name, query = parse_agent_mentions(text, settings)
        except Exception:  # noqa: BLE001
            mentioned_agents, agent_name, query = [], None, text

        # Two or more agents (or an agent mentioned mid-message): hand the whole
        # request to the main agent and let it orchestrate the named subagents in
        # order via `task`. @file mentions are expanded by the normal turn path.
        if len(mentioned_agents) >= 2 or (  # noqa: PLR2004
            mentioned_agents and agent_name is None
        ):
            ordered = " → ".join(f"@{a}" for a in mentioned_agents)
            await self._add_message(Text(f"You → {ordered}", style="bold cyan"), "user", Text(text))
            preamble = (
                "This request references specialist subagents by @name. Delegate "
                "each part of the work to the named agent using the `task` tool, "
                "in the order implied by the request, passing results (e.g. edited "
                "files) from one to the next. Referenced agents in order: "
                f"{', '.join(mentioned_agents)}.\n\nRequest:\n{text}"
            )
            await self._stream_prompt(preamble)
            return

        # Single agent at the start: delegate directly to that one subagent.
        if agent_name:
            await self._add_message(Text(f"{agent_name}", style="bold cyan"), "user", Text(query))
            await self._stream_prompt(
                f"Call the '{agent_name}' subagent to do the following:\n\n{query}"
            )
            return

        # Plain prompt — send it to the agent as a single turn.
        await self._add_message(Text("You", style="bold cyan"), "user", Text(text))
        # Surface any background jobs that finished since the last turn (the user
        # sees the clean prompt; the agent sees the note prepended).
        agent_text = text
        if self._pending_job_notes:
            notes = "\n".join(f"- {n}" for n in self._pending_job_notes)
            self._pending_job_notes = []
            agent_text = f"[Background jobs finished since your last turn]\n{notes}\n\n{text}"
        await self._stream_prompt(agent_text)
        # If a plan was approved during this turn, hand off to the main agent.
        await self._maybe_run_approved_plan()

    async def _stream_prompt(self, text: str, assistant_id: str | None = None) -> None:
        """Run a single prompt through the agent and render its events.

        Serialized on the shared remote lock so local and remote turns never
        interleave on the same checkpointer thread.
        """
        # Remembered so a ContextOverflow can compact and re-send this exact
        # prompt rather than losing the user's message.
        self._last_user_prompt = text
        lock = getattr(self.session_state, "_remote_message_lock", None)
        self._reset_streaming()
        self._current_assistant_id = assistant_id
        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("thinking…")
        try:
            if lock is not None:
                async with lock:
                    await self._do_stream(text, assistant_id)
            else:
                await self._do_stream(text, assistant_id)
        except asyncio.CancelledError:
            self._reset_streaming()
            # A Ctrl+B detach surfaces as an ev.Cancelled event (handled with its
            # own "moved to background" note); only a real cancel reaches here.
            if not getattr(self, "_detach_cancelling", False):
                self._log(Text("Cancelled.", style="yellow"))
        except Exception as ex:  # noqa: BLE001
            msg = str(ex).lower()
            if any(
                kw in msg
                for kw in ("429", "rate limit", "usage limit", "quota", "too many requests")
            ):
                self._log(Text("⚠️ Warning: Rate Limit / Quota Reached", style="bold yellow"))
                self._log(
                    Text(
                        "The model provider is rate-limiting requests or your usage limit is exhausted.",
                        style="yellow",
                    )
                )
                self._log(Text(f"Detail: {ex}", style="dim yellow"))
            elif any(
                kw in msg for kw in ("401", "unauthorized", "api key", "auth", "forbidden", "403")
            ):
                self._log(Text("⚠️ Warning: Authentication / API Key Error", style="bold yellow"))
                self._log(
                    Text("Please verify your API keys or subscription status.", style="yellow")
                )
                self._log(Text(f"Detail: {ex}", style="dim yellow"))
            else:
                self._log(Text(f"Error: {ex}", style="red"))
        finally:
            self._turn_active = False
            self._detach_cancelling = False
            self._set_status("ready")
            self._clear_live_steers()
            # Safety net: clear the Nova review indicator if it's still showing
            # (e.g. a review triggered on the final turn never drained its
            # completion event).
            self._set_nova_indicator("")
        # Refresh the per-category context breakdown from agent state, then
        # proactively manage the context window once the turn has settled.
        await self._update_context_breakdown()
        await self._check_context()
        # Run commands queued during the turn (as commands), then any deferred
        # prompts that weren't consumed as steers.
        await self._drain_deferred_commands()
        await self._drain_deferred_prompts()

    async def _update_context_breakdown(self) -> None:
        """Recompute the context breakdown from agent state after a turn.

        The console renderer does this in its finalization step; without it the
        TUI's /context view and context warnings had no per-category detail.
        Best-effort — never blocks the turn on a state read.
        """
        tracker = self.token_tracker
        if tracker is None or not getattr(tracker, "model_name", None):
            return
        try:
            from novacode_cli.context import ContextManager

            ag, _ = self._active_agent()
            config = {"configurable": {"thread_id": self.session_state.thread_id}}
            state = await ag.aget_state(config)
            msgs = state.values.get("messages", []) if state else []
            if msgs:
                # Off the loop: it can shell out to `ollama show`, which hangs
                # while the local daemon is busy (a 42s UI freeze was measured).
                model = tracker.model_name
                breakdown = await asyncio.to_thread(
                    lambda: ContextManager(model).breakdown(msgs)
                )
                tracker.set_breakdown(breakdown)
        except Exception:  # noqa: BLE001
            pass

        await self._maybe_warn_ollama_offload(tracker.model_name)

    async def _maybe_warn_ollama_offload(self, model_name: str | None) -> None:
        """Warn once if the loaded Ollama model is offloaded to CPU (slow).

        Skips cloud API models entirely. Probes `ollama ps` off the event loop;
        stays "unchecked" until the model is actually loaded so the advisory
        still fires on a later turn, then latches off.
        """
        if self._ollama_offload_checked or not model_name:
            return
        from novacode_cli.context._dynamic import is_ollama_cloud_model

        if model_name.lower().startswith(
            ("claude-", "gpt-", "gemini-", "o1", "o3", "o4")
        ) or is_ollama_cloud_model(model_name):
            # Cloud (API or Ollama-cloud) runs remotely — never offloads locally.
            self._ollama_offload_checked = True
            return
        try:
            from novacode_cli.context._dynamic import (
                check_ollama_offloading,
                get_ollama_runtime_info,
            )

            info = await asyncio.to_thread(get_ollama_runtime_info, model_name)
            if info is None:
                return  # not loaded yet — retry on a later turn
            self._ollama_offload_checked = True
            warning = await asyncio.to_thread(check_ollama_offloading, model_name)
            if warning:
                self._log(Text(f"⚠ {warning}", style="bold #e0af68"))
        except Exception:  # noqa: BLE001
            self._ollama_offload_checked = True

    def _clear_live_steers(self) -> None:
        """Drop transient live-steer instructions added during the turn.

        Unconsumed steers (the agent finished before the middleware could
        inject them) are saved to ``_deferred_prompts`` so they can be
        dispatched as a fresh turn rather than silently vanishing.
        """
        if not self._live_steers:
            return
        instrs = getattr(self.session_state, "steering_instructions", None) or []
        for si in self._live_steers:
            # If the middleware never delivered this steer, requeue it.
            if not si.consumed:
                self._deferred_prompts.append(si.instruction)
            try:
                instrs.remove(si)
            except ValueError:
                pass
        self._live_steers.clear()

    async def _drain_deferred_commands(self) -> None:
        """Run slash/bash commands that were queued during the turn — as actual
        commands (routing like _dispatch), not as agent messages."""
        while self._deferred_commands:
            cmd = self._deferred_commands.pop(0)
            self._log(Text(f"↳ Running queued command: {cmd}", style="italic #9ece6a"))
            low = cmd.strip().lower()
            if low in ("/quit", "/exit", "quit", "exit", "q"):
                await self.action_quit()
                return
            if cmd.startswith("!"):
                await self._run_bash(cmd)
            elif cmd.startswith("/"):
                await self._run_slash(cmd)

    async def _drain_deferred_prompts(self) -> None:
        """Dispatch prompts that were queued during the previous turn.

        Called after ``_stream_prompt`` finishes. Each deferred prompt is
        shown as a user message and run through the agent as a new turn,
        giving the user seamless "send while busy" behaviour.
        """
        while self._deferred_prompts:
            prompt = self._deferred_prompts.pop(0)
            self._log(
                Text(
                    f"↗ Processing queued message: {prompt}",
                    style="italic #9ece6a",
                )
            )
            await self._add_message(Text("You", style="bold cyan"), "user", Text(prompt))
            await self._stream_prompt(prompt)

    async def _check_context(self) -> None:
        """Warn (and optionally auto-compact) as the context window fills up.

        The TUI previously showed ctx% passively but never nudged — so a long
        session could silently approach the model's limit and then error. Here we
        warn once at the warning threshold and, at critical, auto-compact to
        avoid a hard overflow on the next turn.
        """
        if self.token_tracker is None:
            return
        try:
            bd = self.token_tracker.get_breakdown()
        except Exception:  # noqa: BLE001
            return
        if not bd:
            return
        pct = getattr(bd, "usage_percentage", 0.0)
        # Policy lives in novacode_cli/context/pressure.py so the Rich REPL and
        # this TUI cannot drift. Auto-compact fires at AUTO_COMPACT_THRESHOLD
        # (0.82), deliberately below deepagents' 0.85 summarization backstop, so
        # Nova's own compaction wins the race and the library only catches
        # mid-turn overflow.
        from novacode_cli.context import PressureAction, assess_pressure

        decision = assess_pressure(
            pct,
            compacted_last_turn=getattr(self, "_compacted_last_turn", False),
            auto_compact_enabled=self._auto_compact,
        )
        self._compacted_last_turn = decision.compacted_last_turn

        if decision.disable_auto_compact:
            self._auto_compact = False

        if decision.action is PressureAction.COMPACT:
            self._log(Text(f"⚠ {decision.reason}", style="bold #f7768e"))
            await self._run_compact("")
            self._compacted_last_turn = True
            # Floor: if compaction couldn't get us back under the critical line,
            # further auto-compaction is futile (the summary itself is near the
            # window — usually a too-small model). Stop the per-turn loop and
            # tell the user how to recover.
            try:
                bd2 = self.token_tracker.get_breakdown()
            except Exception:  # noqa: BLE001
                bd2 = None
            if bd2 is not None:
                from novacode_cli.context import post_compaction_still_critical

                after = post_compaction_still_critical(
                    getattr(bd2, "usage_percentage", 0.0),
                    auto_compact_enabled=self._auto_compact,
                )
                if after.disable_auto_compact:
                    self._auto_compact = False
                    self._log(Text(f"⚠ {after.reason}", style="bold #f7768e"))
            self._ctx_warned = True
        elif decision.action is PressureAction.WARN:
            if decision.disable_auto_compact or not self._auto_compact:
                # Critical-but-disabled: the user must act. Always shown.
                self._log(Text(f"⚠ {decision.reason}", style="bold #f7768e"))
            elif not self._ctx_warned:
                self._log(Text(f"⚠ {decision.reason}", style="#e0af68"))
            self._ctx_warned = True
        else:
            # Dropped back below the warning line (e.g. after /compact) — re-arm.
            self._ctx_warned = False
            self._compacted_last_turn = False

    async def _do_stream(self, text: str, assistant_id: str | None = None) -> None:
        ag, backend = self._active_agent()
        aid = assistant_id or self.assistant_id
        async for e in run_agent_stream(
            text,
            ag,
            aid,
            self.session_state,
            backend=backend,
            image_tracker=self.image_tracker,
            seen_message_ids=self._seen,
        ):
            # Through _deliver, not _render directly: if the user switches tabs
            # mid-turn, this turn's events must keep going to the ROOT pane
            # rather than into whichever pane is now on screen.
            await self._deliver(getattr(self, "_root_pane", None), e)

    @work(group="remote")
    async def _remote_consumer(self) -> None:
        """Render remote (Discord/Telegram) prompts in the TUI and reply back.

        Mirrors the legacy remote processor but streams through the TUI instead
        of the console. Turn serialization is handled by ``_stream_prompt``'s
        lock (shared with local input)."""
        import asyncio

        from novacode_cli.remote.processor import _extract_response

        queue = self.session_state._remote_message_queue
        while True:
            try:
                msg = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                if await self._remote_route(msg):
                    queue.task_done()
                    continue
                # The main session runs in this process and streams into the
                # visible tab: bring it on screen before its turn starts.
                root = getattr(self, "_root_pane", None)
                if root is not None and getattr(self, "_active_pane", root) is not root:
                    await self._switch_to(root)
                lock = getattr(self.session_state, "_remote_message_lock", None)
                if self._turn_active or (lock is not None and lock.locked()):
                    try:
                        # Log it in the TUI transcript so the local user sees it
                        await self._add_message(
                            Text(
                                f"📡 {msg.user_name} ({msg.platform.value})",
                                style="bold cyan",
                            ),
                            "user",
                            Text(msg.text),
                        )
                        # Treat as steer / question response
                        if (
                            self._remote_question_future is not None
                            and not self._remote_question_future.done()
                        ):
                            react_fn = getattr(msg, "react_fn", None)
                            if react_fn is not None:
                                try:
                                    await react_fn("📥")
                                except Exception:
                                    pass
                            self._remote_question_future.set_result(msg)
                            continue

                        text = (getattr(msg, "text", "") or "").strip()
                        low = text.lower()
                        if low.startswith("/steer"):
                            text = text[len("/steer") :].strip()
                        elif text.startswith("/"):
                            reply_fn = getattr(msg, "reply_fn", None)
                            if reply_fn is not None:
                                try:
                                    await reply_fn(
                                        "⏳ Busy with the current task — send "
                                        "`/steer <text>` (or just text) to add to it."
                                    )
                                except Exception:
                                    pass
                            continue
                        if not text:
                            continue

                        self._add_live_steer(text)
                        react_fn = getattr(msg, "react_fn", None)
                        reply_fn = getattr(msg, "reply_fn", None)
                        if react_fn is not None:
                            try:
                                await react_fn("↗")
                            except Exception:
                                pass
                        elif reply_fn is not None:
                            try:
                                await reply_fn(f"↗ Added to the running task: {text}")
                            except Exception:
                                pass
                    except Exception as ex:
                        self._log(Text(f"Steer error: {ex}", style="red"))
                    finally:
                        queue.task_done()
                    continue
                await self._add_message(
                    Text(
                        f"📡 {msg.user_name} ({msg.platform.value})",
                        style="bold cyan",
                    ),
                    "user",
                    Text(msg.text),
                )
                # Remote turns auto-approve tools (no local prompt to answer).
                prev_auto = getattr(self.session_state, "auto_approve", False)
                self.session_state.auto_approve = True
                config = {"configurable": {"thread_id": self.session_state.thread_id}}
                typing_task: "asyncio.Task | None" = None
                try:
                    self._remote_msg = msg
                    self._remote_activity = []  # tool/subagent names for the status
                    self._remote_status = None
                    self._remote_react("🤔")  # acknowledge: thinking
                    # Keep the "typing…" indicator alive for the whole turn so it
                    # reads like a person typing, then sends a message (the platform
                    # indicator only lasts ~10s, so it must be re-triggered).
                    if msg.typing_fn is not None:

                        async def _typing_loop(typing_fn=msg.typing_fn) -> None:
                            try:
                                while True:
                                    await typing_fn()
                                    await asyncio.sleep(8)
                            except asyncio.CancelledError:
                                return

                        typing_task = asyncio.create_task(_typing_loop())

                    # Slash commands from chat: handle the remote-safe subset
                    # directly (info/toggles/conversation), stream skills as a
                    # turn, and decline interactive/local-only ones.
                    prompt_text = msg.text.strip()
                    slash_reply: str | None = None
                    if prompt_text.startswith("/"):
                        slash_reply, resolved = await self._remote_slash(prompt_text)
                        if resolved is not None:
                            prompt_text = resolved  # e.g. a resolved skill prompt

                    if slash_reply is not None:
                        # Command fully handled — reply directly, no agent turn.
                        try:
                            await msg.reply_fn(slash_reply)
                        except Exception:  # noqa: BLE001
                            pass
                        self._remote_react("✅")
                    else:
                        pre = await self.agent.aget_state(config)
                        pre_count = len(pre.values.get("messages", [])) if pre else 0
                        # A compact status line edits in place to show live tool/
                        # subagent activity (condensed counts) — SEPARATE from the
                        # answer, which is sent as a fresh chat message below.
                        if getattr(msg, "edit_fn", None) is not None:
                            from novacode_cli.remote.status import RemoteStatusLine

                            self._remote_status = RemoteStatusLine(
                                msg.edit_fn, label=self._remote_label(None)
                            )
                            self._remote_status.start()
                        # While the turn runs, drain further remote messages as
                        # live steers so the user can "add to the previous prompt".
                        steer_drain = asyncio.create_task(self._remote_steer_drain(queue))

                        async def _run_remote_turn() -> None:
                            if isinstance(prompt_text, str):
                                await self._stream_prompt(prompt_text)
                            elif callable(prompt_text):
                                import inspect

                                if inspect.iscoroutinefunction(prompt_text):
                                    await prompt_text()
                                else:
                                    res = prompt_text()
                                    if inspect.iscoroutine(res):
                                        await res

                        # Run the turn as its OWN task so escape can cancel it.
                        # This consumer lives in the "remote" worker group, which
                        # action_cancel_turn deliberately does not touch —
                        # cancelling the group would tear down the message loop
                        # and silently detach the bridge. Without a separate
                        # handle there was nothing escape could cancel, so a
                        # turn started from Telegram/Discord ignored the key.
                        turn_task = asyncio.create_task(_run_remote_turn())
                        self._remote_turn_task = turn_task
                        try:
                            await turn_task
                        finally:
                            self._remote_turn_task = None
                            steer_drain.cancel()
                            try:
                                await steer_drain
                            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                                pass
                        # Settle the status line to a done-summary, then send the
                        # answer as its own message (no footer — the status carries
                        # the tool/subagent summary).
                        if self._remote_status is not None:
                            await self._remote_status.finalize()
                        post = await self.agent.aget_state(config)
                        reply = _extract_response(post, pre_count) or "✅ Task completed."
                        if self._remote_label(None):
                            reply = f"**[main]** {reply}"
                        try:
                            await msg.reply_fn(reply)
                        except Exception:  # noqa: BLE001
                            pass
                        self._remote_react("✅")
                finally:
                    self._remote_msg = None
                    self._remote_status = None
                    if typing_task is not None:
                        typing_task.cancel()
                        try:
                            await typing_task
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
                    self.session_state.auto_approve = prev_auto
            except asyncio.CancelledError:
                self._log(Text("Remote turn cancelled.", style="yellow"))
            except Exception as ex:  # noqa: BLE001
                self._log(Text(f"Remote error: {ex}", style="red"))
                self._remote_react("❌", msg)
            finally:
                from contextlib import suppress

                with suppress(ValueError):
                    queue.task_done()

    # Slash commands that require the interactive local TUI (modals, pickers,
    # launchers) and can't be driven over a chat bridge.
    _REMOTE_LOCAL_ONLY = frozenset(
        {
            "sessions",
            "mcp",
            "theme",
            "remote",
            "agents",
            "skills",
            "init",
            "trello",
            "browser-use",
            "hooks",
            "servers",
            "files",
            "images",
            "vision",
            "kill",
            "restore",
            "reindex",
            "plan",
            "steer",
            "notifications",
            "trace",
            "log",
            "tests",
            "fast",
        }
    )

    def _remote_help_text(self) -> str:
        """Plain-text help listing the commands that work over a remote chat."""
        return (
            "Remote commands:\n"
            "• /help — this list\n"
            "• /context (/tokens, /cost) — context window usage\n"
            "• /model — show the current model\n"
            "• /clear — reset the conversation\n"
            "• /compact — summarize & free up context\n"
            "• /save — save the session\n"
            "• /verbose — toggle settings\n"
            "• /ingest <path> — ingest a raw source into the wiki\n"
            "• /ask <question> — ask with wiki context\n"
            "• /wiki — show Obsidian LLM Wiki browser\n"
            "• /research <query> — launch multi-agent research swarm\n"
            "• /ralph <task> — run autonomously (looping mode)\n"
            "• /evolution — view self-evolution logs\n"
            "• /dream — consolidate memory from previous sessions\n"
            "• /<skill> (e.g. /graphify) — run a skill\n"
            "Sessions (several at once):\n"
            "• /sessions — list them · /new <name>: <task> — start one\n"
            "• /use <name> — where plain messages go (main = the first one)\n"
            "• @name <text> — send to one session, or reply to its message\n"
            "• In a group with Topics on, each session gets its own topic\n"
            "Anything without a leading / is sent to the agent. Interactive "
            "panels (/model picker, /mcp, /theme…) are local-only."
        )

    async def _remote_slash(self, text: str) -> "tuple[str | None, Any]":
        """Route a slash command arriving from Discord/Telegram.

        Returns ``(reply_text, stream_prompt_or_callable)``:
          * ``(str, None)`` — send this text back; no agent turn.
          * ``(None, str)`` — stream this prompt as an agent turn (skills).
          * ``(None, callable)`` — execute this coroutine/callable in the turn context.
        Interactive / local-only commands return an explanatory reply.
        """
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("help", "?", "commands"):
            return self._remote_help_text(), None
        if cmd in ("tokens", "context", "cost"):
            try:
                return self._token_text().plain, None
            except Exception:  # noqa: BLE001
                return "(token usage unavailable)", None
        if cmd == "model":
            return (
                f"Current model: {self.model_name}\n"
                "Switching models is only available in the local TUI (/model).",
                None,
            )
        if cmd == "verbose":
            new = self.session_state.toggle_verbose()
            return f"Verbose mode {'on' if new else 'off'}.", None
        if cmd == "clear":
            await self._run_clear()
            return "✅ Conversation cleared.", None
        if cmd == "compact":
            await self._run_compact("")
            return "✅ Context compacted.", None
        if cmd == "save":
            await self._run_save()
            return "✅ Session saved.", None
        if cmd == "steer":
            text = arg.strip()
            if text.lower() in ("clear", "reset", "off"):
                self._clear_live_steers()
                return "✅ Steering cleared.", None
            if not text:
                return (
                    "Usage: /steer <instruction> — extra guidance the agent "
                    "follows on its next step (and a running turn picks up now).",
                    None,
                )
            self._add_live_steer(text)
            return f"↗ Steering added: {text}", None

        if cmd == "ingest":
            from novacode_cli.wiki.ingest import IngestEngine

            try:
                engine = IngestEngine()
                if not arg:
                    # Auto-discover the local wiki's Clipping/ + raw/ contents.
                    sources = engine.list_raw_sources()
                    if sources:
                        listing = "\n".join(f"  • {s}" for s in sources)
                        return (
                            "Usage: /ingest <path> (filename found anywhere in "
                            f"Clipping/ or raw/)\nAvailable sources:\n{listing}",
                            None,
                        )
                    return (
                        "No sources yet — save web clips into "
                        f"{engine._mgr.root / 'Clippings'} first.",
                        None,
                    )
                # Resolve the source (Clipping/ or raw/), then stream as a turn.
                source_full = engine.resolve_source(arg)
                rel = source_full.relative_to(engine._mgr.root).as_posix()
                source_content = source_full.read_text(encoding="utf-8")
                prompt = (
                    "Please analyze this source and create a wiki page at "
                    "/.nova/wiki/ for it.\n\n"
                    f"Source ({rel}):\n```\n{source_content[:8000]}\n```"
                )
                return None, prompt
            except (FileNotFoundError, ValueError) as ex:
                return f"Error: {ex}", None
            except Exception as ex:  # noqa: BLE001
                return f"/ingest error: {ex}", None

        if cmd == "ask":
            if not arg:
                return "Usage: /ask <question>", None
            # Search wiki and prepend context
            from novacode_cli.wiki.ask import WikiAskEngine

            try:
                engine = WikiAskEngine()
                prompt = await engine.build_prompt(arg)
                return None, prompt
            except Exception as ex:  # noqa: BLE001
                return f"/ask error: {ex}", None

        if cmd == "research":
            from novacode_cli.commands.research_handler import handle_research_command

            if not arg:
                with _rich_console.capture() as cap:
                    await handle_research_command(
                        self.agent, self.session_state, self.token_tracker, cmd_args=None
                    )
                out = Text.from_ansi(cap.get()).plain.strip()
                return out, None

            from novacode_cli.commands.research_handler import (
                _parse_args,
                _MODE_AGENTS,
                _MODE_DESCRIPTIONS,
            )

            mode, query, agent_count, fast_mode = _parse_args(arg)
            if not query:
                return (
                    f"Error: no research query provided.\nUsage: /research {mode} <your question>",
                    None,
                )

            async def run_res(msg_obj=self._remote_msg):
                self._log(
                    Text(
                        f"📡 Remote ({msg_obj.platform.value if msg_obj else 'Remote'}) triggered research swarm: {query}",
                        style="bold cyan",
                    )
                )
                base_agents = _MODE_AGENTS[mode]
                agents = (base_agents * ((agent_count // len(base_agents)) + 1))[:agent_count]
                base_dir = Path(".nova") / "research"
                try:
                    base_dir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass

                conversation_context = ""
                try:
                    from novacode_cli.context import ContextManager

                    conversation_context = await ContextManager().digest(
                        self.agent, self.session_state.thread_id
                    )
                except Exception:
                    pass

                prompt = render_template(
                    "research_swarm.jinja",
                    research_query=query,
                    mode=mode,
                    mode_description=_MODE_DESCRIPTIONS[mode],
                    agent_count=agent_count,
                    agents=agents,
                    base_dir=base_dir.as_posix(),
                    fast_mode=fast_mode,
                    conversation_context=conversation_context,
                )

                reply_msg = f"🔬 Starting research swarm (mode: {mode}, agents: {agent_count})..."
                if fast_mode:
                    reply_msg += " (fast mode)"
                if msg_obj is not None:
                    try:
                        await msg_obj.reply_fn(reply_msg)
                    except Exception:
                        pass
                await self._tui_execute_fn(
                    prompt,
                    self.agent,
                    "dora",
                    self.session_state,
                    self.token_tracker,
                    self.backend,
                )

            return None, run_res

        if cmd == "evolution":

            async def run_evo(msg_obj=self._remote_msg):
                lines = []

                def _emit(m=""):
                    if m:
                        lines.append(m)

                from novacode_cli.commands.evolution_command import handle_evolution_command

                try:
                    await handle_evolution_command(emit=_emit)
                except ImportError:
                    from novacode_cli.commands.evolution_handler import handle_evolution_command

                    await handle_evolution_command(emit=_emit)
                text_out = "\n".join(lines)
                plain = Text.from_markup(text_out).plain.strip()
                if msg_obj is not None:
                    try:
                        await msg_obj.reply_fn(plain or "No evolution logs yet.")
                    except Exception:
                        pass

            return None, run_evo

        if cmd == "dream":

            async def run_dream(msg_obj=self._remote_msg):
                status_lines = []

                def _emit(m=""):
                    if m:
                        status_lines.append(m)

                from novacode_cli.commands.dream_handler import handle_dream_command

                result = await handle_dream_command(
                    self.session_state, self.assistant_id, emit=_emit
                )
                if status_lines and msg_obj is not None:
                    plain = Text.from_markup("\n".join(status_lines)).plain.strip()
                    try:
                        await msg_obj.reply_fn(plain)
                    except Exception:
                        pass
                if isinstance(result, str) and result.strip():
                    await self._stream_prompt(result)

            return None, run_dream

        if cmd == "ralph":
            # For --status, it's fast, so return directly
            if arg.strip() == "--status":
                lines = []

                def _emit(m=""):
                    if m:
                        lines.append(m)

                from novacode_cli.commands.ralph_handler import handle_ralph_status

                await handle_ralph_status(self.session_state, emit=_emit)
                text_out = "\n".join(lines)
                plain = Text.from_markup(text_out).plain
                return plain, None

            # For running ralph task, run it in the turn context
            async def run_ralph(msg_obj=self._remote_msg):
                self._log(
                    Text(
                        f"📡 Remote ({msg_obj.platform.value if msg_obj else 'Remote'}) triggered autonomous Ralph run: {arg or '(resume)'}",
                        style="bold cyan",
                    )
                )

                # We want to forward ralph's emit events to both the local TUI and the remote user
                async def _emit_remote(message: str = "") -> None:
                    if not message:
                        return
                    try:
                        renderable = Text.from_markup(message)
                    except Exception:
                        renderable = Text(message)

                    # Log to TUI locally
                    self._log(renderable)

                    # Reply to remote user
                    plain = renderable.plain.strip()
                    if plain and msg_obj is not None:
                        try:
                            await msg_obj.reply_fn(plain)
                        except Exception:
                            pass

                from novacode_cli.commands.ralph_handler import handle_ralph_command

                parts = text.split(maxsplit=1)
                ralph_args = parts[1].strip() if len(parts) > 1 else ""

                await handle_ralph_command(
                    self.agent,
                    self.session_state,
                    self.assistant_id,
                    self.token_tracker,
                    ralph_args or None,
                    execute_fn=self._tui_execute_fn,
                    emit=_emit_remote,
                )

            return None, run_ralph

        if cmd in self._REMOTE_LOCAL_ONLY:
            return f"/{cmd} is only available in the local TUI.", None

        # Otherwise treat it as a skill: /skill:<name> or a bare /<name>.
        skill_name = cmd[len("skill:") :] if cmd.startswith("skill:") else cmd
        if skill_name:
            try:
                from novacode_cli.commands.skill_invoke import _try_skill_invocation

                skill = await _try_skill_invocation(
                    skill_name, arg or None, self.session_state, self.assistant_id
                )
            except Exception as ex:  # noqa: BLE001
                return f"❌ /{cmd} failed: {ex}", None
            if skill is not None:
                return None, skill.prompt

        return (
            f"Unknown command: /{cmd}. Send /help for what works over remote.",
            None,
        )

    async def _run_bash(self, text: str) -> None:
        """Run a ``!`` shell command in the system terminal by suspending the TUI app."""
        cmd = text[1:].strip()
        if not cmd:
            return

        import sys
        import subprocess
        from novacode_cli.config.config import settings

        # Log the command in the transcript
        self._log(Text(f"Executing: !{cmd}", style="bold yellow"))

        # Suspend Textual and run the command directly on the system terminal
        from textual.app import SuspendNotSupported

        try:
            with self.suspend():
                cwd = settings.get_workspace_root()
                if sys.stdin.isatty():
                    print(f"\n--- Executing command in {cwd.name} ---")
                    print(f"> {cmd}\n")
                try:
                    res = subprocess.run(cmd, shell=True, cwd=cwd)
                    exit_code = res.returncode
                except Exception as ex:  # noqa: BLE001
                    exit_code = -1
                    print(f"Error executing command: {ex}")

                if sys.stdin.isatty():
                    print("\n--- Command finished. Press Enter to return to TUI ---")
                    try:
                        input()
                    except (KeyboardInterrupt, EOFError):
                        pass
        except SuspendNotSupported:
            # Fallback for non-interactive/headless test environments where suspend is not supported
            cwd = settings.get_workspace_root()
            try:
                res = subprocess.run(
                    cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                exit_code = res.returncode
            except Exception:  # noqa: BLE001
                exit_code = -1

        if exit_code == 0:
            self._log(Text("✓ Command finished successfully.", style="green"))
        else:
            self._log(Text(f"❌ Command exited with code {exit_code}.", style="red"))

    # -- background shell (ctrl+b) --------------------------------------------

    @work(group="bgshell")
    async def _bg_shell_worker(self, cmd: str, job_id: int) -> None:
        """Run *cmd* as a non-blocking background subprocess.

        Each call spawns an independent worker in group ``"bgshell"`` (no
        ``exclusive=True``) so multiple Ctrl+B jobs run in true parallel.
        Output streams line-by-line into a ``RichLog`` inside a ``Collapsible``
        card.  When the process exits the card title flips to ✓/✗ and the
        process is deregistered from ProcessManager.
        """
        import os

        from novacode_cli.config.config import settings
        from novacode_cli.process_manager import ProcessInfo, ProcessManager, ProcessStatus

        cwd = settings.get_workspace_root()
        short = cmd if len(cmd) <= 50 else cmd[:47] + "…"

        # Build the card up-front so output starts streaming immediately.
        log_widget = RichLog(classes="bgshell-log", highlight=True, markup=True)
        body = Vertical(log_widget)
        card = Collapsible(body, title=f"⚙ bg[{job_id}]: {short}  [running]", collapsed=False)
        card.add_class("bgshell-card")
        self._close_tool_group()
        await self._transcript().mount(card)
        self._prune_transcript()
        self._scroll_end()

        # Spawn the subprocess with merged stdout+stderr so the log shows both.
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                env=os.environ.copy(),
            )
        except Exception as ex:  # noqa: BLE001
            log_widget.write(f"[bold red]Failed to start: {ex}[/bold red]")
            card.title = f"✗ bg[{job_id}]: {short}  [failed to start]"
            return

        # Register with ProcessManager so `/kill bg-<n>` or `/kill <pid>` works.
        info = ProcessInfo(
            pid=process.pid,
            name=f"bg-{job_id}",
            command=cmd,
            status=ProcessStatus.RUNNING,
            working_dir=str(cwd),
            _process=process,
        )
        ProcessManager.get_instance().register_process(info)

        # Stream output line-by-line into the RichLog.
        assert process.stdout is not None  # noqa: S101  — PIPE guarantees this
        try:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                log_widget.write(line)
                log_widget.scroll_end(animate=False)
        except asyncio.CancelledError:
            process.terminate()
            card.title = f"✗ bg[{job_id}]: {short}  [cancelled]"
            info.status = ProcessStatus.STOPPED
            return

        await process.wait()
        exit_code = process.returncode or 0

        # Update card title and ProcessManager status.
        if exit_code == 0:
            card.title = f"✓ bg[{job_id}]: {short}  [exit 0]"
            card.add_class("bgshell-done")
            info.status = ProcessStatus.STOPPED
        else:
            card.title = f"✗ bg[{job_id}]: {short}  [exit {exit_code}]"
            card.add_class("bgshell-failed")
            info.status = ProcessStatus.FAILED
            log_widget.write(f"[bold red]Exited with code {exit_code}[/bold red]")

        # Collapse finished cards automatically so they don't crowd the transcript.
        card.collapsed = True

    # -- background agent turn (ctrl+b, non-! input) --------------------------

    @work(group="bgagent")
    async def _bg_agent_worker(  # noqa: PLR0915 — linear stream-handling loop
        self, prompt: str, job_id: int
    ) -> None:
        """Run a full agent turn in the background without blocking the main input.

        Uses a fresh thread_id so the background conversation is isolated from the
        main session. All tool approvals are auto-approved (the user opted in by
        pressing Ctrl+B), so the turn never blocks waiting for a decision.
        """
        import uuid

        from novacode_cli.agent_stream import run_agent_stream
        from novacode_cli.ui_events import (
            AssistantMessage,
            Done,
            Error,
            InterruptRequest,
        )

        thread_id = f"bg-{uuid.uuid4().hex[:12]}"
        p_short = prompt if len(prompt) <= 50 else prompt[:47] + "…"

        # Minimal proxy session state — only the fields iterate_agent_events reads.
        # auto_approve=True makes evaluate_tool_actions return allow for every tool
        # and auto-resolves plan interrupts, so no InterruptRequest is yielded for
        # either. Only ask_user_question (kind="question") can still interrupt; we
        # resolve those below with a canned answer.
        class _BgSession:
            def __init__(self_inner, real_ss: Any) -> None:  # noqa: N805
                self_inner.thread_id = thread_id
                self_inner.auto_approve = True
                self_inner.plan_mode_enabled = False
                self_inner.plan_agent = None
                self_inner.plan_content: Any = None
                self_inner.active_goal: str | None = getattr(real_ss, "active_goal", None)

            def add_notification(self_inner, **_kw: Any) -> None:  # noqa: N805
                return None

            def dismiss_notification(self_inner, _nid: Any) -> None:  # noqa: N805
                pass

            def register_pending_approval(self_inner, _iid: Any, _fut: Any) -> None:  # noqa: N805
                pass

            def set_approved_plan(self_inner, _plan: Any) -> None:  # noqa: N805
                pass

            def clear_plan_agent(self_inner) -> None:  # noqa: N805
                pass

        bg_session = _BgSession(self.session_state)
        ag, backend = self._active_agent()

        log_widget = RichLog(classes="bgagent-log", highlight=True, markup=True)
        card = Collapsible(
            Vertical(log_widget),
            title=f"⟳ bg[{job_id}] · {p_short}",
            collapsed=False,
        )
        card.add_class("bgagent-card")
        self._close_tool_group()
        await self._transcript().mount(card)
        self._prune_transcript()
        self._scroll_end()

        # Live "what is happening" line, mirrored into the card title so it is
        # visible even when the card is collapsed. The body keeps the full trace.
        def _set_phase(phase: str) -> None:
            card.title = f"⟳ bg[{job_id}] · {p_short}  ·  {phase}"

        def _write(markup: str) -> None:
            log_widget.write(markup)
            log_widget.scroll_end(animate=False)

        # In-flight tool calls, buffered so a call and its result render as one
        # line (RichLog cannot rewrite a line once written).
        pending: dict[str, str] = {}

        # The final answer, captured so the main agent can be told what the
        # background run concluded (not just that it finished).
        final_text: list[str] = []

        try:
            async for e in run_agent_stream(
                prompt,
                ag,
                self.assistant_id,
                bg_session,
                backend=backend,
                seen_message_ids=set(),
            ):
                if isinstance(e, InterruptRequest):
                    # Only ask_user_question reaches here (tools and plans are
                    # auto-approved via auto_approve=True on the session).
                    # Provide a canned answer so the turn continues unblocked.
                    try:
                        if e.kind == "question":
                            e.future.set_result({"answer": "Please continue autonomously."})
                        else:
                            from novacode_cli.core.agent_loop import default_interrupt_response

                            e.future.set_result(default_interrupt_response(e.kind))
                    except Exception:  # noqa: BLE001
                        pass
                elif isinstance(e, (Done, Error)):
                    break
                else:
                    # Everything else is a progress event: render it into the card
                    # body and update the live phase line.
                    if isinstance(e, AssistantMessage) and e.text:
                        final_text.append(e.text)
                    _render_bg_event(e, _write, _set_phase, self._fileop_summary, pending)
        except asyncio.CancelledError:
            card.title = f"✗ bg[{job_id}] · {p_short}  ·  cancelled"
            return
        except Exception as ex:  # noqa: BLE001
            _write(f"[bold red]Error: {ex}[/bold red]")
            card.title = f"✗ bg[{job_id}] · {p_short}  ·  error"
            card.add_class("bgagent-failed")
            card.collapsed = True
            return

        # A tool call whose result never arrived (turn ended mid-call) still needs
        # to appear, or the trace would silently drop it.
        for prefix in pending.values():
            _write(prefix)
        pending.clear()

        card.title = f"✓ bg[{job_id}] · {p_short}  ·  done"
        card.add_class("bgagent-done")
        card.collapsed = True

        # Report the outcome back to the main agent so it can summarise and act on
        # what the background run found, rather than the user having to relay it.
        self._report_bg_agent_done(job_id, prompt, "\n".join(final_text))

    def _report_bg_agent_done(self, job_id: int, prompt: str, final_text: str) -> None:
        """Queue a note telling the main agent what a background run concluded.

        The note is prepended to the agent's next turn (see the
        ``_pending_job_notes`` drain in the submit path), so the agent can
        summarise the result and act on it without the user relaying it by hand.

        Args:
            job_id: The background job number (``bg[<job_id>]``).
            prompt: The task the background run was given.
            final_text: The run's final assistant prose (may be empty).
        """
        summary = final_text.strip()
        if len(summary) > _BG_REPORT_MAX_CHARS:
            summary = summary[:_BG_REPORT_MAX_CHARS] + "\n… (truncated)"
        note = f"Background agent bg[{job_id}] finished. Task: {prompt}" + (
            f"\n\nIts final answer:\n{summary}" if summary else "\n\n(no final answer)"
        )
        self._pending_job_notes.append(note)
        self._log(
            Text(
                f"✓ Background agent bg[{job_id}] finished — the agent will be told "
                f"on its next turn.",
                style="green",
            )
        )

    async def _run_slash(self, text: str) -> None:
        """Handle the TUI-native slash command subset — table dispatch.

        Lookup order: /skill:<name> prefix → TUI_COMMANDS table (with aliases)
        → plugin-contributed command → bare /<name> skill → unknown notice.
        """
        import inspect

        cmd = text[1:].split(maxsplit=1)[0].lower() if len(text) > 1 else ""
        if cmd.startswith("skill:"):
            # /skill:<name> — resolve + render natively, then stream the prompt.
            await self._run_skill(text)
            return

        spec = TUI_COMMANDS.get(cmd) or TUI_COMMANDS.get(_TUI_COMMAND_ALIASES.get(cmd, ""))
        if spec is not None:
            handler = getattr(self, spec.handler)
            result = handler(text) if spec.wants_text else handler()
            if inspect.isawaitable(result):
                await result
            return

        # A slash command contributed by an enabled plugin.
        if await self._run_plugin_command(text):
            return
        # A bare /<name> may be a skill (e.g. /graphify) — resolve it natively
        # before reporting the command as unavailable.
        if await self._run_skill(text):
            return
        self._log(
            Text(
                f"/{cmd} isn't a recognized command. Type /help to list commands.",
                style="yellow",
            )
        )

    # ── Small handlers extracted from the old _run_slash elif chain ────────
    # (inline blocks became methods so every command fits the table contract)

    def _run_help(self) -> None:
        self._log(self._help_text())

    async def _run_remote_screen(self) -> None:
        await self.push_screen_wait(
            RemoteScreen(
                self.session_state,
                sandbox_id=self._sandbox_id,
                sandbox_type=self._sandbox_type,
            )
        )

    async def _run_theme(self) -> None:
        # Fire-and-forget: the theme screen's result is unused, and awaiting it
        # with push_screen_wait would hold the exclusive "turn" worker open for
        # as long as the modal is on screen, blocking every later command.
        self.push_screen(ThemeScreen())

    def _run_token_view(self) -> None:
        self._log(self._token_text())

    def _run_verbose(self) -> None:
        new = self.session_state.toggle_verbose()
        self._log(
            Text(
                f"Verbose mode {'on' if new else 'off'} — internal context "
                f"{'shown' if new else 'collapsed'}.",
                style="green" if new else "dim",
            )
        )

    async def _run_ralph_screen(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        await self.push_screen_wait(
            RalphScreen(
                session_state=self.session_state,
                agent=self.agent,
                assistant_id=self.assistant_id,
                token_tracker=self.token_tracker,
                args=args,
                execute_fn=self._tui_execute_fn,
            )
        )

    async def _run_middleware(self) -> None:
        await self.push_screen_wait(PluginsScreen())
        # Reload plugin commands so newly-enabled command plugins work this
        # session (middleware/subagents still need a restart, as noted).
        self._load_plugin_commands()

    async def _run_plugins(self, text: str) -> None:
        """Native ``/plugins`` — Claude-compatible plugin installer + marketplaces.

        Mirrors the console handler but logs via ``self._log``. Reuses the shared
        install/marketplace logic in ``plugins.claude_plugins`` / ``.marketplaces``.
        """
        import re as _re

        from novacode_cli.plugins import claude_plugins as cp
        from novacode_cli.plugins import marketplaces as mp

        def log(msg: str, style: str = "") -> None:
            self._log(Text(msg, style=style) if style else Text(msg))

        def fmt(comps: dict) -> str:
            parts = [f"{k}: {', '.join(v)}" for k in ("skills", "commands", "agents", "mcp", "hooks") if (v := comps.get(k))]
            return "  " + " | ".join(parts) if parts else "  (no components)"

        parts = text.split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else "list"
        arg = parts[2].strip() if len(parts) > 2 else ""
        try:
            if sub == "install":
                if not arg:
                    log("Usage: /plugins install <owner/repo | git-url | dir | plugin@marketplace>", "yellow")
                    return
                name = mp.install_plugin(arg) if _re.fullmatch(r"[\w.-]+@[\w.-]+", arg) else cp.install(arg)
                log(f"✓ Installed {name}", "green")
                log(fmt(cp.plugin_components(name)), "cyan")
                log("Restart Nova to activate.", "dim")
            elif sub == "remove":
                log(f"✓ Removed {arg}" if cp.remove(arg) else f"No plugin named '{arg}'",
                    "green" if arg else "yellow")
            elif sub == "search":
                hits = mp.list_marketplace_plugins()
                if arg:
                    q = arg.lower()
                    hits = [p for p in hits if q in p["name"].lower() or q in p["description"].lower()]
                if not hits:
                    log("No matching plugins. Add a marketplace: /plugins marketplace add <owner/repo>", "dim")
                else:
                    log("Available plugins:", "bold")
                    for p in hits:
                        log(f"  • {p['name']}@{p['marketplace']}  {p['description']}")
            elif sub == "marketplace":
                msub, _, marg = arg.partition(" ")
                marg = marg.strip()
                if msub == "add":
                    name = mp.add(marg)
                    log(f"✓ Added marketplace {name} ({len(mp.list_marketplace_plugins())} plugins — /plugins search)", "green")
                elif msub == "remove":
                    log(f"✓ Removed marketplace {marg}" if mp.remove_marketplace(marg) else f"No marketplace '{marg}'",
                        "green" if marg else "yellow")
                else:
                    mkts = mp.list_marketplaces()
                    log("Marketplaces:" if mkts else "No marketplaces. /plugins marketplace add <owner/repo>",
                        "bold" if mkts else "dim")
                    for m in mkts:
                        log(f"  • {m['name']}  {m['source']}")
            else:  # list — open the native plugins viewer instead of a text dump
                await self.push_screen_wait(ClaudePluginsScreen())
        except (ValueError, RuntimeError) as e:
            log(f"Failed: {e}", "red")

    async def _run_reload_plugins(self) -> None:
        """``/reload-plugins`` — rebuild the agent to pick up plugins added/changed
        this session (skills, subagents, MCP), reload hooks, and refresh autocomplete.

        Reuses the agent-rebuild path (``reload_mcp_servers``): a fresh
        ``create_agent_with_config`` re-scans plugin skills/agents/MCP.
        """
        self._set_status("thinking")
        try:
            # The agent-rebuild path prints build/skill/MCP chatter to the global
            # Rich console, which bypasses Textual and corrupts the TUI screen.
            # Capture it (like the other TUI handlers) — our own summary is logged
            # below via self._log.
            with _rich_console.capture():
                new_agent, new_backend = await self.session_state.reload_mcp_servers()
            self.agent = new_agent
            self.backend = new_backend
        except Exception as e:  # noqa: BLE001
            self._log(Text(f"Reload failed: {e}", style="red"))
            self._set_status("ready")
            return

        from novacode_cli import hooks as _hooks
        from novacode_cli.plugins import claude_plugins as cp

        _hooks.reload_hooks()  # plugin hooks re-read on next dispatch
        self._skill_names_cache = None  # refresh skill autocomplete
        self._load_plugin_commands()  # entry-point + Claude plugin commands

        n = len(cp.list_plugins())
        self._log(
            Text(
                f"✓ Reloaded {n} plugin(s) — skills, subagents, MCP, hooks, commands refreshed.",
                style="green",
            )
        )
        self._set_status("ready")

    def _load_plugin_commands(self) -> None:
        """Discover slash commands from enabled plugins and register them.

        Populates ``self._plugin_commands`` (name → async handler) and adds the
        names to the autocomplete list. Built-ins are matched first in
        :meth:`_run_slash`, so a plugin can't shadow a core command.
        """
        try:
            from novacode_cli.plugins.loader import (
                collect_plugin_commands,
                discover_enabled_plugins,
            )

            cmds = collect_plugin_commands(discover_enabled_plugins())  # type: ignore
            self._plugin_commands = {
                name: c["handler"] for name, c in cmds.items() if c.get("handler")
            }
        except Exception:  # noqa: BLE001 — a bad plugin must not break startup
            self._plugin_commands = {}

        # Claude-compatible plugin commands (commands/*.md|*.toml). Invoking one
        # streams its body — with $ARGUMENTS / {{args}} substituted — to the agent
        # as a prompt (mirrors the console's _register_claude_plugin_commands).
        # Entry-point handlers above win on a name collision (setdefault).
        try:
            from novacode_cli.plugins.claude_plugins import plugin_commands

            def _make_claude_handler(body: str):
                async def _handler(args: str) -> str:
                    a = (args or "").strip()
                    prompt = body.replace("$ARGUMENTS", a).replace("{{args}}", a)
                    await self._stream_prompt(prompt)
                    return ""

                return _handler

            for cname, _desc, body in plugin_commands():
                self._plugin_commands.setdefault(cname, _make_claude_handler(body))
        except Exception:  # noqa: BLE001 — a bad plugin must not break startup
            pass

        for name in self._plugin_commands:
            slash = f"/{name}"
            if slash not in _TUI_SLASH_COMMANDS:
                _TUI_SLASH_COMMANDS.append(slash)

    async def _run_plugin_command(self, text: str) -> bool:
        """Dispatch a plugin-contributed slash command. Returns True if handled.

        Built-ins are matched earlier in :meth:`_run_slash`, so they always win.
        The plugin handler is ``async (args) -> str``; its returned text is logged.
        """
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        handler = self._plugin_commands.get(cmd)
        if handler is None:
            return False
        try:
            result = await handler(args)
            if result:
                self._log(Text(str(result)))
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Plugin command /{cmd} failed: {ex}", style="red"))
        return True

    async def _run_copy(self, text: str) -> None:
        """Copy agent output to the clipboard.

        ``/copy``      — copy the last Nova response.
        ``/copy all``  — copy the whole conversation (You/Nova turns).
        """
        parts = text[1:].split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        msgs = list(self._transcript().query(ChatMessage))
        if not msgs:
            self._log(Text("Nothing to copy yet.", style="dim"))
            return

        if arg == "all":
            blocks: list[str] = []
            for m in msgs:
                body = (m.raw_text or "").strip()
                if not body:
                    continue
                who = "You" if m.has_class("user") else "Nova"
                blocks.append(f"## {who}\n{body}")
            payload = "\n\n".join(blocks)
            label = "conversation"
        else:
            nova = [m for m in msgs if m.has_class("nova")]
            if not nova:
                self._log(Text("No agent response to copy yet.", style="dim"))
                return
            payload = (nova[-1].raw_text or "").strip()
            label = "last response"

        if not payload:
            self._log(Text("Nothing to copy.", style="dim"))
            return
        try:
            self.copy_to_clipboard(payload)
            self._log(
                Text(
                    f"📋 Copied {label} ({len(payload):,} chars) to clipboard",
                    style="dim",
                )
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Copy failed: {ex}", style="red"))

    async def _run_skill(self, text: str) -> bool:
        """Resolve a ``/skill:<name>`` (or bare ``/<name>``) and run it natively.

        Renders the "⚡ Invoking skill" block as native widgets (the resolver is
        presentation-free) and streams the skill prompt. Returns ``True`` when a
        skill matched, ``False`` otherwise so the caller can fall back to the
        "command unavailable" notice.
        """
        raw = text[1:]
        if raw.lower().startswith("skill:"):
            raw = raw[len("skill:") :]
        parts = raw.split(maxsplit=1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else None
        if not name:
            self._log(Text("Usage: /<skill-name> [args]", style="yellow"))
            return True

        from novacode_cli.commands.skill_invoke import _try_skill_invocation

        try:
            skill = await _try_skill_invocation(name, args, self.session_state, self.assistant_id)
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/skill:{name} failed: {ex}", style="red"))
            return True
        if skill is None:
            return False

        t = Text()
        t.append(f"⚡ Invoking skill: {skill.name}", style="bold #7aa2f7")
        if skill.description:
            t.append(f"\n  {skill.description}", style="dim")
        t.append(f"\n  Source: {skill.source}", style="dim")
        if skill.args:
            t.append(f"\n  Arguments: {skill.args}", style="dim")
        if skill.supporting_files:
            t.append(
                f"\n  Supporting files: {', '.join(skill.supporting_files)}",
                style="dim",
            )
        self._log(t)
        await self._stream_prompt(skill.prompt)
        return True

    async def _passthrough_command(self, text: str) -> None:
        """Run a print/toggle-only legacy slash command and show its output.

        Captures the global console so the existing handler's ``console.print``
        calls render into the transcript instead of the real terminal.
        """
        from novacode_cli.commands.commands import handle_command

        try:
            with _rich_console.capture() as cap:
                result = await handle_command(
                    text,
                    self.agent,
                    self.token_tracker,
                    self.session_state,
                    self.assistant_id,
                    model_name=self.model_name,
                    image_tracker=self.image_tracker,
                    sandbox_id=self._sandbox_id,
                    sandbox_type=self._sandbox_type,
                )
            out = cap.get()
            if out.strip():
                self._log(Text.from_ansi(out))
            # Sync active TUI agent/backend in case model was switched dynamically.
            # SessionState exposes these as `_agent` / `_backend`; fall back to the
            # current values so a command that doesn't touch them can't blow up.
            if self.session_state is not None:
                self.agent = getattr(self.session_state, "_agent", self.agent)
                self.backend = getattr(self.session_state, "_backend", self.backend)
                model = getattr(self.session_state, "model", None)
                if model:
                    self.model_name = getattr(model, "model_name", None) or getattr(
                        model, "model", "unknown"
                    )
                    if self.token_tracker is not None:
                        try:
                            self.token_tracker.set_model(self.model_name)
                        except Exception:  # noqa: BLE001
                            pass
            # Some handlers return a prompt string to feed back to the agent.
            if isinstance(result, str):
                await self._stream_prompt(result)

            # Reset the voice pipeline on `/voice` settings change so it is re-initialized with the new config.
            if text.strip().lower().startswith(("/voice ", "/voice")):
                self._voice_pipeline = None
                if hasattr(self.session_state, "_voice_pipeline"):
                    self.session_state._voice_pipeline = None
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/{text[1:].split()[0]} failed: {ex}", style="red"))

    @work(group="voice_cmd", exclusive=True)
    async def _run_voice(self, text: str) -> None:
        """Run a ``/voice`` subcommand off the UI coroutine.

        ``/voice test`` and ``/voice download`` can take seconds (model load /
        synthesis / download). Running them in a worker keeps the TUI responsive
        instead of freezing on the captured-output passthrough. Fast subcommands
        (status, on/off, settings…) work here too — the worker just returns
        quickly. Output is captured the same way as ``_passthrough_command``.
        """
        from novacode_cli.commands.commands import handle_command

        try:
            with _rich_console.capture() as cap:
                await handle_command(
                    text,
                    self.agent,
                    self.token_tracker,
                    self.session_state,
                    self.assistant_id,
                    model_name=self.model_name,
                    image_tracker=self.image_tracker,
                    sandbox_id=self._sandbox_id,
                    sandbox_type=self._sandbox_type,
                )
            out = cap.get()
            if out.strip():
                self._log(Text.from_ansi(out))
            # A config-changing subcommand (on/off/mode/speak/settings) must
            # rebuild the cached pipeline so the next use picks up the new config.
            # test/download/status/doctor don't change config — leave the warmed
            # pipeline intact.
            sub = text.split(maxsplit=1)
            subcmd = sub[1].split()[0].lower() if len(sub) > 1 and sub[1].split() else ""
            if subcmd in ("on", "off", "mode", "speak", "settings"):
                self._voice_pipeline = None
                if hasattr(self.session_state, "_voice_pipeline"):
                    self.session_state._voice_pipeline = None
        except Exception as ex:  # noqa: BLE001 — a voice command must never crash the TUI
            self._log(Text(f"/voice failed: {ex}", style="red"))

    async def _run_model(self) -> None:
        """Native /model: choose provider + model, store key, hot-swap the agent."""
        import os

        from novacode_cli.config.model_manager import MODEL_PRESETS, ModelManager

        mm = ModelManager()
        configured = {pid for pid, _ in mm.get_available_providers()}
        current_id = mm.get_current_provider_id()

        saved_base_url = None
        try:
            from novacode_cli.config.nova_config import NovaConfig

            saved_base_url = NovaConfig().get_model_base_url()
        except Exception:  # noqa: BLE001 — prefill is a convenience
            saved_base_url = None

        result = await self.push_screen_wait(
            ModelScreen(current_id, configured, current_base_url=saved_base_url)
        )
        if not result:
            return

        provider = result["provider"]
        preset = MODEL_PRESETS[provider]
        model = result["model"] or preset["default_model"]
        key = result["api_key"]
        base_url = (result.get("base_url") or "").strip()

        # Ensure the API key is present in the environment (model creation reads
        # os.environ). Store a newly entered key in the keychain.
        if preset["requires_api_key"]:
            if key:
                from novacode_cli.onboarding import SecretManager

                SecretManager().store_secret(preset["api_key_var"].lower(), key)
                os.environ[preset["api_key_var"]] = key
            elif not mm.resolve_api_key(provider) and not base_url:
                # resolve_api_key exports a keychain/env key when it exists.
                # A custom endpoint (LM Studio, vLLM, a local proxy) usually
                # needs no key, so don't block the switch on one.
                self._log(Text(f"{preset['name']} requires an API key.", style="red"))
                return

        mm.set_provider(provider, model, base_url or None)
        try:
            from novacode_cli.config.model_create import create_model

            new_model = create_model()
            new_agent, new_backend = await self.session_state.switch_model(new_model)
            self.agent = new_agent
            self.backend = new_backend
            self.model_name = getattr(new_model, "model_name", None) or getattr(
                new_model, "model", "unknown"
            )
            # Keep the recorded provider in step with the live model, so the
            # next save records what the user actually switched to and a later
            # resume restores it rather than the pre-switch model.
            self._model_provider = provider
            if self.token_tracker is not None:
                try:
                    self.token_tracker.set_model(self.model_name)
                except Exception:  # noqa: BLE001
                    pass
            self._set_status("ready")
            self._refresh_info_bar()  # reflect the new model in the footer at once
            self._log(
                Text(
                    f"✓ Switched to {preset['name']} · {self.model_name}",
                    style="green",
                )
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Model switch failed: {ex}", style="red"))

    async def _run_sessions(self) -> None:
        """Open the saved-sessions screen (list + delete)."""
        from novacode_cli.session.session_persistence import SessionManager

        sm = self.session_manager or SessionManager()
        await self.push_screen_wait(
            SessionsScreen(sm, getattr(self.session_state, "session_id", None))
        )

    async def _run_resume(self, text: str) -> None:
        """Resume a saved session for the current path.

        ``/resume``       → pick from sessions saved for THIS workspace.
        ``/resume <id>``  → resume that session id directly (any path).

        The current conversation is auto-saved first, then replaced by the
        chosen session's history and identity on a clean, fresh thread — the
        same continuation build the ``nova --continue`` startup path uses.
        """
        if self.session_manager is None:
            self._log(Text("Session resume is unavailable.", style="yellow"))
            return

        from novacode_cli.config.config import (
            get_default_coding_instructions,
            settings,
        )
        from novacode_cli.session.session_prompt_builder import (
            build_continuation_prompt,
            load_NOVA_md,
        )
        from novacode_cli.session.session_restore import (
            _truncate,
            format_session_age,
            restore_session,
        )
        from novacode_cli.tracking.workspace_anchoring import scan_workspace

        sm = self.session_manager
        workspace_root = settings.get_workspace_root()
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Resolve the target session id: explicit arg, else a cwd-filtered picker.
        if arg:
            target_id: str | None = arg
        else:
            ws = str(workspace_root)
            current_id = getattr(self.session_state, "session_id", None)
            sessions = [
                s
                # Reads every session's metadata file: off the UI loop.
                for s in await asyncio.to_thread(sm.list_sessions, limit=200)
                if s.project_root == ws and s.session_id != current_id
            ][:20]
            if not sessions:
                self._log(
                    Text(
                        "No saved sessions for this path yet. "
                        "(Sessions autosave as you work; use /save to force one.)",
                        style="yellow",
                    )
                )
                return
            options = [
                f"{s.session_id[:8]}  ({s.model_name or 'unknown'}) · "
                f"{s.message_count} msgs · {format_session_age(s.last_active)} · "
                f"{_truncate(getattr(s, 'current_task', None), 40) or '—'}"
                for s in sessions
            ]
            idx = await self.push_screen_wait(
                PickScreen(
                    "Resume a session (this path)",
                    options,
                    hint="↑/↓ select · Enter resume · Esc cancel",
                )
            )
            if not (0 <= idx < len(sessions)):
                return
            target_id = sessions[idx].session_id

        # Load the session and build the continuation prompt off the event loop
        # (mirrors the --continue startup path in main.py).
        result = await asyncio.to_thread(restore_session, sm, target_id, workspace_root)
        if not result:
            self._log(Text(f"Session '{target_id[:8]}' not found.", style="yellow"))
            return
        session_data, warnings = result

        recent_messages, current_workspace, nova_md = await asyncio.gather(
            asyncio.to_thread(sm.load_recent_messages, session_data.meta.session_id),
            asyncio.to_thread(scan_workspace, workspace_root),
            asyncio.to_thread(load_NOVA_md, workspace_root),
        )
        session_data.messages = list(session_data.messages or []) or recent_messages
        initial_messages = build_continuation_prompt(
            session_data=session_data,
            system_prompt=get_default_coding_instructions(),
            NOVA_md_content=nova_md,
            workspace_state=current_workspace,
        )

        # Auto-save the CURRENT conversation before replacing it, then take a
        # clean slate (fresh thread_id → empty checkpointer) and adopt the
        # resumed session's identity + todos.
        await self._save_session()
        resumed_id = session_data.meta.session_id
        resumed_todos = session_data.todos
        self.session_state.reset_conversation()  # fresh thread_id, cleared state
        self.session_state.session_id = resumed_id
        self.session_state.is_continued = True
        if resumed_todos:
            self.session_state.todos = resumed_todos

        # Re-own any live sandbox to the resumed session id so the orphan sweep
        # never reclaims a container this chat still uses (mirrors /clear).
        if self._sandbox_id:
            try:
                from novacode_cli.integrations import sandbox_registry

                sandbox_registry.retie(self._sandbox_id, resumed_id)
            except Exception:  # noqa: BLE001
                pass

        # Seed the fresh thread with the continuation history.
        config = {"configurable": {"thread_id": self.session_state.thread_id}}
        try:
            await self.agent.aupdate_state(config, values={"messages": initial_messages})
        except Exception as exc:  # noqa: BLE001
            self._log(Text(f"Resume failed while seeding state: {exc}", style="red"))
            return

        # Reset per-conversation UI/tracking and re-baseline token accounting.
        self._reset_streaming()
        self._clear_live_steers()
        self._seen.clear()
        if self.token_tracker is not None:
            try:
                self.token_tracker.reset()
            except Exception:  # noqa: BLE001
                pass

        # Replace the transcript with the resumed session's recent history.
        await self._transcript().remove_children()
        self._restored_messages = list(recent_messages or [])
        self._replay_history()
        # Paint the resumed checklist. Previously the restored todos only
        # reached session_state and nothing rendered them, so a resumed
        # session showed no todos until the agent next emitted an update.
        self._todos = list(resumed_todos or [])
        self._todos_agent = None
        self._paint_todos(self._todos)
        self._update_mode_badge()
        self._refresh_status()

        for w in warnings:
            self._log(Text(f"⚠ {w}", style="yellow dim"))
        self._log(
            Text(
                f"✓ Resumed session {resumed_id[:8]} — "
                f"{len(recent_messages or [])} recent message(s) replayed.",
                style="green",
            )
        )

    async def _run_artifacts(self) -> None:
        """Open the artifacts list (same as clicking the ◈ Artifacts component)."""
        self._open_artifacts_list()

    async def _run_tasks(self) -> None:
        """Open the Background Tasks panel (same as clicking the ⚙ indicator)."""
        self._open_tasks_panel()

    async def _run_cowork(self, text: str) -> None:
        """Launch (or focus) the Nova Cowork desktop app in the browser.

        Reuses Nova's FastAPI agent server + the /cowork UI + WorkspacePolicy
        broker. Server startup is heavy, so it runs off the event loop.
        """
        parts = text.split(maxsplit=1)
        task = parts[1].strip() if len(parts) > 1 else None
        self._log(Text("◆ Launching Nova Cowork desktop… (grant a folder to begin)", style="#7aa2f7"))
        self._launch_cowork(task)

    @work(thread=True, exclusive=True, group="cowork")
    def _launch_cowork(self, task: str | None) -> None:
        try:
            from novacode_cli.cowork.launcher import cowork_url

            sid = getattr(self.session_state, "session_id", None)
            url = cowork_url(session_id=sid, task=task)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log, Text(f"Cowork failed to launch: {e}", style="red"))
            return
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        self.call_from_thread(
            self._log,
            Text.assemble(("◆ Nova Cowork ready — ", "bold #7aa2f7"), (url, "underline #7aa2f7")),
        )

    async def _run_mcp(self) -> None:
        """Open the MCP servers screen (view + remove)."""
        await self.push_screen_wait(McpScreen())

    def _build_init_agent(self) -> tuple[Any, Any]:
        """Build a dedicated **no-HITL, LOCAL-filesystem** agent for /init.

        Two deliberate deviations from the session agent:

        1. ``auto_approve=True`` → ``interrupt_on={}``: the main agent AND every
           subagent (incl. deepagents' auto-injected general-purpose one) run
           tools without approval interrupts. A subagent's HITL interrupt is
           unresolvable (it bubbles out of `task`'s ainvoke as a GraphInterrupt),
           so this is required for /init's `task` workers to run unattended.

        2. ``sandbox=None`` → a LOCAL FilesystemBackend rooted at the project
           (virtual_mode). /init is inherently a local operation: graphify reads
           the local files, and the graph fragments must be written to the local
           ``.nova/graph_fragments/`` so `_read_and_merge_fragments` can read
           them back. Running through the session's *sandbox* backend broke this
           two ways — `/`-prefixed virtual paths resolve to the container root
           (project is at ``/workspace`` → every read_file 404'd), and any
           fragment write would land inside the sandbox, invisible to the local
           merge step. Forcing the local backend fixes both.

        Reuses the session model/tools/store. Raises if no model is configured.
        """
        from novacode_cli.agents.core_agent import create_agent_with_config

        ss = self.session_state
        model = getattr(ss, "_model", None)
        if model is None:
            raise RuntimeError("no model configured")
        return create_agent_with_config(
            model=model,
            assistant_id=getattr(ss, "_assistant_id", None) or self.assistant_id,
            tools=getattr(ss, "_tools", None) or [],
            sandbox=None,  # ← LOCAL filesystem (see docstring #2)
            sandbox_type=None,
            store=getattr(ss, "_store", None),
            checkpointer=getattr(ss, "_checkpointer", None),
            auto_approve=True,  # ← no HITL anywhere (see docstring #1)
            session_id=getattr(ss, "session_id", None) or getattr(ss, "thread_id", None),
        )

    async def _run_init(self, text: str) -> None:
        """Generate NOVA.md: delegates orchestration to :class:`InitOrchestrator`."""
        from pathlib import Path

        from novacode_cli.commands.init_handler import InitFlags, InitOrchestrator
        from novacode_cli.config.config import settings

        cmd_args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else None
        project_root = settings.project_root
        if not project_root:
            self._log(Text("/init requires a project with a .git directory.", style="yellow"))
            return
        nova_md_path = Path(project_root) / ".nova" / "NOVA.md"
        self._log(Text(f"🔍 Initializing NOVA.md for {Path(project_root).name}…", style="bold"))

        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("exploring codebase…")
        _prev_auto = self.session_state.auto_approve
        self.session_state.auto_approve = True

        try:
            renderer = TuiInitRenderer(self)
            orchestrator = InitOrchestrator(
                project_root=Path(project_root),
                nova_md_path=nova_md_path,
                flags=InitFlags(cmd_args),
                renderer=renderer,
                agent=self.agent,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
                token_tracker=self.token_tracker,
            )
            await orchestrator.run()
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/init failed: {ex}", style="red"))
        finally:
            self.session_state.auto_approve = _prev_auto
            self._turn_active = False
            self._set_status("ready")

        if nova_md_path.exists():
            self._log(Text(f"✓ NOVA.md ready → {nova_md_path}", style="green"))

    async def _run_trace(self, text: str) -> None:
        """Native LangSmith tracing: status / enable / disable / projects / traces."""
        import os

        parts = text.split()
        args = parts[1:]
        sub = parts[1].lower() if len(parts) > 1 else "status"
        from novacode_cli.tracking.tracing import (
            configure_tracing,
            get_traces,
            get_tracing_config,
            get_tracing_status,
            list_projects,
        )

        if sub in ("enable", "on"):
            api_key = args[1] if len(args) > 1 and not args[1].startswith("-") else None
            project_name = None
            for i, a in enumerate(args):
                if a == "--project" and i + 1 < len(args):
                    project_name = args[i + 1]
                    break
            cfg = configure_tracing(api_key=api_key, project_name=project_name, enable=True)
            if cfg.is_configured():
                t = Text()
                t.append("✓ LangSmith tracing enabled\n", style="green")
                t.append(f"  project: {cfg.project_name}\n", style="dim")
                t.append("  set LANGSMITH_API_KEY in .env to persist\n", style="dim")
                self._log(t)
            else:
                self._log(
                    Text(
                        "Failed to enable tracing — LANGSMITH_API_KEY is required.",
                        style="red",
                    )
                )
            return
        if sub in ("disable", "off"):
            os.environ["LANGSMITH_TRACING"] = "false"
            self._log(
                Text(
                    "✓ LangSmith tracing disabled for this session "
                    "(set LANGSMITH_TRACING=false in .env to persist).",
                    style="green",
                )
            )
            return
        if sub == "projects":
            projects = list_projects()
            t = Text()
            t.append("LangSmith projects\n", style="bold")
            if projects:
                for p in projects[:20]:
                    t.append(f"  {p['name']}", style="cyan")
                    t.append(f"  {p['url']}\n", style="dim")
            else:
                t.append("  (none found or tracing not configured)\n", style="dim")
            self._log(t)
            return
        if sub in ("traces", "recent"):
            limit = 10
            for i, a in enumerate(args):
                if a in ("-n", "--limit") and i + 1 < len(args):
                    try:
                        limit = int(args[i + 1])
                    except ValueError:
                        pass
            traces = get_traces(limit=limit)
            t = Text()
            t.append(f"Recent traces (last {limit})\n", style="bold")
            if traces:
                for tr in traces[:limit]:
                    created = (tr.get("created_at", "") or "")[:19] or "unknown"
                    inputs = str(tr.get("inputs", {}))[:40]
                    t.append(f"  {tr['name']}", style="cyan")
                    t.append(f"  {created}  {inputs}\n", style="dim")
            else:
                t.append(
                    "  (none — make a request with tracing enabled first)\n",
                    style="dim",
                )
            self._log(t)
            return
        if sub in ("-h", "--help", "help"):
            t = Text()
            t.append("/trace — LangSmith tracing\n", style="bold")
            for name, desc in (
                ("status", "show current tracing configuration"),
                ("enable [KEY] [--project P]", "enable tracing"),
                ("disable", "disable tracing for this session"),
                ("projects", "list LangSmith projects"),
                ("traces [--limit N]", "show recent traces"),
            ):
                t.append(f"  /trace {name}\n", style="cyan")
                t.append(f"      {desc}\n", style="dim")
            self._log(t)
            return
        if sub not in ("status", ""):
            self._log(Text(f"Unknown trace subcommand: {sub} (try /trace help)", style="yellow"))
            return

        st = get_tracing_status()
        t = Text()
        t.append("LangSmith tracing\n", style="bold")
        if not st.get("available"):
            t.append("  langsmith not installed\n", style="dim")
        elif st.get("configured"):
            cfg = get_tracing_config()
            t.append("  ● enabled\n", style="green")
            t.append(f"  project: {cfg.project_name}\n", style="dim")
            if getattr(cfg, "workspace_id", None):
                t.append(f"  workspace: {cfg.workspace_id}\n", style="dim")
            t.append("  view: https://smith.langchain.com\n", style="dim")
        else:
            t.append("  ○ not configured\n", style="yellow")
            t.append("  set LANGSMITH_API_KEY and LANGSMITH_TRACING=true\n", style="dim")
        self._log(t)

    async def _run_log(self, text: str) -> None:
        """Native recent-runs list and `/log show <id>` detail."""
        from novacode_cli.commands.log_commands import (
            _list_runs,
            _load_json,
            _runs_dir,
        )

        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "list"
        ws = str(getattr(self.session_state, "workspace_root", "") or "") or None
        runs_dir = _runs_dir(ws)

        if sub == "show" and len(parts) > 2:
            run_id = parts[2]
            matches = [p for p in _list_runs(runs_dir) if p.name.startswith(run_id)]
            if not matches:
                self._log(Text(f"No run matching '{run_id}'", style="red"))
                return
            run_dir = matches[0]
            t = Text()
            t.append(f"Run: {run_dir.name}\n", style="bold")
            for label, fname in (
                ("Meta", "meta.json"),
                ("Summary", "summary.json"),
                ("Verdict", "user_verdict.json"),
            ):
                data = _load_json(run_dir / fname)
                if data:
                    t.append(f"\n{label}\n", style="bold")
                    for k, v in data.items():
                        t.append(f"  {k}: {v}\n", style="dim")
            self._log(t)
            return

        if sub == "grep":
            import re as _re

            if len(parts) < 3:
                self._log(Text("Usage: /log grep <pattern>", style="red"))
                return
            pattern = parts[2]
            limit = 50
            for i, a in enumerate(parts):
                if a == "--limit" and i + 1 < len(parts):
                    try:
                        limit = int(parts[i + 1])
                    except ValueError:
                        pass
            try:
                rx = _re.compile(pattern, _re.IGNORECASE)
            except _re.error as e:
                self._log(Text(f"Invalid pattern: {e}", style="red"))
                return
            t = Text()
            t.append(f"grep '{pattern}'\n", style="bold")
            hits = 0
            for run_dir in _list_runs(runs_dir):
                turns_dir = run_dir / "turns"
                if not turns_dir.exists():
                    continue
                for turn in sorted(turns_dir.iterdir()):
                    for fname in ("prompt.txt", "response.json"):
                        fpath = turn / fname
                        if not fpath.exists():
                            continue
                        content = fpath.read_text(encoding="utf-8", errors="replace")
                        for lineno, line in enumerate(content.splitlines(), 1):
                            if rx.search(line):
                                t.append(
                                    f"  {run_dir.name[:16]}/{turn.name}/{fname}:{lineno}  ",
                                    style="dim",
                                )
                                t.append(f"{line.strip()[:120]}\n")
                                hits += 1
                                if hits >= limit:
                                    t.append(f"  … stopped at {limit} hits\n", style="dim")
                                    self._log(t)
                                    return
            if hits == 0:
                t.append(f"  (no matches for '{pattern}')\n", style="dim")
            self._log(t)
            return

        if sub not in ("list", ""):  # diff / verdict / frontier
            await self._passthrough_command(text)
            return

        # /log list → recent interactive SESSIONS. The .nova/runs/ turn format the
        # other subcommands read is only produced by the offline eval harness;
        # interactive work is recorded under ~/.nova/sessions instead, so list
        # from there (that's what "recent runs" means in normal use).
        from novacode_cli.session.session_persistence import SessionManager

        sm = self.session_manager or SessionManager()
        try:
            sessions = await asyncio.to_thread(sm.list_sessions, limit=20)
        except Exception:  # noqa: BLE001
            sessions = []
        t = Text()
        t.append("Recent sessions\n", style="bold")
        if not sessions:
            t.append("  (no sessions yet — start chatting to record one)\n", style="dim")
        else:
            for s in sessions:
                t.append(f"  {self._session_log_line(s)}\n", style="dim")
        self._log(t)

    @staticmethod
    def _session_log_line(s: Any) -> str:
        """One-line summary of a saved session for /log list."""
        sid = (getattr(s, "session_id", "") or "")[:8]
        model = getattr(s, "model_name", None) or "?"
        msgs = getattr(s, "message_count", 0)
        status = getattr(s, "task_status", "") or ""
        when = getattr(s, "last_active", "") or ""
        try:
            from datetime import datetime

            when = datetime.fromisoformat(when).strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            when = when[:16]
        task = getattr(s, "current_task", None)
        task_str = f"  · {task[:40]}" if task else ""
        return f"{sid}  {when}  msgs={msgs}  {status}  {model}{task_str}"

    # -- plan mode ------------------------------------------------------------
    def _active_agent(self) -> tuple[Any, Any]:
        """Route to the plan agent while plan mode is active, else the main agent.

        During /init a dedicated no-HITL agent (``_init_agent``) takes priority so
        the pipeline's `task` subagents can read/write files unattended — the
        shared session agent gates those tools and a subagent's interrupt is
        unresolvable (it bubbles out of `task`'s ainvoke as a GraphInterrupt).
        """
        init_agent = getattr(self, "_init_agent", None)
        if init_agent is not None:
            return init_agent, getattr(self, "_init_backend", None)
        if getattr(self.session_state, "plan_mode_enabled", False) and (
            getattr(self.session_state, "plan_agent", None) is not None
        ):
            return self.session_state.plan_agent, getattr(self.session_state, "plan_backend", None)
        return self.agent, self.backend

    async def _enable_plan_mode(self) -> bool:
        try:
            from novacode_cli.agents.plan_agent import create_plan_agent_with_config
            from novacode_cli.tools.plan_mode_tools import (
                ask_user_question,
                enter_plan_mode,
                exit_plan_mode,
            )

            model = getattr(self.session_state, "_model", None)
            if model is None:
                self._log(Text("Plan mode needs a model; none configured.", style="red"))
                return False
            plan_agent, plan_backend = create_plan_agent_with_config(
                model=model,
                assistant_id=getattr(self.session_state, "_assistant_id", None) or "nova",
                tools=[ask_user_question, enter_plan_mode, exit_plan_mode],
                steering_instructions=getattr(self.session_state, "steering_instructions", None),
                auto_approve=getattr(self.session_state, "auto_approve", False),
                # Share the core agent's checkpointer + store so plan mode sees
                # the ongoing conversation (same thread_id) and persists.
                checkpointer=getattr(self.session_state, "_checkpointer", None),
                store=getattr(self.session_state, "_store", None),
            )
            self.session_state.plan_mode_enabled = True
            self._update_mode_badge()
            self.session_state.plan_content = None
            self.session_state.approved_plan_content = None
            if not getattr(self.session_state, "auto_approve", False):
                self.session_state.auto_approve = False
            self.session_state.plan_agent = plan_agent
            self.session_state.plan_backend = plan_backend
            return True
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Plan mode failed: {ex}", style="red"))
            self.session_state.plan_mode_enabled = False
            self._update_mode_badge()
            return False

    async def _maybe_run_approved_plan(self) -> None:
        """After a plan-mode turn, execute an approved plan via the main agent.

        Clears the agent's conversation context first (fresh thread_id, empty
        checkpointer state) so execution starts with a clean slate — just the
        plan, no history from the planning conversation. The TUI transcript is
        also cleared so the user sees a fresh execution view.
        """
        try:
            approved = self.session_state.consume_approved_plan()
        except Exception:  # noqa: BLE001
            approved = None
        try:
            if approved:
                try:
                    self.session_state.clear_plan_agent()
                except Exception:  # noqa: BLE001
                    pass

                # Clear conversation context so the agent starts fresh with
                # only the plan execution instruction — no planning history.
                self.session_state.reset_conversation()
                await self._transcript().remove_children()
                self._show_home_banner()
                self._log(Text("✓ Plan approved — starting fresh execution…", style="cyan"))
                _tid = getattr(self.session_state, "thread_id", "?")
                self._log(Text(f"[plan-debug] executing on thread {str(_tid)[:8]}, plan {len(approved)} chars", style="dim"))
                await self._stream_prompt(
                    "The user has approved the following plan. Execute it step by step, "
                    "marking each step complete as you go:\n\n" + approved
                )
                try:
                    _st = await self.agent.aget_state({"configurable": {"thread_id": _tid}})
                    _msgs = _st.values.get("messages", []) if _st else []
                    _kinds = [type(m).__name__ for m in _msgs][-6:]
                    self._log(Text(f"[plan-debug] after execution: {len(_msgs)} msgs, last={_kinds}", style="dim"))
                except Exception as _ex:  # noqa: BLE001
                    self._log(Text(f"[plan-debug] state read failed: {_ex}", style="dim red"))
        finally:
            # "Auto-approve edits" was scoped to this plan run — restore, so
            # the NEXT plan prompts for approval again instead of silently
            # self-approving (see the plan-approval modal handler).
            if getattr(self, "_plan_scoped_auto_approve", False):
                self._plan_scoped_auto_approve = False
                self.session_state.auto_approve = False

    async def _run_plan(self, text: str) -> None:
        """Native /plan: status / off / enable (+ optional prompt)."""
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        low = args.lower()
        if low == "status":
            enabled = getattr(self.session_state, "plan_mode_enabled", False)
            self._log(
                Text(
                    f"Plan mode: {'enabled' if enabled else 'disabled'}",
                    style="cyan" if enabled else "dim",
                )
            )
            return
        if low == "off":
            self.session_state.plan_mode_enabled = False
            self._update_mode_badge()
            try:
                self.session_state.clear_plan_agent()
            except Exception:  # noqa: BLE001
                pass
            self._log(Text("Plan mode disabled.", style="yellow"))
            return
        if not await self._enable_plan_mode():
            return
        self._log(
            Text(
                "▌ Plan mode is Active",
                style="cyan",
            )
        )
        if args:
            await self._stream_prompt(args)
            await self._maybe_run_approved_plan()

    async def _run_goal(self, text: str) -> None:
        """Set, show, or clear the active goal for autonomous goal-mode execution.

        Usage:
          /goal <description>   — set the goal and kick off the agent
          /goal status          — show the current goal
          /goal clear           — remove the active goal
        """
        from novacode_cli.commands.side_commands import handle_goal_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        # Shared parse + state mutation (kept identical across TUI / REPL / remote).
        result = handle_goal_command(self.session_state, args)

        if result.action == "status":
            if result.goal:
                t = Text()
                t.append("🎯 Active goal\n", style="bold #e0af68")
                t.append(result.goal, style="italic")
                self._log(t)
            else:
                self._log(Text(result.message, style="dim"))
            return

        if result.action == "clear":
            self._update_mode_badge()
            self._log(Text("Goal cleared.", style="yellow"))
            return

        if result.action == "usage":
            self._log(Text(result.message, style="dim"))
            return

        # action == "set"
        self._update_mode_badge()
        t = Text()
        t.append("🎯 Goal set\n", style="bold #e0af68")
        t.append(result.goal or "", style="italic")
        self._log(t)

        if result.kickoff:
            await self._stream_prompt(result.kickoff)

    # -- btw (concurrent side-channel question with web search) ----------------

    def _get_btw_agent(self) -> Any:
        """Return the cached btw agent (shared process-wide with the remote bridge)."""
        from novacode_cli.commands.side_commands import get_btw_agent

        return get_btw_agent()

    async def _run_btw(self, text: str) -> None:
        """Dispatch a /btw side question — runs concurrently with the main agent."""
        import uuid

        parts = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""

        if not question:
            self._log(Text("Usage: /btw <question>", style="dim"))
            return

        try:
            agent = self._get_btw_agent()
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"↩ btw: could not start web-search agent — {ex}", style="red"))
            return

        thread_id = f"btw-{uuid.uuid4().hex[:12]}"
        self._btw_worker(question, agent, thread_id)

    @work(group="btw")
    async def _btw_worker(self, question: str, agent: Any, thread_id: str) -> None:
        """Run the btw question on its own thread, concurrently with the main agent.

        Uses a dedicated ``group="btw"`` work group so it never blocks — or is
        blocked by — the main ``group="turn"`` agent. Multiple /btw calls queue
        within the "btw" group and run one at a time (sequential but not blocking
        the main UI).
        """
        from novacode_cli.agent_stream import run_agent_stream
        from novacode_cli.ui_events import AssistantMessage, Done, Error

        # Minimal session-state shim: only thread_id matters for the btw agent
        # (no checkpointer sharing, no goal/plan injection).
        class _BtwSession:
            def __init__(self) -> None:
                self.thread_id = thread_id
                self.active_goal: str | None = None
                self.plan_mode_enabled: bool = False
                self.auto_approve: bool = True

        btw_state = _BtwSession()

        q_short = question if len(question) <= 55 else question[:52] + "…"
        # Show a transient "btw thinking" note while the request is in flight.
        indicator = Static(
            Text(f"↩ btw: {q_short}…", style="dim italic"),
            classes="logline",
        )
        self._close_tool_group()
        await self._transcript().mount(indicator)
        self._scroll_end()

        answer_parts: list[str] = []
        try:
            async for e in run_agent_stream(
                question,
                agent,
                "btw-agent",
                btw_state,
                backend=None,
                seen_message_ids=set(),
            ):
                if isinstance(e, AssistantMessage):
                    answer_parts.append(e.text)
                elif isinstance(e, (Done, Error)):
                    break
                # ToolCall / ToolResult / StatusUpdate / TextDelta — silently
                # consumed; tool activity is invisible to the user by design.
        except asyncio.CancelledError:
            indicator.remove()
            return
        except Exception as ex:  # noqa: BLE001
            indicator.update(Text(f"↩ btw failed: {ex}", style="red"))
            return

        # Replace the "thinking" indicator with the finished answer card.
        answer = "\n\n".join(answer_parts).strip() or "(no response)"
        title_q = question if len(question) <= 50 else question[:47] + "…"
        body = Static(Markdown(answer), classes="btw-body")
        card = Collapsible(body, title=f"↩ btw: {title_q}", collapsed=False)
        card.add_class("btw-card")
        await indicator.remove()
        self._close_tool_group()
        await self._transcript().mount(card)
        self._prune_transcript()
        self._scroll_end()

    async def _run_compact(self, text: str) -> None:
        """Compact the conversation natively (spinner + result component)."""
        from novacode_cli.compaction import compact_conversation
        from novacode_cli.config.model_create import create_model

        parts = text.split(maxsplit=1)
        focus = parts[1].strip() if len(parts) > 1 else None
        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("compacting…")
        try:
            # Summarize with the SESSION's live model (honors an in-session /model
            # switch), falling back to config only if none is set.
            model = getattr(self.session_state, "_model", None) or create_model()
            # Resolve the agent dir so durable learnings can be persisted to memory.
            agent_dir = None
            try:
                from novacode_cli.config.config import settings

                if getattr(self, "assistant_id", None):
                    agent_dir = settings.get_agent_dir(self.assistant_id)
            except Exception:  # noqa: BLE001 — persistence is best-effort
                agent_dir = None
            result = await compact_conversation(
                agent=self.agent,
                model=model,
                thread_id=self.session_state.thread_id,
                focus_instructions=focus,
                # The tracker's effective window (accounts for Ollama num_ctx) so
                # the summarizer input is budgeted to what this model can accept.
                context_window=getattr(self.token_tracker, "context_window_size", None),
                agent_dir=agent_dir,
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"/compact failed: {ex}", style="red"))
            return
        finally:
            self._turn_active = False
            self._set_status("ready")
        if getattr(result, "success", False):
            if self.token_tracker is not None:
                try:
                    # reset() clears the stale pre-compaction peak; recompute the
                    # breakdown from the actual post-compaction messages so ctx%
                    # is accurate immediately (not just after the next turn).
                    self.token_tracker.reset()
                    await self._update_context_breakdown()
                except Exception:  # noqa: BLE001
                    pass
                self._refresh_status()
            # A compaction is a one-off housekeeping event, not conversation
            # content: it collapses to a single line so it never buries the
            # transcript, and expands on demand for the stats and the summary.
            learnings = getattr(result, "learnings", "") or ""
            summary = getattr(result, "summary", "") or ""
            body = Text()
            body.append(
                f"messages: {result.messages_before} → {result.messages_after}\n",
                style="dim",
            )
            body.append(f"tokens saved: ~{result.tokens_saved:,}\n", style="dim")
            if learnings:
                # `learnings` is the summary text itself, written as ONE memory
                # entry — counting its newlines reported "42 learnings" for a
                # 42-line summary, which overstated what was saved.
                body.append("🧠 summary preserved to memory\n", style="green")
            if summary:
                body.append(
                    "\n" + summary[:400] + ("…" if len(summary) > 400 else ""),
                    style="dim italic",
                )
            card = Collapsible(
                Static(body, classes="compact-body"),
                title=(
                    f"✓ Conversation compacted  ·  "
                    f"{result.messages_before} → {result.messages_after} msgs  ·  "
                    f"~{result.tokens_saved:,} tokens saved"
                ),
                collapsed=True,
            )
            card.add_class("compact-card")
            self._close_tool_group()
            await self._transcript().mount(card)
            self._prune_transcript()
            self._scroll_end(force=False)
        else:
            self._log(
                Text(
                    f"Compaction skipped: {getattr(result, 'error', '') or 'unknown'}",
                    style="yellow",
                )
            )

    async def _run_save(self) -> None:
        """Manually persist the session (native confirmation)."""
        if self.session_manager is None:
            self._log(Text("Session saving is unavailable.", style="yellow"))
            return
        await self._save_session()
        sid = str(getattr(self.session_state, "session_id", "") or "")[:8]
        self._log(
            Text(
                f"✓ Session saved — resume with:  nova --continue {sid}",
                style="green",
            )
        )

    async def _run_clear(self) -> None:
        """Start a fresh chat — a total reset. Save the current conversation first.

        Clearing only the transcript would leave the agent's full history in the
        checkpointer (same thread_id), so it would still "remember" everything.
        A real reset assigns a new thread_id + session_id (fresh checkpointer
        state) AND clears every piece of carried-over context — todos, steering
        instructions, and plan mode — then drops per-conversation UI/tracking
        state and re-baselines token usage. Long-term memory, the Nova learning
        store, and the agent itself are preserved. The previous conversation is
        saved first so nothing is lost.
        """
        saved = self.session_manager is not None
        # Preserve the current conversation under its existing id, but mark it
        # cleared so neither --continue nor the --resume picker brings it back.
        await self._save_session(cleared=True)
        # Distil this conversation's un-reviewed work before it's gone.
        await self._consolidate_learning()
        # Belt-and-suspenders: explicitly mark the session cleared even if the
        # save above early-returned (e.g. the checkpointer read timed out or had
        # no messages) but a prior /save had already written it as not-cleared.
        if self.session_manager is not None:
            try:
                self.session_manager.mark_cleared(self.session_state.session_id)
            except Exception:  # noqa: BLE001
                pass

        # Total reset of session/conversation state: new thread+session id
        # (empty checkpointer state), cleared todos / steering / plan mode.
        self.session_state.reset_conversation()

        # Re-own the live sandbox to the new session so resume reconnects to it
        # and the orphan sweep never reclaims a container the new chat still uses
        # (the container's Docker label is immutable; the registry is the source
        # of truth for ownership).
        if self._sandbox_id:
            try:
                from novacode_cli.integrations import sandbox_registry

                sandbox_registry.retie(self._sandbox_id, self.session_state.session_id)
            except Exception:  # noqa: BLE001
                pass

        # Drop per-conversation UI/tracking state.
        self._reset_streaming()
        self._clear_live_steers()
        self._todos = []
        self._todos_agent = None
        self._paint_todos(None)
        self._seen.clear()
        self._restored_messages = []
        # Discard anything queued for the (now-cleared) conversation.
        self._deferred_commands.clear()
        self._deferred_prompts.clear()
        # Stale background-task completion notes must not bleed into the new chat.
        self._pending_job_notes.clear()
        # Attached images are conversation context (re-attached to every turn) —
        # clear them so the fresh chat doesn't inherit the old conversation's images.
        if self.image_tracker is not None:
            try:
                self.image_tracker.clear()
            except Exception:  # noqa: BLE001
                pass

        await self._transcript().remove_children()

        # Refresh the home screen: re-show the ASCII-art banner so /clear looks
        # like a fresh launch, not just an empty transcript.
        self._show_home_banner()

        # Re-baseline context/token accounting for the fresh chat.
        if self.token_tracker is not None:
            try:
                self.token_tracker.reset(reset_session=True)
            except Exception:  # noqa: BLE001
                pass
        # Plan/steer were cleared above — refresh the input badge to match.
        self._update_mode_badge()
        self._refresh_status()

        self._log(
            Text(
                "✓ Started a new chat" + (" — previous conversation saved." if saved else "."),
                style="green",
            )
        )

    def _show_home_banner(self) -> None:
        """Render the home banner: the NOVA ASCII logo composited over the rain.

        The ASCII art (from ``config.get_responsive_ascii``, sized to the live
        terminal width) is stamped on top of the Matrix rain inside a single
        :class:`MatrixRain` widget, so the rain falls *behind* the logo and the
        logo is tinted with the active TUI theme color.
        """
        try:
            from novacode_cli.config.config import get_responsive_ascii

            try:
                width = self.size.width or None
            except Exception:  # noqa: BLE001
                width = None
            art = get_responsive_ascii(width=width)

            rain = MatrixRain(art=art, width=width)
            self._home_banner = rain
            self._transcript().mount(rain)
            self._prune_transcript()
        except Exception:  # noqa: BLE001
            self._home_banner = None

    def on_resize(self, event: events.Resize) -> None:
        """Reflow the home banner and apply responsive breakpoints on resize.

        The rain grid width and the ASCII-art size variant are chosen from the
        terminal width, so on resize we re-pick the art variant and re-grid the
        rain. Only acts while the banner is still on screen (home screen).
        """
        self._apply_responsive_layout(event)
        rain = self._home_banner
        if not isinstance(rain, MatrixRain) or not rain.is_mounted:
            return
        try:
            from novacode_cli.config.config import get_responsive_ascii

            size = getattr(event, "size", None)
            width = (size.width if size else self.size.width) or None
            rain.reflow(get_responsive_ascii(width=width), width)
        except Exception:  # noqa: BLE001
            pass

    def _apply_responsive_layout(self, event: events.Resize) -> None:
        """Toggle breakpoint classes from the terminal size.

        - ``narrow`` (width < _NARROW_WIDTH): the screen sheds its widest info
          columns (CSS) and the status line drops its right-side counts.
        - ``tiny`` (below the _MIN_* floor): the layout can't fit; surface a
          one-shot notice rather than render a broken, clipped screen.

        Only repaints when a breakpoint actually flips, so a drag-resize that
        stays in one band costs nothing extra.
        """
        from contextlib import suppress

        width = event.size.width or 0
        height = event.size.height or 0
        narrow = width < _NARROW_WIDTH
        tiny = width < _MIN_WIDTH or height < _MIN_HEIGHT
        if narrow == self._narrow and tiny == self._tiny:
            return
        self._narrow = narrow
        self._tiny = tiny
        with suppress(Exception):
            self.screen.set_class(narrow, "narrow")
            self.screen.set_class(tiny, "tiny")
        # Status line's right-side counts are baked into a Text (not a widget),
        # so CSS can't hide them — rebuild the tail to add/drop them.
        self._status_tail = None
        self._refresh_status()
        if tiny:
            self._set_nova_indicator(
                "⚠ terminal too small — enlarge the window", style="yellow", auto_clear=4.0
            )

    async def _run_learning(self, text: str) -> None:
        """Handle /learning natively: toggle the Hermes autonomous-learning loop.

        The loop is off by default and its middleware's ``enabled`` flag is baked
        at agent-build time, so turning it on/off rebuilds the agent (same model)
        to apply the change to the running session — mirroring /effort.
        """
        from novacode_cli.config.model_create import create_model
        from novacode_cli.config.nova_config import NovaConfig

        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        cfg = NovaConfig()
        current = cfg.get_learning_enabled()

        if arg in ("", "status"):
            t = Text()
            t.append("Nova Learning (Hermes)\n", style="bold")
            t.append("Status: ", style="dim")
            t.append("on\n" if current else "off\n", style="bold green" if current else "bold yellow")
            t.append(
                "\nWhen on, Nova periodically self-reviews its tool usage, extracts\n"
                "lessons to memory, and creates/refines skills as you work.\n",
                style="dim",
            )
            t.append("Usage: /learning <on|off>\n", style="dim")
            self._log(t)
            return

        if arg not in ("on", "off"):
            self._log(
                Text(f"Invalid option '{arg}'. Usage: /learning <on|off|status>", style="red")
            )
            return

        want = arg == "on"
        if want == current:
            self._log(Text(f"Nova learning already {arg}.", style="dim"))
            return

        cfg.set_learning_enabled(want)
        t = Text(
            f"✓ Nova learning {'enabled' if want else 'disabled'} and saved to config.\n",
            style="green",
        )

        applied = False
        if self.session_state is not None:
            try:
                new_agent, new_backend = await self.session_state.switch_model(create_model())
                self.agent = new_agent
                self.backend = new_backend
                applied = True
            except Exception as e:  # noqa: BLE001
                t.append(f"⚠ Could not apply to the running session: {e}\n", style="yellow")

        if applied:
            t.append(
                "✓ Applied to this session." if want else "✓ Stopped for this session.",
                style="green",
            )
        else:
            t.append("Takes effect on restart.", style="dim")

        self._log(t)
        self._refresh_status()

    async def _run_effort(self, text: str) -> None:
        """Handle /effort natively: set reasoning effort level."""
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli.config.model_create import create_model

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        nova_config = NovaConfig()
        current = nova_config.get("reasoning_effort", "off")

        if not args:
            t = Text()
            t.append("Reasoning Effort Configuration\n", style="bold")
            t.append(f"Current: ", style="dim")
            t.append(f"{current}\n", style="bold cyan")
            t.append("\nUsage: /effort <low|medium|high|off>\n", style="dim")
            self._log(t)
            return

        val = args.strip().lower()
        if val in ("none", "default"):
            val = "off"
        if val not in ("low", "medium", "high", "off"):
            self._log(
                Text(f"Invalid effort level '{args}'. Choose: low, medium, high, off", style="red")
            )
            return

        nova_config.set("reasoning_effort", val)
        t = Text(f"✓ Reasoning effort set to '{val}' and saved to config.\n", style="green")

        # Hot-swap the model
        if self.session_state is not None:
            try:
                new_model = create_model()
                new_agent, new_backend = await self.session_state.switch_model(new_model)
                self.agent = new_agent
                self.backend = new_backend
                self.model_name = getattr(new_model, "model_name", None) or getattr(
                    new_model, "model", "unknown"
                )
                if self.token_tracker is not None:
                    try:
                        self.token_tracker.set_model(self.model_name)
                    except Exception:  # noqa: BLE001
                        pass
                t.append("✓ Model recreated with new reasoning effort dynamically!", style="green")
            except Exception as e:
                t.append(f"⚠ Could not recreate model dynamically: {e}", style="yellow")
                t.append(
                    "\nThe change will take effect on next model switch or restart.", style="dim"
                )
        else:
            t.append("The change will take effect on restart.", style="dim")

        self._log(t)
        self._refresh_info_bar()  # the model may have been recreated — refresh footer

    async def _run_steer(self, text: str) -> None:
        """Manage persistent steering instructions natively (add/list/clear/remove)."""
        import re

        from novacode_cli.bootstrap.steering import (
            SteeringInstruction,
            classify_instruction,
        )

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        if getattr(self.session_state, "steering_instructions", None) is None:
            self.session_state.steering_instructions = []
        instr = self.session_state.steering_instructions
        low = args.lower()

        if not args or low in ("list", "ls", "show"):
            t = Text()
            t.append("Steering instructions\n", style="bold")
            if not instr:
                t.append("  (none — /steer <instruction> to add)\n", style="dim")
            else:
                for i, si in enumerate(instr, 1):
                    t.append(f"  {i}. ", style="cyan")
                    t.append(f"{si.label}: {si.instruction}\n", style="dim")
            self._log(t)
            return
        if low in ("clear", "reset"):
            n = len(instr)
            instr.clear()
            self._log(Text(f"Cleared {n} steering instruction(s).", style="green"))
            return
        m = re.match(r"(?:remove|rm|del|delete)\s+(\d+)", args, re.IGNORECASE)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(instr):
                removed = instr.pop(idx)
                self._log(Text(f"Removed: {removed.label}", style="green"))
            else:
                self._log(Text(f"Invalid index {idx + 1}.", style="yellow"))
            return
        label = classify_instruction(args)
        instr.append(SteeringInstruction(label=label, instruction=args))
        self._log(
            Text(
                f"✓ Added steering [{label}]: {args}\n"
                f"  {len(instr)} active — injected into every turn.",
                style="green",
            )
        )

    async def _run_notifications(self, text: str) -> None:
        """Native /notifications: list, dismiss <id>, approve <id>, or clear."""
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        ap = args.split(maxsplit=1)
        sub = ap[0].lower() if ap else ""
        ss = self.session_state

        if sub in ("clear", "reset"):
            # Dismiss all — reject any pending approvals.
            for n in list(ss.notifications):
                if n.action_id and not n.dismissed:
                    ss.resolve_approval(n.action_id, approve=False)
            n = ss.clear_notifications()
            self._log(Text(f"Cleared {n} notification(s).", style="green"))
            self._refresh_status()
            return
        if sub in ("dismiss", "rm", "ack") and len(ap) > 1:
            nid = ap[1].strip()
            n = self._find_notification(ss, nid)
            if n and n.action_id and not n.dismissed:
                ss.resolve_approval(n.action_id, approve=False)
                self._log(Text(f"Dismissed {nid} (rejected approval).", style="green"))
            elif ss.dismiss_notification(nid):
                self._log(Text(f"Dismissed {nid}", style="green"))
            else:
                self._log(Text(f"Notification {nid} not found", style="yellow"))
            self._refresh_status()
            return
        if sub == "approve" and len(ap) > 1:
            nid = ap[1].strip()
            n = self._find_notification(ss, nid)
            if n and n.action_id and not n.dismissed:
                if ss.resolve_approval(n.action_id, approve=True):
                    self._log(Text(f"Approved {nid}.", style="green"))
                else:
                    self._log(Text(f"Approval {nid} already resolved.", style="yellow"))
            else:
                self._log(
                    Text(
                        f"Notification {nid} not found or has no pending approval.",
                        style="yellow",
                    )
                )
            self._refresh_status()
            return

        notes = list(ss.notifications)
        colors = {
            "info": "cyan",
            "success": "green",
            "warning": "yellow",
            "error": "red",
            "approval": "magenta",
        }
        pending = ss.pending_approval_count()
        title = f"Notifications ({ss.unread_notification_count()} unread"
        if pending:
            title += f", {pending} pending approval"
        title += ")"
        t = Text()
        t.append(f"{title}\n", style="bold")
        if not notes:
            t.append("  (none yet — long-running tasks notify here)\n", style="dim")
        else:
            for n in notes:
                c = colors.get(n.level, "white")
                marker = "●" if not n.dismissed else "○"
                if n.action_id and not n.dismissed and n.action_type == "approve":
                    marker = f"⚡{marker}"
                t.append(f"  {marker} ", style=c)
                t.append(f"{n.id} ", style="dim")
                t.append(f"{n.timestamp.strftime('%H:%M:%S')} ", style="dim")
                t.append(f"[{n.source}] ", style="dim")
                t.append(f"{n.title}", style=c)
                if n.message:
                    t.append(f" — {n.message[:60]}", style="dim")
                t.append("\n")
            t.append(
                "  /notifications dismiss <id> · /notifications approve <id>"
                " · /notifications clear\n",
                style="dim",
            )
        self._log(t)

    @staticmethod
    def _find_notification(ss, nid: str) -> object | None:
        """Return the Notification with the given id, or None."""
        for n in ss.notifications:
            if n.id == nid:
                return n
        return None

    async def _tui_execute_fn(
        self,
        user_input,
        agent=None,
        assistant_id=None,
        session_state=None,
        token_tracker=None,
        backend=None,
        is_subagent=False,
        image_tracker=None,
        seen_message_ids=None,
        *,
        skip_file_mentions=False,
    ) -> None:
        await self._stream_prompt(user_input, assistant_id=assistant_id)

    async def _tui_quiet_execute_fn(
        self,
        user_input,
        agent=None,
        assistant_id=None,
        session_state=None,
        token_tracker=None,
        backend=None,
        is_subagent=False,
        image_tracker=None,
        seen_message_ids=None,
        *,
        skip_file_mentions=False,
    ) -> None:
        """Execute the agent run quietly without streaming events to the TUI transcript.

        This avoids event loop flooding and unresponsiveness during intensive
        background operations like /init.
        """
        from novacode_cli.agent_stream import run_agent_stream
        from novacode_cli.ui_events import InterruptRequest, StatusUpdate

        ag = agent or self._init_agent or self.agent
        aid = assistant_id or self.assistant_id

        async for e in run_agent_stream(
            user_input,
            ag,
            aid,
            self.session_state,
            backend=backend or self._init_backend or self.backend,
            image_tracker=self.image_tracker,
            seen_message_ids=self._seen,
        ):
            if isinstance(e, StatusUpdate):
                self._set_status(e.message or "ready")
            elif isinstance(e, InterruptRequest):
                await self._handle_interrupt(e)

    async def _run_research(self, text: str) -> None:
        """Launch the research swarm, streaming the run as native widgets."""
        from novacode_cli.commands.research_handler import handle_research_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        if not args:
            # No query → let the handler emit its usage block, surface natively.
            with _rich_console.capture() as cap:
                await handle_research_command(
                    self.agent, self.session_state, self.token_tracker, cmd_args=None
                )
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /research <query>", style="dim"))
            return
        self._log(Text(f"🔬 Research: {args}", style="bold"))
        # Capture (and discard) the handler's setup prints; the agent run itself
        # streams natively through _tui_execute_fn → _stream_prompt.
        with _rich_console.capture():
            await handle_research_command(
                self.agent,
                self.session_state,
                self.token_tracker,
                cmd_args=args,
                execute_fn=self._tui_execute_fn,
            )

    async def _run_ingest(self, text: str) -> None:
        """Ingest a raw source into the wiki."""
        from novacode_cli.commands.wiki_commands import handle_ingest

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        if not args:
            # Show usage
            with _rich_console.capture() as cap:
                from novacode_cli.commands import CommandContext

                mock_ctx = CommandContext(
                    cmd="ingest",
                    cmd_args=None,
                    agent=self.agent,
                    token_tracker=self.token_tracker,
                    session_state=self.session_state,
                    assistant_id=self.assistant_id,
                )
                await handle_ingest(mock_ctx)
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /ingest <raw_path>", style="dim"))
            return
        self._log(Text(f"📥 Ingesting: {args}", style="bold"))
        with _rich_console.capture() as cap:
            from novacode_cli.commands import CommandContext

            mock_ctx = CommandContext(
                cmd="ingest",
                cmd_args=args,
                agent=self.agent,
                token_tracker=self.token_tracker,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
            )
            await handle_ingest(mock_ctx, execute_fn=self._tui_execute_fn)
        out = cap.get().strip()
        if out:
            self._log(Text.from_ansi(out))

    async def _run_ask(self, text: str) -> None:
        """Ask a question with wiki context prepended."""
        from novacode_cli.commands.wiki_commands import handle_ask

        parts = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""
        if not question:
            with _rich_console.capture() as cap:
                from novacode_cli.commands import CommandContext

                mock_ctx = CommandContext(
                    cmd="ask",
                    cmd_args=None,
                    agent=self.agent,
                    token_tracker=self.token_tracker,
                    session_state=self.session_state,
                    assistant_id=self.assistant_id,
                )
                await handle_ask(mock_ctx)
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /ask <question>", style="dim"))
            return
        self._log(Text(f"📚 Asking: {question}", style="bold"))
        with _rich_console.capture() as cap:
            from novacode_cli.commands import CommandContext

            mock_ctx = CommandContext(
                cmd="ask",
                cmd_args=question,
                agent=self.agent,
                token_tracker=self.token_tracker,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
            )
            await handle_ask(mock_ctx, execute_fn=self._tui_execute_fn)
        out = cap.get().strip()
        if out:
            self._log(Text.from_ansi(out))

    async def _run_file(self, text: str) -> None:
        """File conversation knowledge into the wiki."""
        from novacode_cli.commands.wiki_commands import handle_file

        parts = text.split(maxsplit=1)
        topic = parts[1].strip() if len(parts) > 1 else ""
        if not topic:
            with _rich_console.capture() as cap:
                from novacode_cli.commands import CommandContext

                mock_ctx = CommandContext(
                    cmd="file",
                    cmd_args=None,
                    agent=self.agent,
                    token_tracker=self.token_tracker,
                    session_state=self.session_state,
                    assistant_id=self.assistant_id,
                )
                await handle_file(mock_ctx)
            out = Text.from_ansi(cap.get()).plain.strip()
            self._log(Text(out or "Usage: /file <topic>", style="dim"))
            return
        self._log(Text(f"📝 Filing: {topic}", style="bold"))
        with _rich_console.capture() as cap:
            from novacode_cli.commands import CommandContext

            mock_ctx = CommandContext(
                cmd="file",
                cmd_args=topic,
                agent=self.agent,
                token_tracker=self.token_tracker,
                session_state=self.session_state,
                assistant_id=self.assistant_id,
            )
            await handle_file(mock_ctx, execute_fn=self._tui_execute_fn)
        out = cap.get().strip()
        if out:
            self._log(Text.from_ansi(out))

    async def _run_wiki(self) -> None:
        """Show the Obsidian LLM Wiki browser (interactive)."""
        await self.push_screen_wait(WikiScreen())

    async def _run_dream(self) -> None:
        """Run /dream: show a native memory-consolidation summary, then stream it."""
        from novacode_cli.commands.dream_handler import handle_dream_command

        # Collect the handler's status lines and render them as ONE cohesive
        # native block (blank separators are dropped — no empty log widgets).
        status_lines: list[str] = []

        def _emit(message: str = "") -> None:
            if message:
                status_lines.append(message)

        result = await handle_dream_command(self.session_state, self.assistant_id, emit=_emit)

        if status_lines:
            block = Text()
            for i, line in enumerate(status_lines):
                try:
                    block.append_text(Text.from_markup(line))
                except Exception:  # noqa: BLE001 - bad markup: show literally
                    block.append(line)
                if i < len(status_lines) - 1:
                    block.append("\n")
            self._log(block)

        if isinstance(result, str) and result.strip():
            self._log(Text("💭 Dreaming over memories…", style="bold"))
            await self._stream_prompt(result)

    async def _run_evolution(self) -> None:
        """Run /evolution: show the self-evolution log as a native block."""
        from novacode_cli.commands.evolution_handler import handle_evolution_command

        lines: list[str] = []

        def _emit(message: str = "") -> None:
            if message:
                lines.append(message)

        await handle_evolution_command(emit=_emit)

        if lines:
            block = Text()
            for i, line in enumerate(lines):
                try:
                    block.append_text(Text.from_markup(line))
                except Exception:  # noqa: BLE001 - bad markup: show literally
                    block.append(line)
                if i < len(lines) - 1:
                    block.append("\n")
            self._log(block)

    async def _run_reindex(self) -> None:
        """Rebuild the semantic code-search index, with a native status."""
        try:
            from novacode_cli.tools.code_search_tools import (
                _get_index,
                _is_semble_available,
                _reset_index,
            )
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Code search unavailable: {ex}", style="yellow"))
            return
        if not _is_semble_available():
            self._log(
                Text(
                    "Code search is not available. Install 'semble' to enable "
                    "semantic code search (pip install semble).",
                    style="yellow",
                )
            )
            return


        from novacode_cli.config.config import settings as _settings

        workspace = _settings.get_workspace_root()
        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("re-indexing…")
        try:
            _reset_index()
            idx = await asyncio.to_thread(_get_index, workspace)
            if idx is not None:
                self._log(Text(f"✓ Code search index rebuilt for {workspace}", style="green"))
            else:
                self._log(Text("Failed to build code search index.", style="red"))
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Reindex failed: {ex}", style="red"))
        finally:
            self._turn_active = False
            self._set_status("ready")

    async def _run_images(self, text: str) -> None:
        """Native /images: list, remove, or clear conversation images."""
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        it = self.image_tracker
        if it is None:
            self._log(Text("Image tracking not available.", style="yellow"))
            return
        arg_parts = args.split(maxsplit=1)
        sub = arg_parts[0].lower() if arg_parts else ""

        if not args or sub == "list":
            images = it.list_images()
            t = Text()
            t.append("Images in conversation\n", style="bold")
            if not images:
                t.append(
                    "  (none — paste with Ctrl+V or reference @path/to/image.png)\n",
                    style="dim",
                )
            else:
                for img in images:
                    t.append(f"  {img['id']}", style="cyan")
                    t.append(
                        f"  {img['format'].upper()}  {img['size_kb']:.1f} KB  "
                        f"{img['placeholder']}\n",
                        style="dim",
                    )
                t.append(
                    f"  Total: {len(images)} — /images remove <id> · /images clear\n",
                    style="dim",
                )
            self._log(t)
            return
        if sub == "remove":
            if len(arg_parts) < 2:
                self._log(Text("Usage: /images remove <id>", style="red"))
                return
            image_id = arg_parts[1].strip()
            if not image_id.startswith("image-"):
                image_id = f"image-{image_id}"
            if it.remove_image(image_id):
                self._log(Text(f"Removed {image_id}", style="green"))
            else:
                avail = ", ".join(i["id"] for i in it.list_images())
                msg = f"Image not found: {image_id}"
                if avail:
                    msg += f" (available: {avail})"
                self._log(Text(msg, style="red"))
            return
        if sub == "clear":
            count = it.count
            if count == 0:
                self._log(Text("No images to clear.", style="dim"))
            else:
                it.clear()
                self._log(Text(f"Cleared {count} image(s) from conversation.", style="green"))
            return
        self._log(Text("Usage: /images [list | remove <id> | clear]", style="red"))

    async def _run_files(self) -> None:
        """Native /files: session read/write summary from the file tracker."""
        from novacode_cli.tracking.file_tracker import get_session_tracker

        tr = get_session_tracker()
        t = Text()
        t.append("Session file operations\n", style="bold")
        t.append(f"  read: {len(tr.files_read)} files / {tr.total_reads} ops\n", style="dim")
        t.append(
            f"  modified: {len(tr.files_written)} files / {tr.total_writes} ops\n",
            style="dim",
        )
        if getattr(tr, "rejected_edits", 0):
            t.append(f"  rejected edits (unread files): {tr.rejected_edits}\n", style="red")
        if tr.files_read:
            t.append("\nRecently read\n", style="bold")
            for path in tr.read_order[-15:]:
                rec = tr.files_read[path]
                disp = path if len(path) <= 60 else "..." + path[-57:]
                t.append(f"  {disp}", style="cyan")
                t.append(f"  ({rec.line_count} lines)\n", style="dim")
        if tr.files_written:
            t.append("\nRecently modified\n", style="bold")
            for path in tr.write_order[-15:]:
                recs = tr.files_written[path]
                disp = path if len(path) <= 60 else "..." + path[-57:]
                ops = ", ".join(r.operation for r in recs[-3:])
                if len(recs) > 3:
                    ops = f"({len(recs)}x) " + ops
                t.append(f"  {disp}", style="yellow")
                t.append(f"  {ops}\n", style="dim")
        self._log(t)

    async def _run_tests(self, text: str) -> None:
        """Native /tests: detect framework (or use args) and stream results."""
        import threading

        from novacode_cli.server_runner.test_runner import (
            detect_test_framework,
            get_default_test_command,
            run_tests,
        )

        parts = text.split(maxsplit=1)
        cmd_args = parts[1].strip() if len(parts) > 1 else ""
        from novacode_cli.config.config import settings
        working_dir = str(settings.get_workspace_root())
        if not cmd_args:
            framework = detect_test_framework(working_dir)
            command = get_default_test_command(framework)
            if not command:
                self._log(
                    Text(
                        "Could not auto-detect test framework. "
                        "Specify one: /tests pytest  or  /tests npm test",
                        style="yellow",
                    )
                )
                return
            self._log(Text(f"Detected {framework.value} — running: {command}", style="dim"))
        else:
            command = cmd_args
            self._log(Text(f"Running: {command}", style="dim"))

        loop_tid = threading.get_ident()

        def _cb(line: str) -> None:
            if threading.get_ident() == loop_tid:
                self._log(Text(line, style="dim"))
            else:
                try:
                    self.call_from_thread(self._log, Text(line, style="dim"))
                except Exception:  # noqa: BLE001
                    pass

        self._turn_active = True
        self._turn_start = time.monotonic()
        self._set_status("running tests…")
        try:
            result = await run_tests(command=command, working_dir=working_dir, output_callback=_cb)
            t = Text()
            t.append(
                "✓ Tests passed\n" if result.success else "✗ Tests failed\n",
                style="green" if result.success else "red",
            )
            stats = []
            if result.tests_run is not None:
                stats.append(f"{result.tests_run} run")
            if result.tests_passed is not None:
                stats.append(f"{result.tests_passed} passed")
            if result.tests_failed is not None:
                stats.append(f"{result.tests_failed} failed")
            if result.duration_seconds is not None:
                stats.append(f"{result.duration_seconds:.2f}s")
            if stats:
                t.append("  " + ", ".join(stats) + "\n", style="dim")
            if result.error:
                t.append(f"  error: {result.error}\n", style="red")
            self._log(t)
            self._notify_test_result(result)
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Test run failed: {ex}", style="red"))
        finally:
            self._turn_active = False
            self._set_status("ready")

    def _notify_test_result(self, result) -> None:
        """Record a notification summarizing a finished test run."""
        try:
            ok = bool(getattr(result, "success", False))
            passed = getattr(result, "tests_passed", None)
            failed = getattr(result, "tests_failed", None)
            total = getattr(result, "tests_run", None)
            dur = getattr(result, "duration_seconds", None)
            if passed is not None and total:
                title = f"Tests: {passed}/{total} passed"
            else:
                title = "Tests passed" if ok else "Tests failed"
            msg = f"{failed} failed" if failed is not None else ("ok" if ok else "failed")
            if dur is not None:
                msg += f" · {dur:.1f}s"
            self.session_state.add_notification(
                level="success" if ok else "error",
                title=title,
                message=msg,
                source="tests",
            )
        except Exception:  # noqa: BLE001
            pass

    async def _run_servers(self) -> None:
        """Show running servers (interactive)."""
        await self.push_screen_wait(ServersScreen())

    async def _run_kill(self, text: str) -> None:
        """Native /kill: kill a process by PID/name (arg) or via a picker."""
        from novacode_cli.process_manager import ProcessManager

        manager = ProcessManager.get_instance()
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg:
            try:
                pid = int(arg)
                ok = await manager.stop_process(pid)
                self._log(
                    Text(
                        (f"✓ Killed process {pid}" if ok else f"No process with PID {pid}"),
                        style="green" if ok else "yellow",
                    )
                )
                return
            except ValueError:
                pass
            ok = await manager.stop_by_name(arg)
            self._log(
                Text(
                    f"✓ Killed process '{arg}'" if ok else f"No process named '{arg}'",
                    style="green" if ok else "yellow",
                )
            )
            return

        processes = manager.list_processes(alive_only=True)
        if not processes:
            self._log(Text("No managed processes running.", style="yellow"))
            return
        opts = [f"[{p.pid}] {p.name}" + (f" (port {p.port})" if p.port else "") for p in processes]
        idx = await self.push_screen_wait(PickScreen("Kill which process?", opts))
        if 0 <= idx < len(processes):
            info = processes[idx]
            ok = await manager.stop_process(info.pid)
            self._log(
                Text(
                    (
                        f"✓ Killed '{info.name}' (PID {info.pid})"
                        if ok
                        else "Failed to kill process"
                    ),
                    style="green" if ok else "red",
                )
            )

    async def _run_restore(self, text: str) -> None:
        """Native /restore: restore a file snapshot by arg or via a picker."""
        from datetime import datetime

        from novacode_cli.recovery import REASON_LABELS, get_recovery_manager

        mgr = get_recovery_manager()
        if mgr is None:
            self._log(Text("No recovery manager active for this session.", style="yellow"))
            return
        snapshots = mgr.list_snapshots(include_past_sessions=True)
        if not snapshots:
            self._log(
                Text(
                    "No file snapshots found. Snapshots are created before "
                    "rm/write_file/edit_file.",
                    style="yellow",
                )
            )
            return

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        def _restore(idx: int) -> None:
            session_id, entry = snapshots[idx]
            ok = mgr.restore(entry, session_id=session_id)
            self._log(
                Text(
                    (
                        f"✓ Restored {entry.original_path}"
                        if ok
                        else f"Failed to restore {entry.original_path}"
                    ),
                    style="green" if ok else "red",
                )
            )

        if arg:
            if arg.isdigit():
                i = int(arg) - 1
                if 0 <= i < len(snapshots):
                    _restore(i)
                else:
                    self._log(Text(f"No snapshot at index {arg}.", style="red"))
                return
            for i, (_sid, entry) in enumerate(snapshots):
                if arg in entry.original_path or entry.original_path.endswith(arg):
                    _restore(i)
                    return
            self._log(Text(f"No snapshot matching '{arg}'.", style="red"))
            return

        now = datetime.now()
        opts = []
        for _sid, entry in snapshots:
            label = REASON_LABELS.get(entry.reason, entry.reason)
            try:
                secs = int((now - datetime.fromisoformat(entry.timestamp)).total_seconds())
                age = (
                    f"{secs}s ago"
                    if secs < 60
                    else (
                        f"{secs // 60}m ago"
                        if secs < 3600
                        else (f"{secs // 3600}h ago" if secs < 86400 else f"{secs // 86400}d ago")
                    )
                )
            except Exception:  # noqa: BLE001
                age = entry.timestamp
            opts.append(f"{entry.original_path}  — {label} ({age})")
        idx = await self.push_screen_wait(PickScreen("Restore which snapshot?", opts))
        if 0 <= idx < len(snapshots):
            _restore(idx)

    async def _run_hooks(self, text: str) -> None:
        """Show hooks manager (interactive)."""
        await self.push_screen_wait(HooksScreen())

    async def _run_browser_use(self, text: str) -> None:
        """Run /browser-use; the agent analysis streams natively via execute_fn."""
        from novacode_cli.commands.browser_use_handler import handle_browser_use_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        self._log(Text(f"🌐 Browser task: {args or '(status)'}", style="bold"))
        # Browser automation + setup prints are captured/discarded; the follow-up
        # agent run renders natively through _tui_execute_fn.
        with _rich_console.capture():
            await handle_browser_use_command(
                self.agent,
                self.session_state,
                self.assistant_id,
                self.token_tracker,
                args or None,
                execute_fn=self._tui_execute_fn,
            )

    # --- /ralph native widgets -------------------------------------------
    _RALPH_ITER_GLYPH = {
        "running": ("▶", "yellow"),
        "done": ("✓", "green"),
        "failed": ("✗", "red"),
    }

    def _ralph_mount(self, widget: Widget) -> None:
        """Mount a native Ralph card into the transcript (app thread only)."""
        self._close_tool_group()
        self._transcript().mount(widget)
        self._prune_transcript()
        self._scroll_end()

    def _ralph_run_text(self, event: Any) -> Text:
        """Header card for a Ralph run: task, iteration budget, and mode."""
        iters = "unlimited" if event.max_iterations == 0 else str(event.max_iterations)
        title = "🔁 Ralph Mode (Resumed)" if event.resumed_from else "🔁 Ralph Mode"
        t = Text()
        t.append(f"{title}\n", style="bold")
        t.append("Task: ", style="bold")
        t.append(f"{event.task}\n")
        t.append("Max iterations: ", style="bold")
        t.append(f"{iters}\n")
        if event.resumed_from:
            t.append("Resuming from: ", style="bold")
            t.append(f"iteration {event.resumed_from}\n")
        t.append("Mode: ", style="bold")
        t.append("background (non-blocking)" if event.background else "foreground")
        return t

    def _ralph_iter_text(
        self,
        iteration: int,
        max_iterations: int,
        status: str,
        elapsed: float | None = None,
        error: str | None = None,
    ) -> Text:
        """One iteration card line, styled by ``status`` (running/done/failed)."""
        glyph, color = self._RALPH_ITER_GLYPH.get(status, ("•", "dim"))
        disp = f"{iteration}/{max_iterations}" if max_iterations > 0 else str(iteration)
        t = Text()
        t.append(f"{glyph} Iteration {disp}", style=f"bold {color}")
        if status == "running":
            t.append("  — running…", style="dim")
        else:
            t.append(f"  — {'done' if status == 'done' else 'failed'}", style=color)
            if elapsed is not None:
                t.append(f" ({elapsed:.1f}s)", style="dim")
            if error:
                t.append(f"\n    {error}", style="red")
        return t

    def _ralph_status_renderable(self, snap: Any) -> Any:
        """Render a ``/ralph --status`` snapshot as a native table card."""
        from rich.console import Group
        from rich.table import Table

        header = Text("Ralph Background Tasks\n", style="bold")
        if not snap.rows:
            header.append("No background Ralph tasks running.", style="dim")
            return header

        table = Table(show_edge=False, pad_edge=False, expand=False)
        table.add_column("", width=2)
        table.add_column("Iter")
        table.add_column("Status")
        table.add_column("Elapsed", justify="right")
        table.add_column("Task")
        glyphs = {
            "running": ("⏳", "yellow"),
            "completed": ("✓", "green"),
            "failed": ("✗", "red"),
        }
        for row in snap.rows:
            g, color = glyphs.get(row.status, ("•", "dim"))
            disp = (
                f"{row.iteration}/{row.max_iterations}"
                if row.max_iterations > 0
                else str(row.iteration)
            )
            desc = row.task if len(row.task) <= 50 else row.task[:50] + "…"
            table.add_row(
                Text(g, style=color),
                disp,
                Text(row.status, style=color),
                f"{row.elapsed:.0f}s",
                desc,
            )
        summary = Text()
        summary.append(f"\nTotal {snap.total}", style="dim")
        summary.append(f"  ·  running {snap.running}", style="yellow")
        summary.append(f"  ·  completed {snap.completed}", style="green")
        summary.append(f"  ·  failed {snap.failed}", style="red")
        return Group(header, table, summary)

    def _ralph_on_event(self, event: Any) -> None:
        """Drive native Ralph widgets from a structured handler event (app thread).

        The UI-agnostic handler reports run milestones through
        :mod:`novacode_cli.commands.ralph_events`, and this turns each into a
        native card instead of a flat log line.
        """
        from novacode_cli.commands import ralph_events as rev

        if isinstance(event, rev.RalphStarted):
            self._ralph_iter_cards.clear()
            self._ralph_mount(Static(self._ralph_run_text(event), classes="ralph-run"))
        elif isinstance(event, rev.IterationStarted):
            card = Static(
                self._ralph_iter_text(event.iteration, event.max_iterations, "running"),
                classes="ralph-iter running",
            )
            self._ralph_iter_cards[event.iteration] = card
            self._ralph_mount(card)
        elif isinstance(event, rev.IterationFinished):
            status = "done" if event.ok else "failed"
            text = self._ralph_iter_text(
                event.iteration, event.max_iterations, status, event.elapsed, event.error
            )
            card = self._ralph_iter_cards.get(event.iteration)
            updated = False
            if card is not None:
                try:
                    card.set_classes(f"ralph-iter {status}")
                    card.update(text)
                    updated = True
                except Exception:  # noqa: BLE001 - card may have been pruned
                    updated = False
            if not updated:
                self._ralph_mount(Static(text, classes=f"ralph-iter {status}"))
        elif isinstance(event, rev.RalphFinished):
            t = Text()
            t.append("📊 Ralph finished", style="bold")
            t.append(f" — {event.completed} completed", style="green")
            if event.failed:
                t.append(f", {event.failed} failed", style="red")
            t.append(f" of {event.total} iteration(s)", style="dim")
            self._ralph_mount(Static(t, classes="ralph-summary"))
        elif isinstance(event, rev.StatusSnapshot):
            self._ralph_mount(Static(self._ralph_status_renderable(event), classes="ralph-status"))

    async def _run_ralph(self, text: str) -> None:
        """Run /ralph natively: structured milestones render as native cards via
        ``on_event``, free-form notices via a thread-safe ``emit``, and foreground
        iterations stream through ``_tui_execute_fn``."""
        import threading

        from novacode_cli.commands.ralph_handler import handle_ralph_command

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        self._log(Text(f"🔁 Ralph: {args or '(status)'}", style="bold"))

        loop_tid = threading.get_ident()

        def _emit(message: str = "") -> None:
            try:
                renderable = Text.from_markup(message) if message else Text("")
            except Exception:  # noqa: BLE001 - never let bad markup break the run
                renderable = Text(message)
            if threading.get_ident() == loop_tid:
                self._log(renderable)
            else:
                try:
                    self.call_from_thread(self._log, renderable)
                except Exception:  # noqa: BLE001 - app may be shutting down
                    pass

        def _on_event(event: Any) -> None:
            # Background runs fire events from a worker thread; hop to the app
            # thread before touching widgets (same contract as ``_emit``).
            if threading.get_ident() == loop_tid:
                self._ralph_on_event(event)
            else:
                try:
                    self.call_from_thread(self._ralph_on_event, event)
                except Exception:  # noqa: BLE001 - app may be shutting down
                    pass

        await handle_ralph_command(
            self.agent,
            self.session_state,
            self.assistant_id,
            self.token_tracker,
            args or None,
            execute_fn=self._tui_execute_fn,
            emit=_emit,
            on_event=_on_event,
        )

    async def _run_trello(self, text: str) -> None:
        """Run /trello; start the server inline, then watch for tasks in background."""
        from novacode_cli.commands.trello_handler import (
            _handle_status,
            _handle_stop,
        )
        from novacode_cli.commands.trello_server import TrelloServer

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        # Subcommands that don't need the processing loop
        if args == "stop":
            with _rich_console.capture() as cap:
                _handle_stop(self.session_state)
            self._log(Text.from_ansi(cap.get()))
            return
        if args == "status":
            with _rich_console.capture() as cap:
                _handle_status(self.session_state)
            self._log(Text.from_ansi(cap.get()))
            return

        # Check if already running
        existing_server: TrelloServer | None = getattr(self.session_state, "trello_server", None)
        if existing_server and existing_server.is_running:
            self._log(
                Text(
                    f"Trello board already running at http://localhost:{existing_server.port}",
                    style="yellow",
                )
            )
            return

        # Start the server
        server = TrelloServer()
        port = await server.start()
        self.session_state.trello_server = server
        self._log(
            Text(
                f"📋 Trello board started at http://localhost:{port}",
                style="bold green",
            )
        )
        self._log(
            Text(
                "Add tasks in the browser. The agent will process them one at a time.",
                style="dim",
            )
        )

        # Launch the processing loop as a background task so the TUI stays responsive
        asyncio.create_task(self._trello_watch_loop(server))

    async def _run_create(self, text: str) -> None:
        """Run /create; start the Skills & Agents web UI server."""
        from novacode_cli.commands.create_server import CreateServer

        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        # Subcommand: stop
        if args == "stop":
            server: CreateServer | None = getattr(self.session_state, "create_server", None)
            if server and server.is_running:
                server.stop()
                self.session_state.create_server = None
                self._log(Text("Create UI stopped.", style="green"))
            else:
                self._log(Text("Create UI is not running.", style="yellow"))
            return

        # Check if already running
        existing_server: CreateServer | None = getattr(self.session_state, "create_server", None)
        if existing_server and existing_server.is_running:
            self._log(
                Text(
                    f"Create UI already running at http://localhost:{existing_server.port}",
                    style="yellow",
                )
            )
            return

        # Start the server
        server = CreateServer()
        port = await server.start()
        self.session_state.create_server = server
        self._log(
            Text(
                f"Create UI started at http://localhost:{port}",
                style="bold green",
            )
        )
        self._log(
            Text(
                "Browse, preview, edit, and create skills & agents in the browser.",
                style="dim",
            )
        )

    async def _trello_watch_loop(self, server: Any) -> None:
        """Background loop: process board tasks via the shared watch loop."""
        from novacode_cli.commands.trello_handler import trello_watch_loop

        def _log(message: str, style: str = "") -> None:
            self._log(Text(message, style=style or None))

        try:
            await trello_watch_loop(
                server,
                self.agent,
                self.assistant_id,
                self.session_state,
                self.token_tracker,
                self._tui_execute_fn,
                _log,
            )
        except Exception:
            pass  # Server stopped, loop ends

    # ── /council — collaborative planning ───────────────────────────────────
    #
    # The council plans; it never edits. A run stops at "awaiting approval" and
    # only an explicit `/council approve N` turns a plan into work for the
    # coding agent. Approval reads the run back from its artifact rather than
    # from memory, so a plan can still be approved after a restart.

    def _council_store(self):
        from novacode_cli.council_planner import CouncilArtifactStore

        return CouncilArtifactStore(Path.cwd())

    def _council_target(self, store):
        """The run a bare /council subcommand acts on: this session's, else the
        most recent on disk."""
        run_id = getattr(self, "_council_run_id", None)
        return store.load(run_id) if run_id else store.latest()

    async def _run_council(self, text: str) -> None:
        """Run /council — plan a task, or open the Council web UI."""
        parts = text.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        rest = parts[2].strip() if len(parts) > 2 else ""

        # Preserved: a bare /council (and /council stop) is the web UI this
        # command has always opened.
        if sub in ("", "stop"):
            await self._run_chat(text)
            return
        if sub in ("web", "open"):
            await self._run_chat("/council")
            return
        if sub in ("view", "approve", "revise", "reject", "cancel", "history"):
            await self._council_subcommand(sub, rest)
            return

        # Anything else is the task to plan.
        self.run_worker(self._council_plan(text.split(maxsplit=1)[1].strip()))

    async def _council_subcommand(self, sub: str, rest: str) -> None:
        from novacode_cli import council_planner as cp

        store = self._council_store()

        if sub == "history":
            ids = store.history()
            if not ids:
                self._log(Text("No council runs yet.", style="dim"))
                return
            for run_id in ids:
                run = store.load(run_id)
                if run is None:
                    continue
                self._log(
                    Text.assemble(
                        (f"{run_id}  ", "cyan"),
                        (f"{run.status:<18}", "dim"),
                        (run.prompt.splitlines()[0][:60] if run.prompt else "", ""),
                    )
                )
            return

        run = self._council_target(store)
        if run is None:
            self._log(Text("No council run to act on. Try /council <task>.", "yellow"))
            return

        if sub == "view":
            if rest.isdigit():
                await self._council_show_plan(run, int(rest))
            else:
                self._council_render_plans(run)
            return

        if sub in ("reject", "cancel"):
            (cp.reject if sub == "reject" else cp.cancel)(run, store=store)
            self._log(Text(f"Council run {run.id} {run.status}.", style="yellow"))
            return

        if sub == "revise":
            if not rest:
                self._log(Text("Say what to change: /council revise <notes>", "yellow"))
                return
            cp.request_revision(run, rest, store=store)
            self._log(Text("↻ Replanning with your revisions…", style="cyan"))
            self.run_worker(self._council_plan(run.prompt, run=run))
            return

        # approve
        if not rest.split(" ")[0].isdigit():
            self._log(Text("Which plan? /council approve 1", style="yellow"))
            return
        try:
            plan = cp.approve(run, int(rest.split(" ")[0]), store=store)
        except cp.CouncilApprovalError as exc:
            self._log(Text(f"✗ {exc}", style="red"))
            return
        self._log(Text(f"✓ Plan approved — {run.approved_proposal_id}", "bold green"))
        self._log(
            Text(f"  saved to .nova/council/{run.id}/approved-plan.md", style="dim")
        )
        self._log(Text("Handing the approved plan to the coding agent…", "cyan"))
        # The executor is told the plan carries the user's authorization, so it
        # implements rather than re-opening the choice the council already made.
        cp.mark_executing(run, store=store)
        try:
            await self._stream_prompt(
                "The user reviewed and APPROVED the following implementation plan "
                "from a planning council. Implement it. Do not redesign it or "
                "propose alternatives; if a step turns out to be wrong, say so and "
                "stop rather than silently substituting your own approach.\n\n"
                + plan
            )
        finally:
            # Even on an error, the run must not be left reading "executing"
            # forever — /council history would then misreport it as in flight.
            cp.mark_completed(run, store=store)

    async def _council_plan(self, prompt: str, run: Any = None) -> None:
        """Drive a planning council, logging each phase as it lands."""
        from novacode_cli import council_planner as cp
        from novacode_cli.council import get_council_model

        try:
            model = get_council_model()
        except Exception as exc:  # noqa: BLE001 — no provider configured
            self._log(Text(f"Council unavailable: {exc}", style="red"))
            return

        store = self._council_store()
        from novacode_cli.council_planner import PLANNING_PERSONAS

        self._log(Text("◈ Council convening — planning, not editing.", "bold cyan"))
        # Said up front because this is genuinely slow: four phases of whole
        # structured plans, and a hosted reasoning model can take minutes per
        # call. Without this the user reads a quiet screen as a hang.
        self._log(
            Text(
                f"  {len(PLANNING_PERSONAS)} agents · brainstorm → critique → vote "
                "→ judge. This takes a few minutes.",
                style="dim",
            )
        )
        phase_label = {
            cp.BRAINSTORMING: "Brainstorming independently",
            cp.CRITIQUING: "Critiquing (anonymized)",
            cp.VOTING: "Anonymous ranked vote",
            cp.JUDGING: "Judge scoring against the rubric",
        }
        try:
            async for event in cp.run_planning_council(
                prompt,
                model,
                context=self._council_context(),
                store=store,
                run=run,
            ):
                self._council_run_id = event["run_id"]
                name, payload = event["event"], event["payload"]
                if name == "council.phase.started":
                    label = phase_label.get(payload.get("phase", ""))
                    if label:
                        self._log(Text(f"  → {label}", style="cyan"))
                elif name == "proposal.created":
                    self._log(
                        Text(f"    ✓ {payload['proposal_id']} — {payload['title']}", "dim")
                    )
                elif name == "agent.failed":
                    # With the reason: "dropped out" alone leaves the user
                    # unable to tell a slow model from an unreachable one.
                    why = payload.get("reason") or "no reply"
                    self._log(
                        Text(f"    ✗ {payload['name']} — {why}", style="yellow")
                    )
                elif name == "vote.tallied":
                    entropy = payload["tally"].get("entropy", 0.0)
                    self._log(
                        Text(f"    ballots in · disagreement {entropy:.2f}", "dim")
                    )
                elif name == "council.failed":
                    self._log(Text(f"✗ {payload['reason']}", style="red"))
                elif name == "plans.selected":
                    latest = store.load(event["run_id"])
                    if latest is not None:
                        self._council_render_plans(latest)
        except Exception as exc:  # noqa: BLE001 — a council must not kill the TUI
            self._log(Text(f"Council failed: {exc}", style="red"))

    def _council_context(self) -> str:
        """Light repo context for the planners: the project's own NOVA.md.

        Deliberately not a retrieval engine — the council plans an approach, and
        the coding agent reads the actual files when it implements. Whatever
        NOVA.md says about the project is the highest-value context per token.
        """
        try:
            for name in ("NOVA.md", ".nova/NOVA.md"):
                path = Path.cwd() / name
                if path.is_file():
                    return path.read_text(encoding="utf-8")[:4000]
        except OSError:
            pass
        return ""

    def _council_render_plans(self, run: Any) -> None:
        """The top-3 cards, plus how to act on them."""
        plans = run.ranked_plans()
        if not plans:
            self._log(Text("No plans were selected.", style="yellow"))
            return
        self._log(Text(f"◈ Council results — {len(plans)} plans", style="bold cyan"))
        if run.judgment is not None and run.judgment.fell_back_to_vote:
            self._log(
                Text(
                    "  ⚠ The judge was unreachable — this order is the council's "
                    "vote, not a rubric verdict.",
                    style="yellow",
                )
            )
        elif run.tally.get("entropy", 0.0) >= 0.9:
            self._log(
                Text(
                    "  ⚠ The council did not converge — this ranking is a "
                    "judgement call, not a consensus.",
                    style="yellow",
                )
            )
        for plan in plans:
            proposal = run.proposal(plan.proposal_id)
            if proposal is None:
                continue
            head = "Recommended" if plan.rank == 1 else "Alternative"
            self._log(
                Text.assemble(
                    (f"  #{plan.rank} ", "bold"),
                    (f"{head} ", "green" if plan.rank == 1 else "dim"),
                    (proposal.title, "bold"),
                )
            )
            self._log(Text(f"     {proposal.summary}", style="dim"))
            for pro in proposal.tradeoffs.get("pros", [])[:2]:
                self._log(Text(f"     + {pro}", style="green"))
            for con in proposal.tradeoffs.get("cons", [])[:2]:
                self._log(Text(f"     - {con}", style="yellow"))
        self._log(
            Text(
                "  /council view <n> · /council approve <n> · /council revise <notes>",
                style="dim",
            )
        )

    async def _council_show_plan(self, run: Any, index: int) -> None:
        plans = run.ranked_plans()
        if not 1 <= index <= len(plans):
            self._log(Text(f"Pick a plan between 1 and {len(plans)}.", "yellow"))
            return
        from novacode_cli.council_planner import to_implementation_plan

        selected = plans[index - 1]
        proposal = run.proposal(selected.proposal_id)
        if proposal is None:
            self._log(Text("That plan is missing from the run.", style="red"))
            return
        self._log(Markdown(to_implementation_plan(run, proposal, selected)))

    async def _run_chat(self, text: str) -> None:
        """Run the Council web UI — start or stop it in the browser."""
        from novacode_cli.commands.chat_handler import (
            get_server_url,
            is_server_running,
            set_agent_refs,
            start_chat_server,
            stop_chat_server,
        )

        parts = text.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else ""

        # Wire agent refs (same as the CLI handler does)
        set_agent_refs(
            self.agent,
            self.assistant_id,
            self.session_state,
            asyncio.get_running_loop(),
        )

        if sub == "stop":
            if not is_server_running():
                self._log(Text("Council server is not running.", style="yellow"))
                return
            stop_chat_server()
            self._log(Text("✓ Council server stopped.", style="green"))
            return

        if is_server_running():
            url = get_server_url()
            self._log(Text(f"Council UI already running at {url}", style="green"))
            return

        url = start_chat_server()
        self._log(
            Text(
                f"Council UI started at {url} — present a topic to convene",
                style="bold green",
            )
        )

    async def _run_agents(self) -> None:
        """Show configured subagents (interactive)."""
        await self.push_screen_wait(AgentsScreen())

    async def _run_skills(self) -> None:
        """Show installed skills (interactive)."""
        await self.push_screen_wait(SkillsScreen())

    def _collect_skill_names(self) -> list[str]:
        from pathlib import Path

        from novacode_cli.config.config import Settings, settings

        dirs: list = []
        try:
            dirs.append(settings.ensure_user_skills_dir())
        except Exception:  # noqa: BLE001
            pass
        try:
            claude_skills_dir = Settings.get_global_claude_skills_dir()
            if claude_skills_dir.exists():
                dirs.append(claude_skills_dir)
        except Exception:  # noqa: BLE001
            pass
        try:
            dirs.extend(settings.get_project_skills_dirs())
        except Exception:  # noqa: BLE001
            pass
        try:
            from novacode_cli.plugins.claude_plugins import plugin_skill_dirs

            dirs.extend(d for _, d in plugin_skill_dirs())
        except Exception:  # noqa: BLE001
            pass

        names: list[str] = []
        seen: set[str] = set()
        for d in dirs:
            if not d:
                continue
            p = Path(d)
            if not p.exists():
                continue
            for sk in sorted(p.iterdir()):
                if sk.is_dir() and (sk / "SKILL.md").exists() and sk.name not in seen:
                    seen.add(sk.name)
                    names.append(sk.name)
        return names

    def _get_skill_names(self) -> list[str]:
        if self._skill_names_cache is None:
            self._skill_names_cache = self._collect_skill_names()
        return self._skill_names_cache

    def _cached_enabled_skill_count(self) -> int:
        """Number of *enabled* skills (installed minus curation-disabled).

        This is what the status bar shows, so it must drop when skills are
        deactivated via ``/skills``. Resolving the disabled set reads two small
        prefs files, so the result is cached ~1s to keep the throttled status
        tail cheap; ``action_toggle`` sets ``_skill_count_cache = None`` to make
        a toggle reflect immediately.
        """
        now = time.monotonic()
        if self._skill_count_cache is not None and now - self._skill_count_ts < 1.0:
            return self._skill_count_cache
        try:
            from novacode_cli.skills.skills_prefs import effective_disabled

            disabled = effective_disabled()
            count = sum(1 for n in self._get_skill_names() if n not in disabled)
        except Exception:  # noqa: BLE001
            count = 0
        self._skill_count_cache = count
        self._skill_count_ts = now
        return count

    def _cached_agent_md_count(self) -> int:
        """Project NOVA.md/CLAUDE.md count, stat'd at most ~once per second.

        ``_refresh_status`` runs at 20fps while a turn is active. Calling
        ``get_project_agent_md_paths()`` there re-stat'd four candidate paths
        every frame (~80 disk stats/sec) — the dominant in-turn UI lag. These
        files don't change mid-frame, so a 1s TTL cache is plenty.
        """
        now = time.monotonic()
        if hasattr(self, "_md_count_cache") and now - self._md_count_ts < 1.0:
            return self._md_count_cache
        try:
            from novacode_cli.config.config import settings

            count = len(settings.get_project_agent_md_paths())
        except Exception:  # noqa: BLE001
            count = 0
        self._md_count_cache = count
        self._md_count_ts = now
        return count

    def _get_agent_names(self) -> list[str]:
        if self._agent_names_cache is None:
            names: list[str] = []
            try:
                from novacode_cli.config.config import settings

                for name, _d, _scope in settings.get_all_agents():
                    names.append(name)
            except Exception:  # noqa: BLE001
                pass
            self._agent_names_cache = names
        return self._agent_names_cache

    # -- slash helpers --------------------------------------------------------
    def _help_text(self) -> Text:
        """Render /help — derived from the TUI_COMMANDS table, so a command
        registered there can never be missing here."""
        width = max(len(name) for name in TUI_COMMANDS) + 2  # + leading slash pad
        t = Text()
        t.append("Nova TUI commands\n", style="bold")
        for name, spec in TUI_COMMANDS.items():
            t.append(f"  /{name:<{width}}", style="cyan")
            t.append(f"{spec.help}\n", style="dim")
        t.append(f"  {'!<command>':<{width + 1}}", style="magenta")
        t.append("run a shell command on the host\n", style="dim")
        t.append(f"  {'/skill:<name>':<{width + 1}}", style="green")
        t.append("invoke a skill (autocompletes)\n", style="dim")
        t.append(f"  {'@<agent> <task>':<{width + 1}}", style="green")
        t.append("delegate to a named subagent (autocompletes)\n", style="dim")
        t.append("\nEsc cancels the current turn · Ctrl+Q quits", style="dim")
        return t

    def _token_text(self) -> Text:
        if self.token_tracker is None:
            return Text("No token data available.", style="dim")
        try:
            bd = self.token_tracker.get_breakdown()
        except Exception:  # noqa: BLE001
            bd = None
        if not bd:
            return Text("No token usage captured yet.", style="dim")
        return Text(
            f"Context: {bd.usage_percentage:.1f}% used ({getattr(bd, 'tokens_used', 0):,} tokens)",
            style="dim",
        )

    async def _render(self, e: Any) -> None:
        if isinstance(e, ev.StatusUpdate):
            self._set_status(e.message or "ready")
        elif isinstance(e, ev.ReasoningDelta):
            # Stream the model's reasoning into a dim, transient message widget.
            # The actual repaint is coalesced (~20fps) via _schedule_stream_flush.
            self._reasoning_buf += e.text
            if self._reason_msg is None:
                self._reason_msg = ChatMessage(
                    Text("💭 thinking", style="dim italic"), "reason", collapsible=True
                )
                await self._mount(self._reason_msg)
            self._schedule_stream_flush()
            if self._activity != "thinking…":
                self._set_status("thinking…")
        elif isinstance(e, ev.TextDelta):
            # Stream incremental prose into the in-progress Nova message widget.
            # Coalesced repaint (~20fps) — see _schedule_stream_flush/_flush_stream.
            self._live_buf += e.text
            if self._stream_msg is None:
                name, color = self._current_agent_info()
                self._stream_msg = ChatMessage(Text(name, style=f"bold {color}"), "nova")
                await self._mount(self._stream_msg)
            self._schedule_stream_flush()
            if self._activity != "responding…":
                self._set_status("responding…")
        elif isinstance(e, ev.TextDiscard):
            self._stream_flush_scheduled = False
            if self._stream_msg is not None:
                try:
                    await self._stream_msg.remove()
                except Exception:  # noqa: BLE001
                    pass
                self._stream_msg = None
            self._live_buf = ""
        elif isinstance(e, ev.AssistantMessage):
            # Commit: finalize the streaming widget as rendered markdown. Cancel
            # any pending coalesced flush so it can't repaint a finalized widget.
            self._stream_flush_scheduled = False
            if self._stream_msg is not None:
                self._stream_msg.update_header(Text(e.agent_name, style=e.agent_color))
                self._stream_msg.update_body(Markdown(e.text))
                self._stream_msg = None
            else:
                await self._add_message(
                    Text(e.agent_name, style=e.agent_color), "nova", Markdown(e.text)
                )
            self._live_buf = ""
            await self._remove_reasoning()
            self._scroll_end()
            # Accumulate the reply's prose instead of speaking immediately.
            # Speech is deferred until the turn finishes (ev.Done) or pauses (ev.InterruptRequest)
            # to prevent it from getting cut off by intermediate events or subsequent steps.
            if getattr(self, "_accumulated_reply", None):
                self._accumulated_reply += "\n\n" + e.text
            else:
                self._accumulated_reply = e.text
        elif isinstance(e, ev.ToolCall):
            self._set_status(f"running {e.name}…")
            base = f"{e.icon} {_esc(e.display_str)}"
            if e.name in _DETAILED_TOOL_NAMES:
                # Write/edit and execution tools keep a DEDICATED Collapsible.
                # Mounting it (via _mount) closes any open tool group, keeping
                # transcript order correct.
                if e.name in {
                    "shell",
                    "bash",
                    "execute",
                    "execute_bash",
                    "run_command",
                    "run_tests",
                    "start_dev_server",
                }:
                    body = RichLog(classes="terminal-log", highlight=True, markup=True)
                    # Starts expanded (collapsed=False) to show live output!
                    comp = Collapsible(body, title=f"{base}  · running…", collapsed=False)
                else:
                    body = Static("", classes="toolbody")
                    # Starts collapsed for file diffs
                    comp = Collapsible(body, title=f"{base}  · running…", collapsed=True)
                comp.add_class("tool")
                comp.add_class("tool-active")
                animate_entrance(comp, "zoom")
                await self._mount(comp)
                entry = (comp, body, base)
                if e.call_id:
                    self._tool_components[e.call_id] = entry
                self._last_tool = entry
            else:
                # Everything else (reads, search, exec, MCP, …) condenses into
                # the shared tool group — one compact line per call.
                await self._ensure_tool_group()
                self._add_tool_group_call(e.call_id, f"{e.icon} {e.display_str}", e.name)
        elif isinstance(e, ev.ToolResult):
            if e.call_id and e.call_id in self._tool_components:
                # Dedicated panel (write/edit) — finalize with full output body.
                self._finalize_tool(e.call_id, e.preview, e.full_output, is_error=e.is_error)
            else:
                self._mark_tool_group_result(e.call_id, is_error=e.is_error, detail=e.preview)
            self._scroll_end()
        elif isinstance(e, ev.FileOp):
            # File ops are the result of their tool call. Write/edit (a dedicated
            # panel was opened at ToolCall) render the full colored diff body so
            # the user can see exactly what changed; reads condense into the
            # group with a concise "Read N lines" summary.
            rec = e.record
            errored = bool(getattr(rec, "error", None)) or (getattr(rec, "status", "") == "error")
            if e.call_id and e.call_id in self._tool_components:
                comp, body, base = self._tool_components.pop(e.call_id)
                if self._last_tool is not None and self._last_tool[0] is comp:
                    self._last_tool = None
                mark = "✗" if errored else "✓"
                comp.title = f"{base}  {mark} {_esc(self._fileop_summary(rec))}".rstrip()
                # Expand on failure (surface the error) AND on a successful change
                # with a diff — these dedicated write/edit panels exist precisely
                # to show what changed, so a collapsed diff defeats the purpose.
                if errored or getattr(rec, "diff", None):
                    comp.collapsed = False
                body.update(self._fileop_body(rec, e.full_output))
            else:
                self._mark_tool_group_result(
                    e.call_id, is_error=errored, detail=self._fileop_summary(rec)
                )
            self._scroll_end()
        elif isinstance(e, ev.TodoUpdate):
            # Held in per-pane state so a session switch can repaint the
            # app-global dock with the pane the user is actually looking at.
            self._todos = list(e.todos or [])
            self._todos_agent = e.agent_name
            self._paint_todos(self._todos, e.agent_name)
        elif isinstance(e, ev.ErrorOutput):
            self._log(Text(e.text, style="red"))
        elif isinstance(e, ev.CompactionNotice):
            self._log(Text("⟳ Context compacted", style="dim"))
            # Context just shrank. The API-sourced current_context is the turn's
            # PEAK (pre-compaction) and would otherwise mask the reduction, so
            # reset() clears has_api_data and lets the recomputed, message-based
            # breakdown show the real post-compaction size. Then refresh the
            # status line so ctx% reflects it immediately.
            if self.token_tracker is not None:
                try:
                    self.token_tracker.reset()
                    await self._update_context_breakdown()
                except Exception:  # noqa: BLE001
                    pass
                self._refresh_status()
        elif isinstance(e, ev.ContextMessage):
            # Review-cycle start/complete are transient status, not log entries:
            # surface them on the live indicator above the input instead of
            # letting them scroll away in the transcript.
            if e.event_type == "nova_review_start":
                self._set_nova_indicator(f"{e.icon} {e.message}", style=e.color)
                return
            if e.event_type == "nova_review_complete":
                # Show briefly, then fade so the indicator doesn't linger.
                self._set_nova_indicator(f"{e.icon} {e.message}", style=e.color, auto_clear=4.0)
                return

            t = Text(e.icon + " ", style=e.color)
            t.append(e.message, style=e.color)
            # Map event_type (e.g. "nova_skill_refinement") to the CSS modifier
            # class ("nova-skill-refinement"): strip the leading "nova_"
            # namespace and convert underscores to hyphens so the per-event
            # border colors defined in the stylesheet actually match.
            css = "nova-event"
            if e.event_type:
                modifier = e.event_type.replace("nova_", "", 1).replace("_", "-")
                css += f" nova-{modifier}"
            # Non-tool content: close any open tool group first to keep order.
            self._close_tool_group()
            self._transcript().mount(Static(t, classes=css))
            self._prune_transcript()
            self._scroll_end()
        elif isinstance(e, ev.SubagentActivity):
            await self._handle_subagent(e)
        elif isinstance(e, ev.UsageUpdate):
            if self.token_tracker is not None:
                try:
                    self.token_tracker.add(
                        e.input_tokens,
                        e.output_tokens,
                        cache_read_tokens=e.cache_read_tokens,
                        cache_creation_tokens=e.cache_creation_tokens,
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif isinstance(e, ev.InterruptRequest):
            if getattr(self, "_accumulated_reply", None):
                self._speak_reply(self._accumulated_reply)
                self._accumulated_reply = ""
            await self._handle_interrupt(e)
        elif isinstance(e, ev.Cancelled):
            self._accumulated_reply = ""
            if getattr(self, "_detach_cancelling", False):
                # The turn was cancelled by a Ctrl+B detach, not a real interrupt —
                # the command is now running as a background task.
                self._log(Text("⚙ Command moved to background — agent is idle.", style="cyan"))
            else:
                self._log(Text("Interrupted.", style="yellow"))
        elif isinstance(e, ev.ContextOverflow):
            # The provider rejected the request for being too long. Unlike other
            # provider errors this has one specific remedy — shrink the
            # conversation — so compact and retry ONCE. A second overflow after
            # compacting means the summary itself doesn't fit, and retrying
            # again would just loop.
            self._accumulated_reply = ""
            if getattr(self, "_overflow_retried", False):
                self._overflow_retried = False
                self._log(
                    Text(
                        "⚠ Still over the context limit after compacting — the "
                        "conversation can't be shrunk further. Use /clear to start "
                        "fresh, or switch to a larger-context model.",
                        style="bold #f7768e",
                    )
                )
                for line in (e.message or "").splitlines():
                    self._log(Text(line, style="yellow"))
                return
            self._overflow_retried = True
            self._log(
                Text("⚠ Context overflow — compacting and retrying…", style="bold #f7768e")
            )
            await self._run_compact("")
            prompt = getattr(self, "_last_user_prompt", None)
            if prompt:
                await self._stream_prompt(prompt)
            else:
                self._log(
                    Text(
                        "Compacted. Re-send your message to continue.",
                        style="dim",
                    )
                )
            self._overflow_retried = False
        elif isinstance(e, ev.Error):
            self._accumulated_reply = ""
            # Provider failures (usage/rate limit, auth, connectivity) are
            # pre-formatted into a clean notice upstream and flagged; render them
            # as a calm warning. The formatter fallback covers any Error that
            # carries a raw provider exception without the flag.
            notice = e.message if e.is_provider_notice else None
            if notice is None and e.exception is not None:
                from novacode_cli.errors import friendly_model_error

                notice = friendly_model_error(e.exception)
            if notice:
                for line in notice.splitlines():
                    self._log(Text(line, style="yellow"))
            else:
                self._log(Text(f"Error: {e.message}", style="red"))
        elif isinstance(e, ev.Done):
            if getattr(self, "_accumulated_reply", None):
                self._speak_reply(self._accumulated_reply)
                self._accumulated_reply = ""
            await self._sync_async_task_watcher()

    def _notify_async_done(self, level: str, title: str, message: str) -> None:
        """Surface a finished async subagent as a notification, and — when the
        agent is idle — proactively trigger a turn that fetches and reports the
        result, so the user doesn't have to ask for a status report."""
        try:
            self.session_state.add_notification(level, title, message, source="async-agent")
        except Exception:  # noqa: BLE001
            pass
        # Extract the full task_id the watcher embeds so we can drive a turn.
        m = re.search(r"\[async_task_id=([^\]]+)\]", message)
        task_id = m.group(1) if m else None
        if not task_id:
            return
        # Only auto-report when the agent is idle — never interrupt an active
        # turn. If busy, the notification (🔔 badge) still surfaces the event.
        if getattr(self, "_turn_active", False):
            return
        self._log(Text(f"↻ Async agent finished — fetching result…", style="cyan"))
        self._auto_report_async_done(task_id)

    @work(exclusive=True, group="turn")
    async def _auto_report_async_done(self, task_id: str) -> None:
        """Proactively report a finished async subagent's result.

        Injects a visible message into the transcript and runs a turn that asks
        the agent to fetch the task result and summarize it — closing the gap
        where a finished remote agent otherwise waits for the user to ask."""
        try:
            await self._add_message(
                Text("You", style="bold cyan"),
                "user",
                Text(f"[async agent finished — auto-reporting result]"),
            )
        except Exception:  # noqa: BLE001
            pass
        prompt = (
            f"[Async subagent finished] A remote async subagent has completed. "
            f"Fetch its result with check_async_task('{task_id}') and give the "
            f"user a concise summary of what it produced. If the task errored, "
            f"report the error clearly."
        )
        await self._stream_prompt(prompt)
        await self._maybe_run_approved_plan()

    async def _sync_async_task_watcher(self) -> None:
        """After a turn, watch any newly-launched async subagent so its completion
        surfaces as a notification. Async subagents are otherwise fire-and-forget
        (start returns a task_id and nothing pushes completion back), so without
        this a finished remote agent never reaches the user until they poll."""
        if self.agent is None or self.session_state is None:
            return
        try:
            config = {"configurable": {"thread_id": self.session_state.thread_id}}
            state = await asyncio.wait_for(self.agent.aget_state(config), timeout=5.0)
            tasks = state.values.get("async_tasks") or {}
        except Exception:  # noqa: BLE001 — never let telemetry break a turn
            return
        if not tasks:
            return
        watcher = getattr(self, "_async_watcher", None)
        if watcher is None:
            from novacode_cli.remote.async_task_watcher import AsyncTaskWatcher

            watcher = AsyncTaskWatcher(self._notify_async_done)
            self._async_watcher = watcher
        watcher.sync_from_state(tasks)
        # Surface newly-running agents in the ⚙ tasks bar right away (and start
        # its 1s runtime ticker, which will also drop them once they finish).
        try:
            self._refresh_tasks_bar()
        except Exception:  # noqa: BLE001
            pass

    async def _ask_remote_question(self, question_request: dict) -> dict:
        """Route an agent question to the remote user via Discord/Telegram."""
        prompt = (
            question_request.get("question")
            or question_request.get("prompt")
            or "The agent has a question:"
        )
        opts = question_request.get("options") or []
        context = question_request.get("context")

        lines = []
        if context:
            lines.append(f"ℹ️ *Context:* {context}\n")
        lines.append(f"❓ *Question:* {prompt}")
        if opts:
            lines.append("\n*Options:*")
            for i, opt in enumerate(opts, 1):
                lines.append(f"{i}. {opt}")
            lines.append("\n*(Please reply with the number or the exact option text)*")
        message_text = "\n".join(lines)
        try:
            await self._remote_msg.reply_fn(message_text)
        except Exception as ex:  # noqa: BLE001
            self._log(Text(f"Failed to send remote question: {ex}", style="red"))

        self._remote_question_future = asyncio.Future()
        try:
            m = await self._remote_question_future
        finally:
            self._remote_question_future = None

        text = (getattr(m, "text", "") or "").strip()
        selected = None
        answer = text
        if text.isdigit() and opts:
            idx = int(text) - 1
            if 0 <= idx < len(opts):
                selected = idx
                answer = opts[idx]
        elif opts:
            for i, opt in enumerate(opts):
                if opt.lower() == text.lower():
                    selected = i
                    answer = opt
                    break

        from novacode_cli.ui.question_prompt import QuestionResponse

        return {"response": QuestionResponse(answer=answer, selected_index=selected)}

    async def _handle_interrupt(self, e: "ev.InterruptRequest") -> None:
        # Resolve in a finally so a handler that raises before set_result fails
        # closed (reject) instead of leaving the agent loop awaiting forever.
        try:
            await self._handle_interrupt_inner(e)
        finally:
            if not e.future.done():
                from novacode_cli.core.agent_loop import default_interrupt_response

                e.future.set_result(default_interrupt_response(e.kind))

    async def _handle_interrupt_inner(self, e: "ev.InterruptRequest") -> None:
        if e.kind == "tool":
            req = e.payload
            from novacode_cli.ui.hitl_approval import check_plan_mode_blocked

            blocked, rejection = check_plan_mode_blocked(req, self.session_state.plan_mode_enabled)
            if blocked and rejection:
                e.future.set_result({"decisions": rejection["decisions"], "any_rejected": True})
                return
            action_requests = req["action_requests"]
            if self.session_state.auto_approve:
                e.future.set_result(
                    {
                        "decisions": [{"type": "approve"} for _ in action_requests],
                        "any_rejected": False,
                    }
                )
                return
            choice = await self.push_screen_wait(
                ApprovalModal(
                    "Tool action requires approval",
                    _approval_details(action_requests),
                )
            )
            if choice == "reject":
                e.future.set_result(
                    {
                        "decisions": [
                            {"type": "reject", "message": "Rejected by user"}
                            for _ in action_requests
                        ],
                        "any_rejected": True,
                    }
                )
                return

            if choice in ("session", "always"):
                from dataclasses import replace

                from novacode_cli.security.remember import apply_remember
                from novacode_cli.security.rule_synthesis import synthesize_rule

                for ar in action_requests:
                    name, args = ar.get("name", ""), ar.get("args", {})
                    if choice == "session":
                        apply_remember("session", name, args)
                        self._log(Text(f"✓ Allowed `{name}` for this session.", style="green"))
                    else:
                        rule = synthesize_rule(name, args)
                        out = await self.push_screen_wait(RememberRuleModal(rule))
                        if out:
                            edited = replace(rule, value=out["value"])
                            res = apply_remember(
                                "always", name, args, target=out["target"], rule=edited
                            )
                            if res.saved_path is not None:
                                self._log(Text(f"✓ Saved to {res.saved_path}", style="green"))
                            else:
                                self._log(
                                    Text(
                                        f"⚠ Could not save rule ({res.error}); "
                                        "kept for this session.",
                                        style="yellow",
                                    )
                                )
                        else:
                            self._log(Text("Not saved — approved this call only.", style="dim"))
                e.future.set_result(
                    {
                        "decisions": [{"type": "approve"} for _ in action_requests],
                        "any_rejected": False,
                    }
                )
                return

            if choice == "auto":
                # Approve everything for the rest of this session.
                self.session_state.auto_approve = True
                self._log(Text("✓ Auto-approve enabled for this session.", style="green"))
            e.future.set_result(
                {
                    "decisions": [{"type": "approve"} for _ in action_requests],
                    "any_rejected": False,
                }
            )
        elif e.kind == "question":
            if self._remote_msg is not None:
                result = await self._ask_remote_question(e.payload)
            else:
                result = await self.push_screen_wait(QuestionModal(e.payload))
            e.future.set_result(result)
        elif e.kind == "plan":
            body: Any = "Review the plan and approve to proceed."
            content = None
            try:
                from novacode_cli.ui.interrupt_handlers import resolve_plan_content

                content, _ = resolve_plan_content(
                    getattr(self.session_state, "todos", None),
                    self.session_state,
                    backend=self.backend,
                    inline_plan=(
                        (e.payload or {}).get("plan") if isinstance(e.payload, dict) else None
                    ),
                )
                if content:
                    body = Markdown(content)
            except Exception:  # noqa: BLE001
                pass
            choice = await self.push_screen_wait(PlanApprovalModal("Plan requires approval", body))
            if choice in ("auto", "manual"):
                self.session_state.plan_mode_enabled = False
                self._update_mode_badge()
                if choice == "auto":
                    # "Auto-approve edits" is scoped to THIS plan's execution
                    # run. auto_approve is session-global and agent_loop
                    # auto-approves FUTURE plan interrupts when it's set — so
                    # without restoring it afterwards, approving one plan with
                    # "auto" silently self-approved every later plan.
                    # _maybe_run_approved_plan restores the flag when the run
                    # ends (unless the user had auto-approve on globally).
                    if not getattr(self.session_state, "auto_approve", False):
                        self._plan_scoped_auto_approve = True
                    self.session_state.auto_approve = True
                # Store the plan for hand-off ONLY when a separate plan agent
                # (/plan) produced it. That agent never executes — agent_loop
                # breaks the turn on approval — so _maybe_run_approved_plan runs
                # the plan on the main agent.
                #
                # For main-agent self-planning (plan_agent is None), the agent
                # RESUMES in-context after approval and executes the plan inline.
                # Stashing it here would make _maybe_run_approved_plan fire a
                # SECOND time — clearing the session and re-executing finished
                # work. This mirrors agent_loop's `using_separate_plan_agent` gate.
                if content and getattr(self.session_state, "plan_agent", None) is not None:
                    try:
                        self.session_state.set_approved_plan(content)
                    except Exception:  # noqa: BLE001
                        pass
                e.future.set_result(
                    {
                        "response": {
                            "approved": True,
                            "mode": choice,
                        },
                        "state_update": {"plan_mode_enabled": False},
                    }
                )
            else:
                # Refine — stay in plan mode; the user's next message routes to
                # the plan agent to revise (the "chat to refine" flow).
                e.future.set_result(
                    {
                        "response": {
                            "approved": False,
                            "action": "refine",
                            "feedback": "",
                        },
                        "state_update": {},
                    }
                )
        else:
            e.future.set_result(None)


async def run_tui(
    *,
    agent,
    assistant_id,
    session_state,
    backend,
    token_tracker,
    image_tracker,
    model_name,
    session_manager=None,
    restored_messages=None,
    sandbox_id: str | None = None,
    sandbox_type: str | None = None,
    sandbox_meta: dict | None = None,
) -> None:
    """Launch the Textual chat app and run until the user exits."""
    app = NovaApp(
        agent=agent,
        assistant_id=assistant_id,
        session_state=session_state,
        backend=backend,
        token_tracker=token_tracker,
        image_tracker=image_tracker,
        model_name=model_name,
        session_manager=session_manager,
        restored_messages=restored_messages,
        sandbox_id=sandbox_id,
        sandbox_type=sandbox_type,
        sandbox_meta=sandbox_meta,
    )
    await app.run_async()
