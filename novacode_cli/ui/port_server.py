"""``UIPort`` implemented over the server socket.

``ServerPort`` is the bridge between "a handler prints" and "a client draws": it
captures the Rich console the same way `sessions/worker.py` and the Textual
``_passthrough_command`` do, turns the output into plain blocks, and hands them
back as a ``CommandResult`` the host sends as a ``command_result`` frame.

Routing is data, not a hand-maintained whitelist:
:data:`novacode_cli.server.catalog.CATALOG` classifies every entry of the TUI's
``TUI_COMMANDS`` table, and a row the port cannot serve yet answers
:func:`not_ported` instead of quietly passing the text to the agent. That keeps
"not ported" honest while the remaining waves land, and makes a newly added
command visible (the catalogue generator fails until it is regenerated).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novacode_cli.loops import loop_command
from novacode_cli.server.catalog import ALIASES, ROUTE_CLIENT, ROUTE_SCREEN, get
from novacode_cli.ui import loops
from novacode_cli.ui.port import (
    Block,
    CommandResult,
    ScreenSpec,
    blocks_from_text,
    not_ported,
    strip_ansi,
)
from novacode_cli.ui.transcript import message_blocks

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# The client-side action each `client`-routed command asks for. The catalogue
# decides *that* a command is a client action; this table says *which* one.
CLIENT_ACTIONS = {"clear": "clear", "quit": "quit"}

# Commands the port answers itself, outside the shared command layer, because the
# behaviour lives on the session the socket client cannot reach. `/status` has no
# `TUI_COMMANDS` row at all; `/auto-approve` exists only as a Textual method.
PORT_COMMANDS: dict[str, str] = {
    "ui-composer": "_composer_command",
    "serena": "_serena_command",
    "browser-use": "_browser_command",
    "mcp": "_mcp_command",
    "status": "_status",
    "auto-approve": "_auto_approve",
    "verbose": "_verbose",
    "stop": "_stop",
    "copy": "_copy_command",
    "goal": "_goal",
    "council": "_council_screen",
    "prime": "_prime_screen",
    "redirect": "_redirect_command",
    "steer": "_steer_command",
    "tokens": "_tokens_command",
    "cost": "_tokens_command",
    "files": "_files_command",
    "restore": "_restore_command",
    "research": "_research_command",
    "tasks": "_tasks_command",
    "export": "_export_command",
    "worktree": "_worktree_command",
    "agent-server": "_agent_server_command",
    "btw": "_btw_command",
    "images": "_images_command",
    "notifications": "_notifications_command",
    "reindex": "_reindex_command",
    "ci-fix": "_ci_fix_command",
    "nova": "_nova_command",
    "proactive": "_proactive_screen",
    "rooms": "_rooms_screen",
    "teammates": "_teammates_screen",
    "computer": "_computer_command",
    "artifacts": "_artifacts_command",
    "cowork": "_cowork_command",
    "reload-plugins": "_reload_plugins",
    "cron": "_cron_command",
    "webhook": "_webhook_command",
    "prompt": "_prompt_command",
    "ponytail-review": "_ponytail_review",
    "ponytail-audit": "_ponytail_audit",
    "ponytail-debt": "_ponytail_debt",
    "ponytail-gain": "_ponytail_gain",
    "ponytail-help": "_ponytail_help",
    "ui": "_ui_command",
    "share": "_share_command",
    "import": "_import_command",
    "compare": "_compare_command",
    "ralph": "_ralph_command",
    "trello": "_trello_command",
    "create": "_create_command",
    "claude-plan": "_claude_plan_command",
    "codex-plan": "_codex_plan_command",
    "claude-review": "_claude_review_command",
    "codex-review": "_codex_review_command",
    "loop": "_loop_command",
    "remote": "_remote_command",
}

# Commands the port serves by building a screen from
# `novacode_cli/ui/screen_specs.py`, and the builder for each. The catalogue
# marks every one route=screen, served=True; a row without a builder here fails
# loudly rather than guessing.
SCREEN_BUILDERS: dict[str, str] = {
    "command-center": "_command_center_screen",
    "context": "_context_screen",
    "rewind": "_rewind_screen",
    "tree": "_tree_screen",
    "resume": "_resume_screen",
    "subagents": "_subagents_screen",
    "theme": "_theme_screen",
    "update": "_update_screen",
    "usage": "_usage_screen",
    "statusline": "_statusline_screen",
    "settings": "_settings_screen",
    "voice": "_voice_screen",
    "oracle-mode": "_oracle_screen",
    "tabs": "_tabs_screen",
}

# Where the Textual UI keeps the chosen theme, so a choice made here survives a
# restart in exactly the same place.
THEME_CONFIG_KEY = "theme"

# The status-line field keys, in the same config slot the Textual picker writes.
STATUSLINE_CONFIG_KEY = "statusline_fields"


# ── persisted preferences ────────────────────────────────────────────────
def _persisted_theme() -> str | None:
    """Return the theme the user last chose, or `None` when they never did."""
    from novacode_cli.config.nova_config import NovaConfig

    with contextlib.suppress(Exception):
        value = NovaConfig().get(THEME_CONFIG_KEY)
        if isinstance(value, str) and value:
            return value
    return None


def _persist_theme(name: str) -> None:
    """Store a theme choice next to the other UI preferences.

    A read-only config directory must not fail the command: the palette still
    applies for this session, so the loss is durability, not behaviour.

    Args:
        name: Theme name to persist.
    """
    from novacode_cli.config.nova_config import NovaConfig

    with contextlib.suppress(Exception):
        NovaConfig().set(THEME_CONFIG_KEY, name)


def _persisted_statusline() -> tuple[str, ...] | None:
    """Return the status-line fields the user chose, or `None` for the defaults."""
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.ui.statusline_fields import selected_fields

    with contextlib.suppress(Exception):
        value = NovaConfig().get(STATUSLINE_CONFIG_KEY)
        if isinstance(value, list):
            return selected_fields(value)
    return None


def _persist_statusline(fields: list[str]) -> None:
    """Store the chosen fields where the Textual UI keeps them.

    A read-only config directory must not fail the command: the choice still
    applies to this session.

    Args:
        fields: Field keys, in the order the status bar draws them.
    """
    from novacode_cli.config.nova_config import NovaConfig

    with contextlib.suppress(Exception):
        NovaConfig().set(STATUSLINE_CONFIG_KEY, fields)


def _truthy(value: Any) -> bool:  # noqa: ANN401 — a toggle answers true, "on" or "1"
    """Read a toggle answer, accepting the shapes a client may send."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"on", "1", "true", "yes"}


def _config() -> Any:  # noqa: ANN401 — NovaConfig is untyped configuration
    """Return the shared configuration object."""
    from novacode_cli.config.nova_config import NovaConfig

    return NovaConfig()


def _effort_setting(config: Any) -> str:  # noqa: ANN401 — see _config
    """Read reasoning effort; an unset value is the provider default."""
    from novacode_cli.ui.screen_specs import EFFORT_OPTIONS

    value = str(config.get("reasoning_effort") or "default").lower()
    return value if value in EFFORT_OPTIONS else "default"


def _budget_setting(config: Any) -> int:  # noqa: ANN401 — see _config
    """Read the context budget in tokens, falling back to the documented default."""
    from novacode_cli.context._analysis import DEFAULT_CONTEXT_BUDGET_TOKENS

    value = config.get("context_budget_tokens")
    try:
        return DEFAULT_CONTEXT_BUDGET_TOKENS if value is None else int(value)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_BUDGET_TOKENS


def _settings_report(effort: str, budget: int) -> str:
    """One line naming both agent preferences, for the transcript."""
    from novacode_cli.ui.screen_specs import budget_label

    return f"Effort: {effort} · context budget: {budget_label(budget)}"


def _setting(config: Any, method: str, *, default: Any) -> Any:  # noqa: ANN401
    """Call an optional config accessor, falling back to a documented default.

    Args:
        config: A `NovaConfig`-shaped object, or a test double exposing `get`/`set`.
        method: Accessor name.
        default: Value when the accessor is missing, raises or returns `None`.

    Returns:
        The accessor's value, or *default*.
    """
    reader = getattr(config, method, None)
    if not callable(reader):
        return default
    with contextlib.suppress(Exception):
        value = reader()
        if value is not None:
            return value
    return default


def _write(config: Any, method: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
    """Call an optional config setter, ignoring a missing one or a read-only store."""
    writer = getattr(config, method, None)
    if callable(writer):
        with contextlib.suppress(Exception):
            writer(*args, **kwargs)


def _preferences_state(config: Any) -> Any:  # noqa: ANN401 — returns screen_specs.Preferences
    """Read every settings value the form edits from the shared config.

    Args:
        config: A `NovaConfig`-shaped object.

    Returns:
        A :class:`novacode_cli.ui.screen_specs.Preferences` snapshot. A slot the
        config cannot answer keeps its documented default instead of failing the
        command, exactly as the Textual screen does.
    """
    from novacode_cli.ui import screen_specs

    voice = _setting(config, "get_voice_config", default={})
    teams = _setting(config, "get_agent_team_settings", default={})
    try:
        fps = int(config.get("animation_fps") or 30)
    except (TypeError, ValueError):
        fps = 30
    return screen_specs.Preferences(
        effort=_effort_setting(config),
        budget=_budget_setting(config),
        decision_pruning=bool(_setting(config, "get_tool_verdicts_enabled", default=False)),
        learning=bool(_setting(config, "get_learning_enabled", default=True)),
        learning_auto=bool(_setting(config, "get_learning_auto_approve", default=False)),
        voice_enabled=bool(voice.get("enabled")),
        voice_mode=str(voice.get("mode") or "push_to_talk"),
        voice_speak=bool(voice.get("speak_responses")),
        stt_provider=str(voice.get("stt_provider") or "faster-whisper"),
        tts_provider=str(voice.get("tts_provider") or "piper"),
        submit_on_enter=config.get("submit_on_enter", True) is True,
        autocomplete=config.get("autocomplete_enabled", True) is True,
        animation_fps=fps,
        low_resource=config.get("low_resource_mode", False) is True,
        auto_approve_decisions=bool(
            _setting(config, "get_auto_approve_decisions_enabled", default=False)
        ),
        teams=bool(teams.get("enabled")),
        swarm=bool(teams.get("swarm")),
    )


def _apply_preferences(config: Any, before: Any, answer: dict[str, Any]) -> Any:  # noqa: ANN401
    """Persist every settings field the form answered.

    A field the client left out keeps its previous value, so an older client can
    still edit the two fields it knows about. Unknown choices are ignored rather
    than written: a free-text answer must never become a config value the
    Textual screen would reject.

    Args:
        config: A `NovaConfig`-shaped object.
        before: The :class:`Preferences` the form opened with.
        answer: The form answer, keyed by field key.

    Returns:
        The resulting :class:`Preferences`.
    """
    from novacode_cli.ui import screen_specs

    def flag(key: str, *, current: bool) -> bool:
        value = answer.get(key)
        return current if value is None else _truthy(value)

    effort = before.effort
    raw_effort = answer.get("reasoning_effort")
    if isinstance(raw_effort, str) and raw_effort in screen_specs.EFFORT_OPTIONS:
        effort = raw_effort
    budget = before.budget
    raw_budget = answer.get("context_budget_tokens")
    if isinstance(raw_budget, str):
        resolved = screen_specs.budget_tokens(raw_budget)
        if resolved is not None:
            budget = resolved

    voice = _setting(config, "get_voice_config", default={})
    voice_enabled = flag("voice_enabled", current=bool(voice.get("enabled")))
    voice_speak = flag("voice_speak_responses", current=bool(voice.get("speak_responses")))
    voice_mode = str(voice.get("mode") or "push_to_talk")
    raw_mode = answer.get("voice_mode")
    if isinstance(raw_mode, str):
        voice_mode = {label: key for key, label in screen_specs.VOICE_MODES}.get(
            raw_mode, voice_mode
        )
    stt = str(voice.get("stt_provider") or "faster-whisper")
    raw_stt = answer.get("voice_stt_provider")
    if isinstance(raw_stt, str) and raw_stt.strip():
        stt = screen_specs.provider_id(raw_stt)
    tts = str(voice.get("tts_provider") or "piper")
    raw_tts = answer.get("voice_tts_provider")
    if isinstance(raw_tts, str) and raw_tts.strip():
        tts = screen_specs.provider_id(raw_tts)

    fps = before.animation_fps
    raw_fps = answer.get("animation_fps")
    if isinstance(raw_fps, str):
        resolved_fps = screen_specs.animation_fps_value(raw_fps)
        if resolved_fps is not None:
            fps = resolved_fps

    teams = flag("agent_teams_enabled", current=before.teams)
    swarm = flag("agent_teams_swarm", current=before.swarm) and teams

    with contextlib.suppress(Exception):
        # "default" clears the override — the stored value must be unset, not
        # the string, or ollama would wire a literal "default" effort.
        config.set("reasoning_effort", None if effort == "default" else effort)
        config.set("context_budget_tokens", budget)
        config.set("submit_on_enter", flag("submit_on_enter", current=before.submit_on_enter))
        config.set(
            "autocomplete_enabled", flag("autocomplete_enabled", current=before.autocomplete)
        )
        config.set("animation_fps", fps)
        config.set("low_resource_mode", flag("low_resource_mode", current=before.low_resource))
    _write(
        config,
        "set_tool_verdicts_enabled",
        flag("decision_pruning", current=before.decision_pruning),
    )
    _write(config, "set_learning_enabled", flag("learning_enabled", current=before.learning))
    _write(
        config,
        "set_learning_auto_approve",
        flag("learning_auto_approve", current=before.learning_auto),
    )
    _write(
        config,
        "set_auto_approve_decisions_enabled",
        flag("auto_approve_decisions_enabled", current=before.auto_approve_decisions),
    )
    _write(config, "set_agent_team_settings", enabled=teams, swarm=swarm)
    _write(
        config,
        "set_voice_config",
        enabled=voice_enabled,
        mode=voice_mode,
        speak_responses=voice_speak,
        stt_provider=stt,
        tts_provider=tts,
    )

    return screen_specs.Preferences(
        effort=effort,
        budget=budget,
        decision_pruning=flag("decision_pruning", current=before.decision_pruning),
        learning=flag("learning_enabled", current=before.learning),
        learning_auto=flag("learning_auto_approve", current=before.learning_auto),
        voice_enabled=voice_enabled,
        voice_mode=voice_mode,
        voice_speak=voice_speak,
        stt_provider=stt,
        tts_provider=tts,
        submit_on_enter=flag("submit_on_enter", current=before.submit_on_enter),
        autocomplete=flag("autocomplete_enabled", current=before.autocomplete),
        animation_fps=fps,
        low_resource=flag("low_resource_mode", current=before.low_resource),
        auto_approve_decisions=flag(
            "auto_approve_decisions_enabled", current=before.auto_approve_decisions
        ),
        teams=teams,
        swarm=swarm,
    )


def _store_settings(effort: str, budget: int) -> None:
    """Persist the two agent preferences next to the other UI settings.

    A read-only config directory must not fail the command: the values still
    apply for this session.

    Args:
        effort: Reasoning effort to store.
        budget: Context budget in tokens to store.
    """
    config = _config()
    with contextlib.suppress(Exception):
        # "default" means unset — never store the literal string.
        config.set("reasoning_effort", None if effort == "default" else effort)
        config.set("context_budget_tokens", budget)


def canonical(name: str) -> str:
    """Normalize a command name as typed by a user.

    Args:
        name: Command as sent by the client, with or without a leading `/`.

    Returns:
        Lower-case, slash-free, alias-resolved command name.
    """
    stripped = name.strip().lstrip("/").lower()
    base = stripped.split(maxsplit=1)[0] if stripped else ""
    return ALIASES.get(base, base)


def _event_frame_or_none(_ev_type: str, message: str) -> dict[str, Any] | None:
    """Build an error event frame without importing the event encoder eagerly."""
    from novacode_cli import ui_events as ev
    from novacode_cli.server import frames

    return frames.event_frame(ev.Error(message))


def _read_log_tail(path: str, lines: int = 40) -> str:
    """The last *lines* of a log file; never raises."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        return "\n".join(content.splitlines()[-lines:])
    except OSError:
        return ""


# ── helpers shared by the app-side commands ──────────────────────────────


def _ponytail_turn(name: str, args: str | None) -> CommandResult:
    """Expand an explicit ponytail helper into the turn it asks the agent to run.

    The Textual handler prints the helper text as a user message and then
    streams it; here the text travels as `prompt`, which is the port's version
    of `_stream_prompt`, so the host starts the same turn.

    Args:
        name: Canonical command name, which selects the helper skill.
        args: Everything after the command.

    Returns:
        A result whose `prompt` carries the expanded helper text.
    """
    from novacode_cli.agents.ponytail import helper_prompt

    return CommandResult(ok=True, message=f"/{name}", prompt=helper_prompt(f"{name} {args or ''}"))


def _open_browser(url: str) -> None:
    """Open *url* in the local browser, ignoring a machine with none."""
    import webbrowser

    with contextlib.suppress(Exception):
        webbrowser.open(url)


def _rewind_label(said: str, limit: int = 70) -> str:
    """Collapse one user message into a one-line checkpoint label.

    Args:
        said: The message text.
        limit: Longest label before an ellipsis.

    Returns:
        The label the Textual picker shows.
    """
    flat = " ".join(said.split())
    return flat if len(flat) <= limit else f"{flat[:limit].rstrip()}…"


def _tree_text(node: Any) -> str:  # noqa: ANN401 — a HistoryTree node, imported lazily
    """One line for a history-tree node, labelled as the Textual tree labels it.

    Args:
        node: A `novacode_cli.session.history_tree.Node`.

    Returns:
        The node's role and a truncated preview of its text.
    """
    preview = " ".join(str(getattr(node, "text", "")).split())[:100]
    kind = str(getattr(node, "kind", ""))
    if kind == "user":
        return f"you: {preview}"
    if kind == "assistant":
        return f"nova: {preview}"
    return preview or f"({kind or 'message'})"


def _resume_row(meta: Any) -> dict[str, str]:  # noqa: ANN401 — a SessionMeta, imported lazily
    """One `@@/resume@@` row, as the Textual picker describes a saved session.

    Args:
        meta: A `novacode_cli.session.session_persistence.SessionMeta`.

    Returns:
        `{"id", "label", "detail"}` for the list screen.
    """
    from pathlib import Path

    from novacode_cli.session.session_restore import _truncate, format_session_age

    project = Path(str(getattr(meta, "project_root", "") or "")).name or "Untitled"
    title = " ".join(str(getattr(meta, "current_task", "") or "").split())
    label = _truncate(title, 60) or f"{project} conversation"
    detail = (
        f"{getattr(meta, 'message_count', 0)} messages · "
        f"{format_session_age(str(getattr(meta, 'last_active', '') or ''))} · {project}"
    )
    return {"id": str(meta.session_id), "label": label, "detail": detail}


def _export_page(tree: Any, session_id: str, model_name: str) -> str:  # noqa: ANN401
    """Render one session tree as the self-contained page `@@/export@@` writes.

    `@@/export@@` and `@@/share@@` publish the same page; building it in one place
    is what keeps the file on disk and the gist identical.

    Args:
        tree: A `novacode_cli.session.history_tree.HistoryTree`.
        session_id: The session the tree belongs to, shown in the subtitle.
        model_name: The model that produced the session, or an empty string.

    Returns:
        The complete HTML document.
    """
    from novacode_cli.session.history_export import export_html

    branches = sum(max(0, len(node.children) - 1) for node in tree.nodes.values())
    branches += max(0, len(tree.roots) - 1)
    first = next((node.text for node in tree.nodes.values() if node.kind == "user"), "")
    title = " ".join(first.split())[:80] or "Nova session"
    subtitle = (
        f"Nova session {session_id[:8]} · {model_name or 'model'}"
        f" · {len(tree.nodes)} messages · {branches + 1} branch(es)"
    )
    return export_html(tree, title=title, subtitle=subtitle)


def _status_argument(text: str) -> tuple[str, str]:
    """Read the optional status filter of `@@/command-center@@`.

    Args:
        text: Whatever followed the command.

    Returns:
        `(status, "")` for a filter `@@STATUSES@@` knows, or `("", complaint)`.
    """
    from novacode_cli.ui.command_center import STATUSES

    wanted = text.strip().casefold()
    if not wanted:
        return STATUSES[0], ""
    for status in STATUSES:
        if status.casefold().startswith(wanted):
            return status, ""
    choices = ", ".join(status.casefold().replace(" ", "") for status in STATUSES)
    return "", f"Unknown filter “{text.strip()}”. Try one of: {choices}."


async def _create_gist(gh: str, page: str) -> tuple[str, str]:
    """Upload *page* as a secret gist and return its URL.

    The page is written to a temporary file because ``gh gist create`` uploads a
    file, not stdin, and the file must not outlive the upload.

    Args:
        gh: The resolved `gh` executable.
        page: The HTML to publish.

    Returns:
        ``(url, "")`` on success, or ``("", reason)`` when nothing was created.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "nova-session.html"
        await asyncio.to_thread(path.write_text, page, encoding="utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                gh,
                "gist",
                "create",
                str(path),
                "--desc",
                "Nova session",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(process.communicate(), timeout=60)
        except (OSError, TimeoutError) as error:
            return "", str(error)
    url = out.decode(errors="replace").strip().splitlines()[-1:] or [""]
    if process.returncode != 0 or not url[0].startswith("https://"):
        reason = err.decode(errors="replace").strip() or "no gist URL was returned"
        return "", reason
    return url[0], ""


def _picked_id(answer: object) -> str:
    """Read the `id` a `list` screen answered, treating anything else as a cancel."""
    return str(answer.get("id") or "") if isinstance(answer, dict) else ""


def saved_theme() -> dict[str, Any] | None:
    """Return the saved theme palette for the initial native frame."""
    from dataclasses import asdict

    from novacode_cli.ui.theme_table import BY_NAME

    entry = BY_NAME.get(_persisted_theme() or "")
    return asdict(entry) if entry is not None else None


async def _rewind_rows(
    thread_id: str, messages: list[Any], turns: list[tuple[int, str]]
) -> list[tuple[int, str, str]]:
    """Describe each checkpoint with how many files changed after it.

    The file walk is one blocking pass per turn, so it runs off the event loop.

    Args:
        thread_id: Conversation thread whose recorded file changes are read.
        messages: Every message in the thread.
        turns: `(index, text)` pairs from `rewind.user_turns`, newest first.

    Returns:
        `(index, text, detail)` triples, in the order given.
    """
    from novacode_cli import rewind

    async def one(index: int, said: str) -> tuple[int, str, str]:
        changed = await asyncio.to_thread(
            rewind.changes_after, thread_id, rewind.call_ids(messages[index:])
        )
        return index, said, f"{len(changed)} file(s) changed since"

    return [await one(index, said) for index, said in turns]


class _AgentExistsError(Exception):
    """The agent file already exists in the chosen scope, so nothing was written."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _plain_markup(message: str) -> str:
    """Drop Rich markup, for a client that renders plain text.

    Handlers write for the terminal (`[bold]...[/bold]`); the socket carries
    text. Malformed markup is shown as written rather than swallowed.

    Args:
        message: Rich markup, or plain text already.

    Returns:
        The text without markup tags.
    """
    from rich.text import Text as RichText

    try:
        return RichText.from_markup(message).plain
    except Exception:  # noqa: BLE001 - a stray bracket must not lose the line
        return message


def _first_line(text: str) -> str:
    """The message a result carries when its handler printed several lines.

    Args:
        text: Captured output.

    Returns:
        Its first non-empty line, or the whole text when it has none.
    """
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return text


def _capture_printing(call: Callable[[], object]) -> tuple[str, tuple[Block, ...]]:
    """Run a handler that prints to the console and capture what it printed.

    Args:
        call: The synchronous handler to run.

    Returns:
        Its output as plain text, and the same text as transcript blocks.
    """
    from novacode_cli.config.config import console

    printed = io.StringIO()
    with console.capture() as captured, contextlib.redirect_stdout(printed):
        call()
    text = strip_ansi(captured.get() + printed.getvalue()).strip()
    return text, blocks_from_text(text)


#: What a subagent name may contain: the create form's placeholder, enforced.
_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _agent_rows() -> list[dict[str, object]]:
    """Read every configured subagent, for the `/subagents` list.

    Blocking: both the directory walk and one file read per agent, so callers run
    it through `asyncio.to_thread`.

    Returns:
        One dict per agent with `name`, `path`, `scope`, `description` and
        `runs_async`, sorted the way the Textual manager sorts them.
    """
    from novacode_cli.commands.agents_commands import agent_summary
    from novacode_cli.config.config import agent_md, settings

    rows: list[dict[str, object]] = []
    for name, path, scope in sorted(settings.get_all_agents(), key=lambda row: row[0].lower()):
        try:
            description, runs_async = agent_summary(agent_md(path))
        except Exception:  # noqa: BLE001 — one unreadable agent must not hide the rest
            description, runs_async = "(could not read this agent)", False
        rows.append(
            {
                "name": name,
                "path": path,
                "scope": scope,
                "description": description,
                "runs_async": runs_async,
            }
        )
    return rows


def _read_agent(name: str) -> tuple[dict[str, object], str]:
    """Find one agent by name and read its system prompt.

    Args:
        name: Agent name, without its `@`.

    Returns:
        The agent's list row and its system prompt.

    Raises:
        LookupError: No configured agent has that name.
    """
    from novacode_cli.agents.agent_file import read_agent
    from novacode_cli.config.config import agent_md

    row = next((row for row in _agent_rows() if row["name"] == name), None)
    if row is None:
        raise LookupError(name)
    _front, prompt = read_agent(agent_md(Path(str(row["path"]))))
    return row, prompt.strip()


def _notify_async_agents_changed() -> None:
    """Tell the launcher the set of async agents changed; failure is not fatal."""
    with contextlib.suppress(Exception):
        from novacode_cli.agents.server_launcher import notify_user_async_agents_changed

        notify_user_async_agents_changed()


def _handoff(lead: str, body: str) -> str:
    """Join a sentence and a pasted report with one blank line.

    The separator is built rather than written as an escape sequence: the
    prompt crosses two frontends and one wire, and a literal is easier to
    grep than two backslash-n inside an f-string.
    """
    return chr(10).join((lead, "", body))


class ServerPort:
    """A :class:`novacode_cli.ui.port.UIPort` that emits protocol frames.

    The port is deliberately dumb about transport: it is constructed with an
    ``emit`` callable (the connection's sink) and the session it belongs to, so
    the same class works for the socket server and for a test that records
    frames into a list.
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        worker: Any,  # noqa: ANN401 — the session worker owns untyped agent state
        cwd: str | None = None,
        *,
        ask_screen: Callable[[ScreenSpec], Awaitable[Any]] | None = None,
        sandbox_id: str | None = None,
        sandbox_type: str | None = None,
        session_rows: Callable[[], list[dict[str, Any]]] | None = None,
        busy: Callable[[], bool] | None = None,
    ) -> None:
        """Bind a port to one session.

        Args:
            emit: Sink for frames, normally the transport's ``send``.
            worker: The session worker that owns agent, state and token tracker.
            cwd: The session's workspace. Reported by ``/status`` instead of the
                server process's own directory, which would leak wherever the
                server happened to start.
            ask_screen: Presents a modal and resolves with its answer. Supplied
                by the host, which owns the screen futures; without it a modal
                request is reported as unsupported instead of hanging.
            sandbox_id: Sandbox the session is tied to, if any. Forwarded to
                ``handle_command`` the way the Textual UI forwards it, so a
                sandboxed handler behaves identically on both frontends.
            sandbox_type: Sandbox kind (``"local"``, ``"docker"``, ...).
            session_rows: Live sessions for ``/tabs``, normally the server's
                registry. Without it the command says the list is unavailable.
            busy: Whether a turn is executing. The host owns the turn task, so it
                supplies this; without it a command assumes the session is idle.
        """
        self._emit = emit
        self._worker = worker
        self._cwd = cwd
        self._ask_screen = ask_screen
        self._sandbox_id = sandbox_id
        self._sandbox_type = sandbox_type
        self._session_rows = session_rows
        self._busy = busy or (lambda: False)
        self._proactive_store_instance: Any = None
        self._proactive_runtime: Any = None
        self._rooms_store_instance: Any = None
        self._council_task: Any = None
        self._loop_states: list[Any] = []
        self._loop_poll: Any = None
        self._loop_jobs: dict[tuple[str, str], Any] = {}
        self._active_loop: Any = None
        self._ralph_task: Any = None
        self._trello_task: Any = None
        self._btw_task: Any = None
        self._reindex_task: Any = None
        self._research_prompt: str | None = None
        self.status: dict[str, str] = {}

    # ── UIPort ───────────────────────────────────────────────────────────
    def log(self, block: Block) -> None:
        """Send one transcript block as a completed command result.

        Args:
            block: The block to append.
        """
        self._emit(
            {
                "t": "command_result",
                "id": "",
                "ok": True,
                "message": "",
                "blocks": [{"kind": block.kind, "text": block.text}],
            }
        )

    def notify(self, message: str, *, level: str = "info") -> None:
        """Send a transient notice.

        Args:
            message: Text to show.
            level: `"info"`, `"warning"` or `"error"`.
        """
        from novacode_cli.server import frames

        self._emit(frames.notice(message, level=level))

    def push_screen(self, spec: ScreenSpec) -> None:
        """Report a modal the client cannot show instead of faking it.

        A fire-and-forget modal is only useful if something consumes it; until
        the screen renderer covers this kind, saying so out loud beats painting
        nothing and looking broken.

        Args:
            spec: The requested screen.
        """
        self.notify(f"{spec.kind} screens need an interactive client", level="warning")

    async def ask(self, spec: ScreenSpec) -> Any:  # noqa: ANN401 — a modal returns its own shape
        """Present a modal and wait for the client's answer.

        The host owns the round trip (a ``screen`` frame out, a
        ``screen_result`` frame back) and fails closed on timeout, so a client
        that dies mid-modal cannot wedge the command coroutine.

        Args:
            spec: The requested screen.

        Returns:
            The client's answer, or ``None` when no modal round trip is
            available (no host, or the screen kind is unrenderable), which
            leaves the calling handler on its documented default path.
        """
        if self._ask_screen is None:
            self.push_screen(spec)
            return None
        return await self._ask_screen(spec)

    def set_status(self, key: str, value: str) -> None:
        """Record one status field for the next ``/status`` reply.

        Args:
            key: Field name.
            value: Display value.
        """
        self.status[key] = value

    # ── command routing ──────────────────────────────────────────────────
    async def run(self, name: str, args: str | None) -> CommandResult:
        """Execute one slash command and return its result.

        Args:
            name: Command name as sent by the client.
            args: Raw argument text, or ``None`.

        Returns:
            The result, always explicit about success.
        """
        command = canonical(name)
        builder = PORT_COMMANDS.get(command) or SCREEN_BUILDERS.get(command)
        if builder is not None:
            return await getattr(self, builder)(args)
        spec = get(command)
        if spec is None or not spec.served:
            # Unknown, or a command whose wave has not landed: say so, never
            # hand the text to the agent and call that a success.
            return not_ported(command or name)
        if spec.route == ROUTE_CLIENT:
            return self._client_action(CLIENT_ACTIONS[spec.name])
        if spec.route == ROUTE_SCREEN:
            # A served screen row must have a builder above; reaching here means
            # the catalogue and the port disagree, which is worth saying out loud.
            return not_ported(command)
        return await self._passthrough(command, args)

    def composer_state(self) -> dict[str, Any]:
        """Expose authoritative session flags to the native composer."""
        from novacode_cli.agents.profiles import profile_identity, selected_profile
        from novacode_cli.config.nova_config import NovaConfig

        state = self._worker.session_state
        name, color = profile_identity(selected_profile(state) or "nova")
        config = NovaConfig()
        tracker = getattr(state, "token_tracker", None)
        return {
            "t": "composer",
            "agent": name,
            "color": color,
            "prime": getattr(state, "prime_enabled", False) is True,
            "passing": getattr(state, "handoff_enabled", False) is True,
            "plan": getattr(state, "plan_mode_enabled", False) is True,
            "browser": getattr(state, "browser_mode_enabled", False) is True,
            "serena": getattr(state, "serena_mode_enabled", False) is True,
            "lite": config.get_compression_mode(),
            "code": config.get_code_mode_enabled(),
            "oracle": getattr(state, "oracle_enabled", False) is True,
            "context_window": getattr(tracker, "context_window_size", 0),
        }

    async def _composer_command(self, args: str | None) -> CommandResult:
        """Act on a composer control without submitting its draft."""
        from novacode_cli.agents.profiles import AGENT_COLORS, profile_readiness
        from novacode_cli.commands.handoff_handler import handle_passing_command
        from novacode_cli.commands.lite_mode_handler import handle_lite_mode_command
        from novacode_cli.commands.prime_handler import handle_prime_command
        from novacode_cli.config.nova_config import NovaConfig

        state = self._worker.session_state
        control = args or ""
        if self._busy() and control in {"agent", "plan", "browser", "serena"}:
            return CommandResult(False, "Wait for the active request to finish.")
        if control == "agent":
            profiles = tuple(AGENT_COLORS)
            locked = await asyncio.to_thread(lambda: [profile_readiness(p) for p in profiles])
            descriptions = (
                "Nova tools", "Serena MCP only", "Autonomous work loop", "Web and browser research",
            )
            selected = _picked_id(await self.ask(ScreenSpec("list", {
                "items": [
                    {"id": p, "label": p.title(), "detail": reason or description, "color": AGENT_COLORS[p]}
                    for p, description, reason in zip(profiles, descriptions, locked, strict=True)
                ],
            }, "Choose agent")))
            if selected not in profiles:
                return CommandResult(True)
            if self._busy():
                return CommandResult(False, "Wait for the active request to finish.")
            if reason := profile_readiness(selected):
                return CommandResult(False, reason)
            state.browser_mode_enabled = False
            state.serena_mode_enabled = False
            state.agent_profile = selected
        elif control == "plan":
            state.plan_mode_enabled = not state.plan_mode_enabled
        elif control == "passing":
            handle_passing_command(state, "off" if state.handoff_enabled else "on")
        elif control == "prime":
            handle_prime_command(state)
        elif control == "oracle":
            state.oracle_enabled = not state.oracle_enabled
        elif control == "ralph-pause" and getattr(state, "agent_profile", None) == "ralph" and self._busy():
            state._ralph_checkpoint_requested = True
        elif control == "lite":
            mode = await asyncio.to_thread(lambda: NovaConfig().get_compression_mode())
            await asyncio.to_thread(handle_lite_mode_command, "on" if mode == "off" else "off")
        elif control == "code":
            def toggle_code() -> None:
                config = NovaConfig()
                config.set_code_mode_enabled(not config.get_code_mode_enabled())
            try:
                await asyncio.to_thread(toggle_code)
            except OSError as exc:
                return CommandResult(False, f"Could not save Code Mode preference: {exc}")
        elif control == "browser":
            return await self._browser_command(None)
        elif control == "serena":
            return await self._serena_command(None)
        else:
            return CommandResult(False, "Unknown composer control.")
        return CommandResult(True)

    async def _serena_command(self, args: str | None) -> CommandResult:
        """Toggle the legacy Serena lens using its live MCP readiness check."""
        from novacode_cli.agents.serena_mode import serena_mcp_ready

        state = self._worker.session_state
        action = (args or "").strip().lower()
        if action == "status":
            detail = "on" if state.serena_mode_enabled else "off"
            return CommandResult(True, f"Serena Mode: {detail}")
        if self._busy():
            return CommandResult(False, "Wait for the active request to finish.")
        if action not in {"", "on", "off"}:
            return CommandResult(False, "Use /serena on, off or status.")
        enabled = action == "on" if action else not state.serena_mode_enabled
        if enabled:
            ready, detail = await asyncio.to_thread(serena_mcp_ready)
            if not ready:
                return CommandResult(False, detail)
        state.serena_mode_enabled = enabled
        return CommandResult(True)

    async def _browser_command(self, args: str | None) -> CommandResult:
        """Manage the trusted browser MCP selection and session mode."""
        from novacode_cli.config.browser_mode import browser_mode_args
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli.mcp.config import MCPConfig

        state = self._worker.session_state
        action, task = browser_mode_args(args)
        if action == "status":
            detail = "on" if state.browser_mode_enabled else "off"
            return CommandResult(True, f"Browser Mode: {detail}")
        if self._busy():
            return CommandResult(False, "Wait for the active request to finish.")
        if action == "settings":
            servers = await asyncio.to_thread(lambda: MCPConfig().list_servers())
            selected = set(await asyncio.to_thread(lambda: NovaConfig().get_browser_mode_servers()))
            answer = await self.ask(ScreenSpec("form", {"fields": [
                {"key": name, "label": name, "toggle": True, "value": "on" if name in selected else "off"}
                for name, config in servers.items() if not config.disabled
            ]}, "Trusted browser MCP servers"))
            if isinstance(answer, dict):
                if self._busy():
                    return CommandResult(False, "Wait for the active request to finish.")
                chosen = [name for name in servers if not servers[name].disabled and answer.get(name) is True]
                await asyncio.to_thread(lambda: NovaConfig().set_browser_mode_servers(chosen))
            return CommandResult(True)
        state.browser_mode_enabled = not state.browser_mode_enabled if action == "toggle" else action == "on"
        return CommandResult(True, prompt=task or None)

    async def _mcp_command(self, args: str | None) -> CommandResult:
        """Use the native picker to manage installed servers and install presets."""
        from novacode_cli.mcp import get_shared_mcp_middleware
        from novacode_cli.mcp.config import MCPConfig
        from novacode_cli.mcp.presets import create_config_from_preset, list_presets

        if args and args.strip():
            return await self._passthrough("mcp", args)
        config = MCPConfig()
        servers = await asyncio.to_thread(config.list_servers)
        middleware = get_shared_mcp_middleware()
        presets = list_presets()
        selection = _picked_id(await self.ask(ScreenSpec("list", {"items": [
            *(
                {"id": f"server:{name}", "label": name, "detail": (
                    "disabled" if server.disabled else middleware.server_status.get(name, "configured")
                )}
                for name, server in servers.items()
            ),
            *(
                {"id": f"preset:{name}", "label": f"Install {preset['name']}", "detail": preset["description"]}
                for name, preset in presets.items() if name not in servers
            ),
        ]}, "MCP servers")))
        if not selection:
            return CommandResult(True)
        kind, _, name = selection.partition(":")
        if kind == "server" and name in servers:
            server = servers[name]
            items = [{"id": "toggle", "label": "Enable" if server.disabled else "Disable"}]
            if not server.disabled:
                items.append({"id": "retry", "label": "Retry connection"})
            action = _picked_id(await self.ask(ScreenSpec("list", {"items": items}, name)))
            if action not in ("retry", "toggle"):
                return CommandResult(True)
            if self._busy():
                return CommandResult(False, "Wait for the active request to finish.")
            if action == "retry" and not server.disabled:
                connected = await middleware.retry_server(name)
                return CommandResult(connected, f"{name}: {'connected' if connected else 'connection failed'}")
            if action != "toggle":
                return CommandResult(False, "Enable the server before retrying.")
            server.disabled = not server.disabled
        elif kind == "preset" and name in presets and name not in servers:
            preset = presets[name]
            values = {}
            if key := preset.get("setup_key"):
                result = await self.ask(ScreenSpec("form", {"fields": [{
                    "key": key,
                    "label": preset["setup_prompt"],
                    "value": "",
                    "secret": key in preset.get("env_mapping", {}),
                }]}, f"Install {preset['name']}"))
                if not isinstance(result, dict):
                    return CommandResult(True)
                value = result.get(key)
                if not isinstance(value, str) or not value.strip():
                    return CommandResult(False, "Enter the required setup value.")
                values[key] = value.strip()
            if self._busy():
                return CommandResult(False, "Wait for the active request to finish.")
            server = create_config_from_preset(name, values)
            if server is None:
                return CommandResult(False, "Unknown MCP preset.")
        else:
            return CommandResult(False, "Unknown MCP selection.")
        await config.add_server_async(name, server)
        reload_servers = getattr(self._worker.session_state, "reload_mcp_servers", None)
        if reload_servers is None:
            return CommandResult(True, f"{name}: saved.")
        try:
            self._worker.agent, self._worker.backend = await reload_servers()
        except Exception:  # noqa: BLE001 — the saved configuration applies after restart.
            return CommandResult(False, f"{name}: saved. Restart the session to apply it.")
        return CommandResult(True, f"{name}: {'disabled' if server.disabled else 'enabled'}.")

    async def _auto_approve(self, args: str | None = None) -> CommandResult:
        """Turn this session's approval preference on, off, or report it.

        The wording matches the Textual handler, so a user switching frontends
        reads the same sentence. The choice is never persisted: it belongs to
        the session, which is exactly what the Textual UI promises.

        Args:
            args: `on`, `off`, `status`, or nothing to toggle.

        Returns:
            The new state as a transcript line, or the usage text.
        """
        state = getattr(self._worker, "session_state", None)
        if state is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        arg = (args or "").strip().lower() or "toggle"
        current = bool(getattr(state, "auto_approve", False))
        if arg == "status":
            text = f"Auto-approve is {'on' if current else 'off'} for this session."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        if arg not in {"toggle", "on", "off"}:
            return CommandResult(ok=False, message="Usage: /auto-approve [on|off|status]")
        enabled = not current if arg == "toggle" else arg == "on"
        state.auto_approve = enabled
        text = f"Auto-approve {'on' if enabled else 'off'} for this session."
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _verbose(self, _args: str | None = None) -> CommandResult:
        """Flip the session's verbose mode, matching the Textual handler."""
        state = getattr(self._worker, "session_state", None)
        toggle = getattr(state, "toggle_verbose", None)
        if state is None or toggle is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        new = bool(toggle())
        text = (
            f"Verbose mode {'on' if new else 'off'} — internal context "
            f"{'shown' if new else 'collapsed'}."
        )
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _stop(self, _args: str | None = None) -> CommandResult:
        """Ask the client to cancel the running turn, as Esc does.

        The port cannot cancel: the turn belongs to the host, which owns the
        task. So the answer is an action the client turns into a `cancel`
        frame, which is exactly the path Esc already takes.

        Returns:
            A result whose action is `cancel`.
        """
        return CommandResult(
            ok=True,
            message="Stopping…",
            action="cancel",
            blocks=(Block("text", "Stopping the running turn."),),
        )

    async def _goal(self, args: str | None = None) -> CommandResult:
        """Set, inspect or steer the thread's goal.

        The parsing and state mutation live in `commands/side_commands.py`, shared
        with the Textual UI and the REPL, so this handler only adapts the result:
        a `kickoff` prompt is returned as `prompt`, which is the protocol's way of
        asking the host to start a turn (`/init` and `/skill:x` use it too).

        Args:
            args: Everything after `/goal`.

        Returns:
            The helper's message as blocks, its kickoff as `prompt`, and
            `ok=False` for a usage answer.
        """
        from novacode_cli.commands.side_commands import handle_goal_command

        state = getattr(self._worker, "session_state", None)
        if state is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        result = handle_goal_command(state, (args or "").strip())
        blocks = blocks_from_text(result.message) if result.message else ()
        return CommandResult(
            ok=result.action != "usage",
            message=result.message.splitlines()[0] if result.message else "",
            blocks=blocks,
            prompt=result.kickoff,
        )

    def _client_action(self, action: str) -> CommandResult:
        """Build the result for a command the client performs itself.

        Args:
            action: `"clear"` or `"quit"`.

        Returns:
            A successful result carrying the action.
        """
        message = "Closing session." if action == "quit" else "Transcript cleared."
        return CommandResult(ok=True, message=message, action=action)

    async def _status(self, _args: str | None = None) -> CommandResult:
        """Compose the ``/status`` report from session facts.

        Returns:
            A successful result whose single block lists the live facts.
        """
        state = getattr(self._worker, "session_state", None)
        lines = [
            f"Session   {getattr(state, 'session_id', None) or 'unsaved'}",
            f"Thread    {getattr(state, 'thread_id', None) or '—'}",
            f"Model     {getattr(self._worker, 'model_name', None) or 'unknown'}",
            f"Cwd       {self._cwd or Path.cwd()}",
            f"Approval  {'auto' if getattr(state, 'auto_approve', False) else 'manual'}",
        ]
        tracker = getattr(self._worker.session_state, "token_tracker", None)
        used = getattr(tracker, "last_total_tokens", None)
        window = getattr(tracker, "context_window_size", None)
        if used and window:
            lines.append(f"Context   {used:,} / {window:,} tokens")
        todos = getattr(state, "todos", None)
        if todos:
            done = sum(1 for todo in todos if str(todo.get("status")) == "completed")
            lines.append(f"Todos     {done}/{len(todos)} complete")
        for key, value in self.status.items():
            lines.append(f"{key:<9} {value}")
        return CommandResult(ok=True, message="Status", blocks=(Block("text", "\n".join(lines)),))

    async def _context_screen(self, args: str | None = None) -> CommandResult:
        """Build the `/context` report and the breakdown table it opens.

        The report travels as blocks so its numbers survive the modal closing,
        and the table travels as a `screen` spec so the client opens it without
        the server blocking on an answer: reading a report needs no reply.

        Args:
            args: `imported [mode] [query]` for an imported context, else nothing.

        Returns:
            A successful result carrying both.
        """
        from novacode_cli.ui import screen_specs

        if (args or "").split()[:1] == ["imported"]:
            return await self._imported_context(args)
        state = getattr(self._worker, "session_state", None)
        tracker = getattr(state, "token_tracker", None)
        usage = screen_specs.context_usage(tracker, getattr(self._worker, "model_name", None))
        return CommandResult(
            ok=True,
            message="Context window",
            blocks=screen_specs.context_blocks(usage),
            screen=screen_specs.context_spec(usage),
        )

    async def _imported_context(self, args: str | None) -> CommandResult:
        """Report the imported context this session carries, or switch its mode.

        The transcript is stored beside the session, so switching mode rewrites
        the one reference message instead of loading the source again: the same
        write path `/import` uses, which keeps both frontends agreeing on what a
        session currently carries.

        Args:
            args: `imported`, optionally followed by `full|compact|relevant` and a query.

        Returns:
            The provenance and size, the rewritten reference, or why nothing changed.
        """
        from dataclasses import replace

        from novacode_cli.session.imported_context import ImportedContext, context_budget

        state = getattr(self._worker, "session_state", None)
        manager = getattr(self._worker, "session_manager", None)
        session_id = getattr(state, "session_id", None)
        if manager is None or not session_id:
            text = "Imported context requires session storage."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        loaded = await asyncio.to_thread(ImportedContext.load, manager.sessions_dir, session_id)
        if loaded is None:
            text = "This Nova session has no imported context. Use /import first."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        rest = (args or "").split()[1:]
        budget = context_budget(getattr(state, "token_tracker", None))
        if not rest:
            report = loaded.describe(budget)
            return CommandResult(
                ok=True, message=_first_line(report), blocks=blocks_from_text(report)
            )
        wanted = replace(loaded, mode=rest[0].lower(), query=rest[1] if rest[1:] else "")
        try:
            wanted.build(budget)
        except ValueError as error:
            text = f"Session import: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        text = f"Imported context · {wanted.mode}"
        return await self._write_imported(wanted, note=text)

    async def _theme_screen(self, args: str | None) -> CommandResult:
        """Pick a theme, then tell the client to draw with it.

        Two paths, same outcome: `@@/theme <name>@@` applies immediately, while
        bare `@@/theme@@` asks the client to choose. The choice is persisted where
        the Textual UI keeps it, so both frontends agree after a restart.

        Args:
            args: A theme name, or `None` to ask the client.

        Returns:
            A successful result naming the theme, or an explicit failure for an
            unknown name. Cancelling the picker leaves the theme unchanged.
        """
        from novacode_cli.ui import screen_specs, theme_table

        requested = (args or "").strip()
        if requested:
            return self._apply_theme(requested)
        current = _persisted_theme()
        answer = await self.ask(screen_specs.theme_spec(current))
        chosen = str(answer.get("id") or "") if isinstance(answer, dict) else ""
        if not chosen:
            return CommandResult(
                ok=True,
                message="Theme unchanged.",
                blocks=(Block("text", f"Theme: {current or theme_table.DEFAULT_THEME}"),),
            )
        return self._apply_theme(chosen)

    def _apply_theme(self, name: str) -> CommandResult:
        """Persist a theme and ask the client to repaint with its palette.

        Args:
            name: Theme name, as listed by `@@/theme@@`.

        Returns:
            The result, explicit about unknown names instead of silently
            keeping the old palette.
        """
        from novacode_cli.server import frames
        from novacode_cli.ui import theme_table

        entry = theme_table.BY_NAME.get(name)
        if entry is None:
            known = "`, `".join(theme.name for theme in theme_table.THEMES)
            return CommandResult(ok=False, message=f"Unknown theme '{name}'. Try: `{known}`")
        _persist_theme(name)
        self._emit(
            frames.ui_theme(
                entry.name,
                dark=entry.dark,
                primary=entry.primary,
                background=entry.background,
                foreground=entry.foreground,
                surface=entry.surface,
                panel=entry.panel,
                muted=entry.muted,
                activity=entry.activity,
                border=entry.border,
                success=entry.success,
                warning=entry.warning,
                error=entry.error,
            )
        )
        kind = "dark" if entry.dark else "light"
        return CommandResult(
            ok=True,
            message=f"Theme: {entry.name}",
            action="theme",
            blocks=(Block("text", f"Theme set to {entry.name} ({kind})."),),
        )

    async def _settings_screen(self, _args: str | None = None) -> CommandResult:
        """Edit every preference the Textual settings screen edits.

        The whole surface is toggles and fixed choices, so one form carries it
        without a new envelope. Anything the answer leaves out keeps its current
        value, and only recognised choices are written.

        Returns:
            A successful result naming the new values, or the unchanged report.
        """
        from novacode_cli.ui import screen_specs

        config = _config()
        before = _preferences_state(config)
        answer = await self.ask(screen_specs.preferences_spec(before))
        if not isinstance(answer, dict):
            report = _settings_report(before.effort, before.budget)
            return CommandResult(
                ok=True, message="Settings unchanged.", blocks=(Block("text", report),)
            )

        after = _apply_preferences(config, before, answer)
        report = _settings_report(after.effort, after.budget)
        return CommandResult(
            ok=True,
            message="Settings saved.",
            action="settings",
            blocks=(Block("text", report),),
        )

    async def _voice_screen(self, args: str | None) -> CommandResult:
        """Open the voice section, or run a voice subcommand unchanged.

        ``/voice`` and ``/voice settings`` open the same form the Textual screen
        scrolls to. Every other subcommand (``test``, ``download``, ``doctor``,
        ``status``, ``on|off``, ``mode``, ``speak``) keeps the shared handler's
        output, because those are actions rather than settings fields.

        Args:
            args: Everything after ``/voice``.

        Returns:
            The form result, or the subcommand's captured output.
        """
        from novacode_cli.ui import screen_specs

        if (args or "").strip() not in {"", "settings"}:
            return await self._passthrough("voice", args)
        config = _config()
        before = _preferences_state(config)
        answer = await self.ask(screen_specs.voice_spec(before))
        if not isinstance(answer, dict):
            return CommandResult(
                ok=True,
                message="Voice settings unchanged.",
                blocks=(
                    Block(
                        "text",
                        f"Voice {'on' if before.voice_enabled else 'off'} · "
                        f"{before.voice_mode} · STT {before.stt_provider} · "
                        f"TTS {before.tts_provider}",
                    ),
                ),
            )
        after = _apply_preferences(config, before, answer)
        return CommandResult(
            ok=True,
            message="Voice settings saved.",
            blocks=(
                Block(
                    "text",
                    f"Voice {'on' if after.voice_enabled else 'off'} · {after.voice_mode} · "
                    f"STT {after.stt_provider} · TTS {after.tts_provider}",
                ),
            ),
        )

    async def _oracle_screen(self, args: str | None) -> CommandResult:
        """Toggle Oracle mode, report it, or edit its validated settings.

        The Textual panel's semantics are preserved: no argument toggles, an
        explicit on/off sets, status reports, and ``settings`` opens the form.
        Oracle settings are validated with the same Pydantic model before they
        are persisted, so an out-of-range answer is a refusal, not a bad config.

        Args:
            args: Everything after ``/oracle-mode``.

        Returns:
            The new state, the saved settings, or the usage line.
        """
        from novacode_cli.core import oracle
        from novacode_cli.core.oracle import SCOPE_LABELS, OracleSettings
        from novacode_cli.ui import screen_specs

        state = getattr(self._worker, "session_state", None)
        if state is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        oracle.initialize(state)
        action = (args or "").strip().lower()

        if action == "settings":
            answer = await self.ask(
                screen_specs.oracle_spec(getattr(state, "oracle_settings", None))
            )
            if not isinstance(answer, dict):
                return CommandResult(
                    ok=True,
                    message="Oracle settings unchanged.",
                    blocks=(Block("text", "Oracle settings unchanged."),),
                )
            scope = {label: key for key, label in SCOPE_LABELS.items()}.get(
                str(answer.get("scope") or ""), "task"
            )
            try:
                settings = OracleSettings(
                    target=float(answer.get("target")),
                    max_rounds=int(answer.get("max_rounds")),
                    reviewers=int(answer.get("reviewers")),
                    default_enabled=_truthy(answer.get("default_enabled")),
                    scope=scope,
                )
            except (TypeError, ValueError):
                return CommandResult(
                    ok=False,
                    message="Enter a target in (0, 10], rounds 1-20 and reviewers 1-5.",
                )
            with contextlib.suppress(Exception):
                _config().set("oracle_settings", settings.model_dump())
            state.oracle_settings = settings
            return CommandResult(
                ok=True,
                message="Oracle settings saved.",
                blocks=(
                    Block(
                        "text",
                        f"Oracle settings saved · target {settings.target:g} · "
                        f"{settings.max_rounds} rounds · {settings.reviewers} reviewer(s) · "
                        f"{SCOPE_LABELS.get(settings.scope, settings.scope)}",
                    ),
                ),
            )

        if action not in {"", "on", "off", "status"}:
            return CommandResult(ok=False, message="Use /oracle-mode on, off, status or settings")
        if action != "status":
            current = bool(getattr(state, "oracle_enabled", False))
            state.oracle_enabled = (action == "on") if action else not current
        enabled = bool(getattr(state, "oracle_enabled", False))
        text = f"Oracle mode {'on' if enabled else 'off'}."
        result = CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        if action == "status":
            run = getattr(state, "oracle_run", None)
            result = CommandResult(
                ok=True,
                message=text,
                blocks=result.blocks,
                screen=screen_specs.oracle_run_spec(run),
            )
        return result

    async def _rooms_screen(  # noqa: PLR0912, PLR0915 — one branch per action
        self, args: str | None
    ) -> CommandResult:
        """Read and write project rooms over the shared `RoomStore`.

        The Textual screen sends messages, renders the task board and edits
        channel settings; this menu covers those owner actions. Reactions,
        message edits/deletes and starting task work remain in the room runtime
        wave and are not invented here.

        Args:
            args: No subcommands.

        Returns:
            The room picker result, or an explicit failure.
        """
        import asyncio
        from pathlib import Path

        from novacode_cli.rooms.store import RoomStore
        from novacode_cli.ui import screen_specs

        if (args or "").strip():
            return not_ported("rooms")
        if self._rooms_store_instance is None:
            try:
                self._rooms_store_instance = await asyncio.to_thread(
                    RoomStore, Path(self._cwd or Path.cwd())
                )
            except Exception as error:  # noqa: BLE001 — storage is the command's job
                return CommandResult(ok=False, message=f"Room storage unavailable: {error}")
        store = self._rooms_store_instance
        while True:
            try:
                rooms = await asyncio.to_thread(
                    store.rows, "SELECT id, name, topic FROM rooms ORDER BY id"
                )
            except Exception as error:  # noqa: BLE001 — sqlite failure is a command failure
                return CommandResult(ok=False, message=f"Could not read rooms: {error}")
            answer = await self.ask(screen_specs.rooms_spec(rooms))
            if not isinstance(answer, dict):
                return CommandResult(
                    ok=True,
                    message="Rooms closed.",
                    blocks=(Block("text", "Rooms closed."),),
                )
            chosen = str(answer.get("id") or "")
            if chosen == "__close__":
                return CommandResult(
                    ok=True,
                    message="Rooms closed.",
                    blocks=(Block("text", "Rooms closed."),),
                )
            if chosen == "__new__":
                form = await self.ask(screen_specs.room_create_spec())
                if not isinstance(form, dict):
                    continue
                try:
                    await asyncio.to_thread(store.create_channel, str(form.get("name") or ""))
                except Exception as error:  # noqa: BLE001 — keep the picker usable
                    self.notify(f"Could not create channel: {error}", level="error")
                continue
            state = next((room for room in rooms if str(room.get("id")) == chosen), None)
            if state is None:
                continue
            action = await self.ask(screen_specs.room_actions_spec(chosen))
            if not isinstance(action, dict):
                continue
            verb = str(action.get("id") or "")
            if verb in {"", "back"}:
                continue
            if verb == "read":
                try:
                    messages = await asyncio.to_thread(store.history, chosen)
                except Exception as error:  # noqa: BLE001 — keep the menu usable
                    self.notify(f"Could not read {chosen}: {error}", level="error")
                    continue
                await self.ask(screen_specs.room_history_spec(chosen, messages))
                continue
            if verb == "messages":
                await self._room_messages(store, chosen)
                continue
            if verb == "send":
                form = await self.ask(screen_specs.room_send_spec(chosen))
                if not isinstance(form, dict):
                    continue
                try:
                    await asyncio.to_thread(
                        store.message, chosen, "owner", str(form.get("text") or "")
                    )
                except Exception as error:  # noqa: BLE001 — keep the menu usable
                    self.notify(f"Could not send: {error}", level="error")
                    continue
                messages = await asyncio.to_thread(store.history, chosen)
                await self.ask(screen_specs.room_history_spec(chosen, messages))
                continue
            if verb == "board":
                try:
                    board = await asyncio.to_thread(store.board, chosen, "owner")
                except Exception as error:  # noqa: BLE001 — keep the menu usable
                    self.notify(f"Could not read the board: {error}", level="error")
                    continue
                await self.ask(screen_specs.room_board_spec(chosen, board))
                continue
            if verb == "settings":
                form = await self.ask(screen_specs.room_settings_spec(state))
                if not isinstance(form, dict):
                    continue
                try:
                    await asyncio.to_thread(
                        store.update_channel,
                        chosen,
                        str(form.get("name") or ""),
                        str(form.get("topic") or ""),
                    )
                except Exception as error:  # noqa: BLE001 — keep the menu usable
                    self.notify(f"Could not save settings: {error}", level="error")
                continue

    async def _room_messages(  # noqa: PLR0912 — one branch per message action
        self,
        store: Any,  # noqa: ANN401 — a RoomStore
        room: str,
    ) -> None:
        """Select, inspect and act on individual messages."""
        import asyncio

        from novacode_cli.ui import screen_specs

        while True:
            try:
                messages = await asyncio.to_thread(store.history, room)
            except Exception as error:  # noqa: BLE001 — keep the menu usable
                self.notify(f"Could not read {room}: {error}", level="error")
                return
            answer = await self.ask(screen_specs.room_messages_spec(room, messages))
            if not isinstance(answer, dict):
                return
            key = str(answer.get("id") or "")
            message = next((item for item in messages if str(item.get("id")) == key), None)
            if message is None:
                continue
            action = await self.ask(screen_specs.room_message_actions_spec(message))
            if not isinstance(action, dict):
                continue
            verb = str(action.get("id") or "")
            if verb in {"", "back"}:
                return
            if verb == "view":
                await self.ask(screen_specs.room_message_spec(room, message))
                continue
            try:
                if verb == "edit":
                    form = await self.ask(screen_specs.room_edit_spec(message))
                    if isinstance(form, dict):
                        await asyncio.to_thread(
                            store.change_message,
                            room,
                            int(key),
                            "edit",
                            str(form.get("text") or ""),
                        )
                elif verb == "delete":
                    confirmed = await self.ask(
                        ScreenSpec("confirm", {"message": f"Delete message #{key}?"})
                    )
                    if confirmed is True:
                        await asyncio.to_thread(store.change_message, room, int(key), "delete")
                elif verb == "react":
                    form = await self.ask(screen_specs.room_react_spec())
                    if isinstance(form, dict):
                        await asyncio.to_thread(
                            store.react, room, int(key), "owner", str(form.get("emoji") or "")
                        )
                elif verb == "pin":
                    await asyncio.to_thread(store.change_message, room, int(key), "pin")
            except Exception as error:  # noqa: BLE001 — keep the message menu usable
                self.notify(f"Message action failed: {error}", level="error")

    async def _teammates_screen(self, args: str | None) -> CommandResult:
        """Manage the detached teammate and inspect its work.

        The socket vocabulary is a roster, one action menu per teammate and the
        same small forms the Textual panel uses. Every action is sent through the
        authenticated worker API; viewing status uses ``launch=False`` so it never
        starts a worker by accident.

        Args:
            args: No subcommands.

        Returns:
            A closing report, or an explicit failure when the worker is down.
        """
        from novacode_cli.teammates.service import request
        from novacode_cli.ui import screen_specs

        if (args or "").strip():
            return not_ported("teammates")
        while True:
            try:
                status = await request("status", launch=False)
            except Exception as error:  # noqa: BLE001 — a stopped worker is a command failure
                return CommandResult(
                    ok=False,
                    message=f"Teammate worker unavailable: {error}. Start it in Textual.",
                )
            roster = status.get("agents")
            if roster is None:
                roster = [status] if status.get("name") else []
            answer = await self.ask(screen_specs.teammates_spec(roster))
            if not isinstance(answer, dict):
                return CommandResult(
                    ok=True,
                    message="Teammates closed.",
                    blocks=(Block("text", "Teammates closed."),),
                )
            chosen = str(answer.get("id") or "")
            if chosen == "__close__":
                return CommandResult(
                    ok=True,
                    message="Teammates closed.",
                    blocks=(Block("text", "Teammates closed."),),
                )
            if chosen == "__new__":
                form = await self.ask(screen_specs.teammate_form_spec(None))
                if not isinstance(form, dict):
                    continue
                await self._teammate_request(request, "create", self._teammate_payload(form))
                continue
            state = next(
                (
                    item
                    for item in roster
                    if str(item.get("agent_id") or item.get("thread") or item.get("name")) == chosen
                ),
                None,
            )
            if state is None:
                continue
            action = await self.ask(screen_specs.teammate_actions_spec(state))
            if not isinstance(action, dict):
                continue
            verb = str(action.get("id") or "")
            if verb in {"", "back"}:
                continue
            await self._teammate_action(request, screen_specs, verb, state)

    def _teammate_payload(
        self, form: dict[str, Any], *, agent_id: str | None = None
    ) -> dict[str, Any]:
        """Translate a teammate form into the worker's create/edit payload."""
        from novacode_cli.ui import screen_specs

        payload: dict[str, Any] = {
            "name": str(form.get("name") or ""),
            "role": str(form.get("role") or ""),
        }
        for field, provider_key, model_key in (
            ("model", "model_provider", "model"),
            ("vision", "vision_provider", "vision_model"),
        ):
            label = str(form.get(provider_key) or "Use Nova's provider")
            model = str(form.get(model_key) or "").strip()
            if label != "Use Nova's provider":
                payload[field] = {
                    "provider": screen_specs.provider_id(label),
                    "model": model,
                }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        return payload

    async def _teammate_request(self, request: Any, action: str, payload: dict[str, Any]) -> None:  # noqa: ANN401
        """Send one worker action and surface its error without closing the screen."""
        try:
            await request(action, payload)
        except Exception as error:  # noqa: BLE001 — keep the roster open for retry
            self.notify(f"Teammate {action} failed: {error}", level="error")
        else:
            self.notify(f"Teammate {action.replace('_', ' ')} done.")

    async def _teammate_action(  # noqa: PLR0911, PLR0912 — one branch per action
        self,
        request: Any,  # noqa: ANN401 — the worker request function
        screen_specs: Any,  # noqa: ANN401 — the pure spec module
        verb: str,
        state: dict[str, Any],
    ) -> None:
        """Carry out one action-menu choice."""
        name = str(state.get("name") or "Teammate")
        agent_id = str(state.get("agent_id") or state.get("thread") or "")
        if verb == "history":
            await self.ask(screen_specs.teammate_detail_spec(state))
            return
        if verb == "preview":
            try:
                result = await request("preview", {"agent_id": agent_id})
            except Exception as error:  # noqa: BLE001 — report and keep the menu
                self.notify(f"Preview failed: {error}", level="error")
                return
            await self.ask(screen_specs.teammate_preview_spec(name, str(result.get("url") or "")))
            return
        if verb == "edit":
            form = await self.ask(screen_specs.teammate_form_spec(state))
            if isinstance(form, dict):
                await self._teammate_request(
                    request, "edit", self._teammate_payload(form, agent_id=agent_id)
                )
            return
        if verb == "delete":
            form = await self.ask(screen_specs.teammate_delete_spec(name))
            if isinstance(form, dict):
                await self._teammate_request(
                    request,
                    "delete",
                    {"agent_id": agent_id, "confirm": str(form.get("confirm") or "")},
                )
            return
        if verb == "assign":
            form = await self.ask(screen_specs.teammate_assign_spec(name))
            if isinstance(form, dict):
                await self._teammate_request(
                    request, "assign", {"agent_id": agent_id, "text": str(form.get("text") or "")}
                )
            return
        if verb == "auto_approve":
            form = await self.ask(screen_specs.teammate_auto_spec(state))
            if isinstance(form, dict):
                await self._teammate_request(
                    request,
                    "set_auto_approve",
                    {"agent_id": agent_id, "auto_approve": _truthy(form.get("auto_approve"))},
                )
            return
        if verb.startswith(("approve:", "deny:")):
            action, key = verb.split(":", 1)
            await self._teammate_request(
                request,
                "decide",
                {"agent_id": agent_id, "id": key, "approve": action == "approve"},
            )
            return
        if verb in {"pause", "resume", "stop"}:
            await self._teammate_request(request, verb, {"agent_id": agent_id})

    def _proactive_store(self) -> Any:  # noqa: ANN401 — a ProactiveStore
        """Open the user's proactive SQLite store once per session."""
        if self._proactive_store_instance is None:
            from novacode_cli.config.config import settings
            from novacode_cli.proactive import ProactiveStore

            self._proactive_store_instance = ProactiveStore(
                settings.user_deepagents_dir / "proactive.sqlite3"
            )
        return self._proactive_store_instance

    def _start_proactive_runtime(self, store: Any) -> None:  # noqa: ANN401 — a ProactiveStore
        """Start the run/tag loops so queued agents actually execute."""
        if self._proactive_runtime is not None:
            return
        from novacode_cli.config.model_create import create_model
        from novacode_cli.proactive_runtime import ProactiveRuntime

        def notify(message: str, **_kwargs: Any) -> None:
            self.notify(message)

        self._proactive_runtime = ProactiveRuntime(store, create_model, notify)

    async def _agent_server_command(  # noqa: PLR0911 — one return per action
        self, args: str | None
    ) -> CommandResult:
        """Report or control the local LangGraph server async subagents run on.

        Status and logs are read-only; start/stop/restart are wrapped in the
        same wording as the Textual handler. Long actions run in a thread and
        report their outcome instead of pretending to be instant.

        Args:
            args: `status|logs|start|stop|restart`, defaulting to status.

        Returns:
            The status report, the log tail, or the action's outcome.
        """
        import asyncio

        from novacode_cli.agents import server_launcher
        from novacode_cli.agents.default_subagents.async_subagents import (
            async_agents_available,
            async_agents_see_workspace,
        )

        parts = (args or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        status = server_launcher.agent_server_status()

        if action in {"", "status"}:
            reachable = await asyncio.to_thread(async_agents_available, refresh=True)
            here = await asyncio.to_thread(async_agents_see_workspace)
            lines = [
                f"local server : {'running' if status['running'] else 'stopped'}"
                + (f" at {status['url']}" if status["url"] else ""),
                f"async agents : {'available' if reachable else 'unavailable'}"
                + (" (this session's server)" if status["running"] else "")
                + (
                    " — not used here: the server is rooted at another project"
                    if reachable and not here
                    else ""
                ),
                "extra        : "
                + (
                    "installed"
                    if server_launcher.server_extra_available()
                    else "missing (uv sync --extra agents-server)"
                ),
            ]
            if status["log_path"]:
                lines.append(f"log          : {status['log_path']}")
            if status["graphs"]:
                lines.append(f"graphs       : {len(status['graphs'])} registered")
            text = "\n".join(lines)
            return CommandResult(ok=True, message="Agent server", blocks=(Block("text", text),))

        if action == "logs":
            if not status["log_path"]:
                return CommandResult(
                    ok=True,
                    message="no agent server log",
                    blocks=(Block("text", "no agent server log: nothing has been launched"),),
                )
            text = await asyncio.to_thread(_read_log_tail, str(status["log_path"]))
            tail = "\n".join(text.splitlines()[-20:])
            return CommandResult(
                ok=True,
                message="Agent server log (tail)",
                blocks=(Block("text", tail or "(empty log)"),),
            )

        if action == "stop":
            preserved = await asyncio.to_thread(server_launcher.shutdown_agent_server)
            note = f" (log kept at {preserved})" if preserved else ""
            text = f"agent server: stopped{note}"
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

        if action in {"start", "restart"}:
            if action == "restart":
                await asyncio.to_thread(server_launcher.shutdown_agent_server)
            started = await asyncio.to_thread(server_launcher.ensure_agent_server)
            if started is None:
                text = (
                    "agent server: not started - another server already answers, "
                    "or the agents-server extra is not installed"
                )
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
            text = f"agent server: running at {started.url}"
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

        return CommandResult(
            ok=False, message="usage: /agent-server [status|start|stop|restart|logs]"
        )

    async def _ci_fix_command(self, args: str | None) -> CommandResult:
        """Fetch a failed GitHub Actions run and hand its log to the agent.

        The prompt is returned as `prompt`, so the host starts the turn exactly
        as the Textual UI does.

        Args:
            args: An optional run id.

        Returns:
            The agent prompt, or the fetch failure.
        """
        import asyncio
        from pathlib import Path

        from novacode_cli.commands.ci_fix import CIFixError, build_ci_fix_prompt
        from novacode_cli.config.config import settings

        run_id = (args or "").strip() or None
        try:
            prompt = await asyncio.to_thread(
                build_ci_fix_prompt,
                settings.get_workspace_root() or Path(self._cwd or Path.cwd()),
                run_id,
            )
        except (CIFixError, OSError, ValueError, KeyError, TypeError) as error:
            return CommandResult(ok=False, message=f"/ci-fix: {error}")
        return CommandResult(
            ok=True,
            message="Handing the failed run to the agent…",
            prompt=prompt,
            blocks=(Block("text", "Handing the failed run to the agent…"),),
        )

    async def _btw_command(self, args: str | None) -> CommandResult:
        """Answer a side question on its own thread, without the main agent.

        The question runs as a background task on the shared btw agent and the
        answer lands as a transcript block; the main turn is untouched.

        Args:
            args: The side question.

        Returns:
            An explicit refusal without a question, else a "started" report.
        """
        import asyncio
        import uuid

        from novacode_cli import agent_stream
        from novacode_cli.commands.side_commands import get_btw_agent

        question = (args or "").strip()
        if not question:
            return CommandResult(ok=False, message="Usage: /btw <question>")
        try:
            agent = await asyncio.to_thread(get_btw_agent)
        except Exception as error:  # noqa: BLE001 — agent build failures are command failures
            return CommandResult(
                ok=False, message=f"↩ btw: could not start web-search agent — {error}"
            )

        thread_id = f"btw-{uuid.uuid4().hex[:12]}"
        question_text = question

        def _btw_session() -> Any:  # noqa: ANN401 — a minimal session shim
            """Only thread_id matters: no checkpointer, goal or plan injection."""
            from types import SimpleNamespace

            return SimpleNamespace(
                thread_id=thread_id,
                active_goal=None,
                plan_mode_enabled=False,
                auto_approve=True,
            )

        async def run() -> None:
            parts: list[str] = []
            try:
                async for event in agent_stream.run_agent_stream(
                    question_text, agent, "btw-agent", _btw_session(), backend=None
                ):
                    text = getattr(event, "text", None)
                    if text:
                        parts.append(text)
            except Exception as error:  # noqa: BLE001 — a side question must not kill the session
                self._emit(_event_frame_or_none("Error", str(error)))
                return
            answer = "".join(parts).strip() or "(no answer)"
            self.log(Block("text", f"↩ btw: {answer}"))  # a completed result block

        self._btw_task = asyncio.create_task(run())
        return CommandResult(
            ok=True,
            message="Side question running.",
            blocks=(Block("text", f"↩ btw: {question[:55]}…"),),
        )

    async def _reindex_command(self, _args: str | None = None) -> CommandResult:
        """Rebuild the semantic code-search index as a background task.

        Semble must be installed and the code-search tools importable; both are
        checked before anything runs. Progress is a `StatusUpdate`, the outcome
        a transcript block.

        Returns:
            A "started" report, or the reason reindexing is unavailable.
        """
        import asyncio

        try:
            from novacode_cli.tools.code_search_tools import (
                _get_index,
                _is_semble_available,
                _reset_index,
            )
        except Exception as error:  # noqa: BLE001 — availability is a command failure
            return CommandResult(ok=False, message=f"Code search unavailable: {error}")
        if not _is_semble_available():
            from novacode_cli.install_commands import missing_package

            return CommandResult(
                ok=False, message=f"Code search is not available: {missing_package('semble')}"
            )
        from novacode_cli.config.config import settings

        workspace = settings.get_workspace_root()
        if not workspace:
            workspace = self._cwd
        if not workspace:
            return CommandResult(ok=False, message="No workspace to index.")

        def status(message: str | None) -> None:
            from novacode_cli import ui_events as ev
            from novacode_cli.server import frames

            frame = frames.event_frame(ev.StatusUpdate(message))
            if frame is not None:
                self._emit(frame)

        async def run() -> None:
            status("re-indexing…")
            try:
                await asyncio.to_thread(_reset_index)
                index = await asyncio.to_thread(_get_index, workspace)
                if index is not None:
                    self.log(Block("text", f"● Code search index rebuilt for {workspace}"))
                else:
                    self.log(Block("text", "Failed to build code search index."))
            except Exception as error:  # noqa: BLE001 — a rebuild must not kill the session
                self.log(Block("text", f"Reindex failed: {error}"))
            finally:
                status(None)

        self._reindex_task = asyncio.create_task(run())
        return CommandResult(
            ok=True, message="Reindex started.", blocks=(Block("text", "Reindex started."),)
        )

    async def _notifications_command(self, args: str | None) -> CommandResult:
        """List, dismiss, approve or clear session notifications.

        Args:
            args: `[list] | dismiss <id> | approve <id> | clear`.

        Returns:
            The requested action's report.
        """
        state = getattr(self._worker, "session_state", None)
        if state is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        parts = (args or "").strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""

        if sub in {"clear", "reset"}:
            for notification in list(getattr(state, "notifications", None) or []):
                action_id = getattr(notification, "action_id", None)
                if action_id and not getattr(notification, "dismissed", False):
                    state.resolve_approval(action_id, approve=False)
            cleared = state.clear_notifications()
            text = f"Cleared {cleared} notification(s)."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        if sub in {"dismiss", "rm", "ack"} and target:
            dismissed = state.dismiss_notification(target)
            text = f"Dismissed {target}." if dismissed else f"Notification {target} not found."
            return CommandResult(ok=dismissed, message=text, blocks=(Block("text", text),))
        if sub == "approve" and target:
            approved = state.resolve_notification(target, approve=True)
            text = f"Approved {target}." if approved else f"Notification {target} not found."
            return CommandResult(ok=approved, message=text, blocks=(Block("text", text),))

        notifications = list(getattr(state, "notifications", None) or [])
        if not notifications:
            text = "No notifications."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        rows = [
            f"- {getattr(n, 'id', '?')} · [{getattr(n, 'level', 'info')}] "
            f"{getattr(n, 'title', '')}: {getattr(n, 'message', '')}"
            for n in notifications
            if not getattr(n, "dismissed", False)
        ]
        text = "\\n".join(rows) if rows else "No pending notifications."
        return CommandResult(ok=True, message="Notifications", blocks=(Block("text", text),))

    async def _images_command(  # noqa: PLR0911 — one return per subcommand
        self, args: str | None
    ) -> CommandResult:
        """List, remove or clear the conversation's tracked images."""
        tracker = getattr(self._worker, "image_tracker", None)
        if tracker is None:
            return CommandResult(ok=False, message="Image tracking not available.")
        parts = (args or "").strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"

        if sub == "list":
            images = tracker.list_images()
            if not images:
                text = "No images in conversation."
                return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
            rows = [
                f"- {image['id']} · {image['format'].upper()} · {image['size_kb']:.1f} KB"
                for image in images
            ]
            text = "\\n".join(rows)
            return CommandResult(
                ok=True, message=f"{len(images)} image(s)", blocks=(Block("text", text),)
            )
        if sub == "remove" and len(parts) > 1:
            image_id = parts[1].strip()
            if tracker.remove_image(image_id):
                text = f"Removed {image_id} from conversation."
                return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
            return CommandResult(ok=False, message=f"Image not found: {image_id}")
        if sub == "clear":
            count = tracker.count
            if count == 0:
                text = "No images to clear."
                return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
            tracker.clear()
            text = f"Cleared {count} image(s) from conversation."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        return CommandResult(ok=False, message="Usage: /images [list | remove <id> | clear]")

    async def _research_command(self, args: str | None) -> CommandResult:
        """Launch the research swarm; its prompt runs as a normal turn.

        Without a query the handler's usage text is captured as blocks. With
        one, the swarm prompt is returned as `prompt`, so the host starts the
        turn exactly as the Textual UI's execute function does.

        Args:
            args: The research query, optionally with mode/flags.

        Returns:
            The usage text or the started report.
        """
        import contextlib
        import io

        from novacode_cli.commands.research_handler import handle_research_command
        from novacode_cli.config.config import console

        worker = self._worker
        query = (args or "").strip()

        async def execute(prompt: str, **_kwargs: Any) -> None:
            self._research_prompt = prompt

        self._research_prompt = None
        captured = io.StringIO()
        try:
            with (
                contextlib.redirect_stdout(captured),
                console.capture() as rich_captured,
            ):
                await handle_research_command(
                    worker.agent,
                    worker.session_state,
                    getattr(worker, "token_tracker", None),
                    cmd_args=query or None,
                    execute_fn=execute,
                )
        except Exception as error:  # noqa: BLE001 — a swarm failure is a command failure
            return CommandResult(ok=False, message=f"/research failed: {error}")
        output = strip_ansi(rich_captured.get() + captured.getvalue()).strip()
        if self._research_prompt:
            return CommandResult(
                ok=True,
                message="Research swarm started.",
                prompt=self._research_prompt,
                blocks=(Block("text", output or "Research swarm started."),),
            )
        return CommandResult(
            ok=True,
            message="Research usage.",
            blocks=blocks_from_text(output or "Usage: /research <query>"),
        )

    async def _tasks_command(self, _args: str | None = None) -> CommandResult:
        """Show this session's background tasks.

        The socket worker does not carry the Textual app's agent-job registry,
        so the report is the honest empty state today: shell jobs and killed
        panels belong to the shell/PTY wave.

        Returns:
            The tasks table, or an explicit failure when the worker is absent.
        """
        from novacode_cli.ui import screen_specs

        if getattr(self._worker, "session_state", None) is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        empty = "No background tasks."
        return CommandResult(
            ok=True,
            message=empty,
            blocks=(Block("text", empty),),
            screen=screen_specs.tasks_spec([]),
        )

    async def _files_command(self, _args: str | None = None) -> CommandResult:
        """Report this session's file reads and writes from the session tracker."""
        from novacode_cli.tracking.file_tracker import get_session_tracker

        tracker = get_session_tracker()
        lines = [
            f"Read     {len(tracker.files_read)} file(s) / {tracker.total_reads} op(s)",
            f"Modified {len(tracker.files_written)} file(s) / {tracker.total_writes} op(s)",
        ]
        if tracker.rejected_edits:
            lines.append(f"Rejected edits (unread files): {tracker.rejected_edits}")
        if tracker.read_order:
            lines.append("")
            lines.append("Recently read")
            lines.extend(f"  {path}" for path in tracker.read_order[-15:])
        if tracker.write_order:
            lines.append("")
            lines.append("Recently modified")
            lines.extend(f"  {path}" for path in tracker.write_order[-15:])
        text = "\n".join(lines)
        return CommandResult(
            ok=True, message="Session file operations", blocks=(Block("text", text),)
        )

    async def _restore_command(  # noqa: PLR0911 — one return per command result
        self, args: str | None
    ) -> CommandResult:
        """Restore a file snapshot by index, path fragment, or a picker.

        Args:
            args: An index (1-based), a path fragment, or nothing to pick.

        Returns:
            The restore outcome.
        """
        import asyncio

        from novacode_cli.recovery import get_recovery_manager
        from novacode_cli.ui import screen_specs

        manager = get_recovery_manager()
        if manager is None:
            return CommandResult(ok=False, message="No recovery manager active for this session.")
        snapshots = manager.list_snapshots(include_past_sessions=True)
        if not snapshots:
            return CommandResult(
                ok=False,
                message=(
                    "No file snapshots found. Snapshots are created before rm/write_file/edit_file."
                ),
            )

        def restore(index: int) -> CommandResult:
            session_id, entry = snapshots[index]
            ok = manager.restore(entry, session_id=session_id)
            text = (
                f"● Restored {entry.original_path}"
                if ok
                else f"Failed to restore {entry.original_path}"
            )
            return CommandResult(ok=ok, message=text, blocks=(Block("text", text),))

        arg = (args or "").strip()
        if arg.isdigit():
            index = int(arg) - 1
            if not 0 <= index < len(snapshots):
                return CommandResult(ok=False, message=f"No snapshot at index {arg}.")
            return await asyncio.to_thread(restore, index)
        if arg:
            for index, (_sid, entry) in enumerate(snapshots):
                if arg in entry.original_path or entry.original_path.endswith(arg):
                    return await asyncio.to_thread(restore, index)
            return CommandResult(ok=False, message=f"No snapshot matching '{arg}'.")

        picker = await self.ask(screen_specs.restore_spec(snapshots))
        chosen = str(picker.get("id") or "") if isinstance(picker, dict) else ""
        if not chosen.isdigit():
            return CommandResult(
                ok=True, message="Restored nothing.", blocks=(Block("text", "Restored nothing."),)
            )
        return await asyncio.to_thread(restore, int(chosen))

    async def _tokens_command(self, _args: str | None = None) -> CommandResult:
        """Show session token usage from the tracker (`/tokens`, `/cost`)."""
        from novacode_cli.ui import screen_specs

        state = getattr(self._worker, "session_state", None)
        tracker = getattr(state, "token_tracker", None)
        if tracker is None:
            # The worker owns the tracker; the session proxy may not expose it.
            tracker = getattr(self._worker, "token_tracker", None)
        message = "Token usage"
        spec = screen_specs.token_view_spec(tracker)
        first = next((line for line in spec.payload["markdown"].splitlines() if line), "")
        return CommandResult(ok=True, message=message, blocks=(Block("text", first),), screen=spec)

    async def _export_command(self, args: str | None) -> CommandResult:
        """Write the session to a file in the chosen format.

        With a connected client the command first asks what to export —
        Markdown or JSON, with optional thinking and tool activity. Without a
        client to answer (headless), the documented default path runs: the
        self-contained HTML page. A session without persistence is an explicit
        failure rather than a silent no-op.

        Args:
            args: An optional destination path; the default lives under
                `NOVA_HOME/exports`.

        Returns:
            The written path, or the reason nothing was written.
        """
        import asyncio

        from novacode_cli.config import config as nova_config
        from novacode_cli.session.history_export import export_json, export_markdown

        worker = self._worker
        manager = getattr(worker, "session_manager", None)
        state = getattr(worker, "session_state", None)
        session_id = getattr(state, "session_id", None)
        if manager is None or not session_id:
            return CommandResult(ok=False, message="Session history is off for this run.")
        await asyncio.to_thread(manager.history_tree, session_id)

        parts = (args or "").strip().split(maxsplit=1)
        options = {"format": "html", "thinking": True, "tools": True}
        if self._ask_screen is not None:
            answer = await self.ask(
                ScreenSpec(
                    "form",
                    {
                        "fields": [
                            {
                                "key": "format",
                                "label": "Format",
                                "value": "markdown",
                                "options": ["markdown", "json"],
                            },
                            {
                                "key": "thinking",
                                "label": "Include thinking",
                                "value": "on",
                                "toggle": True,
                            },
                            {
                                "key": "tools",
                                "label": "Include tools",
                                "value": "on",
                                "toggle": True,
                            },
                        ]
                    },
                    title="Export session",
                )
            )
            if not answer:
                return CommandResult(ok=True, message="Export cancelled.")
            options = {
                "format": str(answer.get("format") or "markdown"),
                "thinking": answer.get("thinking") is True,
                "tools": answer.get("tools") is True,
            }

        def build() -> str:
            tree = manager.history_tree(session_id)
            branches = sum(max(0, len(node.children) - 1) for node in tree.nodes.values())
            branches += max(0, len(tree.roots) - 1)
            first = next((node.text for node in tree.nodes.values() if node.kind == "user"), "")
            title = " ".join(first.split())[:80] or "Nova session"
            subtitle = (
                f"Nova session {session_id[:8]} · {getattr(worker, 'model_name', None) or 'model'}"
                f" · {len(tree.nodes)} messages · {branches + 1} branch(es)"
            )
            if options["format"] == "markdown":
                return export_markdown(
                    tree,
                    title=title,
                    subtitle=subtitle,
                    include_thinking=options["thinking"],
                    include_tools=options["tools"],
                )
            if options["format"] == "json":
                return export_json(
                    tree,
                    title=title,
                    subtitle=subtitle,
                    include_thinking=options["thinking"],
                    include_tools=options["tools"],
                )
            return _export_page(tree, session_id, str(getattr(worker, "model_name", None) or ""))

        page = await asyncio.to_thread(build)
        suffix = {"markdown": ".md", "json": ".json"}.get(options["format"], ".html")
        default = f"nova-session-{session_id[:8]}{suffix}"

        def write() -> Path:
            if parts:
                target = Path(parts[0].strip().strip('"')).expanduser()
                if not target.suffix:
                    target = target.with_suffix(suffix)
            else:
                target = Path(nova_config.HOME_DIR) / "exports" / default
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page, encoding="utf-8")
            return target.resolve()

        try:
            target = await asyncio.to_thread(write)
        except OSError as error:
            return CommandResult(ok=False, message=f"/export failed: {error}")
        return CommandResult(
            ok=True,
            message=f"Exported to {target}.",
            blocks=(Block("text", f"● Exported to {target}"),),
        )

    async def _worktree_command(  # noqa: PLR0911 — one return per command result
        self, args: str | None
    ) -> CommandResult:
        """List, create or remove Nova worktrees.

        ``list`` opens the picker; ``remove <name>`` asks for a confirm before
        deleting; a plain name creates (or reuses) the worktree. Spawning a
        session tab in it stays with the spawning wave.

        Args:
            args: `list`, `remove <name>`, `<name>[: task]`, or nothing.

        Returns:
            The picker/report result, or an explicit failure outside git.
        """
        import asyncio
        from pathlib import Path

        from novacode_cli.sessions import worktree as wt
        from novacode_cli.ui import screen_specs

        base = Path(self._cwd or Path.cwd())
        repo = await asyncio.to_thread(wt.repo_root, base)
        if repo is None or not await asyncio.to_thread(wt.has_commits, repo):
            return CommandResult(
                ok=False,
                message=(
                    "/worktree needs a git repository with at least one commit "
                    "(git init, then commit) — a worktree branches from HEAD."
                ),
            )
        text = (args or "").strip()
        sub, _, rest = text.partition(" ")
        if sub.lower() in {"", "list"}:
            found = await asyncio.to_thread(wt.nova_worktrees, repo)
            answer = await self.ask(screen_specs.worktrees_spec(found))
            chosen = str(answer.get("id") or "") if isinstance(answer, dict) else ""
            if chosen and chosen != "__none__":
                return CommandResult(
                    ok=True,
                    message=f"Worktree {chosen} selected.",
                    create=f"worktree:{chosen}",
                    blocks=(Block("text", f"Worktree {chosen} selected."),),
                )
            return CommandResult(
                ok=True,
                message="Worktrees.",
                blocks=(Block("text", f"{len(found)} worktree(s)."),),
            )
        if sub.lower() == "remove":
            branch = f"nova/{wt.slugify(rest, fallback='')}"
            found = dict(await asyncio.to_thread(wt.nova_worktrees, repo))
            path = found.get(branch)
            if not rest.strip() or path is None:
                return CommandResult(ok=False, message=f"No worktree on {branch}.")
            confirmed = await self.ask(
                ScreenSpec("confirm", {"message": f"Remove worktree {branch}?"})
            )
            if confirmed is not True:
                return CommandResult(ok=True, message="Kept.", blocks=(Block("text", "Kept."),))
            outcome = await asyncio.to_thread(wt.remove_worktree, path, repo=repo)
            return CommandResult(
                ok=True,
                message=f"worktree {outcome}",
                blocks=(Block("text", f"worktree {outcome}"),),
            )
        name, _, _task = text.partition(":")
        if not name.strip():
            return CommandResult(ok=False, message="Usage: /worktree <name>[: task]")
        try:
            info = await asyncio.to_thread(
                lambda: wt.create_worktree(name.strip(), repo=repo, session_id="port")
            )
        except Exception as error:  # noqa: BLE001 — surface, don't kill the session
            return CommandResult(ok=False, message=f"✖ could not create worktree: {error}")
        return CommandResult(
            ok=True,
            message=f"Worktree {name.strip()} ready at {info.path}.",
            create=f"worktree:{info.path}",
            blocks=(Block("text", f"Worktree {name.strip()} ready at {info.path}"),),
        )

    async def _copy_command(self, args: str | None) -> CommandResult:
        """Copy the last Nova response (or the whole chat) to the clipboard.

        The client owns the clipboard, so the result is an `action` naming what
        to copy; the transcript itself already holds the text.

        Args:
            args: `all` for the whole conversation, or empty for the last reply.

        Returns:
            A result whose action the client performs.
        """
        want_all = (args or "").strip().lower() == "all"
        what = "conversation" if want_all else "last response"
        return CommandResult(
            ok=True,
            message=f"Copying the {what} to the clipboard.",
            action="copy" if not want_all else "copy-all",
            blocks=(Block("text", f"Copying the {what} to the clipboard."),),
        )

    async def _steer_command(self, args: str | None) -> CommandResult:
        """Add, list, clear or remove persistent steering instructions."""
        import re

        from novacode_cli.bootstrap.steering import classify_instruction

        state = getattr(self._worker, "session_state", None)
        if state is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        if getattr(state, "steering_instructions", None) is None:
            state.steering_instructions = []
        instructions = state.steering_instructions
        text = (args or "").strip()
        low = text.lower()

        if not text or low in {"list", "ls", "show"}:
            if not instructions:
                body = "Steering instructions\n  (none — /steer <instruction> to add)"
            else:
                rows = [
                    f"  {index}. {entry.label}: {entry.instruction}"
                    for index, entry in enumerate(instructions, 1)
                ]
                body = "Steering instructions\n" + "\n".join(rows)
            return CommandResult(ok=True, message="Steering", blocks=(Block("text", body),))
        if low in {"clear", "reset"}:
            removed = len(instructions)
            instructions.clear()
            body = f"Cleared {removed} steering instruction(s)."
            return CommandResult(ok=True, message=body, blocks=(Block("text", body),))
        match = re.match(r"(?:remove|rm|del|delete)\s+(\d+)", text, re.IGNORECASE)
        if match:
            index = int(match.group(1)) - 1
            if not 0 <= index < len(instructions):
                return CommandResult(ok=False, message="No steering instruction with that number.")
            removed = instructions.pop(index)
            body = f"Removed: {removed.label}: {removed.instruction}"
            return CommandResult(ok=True, message=body, blocks=(Block("text", body),))
        label = classify_instruction(text)
        from novacode_cli.bootstrap.steering import SteeringInstruction

        entry = SteeringInstruction(label, text)
        instructions.append(entry)
        body = f"Steering added ({entry.label}): {entry.instruction}"
        return CommandResult(ok=True, message=body, blocks=(Block("text", body),))

    async def _redirect_command(self, args: str | None) -> CommandResult:
        """Stop the running task and send a message next.

        The port cannot cancel its own turn, so the result carries both a
        `cancel` action (which the client sends as a `cancel` frame) and the
        queued text as a `prompt`, which the host runs after the turn ends.

        Args:
            args: The replacement message.

        Returns:
            A cancel action plus the redirect prompt.
        """
        from novacode_cli.remote.processor import redirect_text

        message = (redirect_text(f"/redirect {args or ''}") or "").strip()
        if not message:
            return CommandResult(ok=False, message="Usage: /redirect <what to do instead>")
        return CommandResult(
            ok=True,
            message="Stopping and redirecting.",
            action="cancel",
            prompt=message,
            blocks=(Block("text", f"Redirecting: {message}"),),
        )

    async def _nova_command(self, _args: str | None = None) -> CommandResult:
        """Toggle the avatar and hand the client its frames.

        The choice is persisted where the Textual UI keeps it, so both frontends
        agree after a restart. Turning the avatar on ships every animation frame
        once; the client then plays them from its own activity, so an animation
        costs no frames per tick.

        Args:
            _args: Ignored: `/nova` takes no argument.

        Returns:
            A successful toggle, or why the sprite could not be built.
        """
        from novacode_cli.config.nova_config import NovaConfig
        from novacode_cli.ui import avatar

        config = NovaConfig()
        showing = not bool(config.get("nova_avatar"))
        config.set("nova_avatar", showing)
        if not showing:
            return CommandResult(
                ok=True,
                message="Avatar hidden.",
                action="avatar",
                blocks=(Block("text", "Avatar hidden."),),
            )
        try:
            frames = avatar.payload()
        except (ImportError, OSError) as error:
            text = f"Avatar unavailable: {error}"
            return CommandResult(
                ok=False, message=text, action="avatar", blocks=(Block("text", text),)
            )
        message = f"Avatar shown at {avatar.WIDTH}x{avatar.HEIGHT}."
        return CommandResult(
            ok=True,
            message=message,
            action="avatar",
            avatar=frames,
            blocks=(Block("text", message),),
        )

    async def _prime_screen(self, args: str | None) -> CommandResult:
        """Toggle Deep Work, show its report, and browse saved work.

        Toggling and status go through the shared `handle_prime_command`, so
        wording and persisted state match the Textual UI exactly. `tasks`,
        `checks` and `report` open screens over `PrimeMemory`/factory records,
        with a confirm before any saved record is forgotten.

        Args:
            args: Everything after ``/prime``.

        Returns:
            The command's message as blocks, or a screen result.
        """
        import asyncio

        from novacode_cli.commands.prime_handler import handle_prime_command
        from novacode_cli.ui import screen_specs

        state = getattr(self._worker, "session_state", None)
        if state is None:
            return CommandResult(ok=False, message="Session is unavailable.")
        parts = (args or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if action in {"tasks", "checks"}:
            answer = await self.ask(screen_specs.prime_saved_spec(state, rest))
            if isinstance(answer, dict):
                chosen = str(answer.get("id") or "")
                if chosen.startswith("checks:"):
                    await self.ask(screen_specs.prime_saved_spec(state, chosen.split(":", 1)[1]))
                elif chosen.startswith(("tasks:", "checks:")):
                    message = await asyncio.to_thread(
                        handle_prime_command, state, f"resume {chosen.split(':', 1)[1]}"
                    )
                    return CommandResult(ok=True, message=message, blocks=(Block("text", message),))
            return CommandResult(
                ok=True, message="Prime saved work.", blocks=(Block("text", "Prime saved work."),)
            )
        if action == "report":
            from novacode_cli.commands.prime_handler import factory_records

            records = factory_records(state)
            answer = await self.ask(screen_specs.prime_report_spec(records[0] if records else None))
            return CommandResult(
                ok=True, message="Prime report.", blocks=(Block("text", "Prime report."),)
            )
        message = await asyncio.to_thread(handle_prime_command, state, (args or "").strip())
        blocks = (Block("text", message),)
        if action == "forget" and "Confirm local removal" in message:
            identity = rest.split(maxsplit=1)[0] if rest else ""
            confirmed = await self.ask(
                ScreenSpec(
                    "confirm",
                    {"message": f"Remove saved Prime work {identity}? Files stay intact."},
                )
            )
            if confirmed is not True:
                return CommandResult(ok=True, message="Forgot nothing.", blocks=blocks)
            message = await asyncio.to_thread(
                handle_prime_command, state, f"forget {identity} confirm"
            )
            blocks = (Block("text", message),)
        return CommandResult(
            ok=True, message=message.splitlines()[0] if message else "", blocks=blocks
        )

    async def _proactive_screen(self, args: str | None) -> CommandResult:
        """Create, edit, run and inspect durable proactive agents.

        The Textual screen is two side-by-side panes; the socket vocabulary is a
        sequence of lists, forms and reports over the same `ProactiveStore`. The
        runtime that executes queued runs is hosted here, because a queued run
        with no worker would be a lie.

        Args:
            args: No subcommands; the screen owns the workflow.

        Returns:
            A closing report, or an explicit failure when storage is unavailable.
        """
        import asyncio

        from novacode_cli.ui import screen_specs

        if (args or "").strip():
            return not_ported("proactive")
        try:
            store = self._proactive_store()
        except (OSError, ValueError) as error:
            return CommandResult(ok=False, message=f"Proactive storage unavailable: {error}")
        self._start_proactive_runtime(store)
        while True:
            try:
                agents = await asyncio.to_thread(store.agents)
            except Exception as error:  # noqa: BLE001 — sqlite failure is a command failure
                return CommandResult(ok=False, message=f"Proactive storage failed: {error}")
            answer = await self.ask(screen_specs.proactive_agents_spec(agents))
            if not isinstance(answer, dict):
                return CommandResult(
                    ok=True,
                    message="Proactive agents closed.",
                    blocks=(Block("text", "Proactive agents closed."),),
                )
            chosen = str(answer.get("id") or "")
            if chosen == "__close__":
                return CommandResult(
                    ok=True,
                    message="Proactive agents closed.",
                    blocks=(Block("text", "Proactive agents closed."),),
                )
            definitions = dict(agents)
            key = None if chosen == "__new__" else chosen
            spec = definitions.get(key) if key is not None else None
            if key is not None and spec is None:
                continue
            await self._proactive_agent(store, key, spec)

    async def _proactive_agent(  # noqa: PLR0912, PLR0915 — one explicit action per branch
        self,
        store: Any,  # noqa: ANN401 — a ProactiveStore
        key: str | None,
        spec: Any,  # noqa: ANN401 — an AgentSpec or None
    ) -> None:
        """Run the action loop for one agent until the user goes back."""
        import asyncio

        from novacode_cli.proactive import AgentSpec
        from novacode_cli.ui import screen_specs

        while True:
            name = str(getattr(spec, "name", "") or "New agent")
            answer = await self.ask(screen_specs.proactive_actions_spec(name))
            if not isinstance(answer, dict):
                return
            action = str(answer.get("id") or "")
            if action in {"", "back"}:
                return
            if action == "edit":
                form = await self.ask(screen_specs.proactive_form_spec(spec, self._cwd or ""))
                if not isinstance(form, dict):
                    continue
                try:
                    draft = AgentSpec.model_validate(
                        {
                            "name": str(form.get("name") or ""),
                            "instructions": str(form.get("instructions") or ""),
                            "workspace": str(form.get("workspace") or ""),
                            "github": str(form.get("github") or ""),
                            "pypi": str(form.get("pypi") or ""),
                            "cron": str(form.get("cron") or ""),
                            "read_workspace": _truthy(form.get("read_workspace")),
                            "watch_tags": _truthy(form.get("watch_tags")),
                            "enabled": _truthy(form.get("enabled")),
                        }
                    )
                    key = await asyncio.to_thread(store.save, key, draft)
                    spec = draft
                except (OSError, TypeError, ValueError) as error:
                    self.notify(str(error), level="error")
                    continue
                self.notify("Proactive agent saved.")
                continue
            if key is None or spec is None:
                continue
            if action == "run":
                form = await self.ask(screen_specs.proactive_run_spec(name))
                if not isinstance(form, dict):
                    continue
                try:
                    await asyncio.to_thread(store.queue, key, str(form.get("context") or ""))
                except (OSError, TypeError, ValueError) as error:
                    self.notify(str(error), level="error")
                    continue
                self.notify("Agent run queued using its saved settings.")
                continue
            if action == "history":
                try:
                    runs = await asyncio.to_thread(store.history, key)
                except Exception as error:  # noqa: BLE001 — report, keep the screen open
                    self.notify(f"Could not read run history: {error}", level="error")
                    continue
                await self.ask(screen_specs.proactive_history_spec(name, runs))
                continue
            if action == "remove":
                confirmed = await self.ask(
                    ScreenSpec(
                        "confirm",
                        {"message": f"Remove proactive agent {name} and its run history?"},
                    )
                )
                if confirmed is not True:
                    continue
                try:
                    await asyncio.to_thread(store.remove, key)
                except (OSError, TypeError, ValueError) as error:
                    self.notify(str(error), level="error")
                    continue
                self.notify("Proactive agent removed.")
                return

    async def close(self) -> None:
        """Stop background work owned by this port."""
        if self._reindex_task is not None and not self._reindex_task.done():
            self._reindex_task.cancel()
            with contextlib.suppress(Exception):
                await self._reindex_task
        self._reindex_task = None
        if self._btw_task is not None and not self._btw_task.done():
            self._btw_task.cancel()
            with contextlib.suppress(Exception):
                await self._btw_task
        self._btw_task = None
        if self._council_task is not None and not self._council_task.done():
            self._council_task.cancel()
            with contextlib.suppress(Exception):
                await self._council_task
        self._council_task = None
        for task_name in ("_ralph_task", "_trello_task"):
            owned = getattr(self, task_name)
            if owned is not None and not owned.done():
                owned.cancel()
                # `gather` collects the cancellation instead of re-raising it here,
                # which `suppress(Exception)` cannot do for `CancelledError`.
                await asyncio.gather(owned, return_exceptions=True)
            setattr(self, task_name, None)
        if self._rooms_store_instance is not None:
            with contextlib.suppress(Exception):
                self._rooms_store_instance.close()
            self._rooms_store_instance = None
        if self._proactive_runtime is not None:
            with contextlib.suppress(Exception):
                await self._proactive_runtime.close()
            self._proactive_runtime = None

    async def _council_screen(  # noqa: PLR0911 — one return per command result
        self, args: str | None
    ) -> CommandResult:
        """Edit council seats; the planning and web commands stay out of scope.

        ``settings`` edits every seat through a list and one form per seat, then
        validates with the production `parse_settings`. ``history`` and ``view``
        read the artifact store, and ``approve`` / ``reject`` / ``cancel`` act on
        the newest stored run. A task argument starts the planner as a background
        task and streams phase status; selecting plans opens a detail screen.
        Revision still needs a live planner loop and answers explicitly.

        Args:
            args: Everything after ``/council``.

        Returns:
            A saved report, a rendered run, an approval handoff, or a refusal.
        """
        from novacode_cli.council_settings import (
            load_settings,
            parse_settings,
            personality_templates,
        )
        from novacode_cli.ui import screen_specs

        parts = (args or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if action in {"history", "view", "approve", "reject", "cancel"}:
            return await self._council_run(action, rest)
        if action == "revise":
            return not_ported("council revise")
        if action not in {"settings", "history", "view", "approve", "reject", "cancel"}:
            prompt_text = (args or "").strip()
            if not prompt_text:
                return not_ported("council")
            import asyncio

            self._council_task = asyncio.create_task(self._run_council_plan(prompt_text))
            return CommandResult(
                ok=True,
                message="Council planning started.",
                blocks=(Block("text", "Council planning started."),),
            )

        draft = load_settings().to_dict()
        templates = {template.name: template.system for template in personality_templates()}
        while True:
            answer = await self.ask(screen_specs.council_seats_spec(draft))
            if not isinstance(answer, dict):
                return CommandResult(
                    ok=True,
                    message="Council settings unchanged.",
                    blocks=(Block("text", "Council settings unchanged."),),
                )
            seat_id = str(answer.get("id") or "")
            if seat_id == "__save__":
                break
            if seat_id == "__reset__":
                from novacode_cli.council_settings import default_settings

                draft = default_settings().to_dict()
                continue
            entries = [*draft.get("members", []), draft.get("judge", {})]
            entry = next((item for item in entries if item.get("id") == seat_id), None)
            if entry is None:
                return CommandResult(ok=False, message=f"Unknown council seat: {seat_id}")
            form = await self.ask(screen_specs.council_member_spec(entry))
            if not isinstance(form, dict):
                continue
            provider_label = str(form.get("provider") or "Session model (inherit)")
            inherit = provider_label == "Session model (inherit)"
            provider = "" if inherit else screen_specs.provider_id(provider_label)
            model = str(form.get("model") or "").strip() if provider else ""
            system = str(form.get("system") or entry.get("system") or "")
            template = str(form.get("template") or "Keep current")
            if template != "Keep current" and template in templates:
                system = templates[template]
            entry.update(
                name=str(form.get("name") or entry.get("name") or "").strip(),
                provider=provider,
                model=model,
                system=system,
            )

        try:
            saved = parse_settings(draft)
        except (TypeError, ValueError) as error:
            return CommandResult(ok=False, message=f"Council settings not saved: {error}")
        _write(_config(), "set", "council", saved.to_dict())
        return CommandResult(
            ok=True,
            message="Council settings saved.",
            action="settings",
            blocks=(
                Block(
                    "text",
                    f"Council settings saved · {len(saved.members)} voices + judge. "
                    "They apply to the next council run.",
                ),
            ),
        )

    def _council_store(self) -> Any:  # noqa: ANN401 — a CouncilArtifactStore
        """The artifact store rooted at this session's workspace."""
        from novacode_cli.council_planner import CouncilArtifactStore

        return CouncilArtifactStore(Path(self._cwd or Path.cwd()))

    def _council_context(self) -> str:
        """Light project context for the planners: the workspace's NOVA.md."""
        from pathlib import Path

        root = Path(self._cwd or Path.cwd())
        for name in ("NOVA.md", ".nova/NOVA.md"):
            path = root / name
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8")[:4000]
            except OSError:
                return ""
        return ""

    async def _run_council_plan(self, prompt: str) -> None:  # noqa: PLR0915 — one linear planner loop
        """Drive the planning council and stream progress into the status line.

        The client has no bespoke council card yet, so progress travels as the
        existing `StatusUpdate` event and the selected plans open as a `detail`
        screen. Approval is unchanged: `/council approve N` acts on the stored run.
        """
        import asyncio

        from novacode_cli import council_planner as cp
        from novacode_cli import ui_events as ev
        from novacode_cli.council import get_council_model
        from novacode_cli.council_settings import load_settings, resolve_models
        from novacode_cli.server import frames
        from novacode_cli.ui import screen_specs

        labels = {
            cp.BRAINSTORMING: "Council · brainstorming",
            cp.CRITIQUING: "Council · critiquing",
            cp.VOTING: "Council · voting",
            cp.JUDGING: "Council · judging",
        }

        def status(message: str | None) -> None:
            frame = frames.event_frame(ev.StatusUpdate(message))
            if frame is not None:
                self._emit(frame)

        try:
            model = getattr(getattr(self._worker, "session_state", None), "_model", None)
            if model is None:
                model = await asyncio.to_thread(get_council_model)
            settings = await asyncio.to_thread(load_settings)
            models = await asyncio.to_thread(resolve_models, settings, model)
            store = self._council_store()
            context = await asyncio.to_thread(self._council_context)
            agents: list[dict] = []
            totals = {"proposals": 0, "critiques": 0, "ballots": 0}
            entropy: float | None = None

            def council_update(name: str, payload: dict, outcome: str = "") -> None:
                nonlocal entropy
                if name == "council.started":
                    agents[:] = list(payload.get("agents") or [])
                elif name == "proposal.created":
                    totals["proposals"] += 1
                elif name == "proposal.critiqued":
                    totals["critiques"] += 1
                elif name == "vote.submitted":
                    totals["ballots"] = int(payload.get("cast") or totals["ballots"] + 1)
                elif name == "vote.tallied":
                    tally = payload.get("tally") or {}
                    entropy = float(tally.get("entropy") or 0.0)
                update = ev.CouncilUpdate(
                    prompt=prompt,
                    run_id=str(event.get("run_id") or ""),
                    event=name,
                    phase=str(payload.get("phase") or ""),
                    agent_id=str(payload.get("agent_id") or payload.get("critic_id") or ""),
                    agent_name=str(payload.get("name") or ""),
                    reason=str(payload.get("reason") or ""),
                    proposals=totals["proposals"],
                    critiques=totals["critiques"],
                    ballots=totals["ballots"],
                    entropy=entropy,
                    agents=list(agents),
                    outcome=outcome,
                )
                frame = frames.event_frame(update)
                if frame is not None:
                    self._emit(frame)

            async for event in cp.run_planning_council(
                prompt,
                model,
                context=context,
                store=store,
                members=settings.personas(),
                member_models=models,
                judge_model=models["judge"],
                judge_perspective=f"{settings.judge.name}\n{settings.judge.system}",
            ):
                name = str(event.get("event") or "")
                payload = event.get("payload") or {}
                if name == "council.phase.started":
                    status(labels.get(str(payload.get("phase") or ""), "Council · working"))
                if name == "plans.selected":
                    council_update(name, payload, outcome="done")
                    run = await asyncio.to_thread(store.load, str(event.get("run_id") or ""))
                    if run is not None:
                        spec = screen_specs.council_plans_spec(run)
                        self._emit(
                            frames.screen(
                                f"council-{run.id}",
                                kind=spec.kind,
                                title=spec.title,
                                payload=spec.payload,
                            )
                        )
                    status(None)
                elif name == "council.failed":
                    council_update(name, payload, outcome="failed")
                    message = str(payload.get("reason") or "Council failed.")
                    frame = frames.event_frame(ev.Error(message))
                    if frame is not None:
                        self._emit(frame)
                    status(None)
                else:
                    council_update(name, payload)
        except Exception as error:  # noqa: BLE001 — a council must not kill the session
            frame = frames.event_frame(ev.Error(f"Council failed: {error}"))
            if frame is not None:
                self._emit(frame)
            status(None)

    async def _council_run(  # noqa: PLR0911 — one return per command result
        self, action: str, rest: str
    ) -> CommandResult:
        """Read or act on the newest stored council run.

        Args:
            action: `history`, `view`, `approve`, `reject` or `cancel`.
            rest: Plan number for approve, ignored for other verbs.

        Returns:
            The run's report, an approval handoff, or an explicit failure.
        """
        import asyncio

        from novacode_cli import council_planner as cp
        from novacode_cli.ui import screen_specs

        store = self._council_store()
        if action == "history":
            ids = await asyncio.to_thread(store.history)
            rows = []
            for run_id in ids:
                run = await asyncio.to_thread(store.load, run_id)
                if run is None:
                    continue
                prompt = " ".join(str(run.prompt).splitlines()[:1])[:60]
                rows.append(f"{run_id}  {run.status:<18}  {prompt}")
            text = "\n".join(rows) if rows else "No council runs yet."
            return CommandResult(ok=True, message="Council history", blocks=(Block("text", text),))

        run = await asyncio.to_thread(store.latest)
        if run is None:
            return CommandResult(ok=False, message="No council run to act on.")
        if action == "view":
            answer = await self.ask(screen_specs.council_plans_spec(run))
            choice = str(answer.get("action") or "") if isinstance(answer, dict) else ""
            if choice.startswith("approve:"):
                return await self._council_run("approve", choice.split(":", 1)[1])
            if choice == "reject":
                return await self._council_run("reject", "")
            text = f"Council run {run.id} ({run.status})."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        if action in {"reject", "cancel"}:
            function = cp.reject if action == "reject" else cp.cancel
            await asyncio.to_thread(function, run, store=store)
            text = f"Council run {run.id} {run.status}."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

        if not rest.split(maxsplit=1)[0].isdigit():
            return CommandResult(ok=False, message="Which plan? /council approve 1")
        try:
            plan = await asyncio.to_thread(
                cp.approve, run, int(rest.split(maxsplit=1)[0]), store=store
            )
        except cp.CouncilApprovalError as error:
            return CommandResult(ok=False, message=str(error))
        text = f"Plan approved — {run.approved_proposal_id}. Implementing it now."
        return CommandResult(
            ok=True,
            message=text,
            blocks=(Block("text", text),),
            prompt=(
                "The user reviewed and APPROVED the following implementation plan "
                "from a planning council. Implement it. Do not redesign it or "
                "propose alternatives; if a step turns out to be wrong, say so and "
                "stop rather than silently substituting your own approach.\n\n" + plan
            ),
        )

    async def _tabs_screen(self, args: str | None) -> CommandResult:
        """List the server's sessions and attach to the chosen one.

        ``/tabs`` and ``/tabs list`` are a picker over the same registry the
        `sessions` frame reads. Choosing a row returns an ``attach`` action: the
        client sends the existing `attach` frame, which the transport uses to
        move its sink. ``new`` asks the client to send ``create_session`` and
        ``close`` asks for ``close_session``: spawning and ownership belong to
        the connection, not to the command coroutine. ``launch`` and ``resume``
        still belong to the worktree wave and say so.

        Args:
            args: Everything after ``/tabs``.

        Returns:
            The picker's result, or an explicit refusal for an unported verb.
        """
        from novacode_cli.ui import screen_specs

        parts = (args or "").strip().split(maxsplit=1)
        verb = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if verb == "new":
            name = rest or "session"
            return CommandResult(
                ok=True,
                message=f"Starting {name}.",
                create=name,
                blocks=(Block("text", f"Starting a new session: {name}."),),
            )
        if verb == "close":
            return CommandResult(
                ok=True,
                message="Closing the session tab.",
                close=rest,
            )
        if verb not in {"", "list"}:
            return not_ported(f"tabs {verb}")
        if self._session_rows is None:
            return CommandResult(ok=False, message="Session tabs are unavailable.")
        rows = self._session_rows()
        active = getattr(getattr(self._worker, "session_state", None), "session_id", None)
        answer = await self.ask(screen_specs.tabs_spec(rows, active))
        chosen = str(answer.get("id") or "") if isinstance(answer, dict) else ""
        if not chosen:
            return CommandResult(
                ok=True,
                message="Session unchanged.",
                blocks=(Block("text", f"{len(rows)} session(s)."),),
            )
        name = next(
            (str(row.get("name") or chosen) for row in rows if row.get("id") == chosen), chosen
        )
        return CommandResult(
            ok=True,
            message=f"Attached to {name}.",
            attach=chosen,
            blocks=(Block("text", f"Switching to {name}."),),
        )

    async def _usage_screen(self, _args: str | None = None) -> CommandResult:
        """Read every provider's quota and this session's usage, then report.

        The same three parts the Textual modal shows, in the same order, under
        the same caption, which now lives in the neutral usage module so both
        frontends say one thing.

        Returns:
            A successful result carrying the report as a `detail` screen, or why
            the providers could not be read.
        """
        from novacode_cli.commands.planner_usage import USAGE_NOTE, usage_text
        from novacode_cli.commands.provider_usage import all_usage
        from novacode_cli.tracking.usage_tree import format_usage_report, load_usage_trees
        from novacode_cli.ui import screen_specs

        state = getattr(self._worker, "session_state", None)
        thread_id = str(getattr(state, "thread_id", "") or "")
        try:
            found, trees = await asyncio.gather(
                asyncio.to_thread(all_usage), load_usage_trees(thread_id)
            )
        except Exception as error:  # noqa: BLE001 — a provider read is best-effort
            text = f"Could not read usage: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        quotas = [usage_text(usage).plain for usage in found]
        spec = screen_specs.usage_spec(quotas, format_usage_report(trees), USAGE_NOTE)
        return CommandResult(
            ok=True,
            message="Usage",
            blocks=(Block("text", str(spec.payload["markdown"])),),
            screen=spec,
        )

    async def _update_screen(self, _args: str | None = None) -> CommandResult:
        """Check for a newer Nova and report what is installed and latest.

        The check is the same one the Textual modal runs. Installing is not
        served: the Textual app replaces its own process and restarts into the
        session, which a server that is mid-session for a client cannot do, so the
        report names the command that can.

        Returns:
            A successful result carrying the update report, or the read failure.
        """
        from novacode_cli.ui import screen_specs
        from novacode_cli.updates import check_for_update, release_details

        try:
            status = await asyncio.to_thread(check_for_update, force=True)
            status = await asyncio.to_thread(release_details, status)
        except Exception as error:  # noqa: BLE001 — a network read is best-effort
            text = f"Could not check for updates: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        install_hint = (
            "Install it with `nova update` in a terminal, then start Nova again: "
            "this session's server cannot replace the process serving it."
        )
        spec = screen_specs.update_spec(status, install_hint=install_hint)
        return CommandResult(
            ok=True,
            message="Updates",
            blocks=(Block("text", str(spec.payload["markdown"].replace("**", ""))),),
            screen=spec,
        )

    async def _subagents_screen(  # noqa: PLR0911 — one return per verb
        self, args: str | None = None
    ) -> CommandResult:
        """List, inspect, create and delete the custom subagents on disk.

        One file is one agent, in either scope, and `async: true` in its
        frontmatter is what also runs it on the agent server. The verbs keep the
        single-screen manager reachable from a list-and-answer client: bare
        `/subagents` is the list, and the rest name what to do.

        Args:
            args: Empty for the list, else `show <name>`, `create` or
                `delete <name>`.

        Returns:
            A screen for each verb, or the usage line for an unknown one.
        """
        from novacode_cli.ui import screen_specs

        verb, _, rest = (args or "").strip().partition(" ")
        verb = verb.lower()
        name = rest.strip().lstrip("@")
        if verb in ("", "list", "ls"):
            rows = await asyncio.to_thread(_agent_rows)
            hint = (
                f"{len(rows)} custom agent(s) · /subagents show <name> · "
                "/subagents create · /subagents delete <name>"
                if rows
                else "No custom subagents configured. Create one with /subagents create."
            )
            return CommandResult(
                ok=True,
                message=f"{len(rows)} agent(s).",
                blocks=(Block("text", hint),),
                screen=screen_specs.agents_spec(rows),
            )
        if verb in ("show", "view"):
            if not name:
                return CommandResult(ok=False, message="Usage: /subagents show <name>")
            try:
                row, prompt = await asyncio.to_thread(_read_agent, name)
            except LookupError:
                text = f"No subagent named @{name}. Try /subagents."
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
            spec = screen_specs.agent_detail_spec(
                name=row["name"],
                scope=str(row["scope"]),
                path=str(row["path"]),
                description=str(row["description"]),
                runs_async=bool(row["runs_async"]),
                prompt=prompt,
            )
            return CommandResult(
                ok=True,
                message=f"@{name}",
                blocks=(Block("text", f"@{name} · {row['scope']}"),),
                screen=spec,
            )
        if verb in ("create", "new"):
            return await self._create_agent()
        if verb in ("delete", "rm"):
            if not name:
                return CommandResult(ok=False, message="Usage: /subagents delete <name>")
            return await self._delete_agent(name)
        usage = "Usage: /subagents [list] | show <name> | create | delete <name>"
        return CommandResult(ok=False, message=usage, blocks=(Block("text", usage),))

    async def _create_agent(self) -> CommandResult:  # noqa: PLR0911 — validate then write
        """Ask for an agent's shape, generate its prompt when blank, write the file.

        Returns:
            A successful report naming the file, or why the agent was not made.
        """
        # Imported here, on the loop thread: `agent_file` pulls the whole
        # middleware stack, and a first import inside the write thread blocks
        # behind the import lock while the loop waits for it.
        from novacode_cli.agents.agent_file import write_agent
        from novacode_cli.config.config import settings
        from novacode_cli.ui import screen_specs

        spec = screen_specs.agent_create_spec(project_available=settings.project_root is not None)
        answer = await self.ask(spec)
        if not isinstance(answer, dict):
            text = "Cancelled."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        name = str(answer.get("name") or "").strip()
        description = str(answer.get("description") or "").strip()
        if not name or not _AGENT_NAME.fullmatch(name):
            text = "A name is letters, numbers, hyphens and underscores only."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        if not description:
            text = "A description is required: it is what the planner reads."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        colors = dict(screen_specs.AGENT_COLORS)
        color = colors.get(str(answer.get("color") or ""), "#0ea5e9")
        wants_async = str(answer.get("kind") or "sync") == "async"
        scope = "project" if str(answer.get("scope") or "global") == "project" else "global"

        def write() -> Path:
            agents_dir = (
                settings.ensure_project_agents_dir()
                if scope == "project"
                else settings.get_agents_root_dir()
            )
            agent_md = agents_dir / f"{name}.md"
            if agent_md.exists() or (agents_dir / name).exists():
                raise _AgentExistsError(name)
            front: dict[str, object] = {
                "color": color,
                "description": description,
                "tools": None,
            }
            if wants_async:
                front["async"] = True
            write_agent(agent_md, front, prompt)
            return agent_md

        prompt = str(answer.get("system_prompt") or "").strip()
        generated = not prompt
        if generated:
            from novacode_cli.commands.agents_commands import _generate_agent_system_prompt

            try:
                prompt = await _generate_agent_system_prompt(name, description)
            except Exception as error:  # noqa: BLE001 — the model call is optional
                text = f"Could not generate a system prompt: {error}"
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
            if not prompt:
                text = "AI generation of system prompt returned empty response."
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        try:
            agent_md = await asyncio.to_thread(write)
        except _AgentExistsError:
            text = f"Agent '{name}' already exists in that scope."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        except Exception as error:  # noqa: BLE001 — a bad path must not kill the session
            text = f"Failed to create agent: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        if wants_async:
            await asyncio.to_thread(_notify_async_agents_changed)
        extra = " It will also run on the agent server, in the background." if wants_async else ""
        text = f"● Custom subagent '@{name}' created successfully!{extra} ({agent_md})"
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _delete_agent(self, name: str) -> CommandResult:
        """Remove one agent's file after a confirmation.

        Args:
            name: Agent name, without its `@`.

        Returns:
            The confirmation outcome, or why the agent was kept.
        """
        try:
            row, _prompt = await asyncio.to_thread(_read_agent, name)
        except LookupError:
            text = f"No subagent named @{name}. Try /subagents."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        confirmed = await self.ask(
            ScreenSpec(
                "confirm",
                {"message": f"Delete @{name} ({row['scope']})? Its file is removed. "},
            )
        )
        if confirmed is not True:
            return CommandResult(ok=True, message="Kept.", blocks=(Block("text", "Kept."),))
        path = Path(str(row["path"]))

        def remove() -> None:
            if path.is_dir():
                import shutil

                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            if row["runs_async"]:
                _notify_async_agents_changed()

        try:
            await asyncio.to_thread(remove)
        except OSError as error:
            text = f"Failed to delete agent: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        text = f"● Custom subagent '@{name}' deleted."
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _rewind_screen(  # noqa: PLR0911, PLR0912, PLR0915 — three pick-then-apply steps
        self, _args: str | None = None
    ) -> CommandResult:
        """Rewind the conversation, and optionally the code, to before a message.

        Three steps, as in the Textual UI: pick a message, pick what to put back
        when files changed after it, then rewrite the checkpoint. The transcript
        is rebuilt from the kept messages because the protocol has no restore
        frame -- a result may clear it and append blocks.

        Args:
            args: Unused; `@@/rewind@@` is interactive on both frontends.

        Returns:
            The rebuilt conversation, or the reason it was left alone.
        """
        from novacode_cli import rewind
        from novacode_cli.ui import screen_specs

        if self._busy():
            text = "Stop the running turn first (Esc), then /rewind."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        agent = getattr(self._worker, "agent", None)
        state = getattr(self._worker, "session_state", None)
        thread_id = str(getattr(state, "thread_id", "") or "")
        if agent is None or not thread_id:
            text = "Nothing to rewind to yet."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        config = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = await asyncio.wait_for(agent.aget_state(config), timeout=15.0)
        except Exception:  # noqa: BLE001 — a broken checkpoint must not kill the session
            text = "Could not read this conversation's checkpoints."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        messages = list((getattr(snapshot, "values", None) or {}).get("messages", []))
        turns = rewind.user_turns(messages)[-20:][::-1]
        if not turns:
            text = "Nothing to rewind to yet."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        rows = [
            {"id": str(index), "label": _rewind_label(said), "detail": count}
            for index, said, count in await _rewind_rows(thread_id, messages, turns)
        ]
        picked = await self.ask(screen_specs.rewind_spec(rows))
        row = next((item for item in rows if item["id"] == _picked_id(picked)), None)
        if row is None:
            text = "Cancelled."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        index = int(row["id"])
        said = next(text for turn_index, text in turns if turn_index == index)
        changed = await asyncio.to_thread(
            rewind.changes_after, thread_id, rewind.call_ids(messages[index:])
        )
        scope = "both"
        if changed:
            scope = _picked_id(await self.ask(screen_specs.rewind_scope_spec(len(changed)))) or ""
            if scope not in ("both", "conversation", "code"):
                text = "Cancelled."
                return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        kept = messages[:index]
        if scope in ("both", "conversation"):
            try:
                await self._set_conversation(agent, config, snapshot, kept)
            except Exception as error:  # noqa: BLE001 — report, never half-apply
                text = f"Could not rewrite the conversation: {error}"
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        restored: list[str] = []
        if scope in ("both", "code") and changed:
            try:
                restored = await asyncio.to_thread(rewind.restore, thread_id, changed)
            except Exception as error:  # noqa: BLE001 — say which half failed
                text = f"Rewound the conversation, but restoring files failed: {error}"
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        parts = []
        if scope in ("both", "conversation"):
            parts.append("conversation rewound")
        if restored:
            parts.append(f"{len(restored)} file(s) restored")
        text = f"⟲ Rewound to before “{_rewind_label(said)}”: " + ", ".join(parts) + "."
        blocks = [Block("text", text)]
        if scope in ("both", "conversation"):
            blocks = [
                *message_blocks(kept),
                Block("heading", "your message, ready to send again"),
                Block("text", said),
            ]
        return CommandResult(
            ok=True,
            message=text,
            action="clear" if scope in ("both", "conversation") else None,
            blocks=tuple(blocks),
        )

    async def _tree_screen(  # noqa: PLR0911 — one return per refusal
        self, _args: str | None = None
    ) -> CommandResult:
        """Continue from any earlier point in this session's message tree.

        Every message the session ever held stays in `tree.jsonl`; continuing
        from an earlier point starts a new branch and leaves the old one. Files
        are untouched (`@@/rewind@@` restores code). As with `@@/rewind@@`, the
        transcript is rebuilt as blocks because the protocol has no restore
        frame.

        Args:
            args: Unused; `@@/tree@@` is interactive on both frontends.

        Returns:
            The rebuilt conversation, or the reason it was left alone.
        """
        from novacode_cli.ui import screen_specs

        if self._busy():
            text = "Stop the running turn first (Esc), then /tree."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        worker = self._worker
        manager = getattr(worker, "session_manager", None)
        state = getattr(worker, "session_state", None)
        session_id = str(getattr(state, "session_id", "") or "")
        if manager is None or not session_id:
            text = "Session history is off for this run."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        tree = await asyncio.to_thread(manager.history_tree, session_id)
        path_nodes = tree.path_to(tree.leaf)
        if not path_nodes:
            text = "Nothing in this session's history yet."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        rows = [
            {
                "id": node.id,
                "label": f"★ {node.label}" if node.label else _tree_text(node),
                "detail": "bookmark" if node.label else "",
            }
            for node in path_nodes
        ]
        picked = _picked_id(await self.ask(screen_specs.tree_spec(rows)))
        if not picked:
            text = "Cancelled."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        kept_path, said = tree.continue_point(picked)
        load = manager._deserialize_message
        kept = [message for node in kept_path if (message := load(node.msg)) is not None]
        agent = getattr(worker, "agent", None)
        thread_id = str(getattr(state, "thread_id", "") or "")
        if agent is None or not thread_id:
            text = "/tree failed; nothing was changed: no conversation to rewrite."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        config = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = await asyncio.wait_for(agent.aget_state(config), timeout=15.0)
            await self._set_conversation(agent, config, snapshot, kept)
        except Exception as error:  # noqa: BLE001 — say nothing was changed
            text = f"/tree failed; nothing was changed: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        where = "your message is back in the prompt" if said else "continue from here"
        text = f"⑂ Moved to an earlier point ({len(kept)} messages kept); {where}."
        blocks = [*message_blocks(kept)]
        if said:
            blocks.extend(
                (Block("heading", "your message, ready to send again"), Block("text", said))
            )
        return CommandResult(ok=True, message=text, action="clear", blocks=tuple(blocks))

    async def _resume_screen(self, args: str | None = None) -> CommandResult:
        """Reopen a saved session in its own tab, continuing its conversation.

        The Textual UI can rewrite the live app's session in place. A socket
        client cannot: the conversation, model and todos belong to the process
        that owns them. So the picker ends by asking this connection to spawn a
        child that starts with ``--continue <id>`` in the saved project folder --
        the child adopts that session exactly as ``nova --continue`` does, and
        the current conversation here is left alone.

        Args:
            args: An optional saved session id or unique id prefix, skipping the
                picker.

        Returns:
            The tab the client should open, or the reason none was opened.
        """
        from novacode_cli.sessions.resume import resolve_saved_tab
        from novacode_cli.ui import screen_specs

        worker = self._worker
        manager = getattr(worker, "session_manager", None)
        state = getattr(worker, "session_state", None)
        if manager is None:
            text = "Session resume is unavailable."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        current = str(getattr(state, "session_id", "") or "")
        parts = (args or "").strip().split(maxsplit=1)
        selector = parts[0].strip() if parts else ""
        if not selector:
            saved = [
                meta
                for meta in await asyncio.to_thread(manager.list_sessions, 200)
                if meta.session_id != current and not getattr(meta, "cleared", False)
            ]
            if not saved:
                text = (
                    "No saved sessions available yet. (Sessions autosave as you work; "
                    "use /save to force one.)"
                )
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
            rows = [_resume_row(meta) for meta in saved]
            selector = _picked_id(await self.ask(screen_specs.resume_spec(rows)))
            if not selector:
                text = "Cancelled."
                return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        try:
            meta = await asyncio.to_thread(resolve_saved_tab, manager, selector)
        except (OSError, ValueError) as error:
            text = f"Could not resume that session: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        if meta.session_id == current:
            text = "That session is already open here."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        project = Path(str(meta.project_root)).name
        text = f"⟲ Resuming {meta.session_id[:8]} in a new tab ({project}), with its full history."
        return CommandResult(
            ok=True,
            message=text,
            blocks=(Block("text", text),),
            create=f"{project} · {meta.session_id[:8]}",
            resume=meta.session_id,
        )

    async def _command_center_screen(  # noqa: PLR0911 — one return per outcome
        self, args: str | None = None
    ) -> CommandResult:
        """Every saved conversation and live tab, in one picker.

        The Textual modal shows a list beside a details pane; a socket client
        shows one screen at a time, so the facts that pane held ride on the row.
        Choosing a live tab attaches to it; choosing a saved session resumes it
        in a new tab, which is what `@@/resume@@` does here -- the port cannot
        adopt another session into this one.

        Args:
            args: An optional status filter: `all`, `needsyou`, `working`,
                `ready` or `inactive`.

        Returns:
            The attachment, the new tab, or the reason neither happened.
        """
        from novacode_cli.ui import command_center as conversations
        from novacode_cli.ui import screen_specs

        status, complaint = _status_argument(args or "")
        if complaint:
            return CommandResult(ok=False, message=complaint, blocks=(Block("text", complaint),))
        worker = self._worker
        manager = getattr(worker, "session_manager", None)
        state = getattr(worker, "session_state", None)
        current = str(getattr(state, "session_id", "") or "")
        registered = self._session_rows() if self._session_rows is not None else []
        saved: list[Any] = []
        note = ""
        if manager is not None:
            try:
                saved = await asyncio.to_thread(manager.list_sessions, 200)
            except Exception:  # noqa: BLE001 — a broken store must not hide live tabs
                note = "Saved conversations could not be read; only live tabs are listed."
        rows = conversations.merge_sessions(
            saved, conversations.registry_sessions(registered, current)
        )
        visible = conversations.filter_sessions(rows, status)
        if not visible:
            text = f"Nothing is {status.lower()} right now."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        chosen = _picked_id(
            await self.ask(screen_specs.command_center_spec(visible, status=status, note=note))
        )
        if not chosen:
            text = "Cancelled."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        row = next((entry for entry in visible if entry.key == chosen), None)
        if row is None:
            text = "That conversation is no longer listed."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        name = row.title or row.session_id[:8] or "that conversation"
        if row.session_id and row.session_id == current:
            text = "That conversation is already open here."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        live_ids = {str(entry.get("id") or "") for entry in registered}
        if row.session_id and row.session_id in live_ids:
            text = f"Attached to {name}."
            return CommandResult(
                ok=True,
                message=text,
                blocks=(Block("text", f"Switching to {name}."),),
                attach=row.session_id,
            )
        if not row.session_id:
            text = "That tab has no saved session yet, so it cannot be reopened here."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        return await self._resume_screen(row.session_id)

    async def _share_command(self, args: str | None = None) -> CommandResult:
        """Share Nova's web pages, or publish this session as a secret gist.

        With an argument this is the shared tunnel handler (`all`, `artifacts`,
        `list`, `stop`). Without one it publishes the same page `@@/export@@`
        writes, through the GitHub CLI, after an explicit confirmation -- the
        two are one command in the Textual UI as well.

        Args:
            args: A tunnel sub-command, or empty to share the session.

        Returns:
            The tunnel's own report, the gist URL, or the reason nothing
            happened.
        """
        text = (args or "").strip()
        if text:
            return await self._share_pages(text)
        return await self._share_session()

    async def _share_pages(self, args: str) -> CommandResult:
        """Run the shared tunnel handler and report what it printed.

        Args:
            args: The tunnel sub-command and its own arguments.

        Returns:
            The handler's captured output, or its failure.
        """
        from novacode_cli.commands import CommandContext
        from novacode_cli.commands.share_handler import handle_share_command
        from novacode_cli.config.config import console
        from novacode_cli.ui.ui_elements import TokenTracker

        worker = self._worker
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed), console.capture() as captured:
                await handle_share_command(
                    CommandContext(
                        cmd="share",
                        cmd_args=args,
                        agent=getattr(worker, "agent", None),
                        token_tracker=TokenTracker(),
                        session_state=getattr(worker, "session_state", None),
                        assistant_id=getattr(worker, "assistant_id", None),
                        session_manager=getattr(worker, "session_manager", None),
                        model_name=getattr(worker, "model_name", None),
                        image_tracker=getattr(worker, "image_tracker", None),
                    )
                )
        except Exception as error:  # noqa: BLE001 — a tunnel failure must not end the session
            text = f"/share failed: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        output = strip_ansi(captured.get() + printed.getvalue()).strip()
        return CommandResult(ok=True, message="", blocks=blocks_from_text(output))

    async def _share_session(self) -> CommandResult:
        """Upload this session's page as a secret gist.

        Returns:
            The preview URL, or the reason nothing was uploaded.
        """
        import shutil

        from novacode_cli.ui import screen_specs

        gh = shutil.which("gh")
        if gh is None:
            text = (
                "/share needs the GitHub CLI: install it (https://cli.github.com) "
                "and run `gh auth login`."
            )
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        worker = self._worker
        manager = getattr(worker, "session_manager", None)
        state = getattr(worker, "session_state", None)
        session_id = str(getattr(state, "session_id", "") or "")
        if manager is None or not session_id:
            text = "Session history is off for this run."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        tree = await asyncio.to_thread(manager.history_tree, session_id)
        if not tree.nodes:
            text = "Nothing in this session's history yet."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        answer = _picked_id(await self.ask(screen_specs.share_gist_spec()))
        if answer != "upload":
            text = "Cancelled."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        page = _export_page(tree, session_id, str(getattr(worker, "model_name", "") or ""))
        url, reason = await _create_gist(gh, page)
        if not url:
            text = f"/share failed: {reason}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        view = f"https://gistpreview.github.io/?{url.rstrip('/').rsplit('/', 1)[-1]}"
        text = f"● Shared: {view}"
        return CommandResult(
            ok=True,
            message=text,
            blocks=(Block("text", f"● Shared: {view}\n  gist: {url}"),),
        )

    async def _import_command(self, args: str | None) -> CommandResult:
        """Write one imported session's reference into this conversation.

        The reference replaces any earlier import in the same slot, which is
        what the message ID is for; Nova's own history is untouched, and the
        imported transcript is stored beside the session so a later
        `/context imported` or resume can find it again.

        Args:
            args: `<provider> <session ID or path> [--mode compact|full|relevant]`.

        Returns:
            What was imported and how much of it loaded, or why nothing changed.
        """
        from novacode_cli.session.imported_context import load_import, parse_import

        text = f"/import {args.strip()}" if (args or "").strip() else "/import"
        try:
            provider, selector, mode = parse_import(text)
            context = await asyncio.to_thread(load_import, provider, selector, mode)
        except (OSError, ValueError, KeyError, TypeError) as error:
            text = f"Session import: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        session = context.session
        note = f"Imported {len(session.messages)} messages from {session.provider}."
        return await self._write_imported(context, note=note)

    async def _write_imported(self, context: Any, *, note: str) -> CommandResult:  # noqa: ANN401
        """Write one imported reference, store its transcript and report the size.

        Args:
            context: The imported context to attach to this conversation.
            note: The line that says what happened, e.g. "Imported 4 messages from codex."

        Returns:
            The result carrying the new budget, or why nothing was written.
        """
        from novacode_cli.session.imported_context import context_budget, import_message

        state = getattr(self._worker, "session_state", None)
        thread_id = getattr(state, "thread_id", None)
        agent = getattr(self._worker, "agent", None)
        if agent is None or not thread_id:
            text = "Session import needs a live conversation."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        budget = context_budget(getattr(state, "token_tracker", None))
        config = {"configurable": {"thread_id": thread_id}}
        updates = {"messages": import_message(context, budget)}
        try:
            await agent.aupdate_state(config, values=updates, as_node="model")
        except Exception as error:  # noqa: BLE001 - the graph write must not kill the session
            text = f"Could not write the imported context: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        manager = getattr(self._worker, "session_manager", None)
        session_id = getattr(state, "session_id", None)
        if manager is not None and session_id:
            try:
                await asyncio.to_thread(context.save, manager.sessions_dir, session_id)
            except OSError as error:
                text = f"Imported the context, but storing it failed: {error}"
                return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        return CommandResult(
            ok=True,
            message=note,
            blocks=(Block("text", note), Block("text", context.describe(budget))),
        )

    async def _compare_command(self, args: str | None) -> CommandResult:
        """Ask one Nova question about two imported histories.

        The comparison is an ordinary new request: the port builds the prompt and
        lets the host start the turn, so no source session's tools or runtimes
        are resumed.

        Args:
            args: Two `<provider>:<session ID>` selectors.

        Returns:
            The prompt to send, or the usage error for a malformed request.
        """
        from novacode_cli.session.imported_context import (
            comparison_prompt,
            context_budget,
            load_import,
        )

        selectors = (args or "").split()
        if len(selectors) != 2 or any(":" not in part for part in selectors):  # noqa: PLR2004
            text = "Use /compare claude:<id> codex:<id>."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        try:
            contexts = await asyncio.gather(
                *(asyncio.to_thread(load_import, *selector.split(":", 1)) for selector in selectors)
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            text = f"Session import: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        state = getattr(self._worker, "session_state", None)
        budget = context_budget(getattr(state, "token_tracker", None)) // 2
        providers = ", ".join(selector.split(":", 1)[0] for selector in selectors)
        return CommandResult(
            ok=True,
            message=f"Comparing {providers}.",
            blocks=(Block("text", f"Comparing {providers}."),),
            prompt=comparison_prompt(contexts, budget),
        )

    async def _ralph_command(self, args: str | None) -> CommandResult:
        """Serve `/ralph`: its loop runs as a task whose lines stream back.

        The loop itself is the shared handler. It is started as a task this port
        owns rather than awaited here, because a socket command must answer: the
        progress the Textual card painted arrives as transcript blocks instead.

        Args:
            args: The task, or `--status` / `--resume`.

        Returns:
            Where the run is, or that it started.
        """
        from novacode_cli.commands.ralph_handler import handle_ralph_status

        state = getattr(self._worker, "session_state", None)
        if state is None:
            text = "/ralph needs a live session."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        text_args = (args or "").strip()
        if text_args in {"--status", "-s"}:
            written: list[str] = []

            def collect(message: str = "") -> None:
                written.append(message)

            await handle_ralph_status(state, emit=collect)
            text = "\n".join(
                line for line in (_plain_markup(line) for line in written) if line.strip()
            )
            return CommandResult(
                ok=True,
                message=_first_line(text) or "Ralph",
                blocks=blocks_from_text(text),
            )
        if self._ralph_task is not None and not self._ralph_task.done():
            text = "Ralph is already running. /ralph --status shows where it is."
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        self._ralph_task = asyncio.create_task(self._run_ralph(text_args or None))
        text = "● Ralph started — progress arrives here. /ralph --status reports where it is."
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _run_ralph(self, args: str | None) -> None:
        """Run the shared Ralph loop, reporting each line as a transcript block.

        Args:
            args: The task, already stripped.
        """
        from novacode_cli.commands.ralph_handler import handle_ralph_command

        worker = self._worker
        state = getattr(worker, "session_state", None)

        def emit(message: str = "") -> None:
            if message.strip():
                self.log(Block("text", _plain_markup(message)))

        with contextlib.suppress(Exception):
            await handle_ralph_command(
                getattr(worker, "agent", None),
                state,
                getattr(worker, "assistant_id", ""),
                getattr(state, "token_tracker", None),
                args,
                emit=emit,
            )

    async def _trello_command(self, args: str | None) -> CommandResult:
        """Serve `/trello`: status, stop, or start the board and process cards.

        The board's loop is the shared `trello_watch_loop`, run as a task this
        port owns. Cards execute through the headless executor, so board work
        never waits for whichever tab the user happens to be looking at.

        Args:
            args: `stop`, `status`, or nothing to start the board.

        Returns:
            The board's URL, its status, or why it did not start.
        """
        from novacode_cli.commands import trello_handler
        from novacode_cli.commands.trello_server import TrelloServer

        state = getattr(self._worker, "session_state", None)
        if state is None:
            text = "/trello needs a live session."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        action = (args or "").strip().lower()
        if action in {"stop", "status"}:
            handler = (
                trello_handler._handle_stop if action == "stop" else trello_handler._handle_status
            )
            text, blocks = _capture_printing(lambda: handler(state))
            return CommandResult(ok=True, message=_first_line(text), blocks=blocks)
        existing = getattr(state, "trello_server", None)
        if existing is not None and getattr(existing, "is_running", False):
            text = f"Kanban board already running at {existing.url}"
            return CommandResult(ok=True, message=text, blocks=(Block("text", text),))
        server = TrelloServer()
        try:
            await server.start()
        except OSError as error:
            text = f"Could not start the kanban board: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        from novacode_cli.commands.trello_remote import connect_telegram

        board_id = str(getattr(state, "session_id", "trello"))
        server.bind_remote(lambda: connect_telegram(state), board_id)
        state.trello_server = server
        self._trello_task = asyncio.create_task(self._trello_watch(server, state))
        text = (
            f"≡ Kanban board at {server.url} — drag a card to Processing and the agent "
            "runs it here, one at a time. /trello stop closes the board."
        )
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _trello_watch(self, server: Any, state: Any) -> None:  # noqa: ANN401
        """Run the shared board loop until the server stops.

        Args:
            server: The board server this task belongs to.
            state: The session state that owns it.
        """
        from novacode_cli.commands.trello_handler import trello_watch_loop
        from novacode_cli.ui.execution import execute_task

        worker = self._worker

        def log(message: str = "", _style: str = "") -> None:
            if message.strip():
                self.log(Block("text", _plain_markup(message)))

        with contextlib.suppress(Exception):
            await trello_watch_loop(
                server,
                getattr(worker, "agent", None),
                getattr(worker, "assistant_id", ""),
                state,
                getattr(state, "token_tracker", None),
                execute_task,
                log,
            )

    async def _create_command(self, args: str | None) -> CommandResult:
        """Serve `/create`: the Skills & Agents browser, through its handler.

        Args:
            args: `stop`, or nothing to start the browser.

        Returns:
            What the handler printed, which names the URL or says it is stopped.
        """
        from novacode_cli.commands.create_handler import handle_create_command

        state = getattr(self._worker, "session_state", None)
        if state is None:
            text = "/create needs a live session."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        try:
            text, blocks = await self._capture_awaiting(lambda: handle_create_command(state, args))
        except Exception as error:  # noqa: BLE001 - a browser command must not kill the session
            text = f"/create failed: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        return CommandResult(ok=True, message=_first_line(text), blocks=blocks)

    async def _capture_awaiting(
        self, call: Callable[[], Awaitable[object]]
    ) -> tuple[str, tuple[Block, ...]]:
        """Await a handler that prints, capturing what it printed.

        Args:
            call: The coroutine factory to run.

        Returns:
            Its output as plain text, and the same text as transcript blocks.
        """
        from novacode_cli.config.config import console

        printed = io.StringIO()
        with console.capture() as captured, contextlib.redirect_stdout(printed):
            await call()
        text = strip_ansi(captured.get() + printed.getvalue()).strip()
        return text, blocks_from_text(text)

    async def _claude_plan_command(self, args: str | None) -> CommandResult:
        """/claude-plan: Claude Code plans headless; Nova implements.

        Args:
            args: The task, or empty to ask for the repository's next step.

        Returns:
            The plan as blocks, plus the prompt that hands it to Nova.
        """
        from novacode_cli.commands import claude_plan

        return await self._external_planner(claude_plan, args, review=False)

    async def _codex_plan_command(self, args: str | None) -> CommandResult:
        """/codex-plan: Codex plans headless; Nova implements.

        Args:
            args: The task, or empty to ask for the repository's next step.

        Returns:
            The plan as blocks, plus the prompt that hands it to Nova.
        """
        from novacode_cli.commands import codex_plan

        return await self._external_planner(codex_plan, args, review=False)

    async def _claude_review_command(self, args: str | None) -> CommandResult:
        """/claude-review [focus]: Claude Code reviews the changes; Nova reports.

        Args:
            args: Optional focus for the review.

        Returns:
            The report as blocks, plus the prompt that has Nova check it.
        """
        from novacode_cli.commands import claude_plan

        return await self._external_planner(claude_plan, args, review=True)

    async def _codex_review_command(self, args: str | None) -> CommandResult:
        """/codex-review [focus]: Codex reviews the changes; Nova reports.

        Args:
            args: Optional focus for the review.

        Returns:
            The report as blocks, plus the prompt that has Nova check it.
        """
        from novacode_cli.commands import codex_plan

        return await self._external_planner(codex_plan, args, review=True)

    async def _external_planner(
        self,
        planner: Any,  # noqa: ANN401 — a module chosen at runtime by command name
        args: str | None,
        *,
        review: bool,
    ) -> CommandResult:
        """Run another agent's CLI headless, then hand the result to Nova.

        The external agent never touches this conversation: it explores the
        repository in its own process, and Nova receives only its text. That
        keeps one model holding the thread, which is the same rule the terminal
        UI follows.

        Args:
            planner: The `claude_plan`/`codex_plan` module to drive.
            args: Task (plan) or focus (review).
            review: Whether this is a read-only review.

        Returns:
            The planner's output as blocks with the prompt Nova should act on.
        """
        text = (args or "").strip()
        if planner.executable() is None:
            return CommandResult(
                ok=False,
                message=(
                    f"{planner.NAME} is not installed. Run {planner.COMMAND} once to install it, "
                    f"or see {planner.DOCS_URL}."
                ),
            )
        cwd = Path(self._cwd or Path.cwd())
        task = text or (
            "Review the changes in this repository"
            if review
            else "Plan how to do this in this repository"
        )
        self.log(Block("text", f"◆ {planner.NAME} is {'reviewing' if review else 'planning'}…"))
        options = dict(planner.REVIEW_ARGS) if review else {}
        try:
            turn = await planner.run_turn(task, cwd, **options)
        except (OSError, RuntimeError, ValueError) as exc:
            return CommandResult(ok=False, message=f"{planner.NAME} could not run: {exc}")
        if turn.error:
            return CommandResult(ok=False, message=f"{planner.NAME} stopped: {turn.error}")
        body = (turn.plan or turn.text or "").strip()
        if not body:
            return CommandResult(ok=False, message=f"{planner.NAME} returned nothing to report.")
        if review:
            return CommandResult(
                ok=True,
                message=f"◆ {planner.NAME} reviewed the changes.",
                blocks=(Block("heading", f"{planner.NAME}'s review"), Block("code", body)),
                prompt=_handoff(
                    f"{planner.NAME} just reviewed this repository and submitted the report below. "
                    "Check each finding against the code, then say what you will fix and what you "
                    "will not, with reasons.",
                    body,
                ),
            )
        return CommandResult(
            ok=True,
            message=f"◆ {planner.NAME} produced a plan.",
            blocks=(Block("heading", f"{planner.NAME}'s plan"), Block("code", body)),
            prompt=_handoff(
                f"{planner.NAME} produced the plan below for this repository. Implement it, "
                "checking each step against the actual code, and say where you deviate.",
                body,
            ),
        )

    async def _loop_command(self, args: str | None) -> CommandResult:
        """/loop: a bounded recurrence this session owns.

        Args:
            args: `30s <prompt>`, `<n>m <prompt>`, `"<cron>" <prompt>`, `list`,
                `status`, or `stop [id]`.

        Returns:
            The scheduler's own wording; a listing arrives as blocks so it stays
            in the transcript while one-shot confirmations can fade.
        """
        state = getattr(self._worker, "session_state", None)
        if state is None or not getattr(state, "thread_id", ""):
            return CommandResult(ok=False, message="Session loops need a live conversation.")
        state._loop_host = True
        if not any(existing is state for existing in self._loop_states):
            self._loop_states.append(state)
        self._start_loop_poll()
        text = (args or "").strip()
        try:
            message = loop_command(state, text)
        except (ValueError, OSError) as exc:
            return CommandResult(ok=False, message=f"/loop: {exc}")
        if text in {"list", "status"} and message:
            lines = message.splitlines()
            return CommandResult(
                ok=True,
                blocks=tuple(Block("text", line) for line in lines),
            )
        return CommandResult(ok=True, message=message)

    def _start_loop_poll(self) -> None:
        """One poll task per port, started on first use."""
        if self._loop_poll is None or self._loop_poll.done():
            self._loop_poll = asyncio.create_task(self._poll_loops())

    async def _poll_loops(self) -> None:
        """Look for a due job once a second, the way the terminal timer does."""
        while True:
            await asyncio.sleep(loops.POLL_SECONDS)
            self._tick_loops()

    def _tick_loops(self) -> None:
        """Expire what ran out of time and claim at most one due job."""
        for state in list(self._loop_states):
            scheduler = getattr(state, "_loop_scheduler", None)
            if scheduler is None:
                continue
            if scheduler.thread_id != getattr(state, "thread_id", ""):
                scheduler.close()
            if scheduler.closed:
                self._loop_states.remove(state)
                continue
            for job_id in scheduler.expire():
                self.log(Block("text", f"Loop {job_id} expired."))
                running = self._loop_jobs.get((scheduler.thread_id, job_id))
                if running is not None:
                    running.cancel()
            idle = not self._busy() and not self._loop_jobs
            job = scheduler.take(idle=idle)
            if job is not None:
                key = (scheduler.thread_id, job.id)
                self._loop_jobs[key] = asyncio.create_task(self._run_loop_job(state, job))

    async def _run_loop_job(self, state: Any, job: Any) -> None:  # noqa: ANN401
        """Run one iteration as a headless turn owned by this port.

        The terminal UI streams its iteration into the transcript. The port
        cannot: a client may not be attached when the timer fires. So the
        iteration runs through the same headless executor `/trello` cards use,
        and its progress arrives as blocks whenever a client is listening.
        """
        from novacode_cli.loops import current_loop
        from novacode_cli.ui.execution import execute_task

        scheduler = state._loop_scheduler
        self._active_loop = (state, job)
        token = current_loop.set((scheduler.thread_id, job.id))
        try:
            prompt = await loops.iteration_prompt(state, job.spec, self._worker.assistant_id)
            if (
                scheduler.closed
                or scheduler.thread_id != getattr(state, "thread_id", "")
                or job.id not in scheduler.jobs
                or time.monotonic() >= job.expires
            ):
                return
            self.log(Block("text", f"↻ Running loop {job.id}"))
            await execute_task(
                loops.instruction(job.id, prompt),
                self._worker.agent,
                self._worker.assistant_id,
                state,
                getattr(self._worker, "token_tracker", None),
            )
        except asyncio.CancelledError:
            scheduler.stop(job.id)
            raise
        except Exception as exc:  # noqa: BLE001 — one bad loop must not kill the poller
            scheduler.stop(job.id)
            self.log(Block("text", f"Loop {job.id} stopped: {exc}"))
        finally:
            current_loop.reset(token)
            scheduler.finish(job)
            self._loop_jobs.pop((scheduler.thread_id, job.id), None)
            if self._active_loop == (state, job):
                self._active_loop = None

    async def _remote_command(self, args: str | None) -> CommandResult:
        """`/remote`: start, stop or inspect the Discord and Telegram bridges.

        The shared handler already owns this surface and prints its own wording,
        so the port delegates instead of re-implementing it. Bare `/remote` shows
        the same status the Textual screen opens with.

        Inbound remote messages still need a consumer that turns them into turns;
        that loop lives in the terminal app, so a bridge started here notifies
        nobody until a client owns it. The ledger row says so rather than
        implying full parity.

        Args:
            args: A sub-command (`status`, `start …`, `stop`, `forget`, …).

        Returns:
            The handler's output as text blocks.
        """
        return await self._passthrough("remote", (args or "").strip() or "status")

    async def _set_conversation(
        self,
        agent: Any,  # noqa: ANN401 — the compiled graph's own typed API
        config: dict[str, Any],
        snapshot: Any,  # noqa: ANN401 — a StateSnapshot from `aget_state`
        kept: list[Any],
    ) -> None:
        """Replace the checkpointed messages with *kept*, as the Textual UI does.

        Removing every message and writing the kept ones is the only way a
        LangGraph checkpoint forgets a turn: `RemoveMessage(REMOVE_ALL_MESSAGES)`
        plus the survivors, written as the `model` node so the graph re-derives
        its next step from them.

        Args:
            agent: The session's compiled graph.
            config: The thread config the state was read with.
            snapshot: The state snapshot the cut was computed from.
            kept: Messages to keep, in order.
        """
        from langchain_core.messages import RemoveMessage
        from langgraph.graph.message import REMOVE_ALL_MESSAGES

        from novacode_cli.session.checkpoint_coordinator import CoordinatedGraph

        values_so_far = getattr(snapshot, "values", None) or {}
        current = list(values_so_far.get("messages", []))
        values: dict[str, Any] = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *kept]}
        event = values_so_far.get("_summarization_event")
        cutoff = (event.get("cutoff_index") or 0) if isinstance(event, dict) else 0
        if cutoff and [m.id for m in current[:cutoff]] != [m.id for m in kept[:cutoff]]:
            # The kept messages no longer start the way the summary says they do,
            # so the summary would describe a conversation that no longer exists.
            values["_summarization_event"] = None
        options: dict[str, Any] = {}
        if isinstance(agent, CoordinatedGraph):
            configurable = (getattr(snapshot, "config", None) or {}).get("configurable", {})
            options["expected_revision"] = configurable.get("checkpoint_id")
        await agent.aupdate_state(config=config, values=values, as_node="model", **options)

    async def _ui_command(self, _args: str | None = None) -> CommandResult:
        """Explain that layout editing belongs to the Textual frontend.

        `/ui` stages, previews and commits a Textual widget layout into
        `~/.nova/ui/<workspace>.json`. The ratatui client draws its own layout and
        has no widgets to patch, so this reports the boundary instead of
        pretending to have changed anything.

        Returns:
            A successful result that says where the answered behaviour lives.
        """
        from novacode_cli.ui.layout_profile import profile_path

        text = (
            "The /ui layout editor belongs to the Textual frontend: it stages and "
            "commits widget layout to "
            f"{profile_path(Path.cwd())}. The ratatui client draws its own "
            "layout, so there is nothing here to patch."
        )
        return CommandResult(ok=True, message="Textual-only.", blocks=(Block("text", text),))

    async def _statusline_screen(self, args: str | None) -> CommandResult:
        """Choose which fields the status line shows.

        The answer is a field-keyed map of booleans, so a field the client never
        mentions keeps its previous state: only what the form returns changes.

        Args:
            args: Rejected when non-empty, matching the Textual usage line.

        Returns:
            A successful result naming the fields now shown, or the usage text.
        """
        from novacode_cli.ui import screen_specs
        from novacode_cli.ui.statusline_fields import FIELDS

        if (args or "").strip():
            return CommandResult(ok=False, message="Usage: /statusline")
        answer = await self.ask(screen_specs.statusline_spec(_persisted_statusline()))
        if not isinstance(answer, dict):
            shown = ", ".join(_persisted_statusline() or ()) or "none"
            return CommandResult(
                ok=True,
                message="Status line unchanged.",
                blocks=(Block("text", f"Status line fields: {shown}"),),
            )
        chosen = [key for key in FIELDS if _truthy(answer.get(key))]
        _persist_statusline(chosen)
        shown = ", ".join(chosen) or "none"
        return CommandResult(
            ok=True,
            message="Status line updated.",
            blocks=(Block("text", f"Status line fields: {shown}"),),
        )

    # ── app-side commands the socket host can answer itself ──────────────
    async def _cron_command(self, args: str | None) -> CommandResult:
        """Manage scheduled tasks through the shared handler.

        `/cron` has no registry entry: `handle_command` dispatches it directly,
        so the port reaches the same code by running the passthrough.

        Args:
            args: Everything after `/cron`.

        Returns:
            The handler's captured output.
        """
        return await self._passthrough("cron", args)

    async def _webhook_command(self, args: str | None) -> CommandResult:
        """Manage the webhook ingress server through the shared handler.

        Args:
            args: Everything after `/webhook`.

        Returns:
            The handler's captured output.
        """
        return await self._passthrough("webhook", args)

    async def _prompt_command(self, args: str | None) -> CommandResult:
        """Manage system-prompt templates through the shared handler.

        Args:
            args: Everything after `/prompt`.

        Returns:
            The handler's captured output.
        """
        return await self._passthrough("prompt", args)

    async def _ponytail_review(self, args: str | None) -> CommandResult:
        """Run the ponytail review helper as a normal turn.

        Args:
            args: Everything after the command.

        Returns:
            A result whose `prompt` starts the helper turn.
        """
        return _ponytail_turn("ponytail-review", args)

    async def _ponytail_audit(self, args: str | None) -> CommandResult:
        """Run the ponytail audit helper as a normal turn.

        Args:
            args: Everything after the command.

        Returns:
            A result whose `prompt` starts the helper turn.
        """
        return _ponytail_turn("ponytail-audit", args)

    async def _ponytail_debt(self, args: str | None) -> CommandResult:
        """Run the ponytail debt helper as a normal turn.

        Args:
            args: Everything after the command.

        Returns:
            A result whose `prompt` starts the helper turn.
        """
        return _ponytail_turn("ponytail-debt", args)

    async def _ponytail_gain(self, args: str | None) -> CommandResult:
        """Run the ponytail gain helper as a normal turn.

        Args:
            args: Everything after the command.

        Returns:
            A result whose `prompt` starts the helper turn.
        """
        return _ponytail_turn("ponytail-gain", args)

    async def _ponytail_help(self, args: str | None) -> CommandResult:
        """Run the ponytail help helper as a normal turn.

        Args:
            args: Everything after the command.

        Returns:
            A result whose `prompt` starts the helper turn.
        """
        return _ponytail_turn("ponytail-help", args)

    async def _reload_plugins(self, _args: str | None = None) -> CommandResult:
        """Rebuild the agent so plugins added this session take effect.

        The agent rebuild, hook reload and plugin count match the Textual
        handler. Plugin *slash commands* are static catalogue rows the server
        discovered at start-up, so the report says they need a client restart
        rather than claiming a refresh the socket cannot perform.

        Args:
            _args: Rejected when non-empty, matching the Textual usage line.

        Returns:
            The reload report, or an explicit failure.
        """
        from novacode_cli import hooks as hooks_module
        from novacode_cli.plugins import claude_plugins

        state = getattr(self._worker, "session_state", None)
        if state is None or not hasattr(state, "reload_mcp_servers"):
            return CommandResult(ok=False, message="Session is unavailable.")
        try:
            new_agent, new_backend = await state.reload_mcp_servers()
        except Exception as error:  # noqa: BLE001 — a plugin failure must not kill the session
            return CommandResult(ok=False, message=f"Reload failed: {error}")
        self._worker.agent = new_agent
        self._worker.backend = new_backend
        hooks_module.reload_hooks()  # plugin hooks re-read on next dispatch
        count = len(claude_plugins.list_plugins())
        text = (
            f"● Reloaded {count} plugin(s) — skills, subagents, MCP and hooks refreshed. "
            "Plugin commands reload when the client restarts."
        )
        return CommandResult(
            ok=True, message=f"Reloaded {count} plugin(s).", blocks=(Block("text", text),)
        )

    async def _cowork_command(self, args: str | None) -> CommandResult:
        """Launch the Cowork desktop app in the browser and report its URL.

        Server start-up is heavy, so the URL is built off the event loop. The
        browser is the local user's, matching the Textual handler.

        Args:
            args: Optional task to preselect.

        Returns:
            The ready report, or an explicit launch failure.
        """

        def launch() -> str:
            import webbrowser

            from novacode_cli.cowork.launcher import cowork_url

            state = getattr(self._worker, "session_state", None)
            url = cowork_url(session_id=getattr(state, "session_id", None), task=task or None)
            with contextlib.suppress(Exception):
                webbrowser.open(url)
            return url

        task = (args or "").strip()
        try:
            url = await asyncio.to_thread(launch)
        except Exception as error:  # noqa: BLE001 — a missing optional extra is a report
            return CommandResult(ok=False, message=f"Cowork failed to launch: {error}")
        text = f"◆ Nova Cowork ready — {url}"
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _artifacts_command(self, _args: str | None = None) -> CommandResult:
        """List this session's artifacts and open the chosen one in a browser.

        Args:
            _args: Rejected when non-empty, matching the Textual usage line.

        Returns:
            A successful no-op, the opened artifact, or the empty-list hint.
        """
        from novacode_cli.artifacts.registry import get_registry
        from novacode_cli.ui import screen_specs

        artifacts = get_registry().list()
        if not artifacts:
            text = (
                "No artifacts yet — ask me to create one, e.g. "
                '"create an artifact showing the changes you made".'
            )
            return CommandResult(
                ok=True, message="No artifacts yet.", blocks=(Block("text", text),)
            )
        answer = await self.ask(screen_specs.artifacts_spec(artifacts))
        chosen = str(answer.get("id") or "") if isinstance(answer, dict) else ""
        artifact = next((item for item in artifacts if item.id == chosen), None)
        if artifact is None:
            return CommandResult(
                ok=True,
                message=f"{len(artifacts)} artifact(s).",
                blocks=(Block("text", f"{len(artifacts)} artifact(s)."),),
            )
        from novacode_cli.artifacts.server import artifact_url

        url = artifact_url(artifact.id)
        with contextlib.suppress(Exception):
            import webbrowser

            await asyncio.to_thread(webbrowser.open, url)
        text = f"◈ Opening {artifact.title} → {url}"
        return CommandResult(ok=True, message=text, blocks=(Block("text", text),))

    async def _computer_command(self, args: str | None) -> CommandResult:
        """Control this session's opt-in guest VM.

        Args:
            args: One of start, preview, pause, resume, stop or status.

        Returns:
            The phase report, the preview URL, or the usage line.
        """
        from novacode_cli.computer.runtime import get_computer

        parts = (args or "").split()
        action = parts[0].lower() if len(parts) == 1 else "status" if not parts else ""
        if action not in {"start", "preview", "pause", "resume", "stop", "status"}:
            usage = "Usage: /computer start|preview|pause|resume|stop|status"
            return CommandResult(ok=False, message=usage, blocks=(Block("text", usage),))
        if action == "start" and self._sandbox_id:
            text = "Computer controls require a local Nova session."
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        state = getattr(self._worker, "session_state", None)
        session_id = getattr(state, "session_id", None) or getattr(state, "thread_id", None)
        blocks: list[Block] = []
        try:
            computer = get_computer(session_id)
            if action in {"start", "preview"}:
                url = computer.preview_url()
                blocks.append(Block("text", f"Computer preview: {url}"))
                await asyncio.to_thread(_open_browser, url)
            if action == "start":
                blocks.append(
                    Block(
                        "text",
                        "Preparing a local Linux VM (2 CPUs, 4 GiB RAM). "
                        "First startup downloads the guest image. QEMU must be installed…",
                    )
                )
            status = await computer.call("status" if action == "preview" else action)
        except Exception as error:  # noqa: BLE001 — an unavailable VM is a report
            text = f"Computer: {error}"
            return CommandResult(ok=False, message=text, blocks=(Block("text", text),))
        message = f"Computer: {status['phase']}"
        if status.get("error"):
            message += f" · {status['error']}"
        elif status["phase"] == "starting":
            message += f" · {status['stage']} · {status['elapsed_seconds']}s elapsed"
        if status["phase"] == "ready":
            if status["paused"]:
                message += " · input paused"
            message += ". Ask Nova to use its computer; /computer preview opens the live view."
        blocks.append(Block("text", message))
        return CommandResult(ok=True, message=message, blocks=tuple(blocks))

    async def _passthrough(self, command: str, args: str | None) -> CommandResult:
        """Run the shared handler and capture what it prints.

        Args:
            command: Command name, already canonical.
            args: Raw argument text, or ``None`.

        Returns:
            The captured output as blocks, a prompt when the handler answered
            with one, an explicit failure when the handler raised, or
            :func:`not_ported` when the shared layer declined the command
            (which happens when plugins or optional extras are absent).
        """
        from novacode_cli.commands.commands import handle_command
        from novacode_cli.config.config import console
        from novacode_cli.ui.ui_elements import TokenTracker

        worker = self._worker
        text = f"/{command}" + (f" {args}" if args else "")
        # Handlers print through the Rich console, but a stray `print()` would
        # otherwise paint over the client's terminal; redirect both.
        printed = io.StringIO()
        try:
            with console.capture() as captured, contextlib.redirect_stdout(printed):
                result = await handle_command(
                    text,
                    worker.agent,
                    TokenTracker(),
                    worker.session_state,
                    worker.assistant_id,
                    session_manager=worker.session_manager,
                    model_name=worker.model_name,
                    image_tracker=worker.image_tracker,
                    sandbox_id=self._sandbox_id,
                    sandbox_type=self._sandbox_type,
                )
        except Exception as exc:  # noqa: BLE001 — a handler must not kill the session
            logger.debug("command %s failed", command, exc_info=True)
            return CommandResult(ok=False, message=f"/{command} failed: {exc}")

        self._sync_session()
        output = strip_ansi(captured.get() + printed.getvalue())
        blocks = blocks_from_text(output)
        if isinstance(result, str):
            # `_stream_prompt` case: the handler answered with a prompt for the
            # agent instead of printing. The host starts that turn.
            return CommandResult(ok=True, blocks=blocks, prompt=result)
        if result is False:
            # The shared layer did not recognise the command, so nothing ran.
            return not_ported(command)
        return CommandResult(ok=True, message="", blocks=blocks)

    def _sync_session(self) -> None:
        """Pick up an agent/backend/model swap made by the handler.

        ``/model`` rebuilds the agent and stashes it on ``session_state``; the
        Textual UI does the same sync after a passthrough command.
        """
        state = getattr(self._worker, "session_state", None)
        if state is None:
            return
        self._worker.agent = getattr(state, "_agent", self._worker.agent) or self._worker.agent
        state_backend = getattr(state, "_backend", None)
        self._worker.backend = state_backend or self._worker.backend
        model = getattr(state, "model", None)
        if model is None:
            return
        self._worker.model_name = getattr(model, "model_name", None) or getattr(
            model, "model", "unknown"
        )
        tracker = getattr(state, "token_tracker", None)
        if tracker is not None:
            with contextlib.suppress(Exception):
                tracker.set_model(self._worker.model_name)


async def run_command(port: ServerPort, name: str, args: str | None) -> CommandResult:
    """Await-able helper so callers need not import ``ServerPort`` internals.

    Args:
        port: The session's port.
        name: Command name.
        args: Raw argument text, or ``None`.

    Returns:
        The command's result.
    """
    return await port.run(name, args)
