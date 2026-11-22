"""Pure builders for the modal screens a command asks the frontend to show.

A `screen` frame carries a *shape*, not a widget (see
`novacode_cli/ui/port.ScreenSpec`), so the data behind a screen has to exist
without a UI toolkit. Every function here takes plain numbers or strings and
returns blocks or a spec, which is what lets the Textual screen and the ratatui
renderer read one source instead of two.

`/context` is the first screen built this way: the Textual `ContextScreen` and
the socket client both call :func:`context_blocks` and :func:`context_spec`, so
the numbers cannot disagree between the two frontends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from novacode_cli.ui.command_center import describe, status_counts
from novacode_cli.ui.port import Block, ScreenSpec

if TYPE_CHECKING:
    from collections.abc import Sequence

    from novacode_cli.ui.command_center import CommandSession

# Percentages are reported to one decimal, matching the Textual screen.
_PERCENT = 100.0

# One row per context category: the label and the breakdown attributes that
# make it up. `system_prompt_tokens` is joined by both memory counters, because
# injected memory travels inside the system prompt.
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("System + memory", ("system_prompt_tokens", "user_memory_tokens", "project_memory_tokens")),
    ("Tool definitions", ("tool_definitions_tokens",)),
    ("User messages", ("user_message_tokens",)),
    ("Assistant messages", ("assistant_message_tokens",)),
    ("Tool results", ("tool_result_tokens",)),
)


@dataclass(frozen=True)
class ContextUsage:
    """The context-window facts one `/context` report needs.

    Attributes:
        model: Model name shown in the heading, or a placeholder.
        used: Tokens the next request is expected to carry.
        window: The model's context window, or `0` when unknown.
        categories: `(label, tokens)` pairs, in report order.
        session_total: Tokens spent in this session, cumulative.
        last_output: Tokens in the last response.
        user_messages: User turns counted by the tracker.
        assistant_messages: Assistant turns counted by the tracker.
        tool_calls: Tool calls counted by the tracker.
        exact: Whether `used` came from the provider rather than an estimate.
    """

    model: str
    used: int
    window: int
    categories: tuple[tuple[str, int], ...]
    session_total: int = 0
    last_output: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    exact: bool = False

    @property
    def remaining(self) -> int:
        """Report the tokens still available in the window.

        Returns:
            `window - used`, never negative.
        """
        return max(0, self.window - self.used)

    @property
    def percent(self) -> float:
        """Report the share of the window in use.

        Returns:
            The percentage, or `0.0` when the window is unknown.
        """
        return (self.used / self.window * _PERCENT) if self.window else 0.0


def _count(source: Any, attribute: str) -> int:  # noqa: ANN401 - tracker objects are untyped
    """Read one integer attribute defensively.

    Args:
        source: The tracker or its breakdown object.
        attribute: Attribute name.

    Returns:
        The value, or `0` when it is missing or not a number.
    """
    value = getattr(source, attribute, 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def context_usage(tracker: Any, model_name: str | None = None) -> ContextUsage | None:  # noqa: ANN401
    """Build the usage facts from a token tracker.

    Args:
        tracker: A `TokenTracker`, or `None` for a session that has none.
        model_name: Fallback model name when the tracker has none.

    Returns:
        The usage, or `None` when no breakdown has been captured yet (the
        state the Textual screen reports as "no token usage captured").
    """
    if tracker is None:
        return None
    try:
        breakdown = tracker.get_breakdown()
    except Exception:  # noqa: BLE001 - a report must never fail the command
        return None
    if breakdown is None:
        return None
    categories = tuple(
        (label, sum(_count(breakdown, attribute) for attribute in attributes))
        for label, attributes in CATEGORIES
    )
    mcp = _count(breakdown, "mcp_tool_tokens") + _count(breakdown, "mcp_prompt_tokens")
    if mcp:
        categories += (("  of which MCP", mcp),)
    model = getattr(tracker, "model_name", None) or model_name or "Model unknown"
    return ContextUsage(
        model=str(model),
        used=_count(breakdown, "total_tokens"),
        window=_count(breakdown, "context_window_size"),
        categories=categories,
        session_total=_count(tracker, "session_total_tokens"),
        last_output=_count(tracker, "last_output"),
        user_messages=_count(breakdown, "user_message_count"),
        assistant_messages=_count(breakdown, "assistant_message_count"),
        tool_calls=_count(breakdown, "tool_call_count"),
        exact=bool(getattr(tracker, "has_api_data", False)),
    )


def _threshold(window: int) -> float:
    """Report the compaction threshold for a window size.

    Args:
        window: The model's context window, or `0`.

    Returns:
        The percentage at which compaction runs, or `0.0` when the level is
        unknown - the same fallback the Textual screen shows.
    """
    if not window:
        return 0.0
    try:
        from novacode_cli.context._analysis import compact_threshold_pct

        return float(compact_threshold_pct(window))
    except Exception:  # noqa: BLE001 - the level is advisory, not required
        return 0.0


def context_blocks(usage: ContextUsage | None) -> tuple[Block, ...]:
    """Build the transcript lines of a `/context` report.

    The hero and the session metrics travel as blocks so the numbers survive
    the modal closing, which is what the Textual screen shows above its table.

    Args:
        usage: The usage facts, or `None` when nothing has been captured.

    Returns:
        The report lines, in order.
    """
    if usage is None:
        return (Block("text", "No token usage captured yet."),)
    source = (
        "Provider-reported current request"
        if usage.exact
        else "Estimated; no provider usage received yet"
    )
    hero = (
        f"{usage.model}  {usage.percent:.1f}%  "
        f"{usage.used:,} / {usage.window:,} tokens  "
        f"({usage.remaining:,} remaining)"
    )
    counts = (
        f"{usage.user_messages} user · {usage.assistant_messages} assistant · "
        f"{usage.tool_calls} tool calls"
    )
    metrics = (
        f"Session total {usage.session_total:,} tokens · Last output {usage.last_output:,} tokens"
    )
    lines = [hero, source, metrics, counts]
    threshold = _threshold(usage.window)
    if threshold:
        state = " · threshold reached" if usage.percent >= threshold else ""
        lines.append(f"Compaction threshold ~{threshold:.0f}%{state}")
    return (Block("text", "\n".join(lines)),)


def context_spec(usage: ContextUsage | None) -> ScreenSpec:
    """Build the context breakdown table.

    Args:
        usage: The usage facts, or `None` for the empty state.

    Returns:
        A `table` spec: one row per category, with its token count and share.
    """
    rows = []
    if usage is not None:
        for index, (label, count) in enumerate(usage.categories):
            share = (count / usage.used * _PERCENT) if usage.used else 0.0
            rows.append(
                {
                    "id": f"category-{index}",
                    "category": label,
                    "tokens": f"{count:,}",
                    "share": f"{share:.1f}%",
                }
            )
    payload = {
        "columns": [
            {"key": "category", "title": "Category", "width": 20},
            {"key": "tokens", "title": "Tokens", "width": 10},
            {"key": "share", "title": "Share", "width": 7},
        ],
        "rows": rows,
    }
    return ScreenSpec("table", payload, "Context window")


def theme_spec(current: str | None = None) -> ScreenSpec:
    """Build the `/theme` picker.

    The list comes from :mod:`novacode_cli.ui.theme_table`, which is generated
    from the Textual UI's own palettes, so both frontends offer the same names.
    Choosing a row answers `{"id": "<theme name>"}`; the port applies it.

    Args:
        current: The theme in force, marked in its row, or `None`.

    Returns:
        A `list` spec: one item per theme, with its brightness and background.
    """
    from novacode_cli.ui.theme_table import THEMES

    items = [
        {
            "id": theme.name,
            "label": f"{theme.name}{' *' if theme.name == current else ''}",
            "detail": f"{'dark' if theme.dark else 'light'} · {theme.background or 'default'}",
        }
        for theme in THEMES
    ]
    return ScreenSpec("list", {"items": items, "current": current or ""}, "Theme")


def statusline_spec(selected: Sequence[str] | None = None) -> ScreenSpec:
    """Build the `@@/statusline@@` toggle form.

    Every field the status bar can show becomes one toggle, pre-set from the
    current choice. The answer is a field-keyed map of booleans, which the port
    turns back into the same tuple the Textual UI persists.

    Args:
        selected: Keys currently shown, or `None` for the defaults.

    Returns:
        A `form` spec whose fields all carry `toggle: True`.
    """
    from novacode_cli.ui.statusline_fields import DEFAULT_FIELDS, FIELDS

    chosen = set(DEFAULT_FIELDS if selected is None else selected)
    fields = [
        {
            "key": key,
            "label": label,
            "value": "on" if key in chosen else "off",
            "toggle": True,
        }
        for key, label in FIELDS.items()
    ]
    return ScreenSpec("form", {"fields": fields}, "Status line")


# Reasoning effort levels, in the order the Textual settings screen offers them.
EFFORT_OPTIONS: tuple[str, ...] = ("off", "low", "medium", "high")

# Context budgets offered by the same screen, as (tokens, label). Zero means "the
# model's whole window".
CONTEXT_BUDGETS: tuple[tuple[int, str], ...] = (
    (64_000, "64K"),
    (128_000, "128K · default"),
    (200_000, "200K"),
    (400_000, "400K"),
    (1_000_000, "1M"),
    (0, "The model's whole window"),
)


def budget_label(tokens: int) -> str:
    """Return the label for a token budget, keeping a hand-set value visible."""
    for value, label in CONTEXT_BUDGETS:
        if value == tokens:
            return label
    return f"{tokens:,}"


def budget_tokens(label: str) -> int | None:
    """Resolve a budget label back to tokens, accepting a plain number.

    Args:
        label: A label from :data:`CONTEXT_BUDGETS`, or digits.

    Returns:
        The token count, or `None` when the answer is not a budget at all.
    """
    for value, known in CONTEXT_BUDGETS:
        if known == label.strip():
            return value
    text = label.strip().replace(",", "").replace("_", "")
    return int(text) if text.isdigit() else None


def settings_spec(effort: str, budget: int) -> ScreenSpec:
    """Build the `@@/settings@@` form for agent preferences.

    Only the AGENT section travels over the socket: reasoning effort and the
    context budget are two fixed choices, which the shared form vocabulary
    already carries. The Textual screen's other sections stay Textual-only
    until their switches are served one by one.

    Args:
        effort: Current reasoning effort, one of :data:`EFFORT_OPTIONS`.
        budget: Current context budget in tokens.

    Returns:
        A `form` spec with two choice fields.
    """
    current = effort if effort in EFFORT_OPTIONS else "off"
    return ScreenSpec(
        "form",
        {
            "fields": [
                {
                    "key": "reasoning_effort",
                    "label": "Reasoning effort",
                    "value": current,
                    "options": list(EFFORT_OPTIONS),
                },
                {
                    "key": "context_budget_tokens",
                    "label": "Context budget",
                    "value": budget_label(budget),
                    "options": [label for _, label in CONTEXT_BUDGETS],
                },
            ]
        },
        "Settings",
    )


# ── full settings surface ────────────────────────────────────────────────
# The Textual `SettingsScreen` edits several independent sections. Each row is
# a toggle or a fixed choice, which is exactly the form vocabulary, so the whole
# screen travels over the socket instead of staying Textual-only. Keys are the
# config slots the Textual screen writes, so a change made here is read by both
# frontends.

#: Voice input modes offered by the Textual settings screen.
VOICE_MODES: tuple[tuple[str, str], ...] = (
    ("push_to_talk", "Push to talk · Ctrl+G"),
    ("listen", "Always listening · Ctrl+L"),
)

#: Animation frame rates, as (value, label).
ANIMATION_FPS: tuple[tuple[int, str], ...] = (
    (15, "15 FPS · reduced CPU use"),
    (30, "30 FPS · balanced"),
    (60, "60 FPS · smooth"),
)

#: Provider tables, loaded once on first use so the module stays cheap to import.
_VOICE_PROVIDER_CACHE: dict[str, dict[str, dict[str, object]]] = {}


def voice_providers() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Return the STT and TTS provider tables.

    Returns:
        `(stt, tts)`, each mapping provider id to its metadata.
    """
    if not _VOICE_PROVIDER_CACHE:
        from novacode_cli.audio.providers import STT_PROVIDERS, TTS_PROVIDERS

        _VOICE_PROVIDER_CACHE["stt"] = STT_PROVIDERS
        _VOICE_PROVIDER_CACHE["tts"] = TTS_PROVIDERS
    return _VOICE_PROVIDER_CACHE["stt"], _VOICE_PROVIDER_CACHE["tts"]


def _provider_options(providers: dict[str, dict[str, object]]) -> list[str]:
    """Render provider choices as `id · name` labels, in registry order.

    Args:
        providers: Provider metadata by id.

    Returns:
        Select options whose value is the id before the first ` · `.
    """
    return [f"{name} · {meta.get('name', name)}" for name, meta in providers.items()]


def provider_id(label: str) -> str:
    """Resolve a provider label back to its registry id.

    Args:
        label: A label from :func:`_provider_options`, or a bare id.

    Returns:
        The provider id.
    """
    return label.split(" · ", 1)[0].strip()


@dataclass(frozen=True)
class Preferences:
    """Every settings value the socket can read and write.

    Defaults mirror `NovaConfig` and the Textual `SettingsScreen`; a caller that
    cannot read one slot leaves it at its default instead of inventing a value.

    Attributes:
        effort: Reasoning effort, one of :data:`EFFORT_OPTIONS`.
        budget: Context budget in tokens, `0` for the whole window.
        decision_pruning: Whether a decision model picks old tool output to drop.
        learning: Whether the learning loop runs.
        learning_auto: Whether new lessons are approved automatically.
        voice_enabled: Whether voice input is on.
        voice_mode: `push_to_talk` or `listen`.
        voice_speak: Whether replies are spoken.
        stt_provider: Speech-to-text provider id.
        tts_provider: Text-to-speech provider id.
        submit_on_enter: Whether Enter sends instead of inserting a line.
        autocomplete: Whether slash/@ suggestions show.
        animation_fps: Live animation frame rate.
        low_resource: Whether the lower active refresh rate is on.
        auto_approve_decisions: Whether auto-approve asks a decision model.
        teams: Whether agent teams are enabled.
        swarm: Whether the autonomous swarm is enabled.
    """

    effort: str = "off"
    budget: int = 128_000
    decision_pruning: bool = False
    learning: bool = True
    learning_auto: bool = False
    voice_enabled: bool = False
    voice_mode: str = "push_to_talk"
    voice_speak: bool = False
    stt_provider: str = "faster-whisper"
    tts_provider: str = "piper"
    submit_on_enter: bool = True
    autocomplete: bool = True
    animation_fps: int = 30
    low_resource: bool = False
    auto_approve_decisions: bool = False
    teams: bool = False
    swarm: bool = False


def _toggle(key: str, label: str, *, value: bool) -> dict[str, object]:
    """Build one checkbox field."""
    return {"key": key, "label": label, "value": "on" if value else "off", "toggle": True}


def _choice(key: str, label: str, value: str, options: list[str]) -> dict[str, object]:
    """Build one fixed-choice field."""
    return {"key": key, "label": label, "value": value, "options": options}


def _voice_fields(prefs: Preferences) -> list[dict[str, object]]:
    """The VOICE section as form fields."""
    stt, tts = voice_providers()
    modes = {key for key, _ in VOICE_MODES}
    mode = prefs.voice_mode if prefs.voice_mode in modes else "push_to_talk"
    return [
        _toggle("voice_enabled", "Voice input", value=prefs.voice_enabled),
        _choice(
            "voice_mode",
            "Listening",
            mode,
            [label for _, label in VOICE_MODES],
        ),
        _toggle("voice_speak_responses", "Speak replies aloud", value=prefs.voice_speak),
        _choice(
            "voice_stt_provider",
            "Speech to text",
            _provider_label(stt, prefs.stt_provider),
            _provider_options(stt),
        ),
        _choice(
            "voice_tts_provider",
            "Text to speech",
            _provider_label(tts, prefs.tts_provider),
            _provider_options(tts),
        ),
    ]


def _provider_label(providers: dict[str, dict[str, object]], provider: str) -> str:
    """The option label for a provider id, keeping a hand-set id visible."""
    if provider in providers:
        return f"{provider} · {providers[provider].get('name', provider)}"
    return f"{provider} · custom"


def voice_spec(prefs: Preferences) -> ScreenSpec:
    """Build the `@@/voice@@` settings form.

    Args:
        prefs: Current preferences.

    Returns:
        A `form` spec carrying only the voice section.
    """
    return ScreenSpec("form", {"fields": _voice_fields(prefs)}, "Voice")


def preferences_spec(prefs: Preferences) -> ScreenSpec:
    """Build the complete `@@/settings@@` form.

    The order matches the Textual screen: AGENT, TOOL OUTPUT, LEARNING, VOICE,
    INPUT, PERFORMANCE, AUTOMATION, EXPERIMENTAL. Appearance stays in
    `@@/theme@@` and is named in the title rather than duplicated here.

    Args:
        prefs: Current preferences.

    Returns:
        A `form` spec with one field per setting.
    """
    rates = {value for value, _ in ANIMATION_FPS}
    fps = prefs.animation_fps if prefs.animation_fps in rates else 30
    fields: list[dict[str, object]] = [
        _choice(
            "reasoning_effort",
            "Reasoning effort",
            prefs.effort if prefs.effort in EFFORT_OPTIONS else "off",
            list(EFFORT_OPTIONS),
        ),
        _choice(
            "context_budget_tokens",
            "Context budget",
            budget_label(prefs.budget),
            [label for _, label in CONTEXT_BUDGETS],
        ),
        _toggle("decision_pruning", "Decision-based tool pruning", value=prefs.decision_pruning),
        _toggle("learning_enabled", "Learn from sessions", value=prefs.learning),
        _toggle(
            "learning_auto_approve", "Approve new lessons automatically", value=prefs.learning_auto
        ),
    ]
    fields.extend(_voice_fields(prefs))
    fields.extend(
        [
            _toggle("submit_on_enter", "Enter sends the prompt", value=prefs.submit_on_enter),
            _toggle(
                "autocomplete_enabled", "Show slash and @ suggestions", value=prefs.autocomplete
            ),
            _choice(
                "animation_fps",
                "Live animation frame rate",
                dict(ANIMATION_FPS).get(fps, f"{fps} FPS"),
                [label for _, label in ANIMATION_FPS],
            ),
            _toggle("low_resource_mode", "Lower active refresh rate", value=prefs.low_resource),
            _toggle(
                "auto_approve_decisions_enabled",
                "Ask a decision model in auto-approve mode",
                value=prefs.auto_approve_decisions,
            ),
            _toggle("agent_teams_enabled", "Agent teams", value=prefs.teams),
            _toggle("agent_teams_swarm", "Autonomous swarm", value=prefs.teams and prefs.swarm),
        ]
    )
    return ScreenSpec("form", {"fields": fields}, "Settings")


def animation_fps_value(label: str) -> int | None:
    """Resolve an animation-rate label back to its number.

    Args:
        label: A label from :data:`ANIMATION_FPS`, or digits.

    Returns:
        The frame rate, or `None` when the answer is not a rate.
    """
    for value, known in ANIMATION_FPS:
        if known == label.strip():
            return value
    text = label.strip().split(" ", 1)[0]
    return int(text) if text.isdigit() else None


# ── Oracle mode ──────────────────────────────────────────────────────────
def oracle_spec(settings: object) -> ScreenSpec:
    """Build the `@@/oracle-mode settings@@` form.

    Args:
        settings: An `OracleSettings`, or `None` to use the documented defaults.

    Returns:
        A `form` spec with target, rounds, reviewers, scope and default-enabled.
    """
    from novacode_cli.core.oracle import MAX_ROUNDS, SCOPE_LABELS

    target = getattr(settings, "target", 8.0)
    rounds = getattr(settings, "max_rounds", 3)
    reviewers = getattr(settings, "reviewers", 3)
    scope = getattr(settings, "scope", "task")
    default_enabled = bool(getattr(settings, "default_enabled", False))
    return ScreenSpec(
        "form",
        {
            "fields": [
                {"key": "target", "label": "Target score (0-10)", "value": f"{target:g}"},
                {
                    "key": "max_rounds",
                    "label": f"Review rounds (1-{MAX_ROUNDS})",
                    "value": str(rounds),
                },
                {
                    "key": "reviewers",
                    "label": "Independent reviewers (1-5)",
                    "value": str(reviewers),
                },
                _choice(
                    "scope",
                    "Review scope",
                    SCOPE_LABELS.get(scope, "This task"),
                    list(SCOPE_LABELS.values()),
                ),
                _toggle(
                    "default_enabled",
                    "Start new threads with Oracle enabled",
                    value=default_enabled,
                ),
            ]
        },
        "Oracle settings",
    )


# ── parallel session tabs ────────────────────────────────────────────────
def tabs_spec(rows: Sequence[dict[str, object]], active: str | None = None) -> ScreenSpec:
    """Build the `@@/tabs@@` session list.

    Rows come from the server's session registry, the same data the `sessions`
    frame carries, so the list cannot disagree with the tab bar.

    Args:
        rows: `{"id", "name", "cwd", "status", "model"}` dicts.
        active: The id this connection is attached to, marked in its row.

    Returns:
        A `list` spec whose item id is the session id; choosing one asks the
        client to send the existing `attach` frame.
    """
    items = []
    for row in rows:
        session_id = str(row.get("id") or "")
        if not session_id:
            continue
        name = str(row.get("name") or session_id[:8])
        marker = " → " if session_id == active else "   "
        detail = " · ".join(
            str(row.get(key) or "") for key in ("status", "model", "cwd") if row.get(key)
        )
        items.append(
            {
                "id": session_id,
                "label": f"{marker}{name}",
                "detail": detail,
            }
        )
    return ScreenSpec("list", {"items": items, "current": active or ""}, "Session tabs")


# ── council settings ─────────────────────────────────────────────────────
def _seat_value(seat: object, key: str, default: object = "") -> object:
    """Read a seat field from an object or a settings dict."""
    if isinstance(seat, dict):
        return seat.get(key, default)
    return getattr(seat, key, default)


def _council_entries(settings: object) -> list[object]:
    """Every editable council seat, members first and the judge last."""
    if isinstance(settings, dict):
        entries = list(settings.get("members") or ())
        judge = settings.get("judge")
    else:
        entries = list(getattr(settings, "members", ()) or ())
        judge = getattr(settings, "judge", None)
    return [*entries, *([judge] if judge is not None else [])]


def council_seats_spec(settings: object) -> ScreenSpec:
    """Build the council-seat list that opens the editor.

    The final row is an explicit Save. Choosing a seat opens its form; the loop
    keeps the draft in the server so no edit is lost between seats.

    Args:
        settings: A `CouncilSettings` snapshot.

    Returns:
        A `list` spec with one row per seat plus Save.
    """
    items = []
    for seat in _council_entries(settings):
        provider = str(_seat_value(seat, "provider") or "")
        model = str(_seat_value(seat, "model") or "")
        model_label = f"{provider}: {model}" if provider else "session model"
        items.append(
            {
                "id": str(_seat_value(seat, "id")),
                "label": str(_seat_value(seat, "name") or "Unnamed voice"),
                "detail": model_label,
            }
        )
    items.append({"id": "__reset__", "label": "Reset all", "detail": "Restore the defaults"})
    items.append({"id": "__save__", "label": "Save and close", "detail": "Validate all seats"})
    return ScreenSpec("list", {"items": items}, "Council settings")


def council_provider_options() -> list[str]:
    """Provider choices as `provider · name`, with inheritance first."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    return [
        "Session model (inherit)",
        *(
            f"{provider} · {preset.get('name', provider)}"
            for provider, preset in MODEL_PRESETS.items()
        ),
    ]


def council_member_spec(member: object) -> ScreenSpec:
    """Build one seat's form: identity, model override and perspective.

    Args:
        member: A `CouncilMember`.

    Returns:
        A `form` spec. The template choice, when set, replaces the system prompt
        on save, matching the Textual editor's Apply button.
    """
    from novacode_cli.council_settings import personality_templates

    provider = str(_seat_value(member, "provider") or "")
    model = str(_seat_value(member, "model") or "")
    templates = personality_templates()
    template_labels = ["Keep current", *(template.name for template in templates)]
    provider_label = next(
        (label for label in council_provider_options() if provider_id(label) == provider),
        "Session model (inherit)",
    )
    return ScreenSpec(
        "form",
        {
            "fields": [
                {"key": "name", "label": "Display name", "value": str(_seat_value(member, "name"))},
                _choice("provider", "Model provider", provider_label, council_provider_options()),
                {"key": "model", "label": "Model ID (needed for an override)", "value": model},
                _choice("template", "Personality template", "Keep current", template_labels),
                {
                    "key": "system",
                    "label": "System prompt",
                    "value": str(_seat_value(member, "system")),
                },
            ]
        },
        f"Council · {_seat_value(member, 'name', 'voice')}",
    )


# ── proactive agents ─────────────────────────────────────────────────────
def proactive_agents_spec(agents: Sequence[tuple[str, object]]) -> ScreenSpec:
    """Build the proactive-agent picker.

    Args:
        agents: `(key, AgentSpec)` pairs from `ProactiveStore.agents()`.

    Returns:
        A `list` spec with New and Close rows.
    """
    items = [{"id": "__new__", "label": "New agent", "detail": "Save a definition"}]
    for key, spec in agents:
        cron = str(getattr(spec, "cron", "") or "")
        github = str(getattr(spec, "github", "") or "")
        enabled = "on" if getattr(spec, "enabled", False) else "paused"
        detail = " · ".join(part for part in (enabled, cron, github) if part)
        items.append({"id": key, "label": str(getattr(spec, "name", "") or key), "detail": detail})
    items.append({"id": "__close__", "label": "Close", "detail": "Leave proactive agents"})
    return ScreenSpec("list", {"items": items}, "Proactive agents")


def proactive_actions_spec(name: str) -> ScreenSpec:
    """Build the action picker for one saved agent.

    Args:
        name: Agent display name.

    Returns:
        A `list` spec: edit, run, history, remove, back.
    """
    return ScreenSpec(
        "list",
        {
            "items": [
                {"id": "edit", "label": "Edit definition", "detail": ""},
                {"id": "run", "label": "Run now", "detail": "Uses the saved settings"},
                {"id": "history", "label": "Run history", "detail": ""},
                {"id": "remove", "label": "Remove agent", "detail": "Deletes triggers and history"},
                {"id": "back", "label": "Back", "detail": ""},
            ]
        },
        name,
    )


def proactive_form_spec(spec: object | None, workspace: str) -> ScreenSpec:
    """Build the create/edit form for one proactive agent.

    Args:
        spec: Existing `AgentSpec`, or `None` for a new agent.
        workspace: Default workspace for a new agent.

    Returns:
        A `form` spec carrying every validated `AgentSpec` field.
    """
    cron = str(getattr(spec, "cron", "") or "")
    return ScreenSpec(
        "form",
        {
            "fields": [
                {"key": "name", "label": "Name", "value": str(getattr(spec, "name", "") or "")},
                {
                    "key": "instructions",
                    "label": "Standing instructions",
                    "value": str(getattr(spec, "instructions", "") or ""),
                },
                {
                    "key": "workspace",
                    "label": "Workspace",
                    "value": str(getattr(spec, "workspace", "") or workspace),
                },
                {
                    "key": "github",
                    "label": "GitHub repo (owner/name)",
                    "value": str(getattr(spec, "github", "") or ""),
                },
                {
                    "key": "pypi",
                    "label": "PyPI package",
                    "value": str(getattr(spec, "pypi", "") or ""),
                },
                {"key": "cron", "label": "Cron schedule (optional)", "value": cron},
                _toggle(
                    "read_workspace",
                    "Read committed Git files",
                    value=bool(getattr(spec, "read_workspace", False)),
                ),
                _toggle(
                    "watch_tags",
                    "Watch for new GitHub tags",
                    value=bool(getattr(spec, "watch_tags", False)),
                ),
                _toggle("enabled", "Enable triggers", value=bool(getattr(spec, "enabled", False))),
            ]
        },
        "Proactive agent",
    )


def proactive_run_spec(name: str) -> ScreenSpec:
    """Build the optional run-context form before queueing a manual run."""
    return ScreenSpec(
        "form",
        {"fields": [{"key": "context", "label": "Run context (optional)", "value": ""}]},
        f"Run {name}",
    )


def proactive_history_spec(name: str, runs: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build a readable history report for one agent.

    Args:
        name: Agent display name.
        runs: Rows from `ProactiveStore.history()`.

    Returns:
        A `detail` spec listing each run newest first.
    """
    from datetime import UTC, datetime

    lines = [f"# {name} · run history", ""]
    if not runs:
        lines.append("No runs yet. Run this agent to see a report.")
    for row in runs:
        created = float(row.get("created") or 0)
        when = (
            datetime.fromtimestamp(created, tz=UTC).strftime("%b %d %H:%M")
            if created
            else "unknown"
        )
        lines.append(
            f"## {str(row.get('status', 'unknown')).title()} · {row.get('trigger', '')} · {when}"
        )
        result = str(row.get("result", "") or "").strip()
        lines.append(result or "_The report appears when the run ends._")
        lines.append("")
    return ScreenSpec("detail", {"markdown": "\n".join(lines), "actions": []}, name)


def council_plans_spec(run: object) -> ScreenSpec:
    """Render one stored council run as a markdown detail screen.

    Args:
        run: A `CouncilRun` loaded from its artifact directory.

    Returns:
        A `split` spec: ranked plans on the left, full detail on the right, and
        actions that approve a plan or reject the run.
    """
    from novacode_cli.council_planner import AWAITING_APPROVAL, render_plans

    run_id = str(getattr(run, "id", "") or "")
    actions = []
    if getattr(run, "status", "") == AWAITING_APPROVAL:
        for plan in getattr(run, "ranked_plans", list)():
            proposal = run.proposal(plan.proposal_id)
            title = proposal.title if proposal is not None else plan.proposal_id
            actions.append(
                {"id": f"approve:{plan.rank}", "label": f"Approve #{plan.rank} — {title}"}
            )
        actions.append({"id": "reject", "label": "Reject all plans"})
    actions.append({"id": "close", "label": "Close"})
    summary = [f"# {run_id}", "", f"**Task:** {getattr(run, 'prompt', '')}", ""]
    for plan in getattr(run, "ranked_plans", list)():
        proposal = run.proposal(plan.proposal_id)
        title = proposal.title if proposal is not None else plan.proposal_id
        summary.append(f"- **#{plan.rank}** {title} · {plan.score:.1f}/10")
    return ScreenSpec(
        "split",
        {
            "left": {"title": "Plans", "markdown": "\n".join(summary)},
            "right": {"title": "Detail", "markdown": render_plans(run)},
            "actions": actions,
        },
        run_id,
    )


def oracle_run_spec(run: object) -> ScreenSpec:
    """Render one Oracle run's reviewers and latest scores.

    Args:
        run: An `OracleRun`, or `None` when no review has run this thread.

    Returns:
        A markdown `detail` spec. Controls stay commands (`/oracle-mode`), so no
        actions are invented.
    """
    if run is None:
        return ScreenSpec(
            "detail", {"markdown": "No Oracle review has run yet.", "actions": []}, "Oracle"
        )
    status = str(getattr(run, "status", "working"))
    settings = getattr(run, "settings", None)
    target = getattr(settings, "target", None)
    round_number = int(getattr(run, "round", 0) or 0)
    lines = [
        f"# Oracle · {status}",
        "",
        f"Round {round_number} · target {target}/10"
        if target is not None
        else f"Round {round_number}",
    ]
    error = str(getattr(run, "error", "") or "")
    if error:
        lines.extend(["", f"**Error:** {error}"])
    reviewers = list(getattr(run, "reviewers", ()) or ())
    if not reviewers:
        lines.extend(["", "No reviewer has started yet."])
    for row in reviewers:
        score = getattr(getattr(row, "review", None), "score", None)
        icon = "✓" if score is not None else ("✖" if getattr(row, "error", "") else "◌")
        pages = ""
        if getattr(row, "page_count", 0):
            pages = f" · {getattr(row, 'pages_read', 0)}/{getattr(row, 'page_count', 0)} pages"
        grade = f" · {score:g}/10" if score is not None else ""
        slot = getattr(row, "slot", "?")
        assignment = getattr(row, "assignment", "")
        lines.append(f"- {icon} Reviewer {slot} · {assignment}{grade}{pages}")
        if getattr(row, "review", None) is not None:
            lines.append(f"  {row.review.summary}")
        elif getattr(row, "error", ""):
            lines.append(f"  {row.error}")
    return ScreenSpec("detail", {"markdown": "\n".join(lines), "actions": []}, "Oracle")


# ── teammates ────────────────────────────────────────────────────────────
def teammates_spec(roster: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build the teammate roster picker.

    Args:
        roster: Agent states from the worker's `status` action.

    Returns:
        A `list` spec. Management actions are not invented here; this is the
        read-only view the socket can serve today.
    """
    items = [{"id": "__new__", "label": "New teammate", "detail": ""}]
    for state in roster:
        name = str(state.get("name") or "Teammate")
        tasks = list(state.get("tasks") or [])
        pending = any(
            proposal.get("state") == "pending" for proposal in state.get("approvals") or []
        )
        status = "input approval needed" if pending else str(state.get("task_state") or "idle")
        latest = ""
        if tasks:
            latest = " ".join(str(tasks[-1].get("text") or "").split())[:60]
        items.append(
            {
                "id": str(state.get("agent_id") or state.get("thread") or name),
                "label": name,
                "detail": f"{status} · {latest}" if latest else status,
            }
        )
    items.append({"id": "__close__", "label": "Close", "detail": ""})
    return ScreenSpec("list", {"items": items}, "Teammates")


def teammate_detail_spec(state: dict[str, object]) -> ScreenSpec:
    """Render one teammate's state, tasks and pending approvals.

    Args:
        state: One agent state from the worker's `status` action.

    Returns:
        A markdown `detail` spec. Results are the public task text/output only.
    """
    name = str(state.get("name") or "Teammate")
    lines = [f"# {name}", ""]
    computer = state.get("computer") or {}
    lines.append(
        f"**Task:** {str(state.get('task_state') or 'idle').capitalize()} · "
        f"**Computer:** {computer.get('phase') or 'not created'!s} · "
        f"**Control:** {'You' if computer.get('manual') else 'Teammate'}"
    )
    recovery = str(state.get("recovery") or "")
    if recovery:
        lines.extend(["", f"> {recovery}"])
    approvals = list(state.get("approvals") or [])
    pending = [a for a in approvals if a.get("state") == "pending"]
    if pending:
        lines.extend(["", "## Pending input approvals", ""])
        lines.extend(
            f"- {approval.get('text') or approval.get('id') or ''!s}" for approval in pending
        )
    tasks = list(state.get("tasks") or [])
    lines.extend(["", "## Tasks", ""])
    if not tasks:
        lines.append("No tasks yet.")
    for task in tasks[-10:]:
        status = str(task.get("status") or "unknown")
        lines.extend([f"### {status.capitalize()} · {task.get('id') or ''!s}", ""])
        lines.append(str(task.get("text") or "").strip() or "_No assignment text._")
        result = str(task.get("error") or task.get("full_output") or "").strip()
        if result:
            heading = "Error" if task.get("error") else "Result"
            lines.extend(["", f"**{heading}**", "", result])
        lines.append("")
    return ScreenSpec("detail", {"markdown": "\n".join(lines), "actions": []}, name)


# ── rooms ────────────────────────────────────────────────────────────────
def rooms_spec(rooms: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build the project-room picker.

    Args:
        rooms: Rows from the room store: id, name, topic.

    Returns:
        A `list` spec. Sending, task work and settings stay commands/actions for
        the fuller room widget.
    """
    items = [{"id": "__new__", "label": "New channel", "detail": ""}]
    items.extend(
        {
            "id": str(room.get("id") or ""),
            "label": str(room.get("name") or room.get("id") or ""),
            "detail": str(room.get("topic") or ""),
        }
        for room in rooms
        if room.get("id")
    )
    items.append({"id": "__close__", "label": "Close", "detail": ""})
    return ScreenSpec("list", {"items": items}, "Rooms")


def room_history_spec(room: str, messages: Sequence[dict[str, object]]) -> ScreenSpec:
    """Render one room's public transcript.

    Args:
        room: Room id.
        messages: Rows from `RoomStore.history`.

    Returns:
        A markdown `detail` spec with author-attributed messages.
    """
    lines = [f"# {room}", ""]
    if not messages:
        lines.append("No messages yet.")
    for message in messages:
        author = str(message.get("author") or "unknown")
        created = str(message.get("created_at") or "")
        lines.append(f"**{author}**" + (f" · {created}" if created else ""))
        text = str(message.get("text") or "").strip()
        lines.append(text or "_empty message_")
        reactions = list(message.get("reactions") or [])
        if reactions:
            grouped: dict[str, int] = {}
            for reaction in reactions:
                emoji = str(reaction.get("emoji") or "?")
                grouped[emoji] = grouped.get(emoji, 0) + 1
            lines.append(" ".join(f"{emoji} x{count}" for emoji, count in grouped.items()))
        lines.append("")
    return ScreenSpec("detail", {"markdown": "\n".join(lines), "actions": []}, room)


# ── teammate management ──────────────────────────────────────────────────
def teammate_provider_options() -> list[str]:
    """Provider choices for teammate models, with Nova's default first."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    return [
        "Use Nova's provider",
        *(
            f"{provider} · {preset.get('name', provider)}"
            for provider, preset in MODEL_PRESETS.items()
        ),
    ]


def _teammate_model_label(selection: object) -> str:
    """Resolve a saved `{provider, model}` selection to its option label."""
    if not isinstance(selection, dict) or not selection.get("provider"):
        return "Use Nova's provider"
    provider = str(selection.get("provider"))
    return next(
        (
            candidate
            for candidate in teammate_provider_options()
            if provider_id(candidate) == provider
        ),
        provider,
    )


def teammate_actions_spec(state: dict[str, object]) -> ScreenSpec:
    """Build the action menu for one teammate.

    Args:
        state: One agent status dict.

    Returns:
        A `list` spec. Approval ids are embedded (`approve:<id>`) so the answer
        cannot accidentally approve a different pending request.
    """
    items: list[dict[str, object]] = [
        {"id": "assign", "label": "Assign a task", "detail": ""},
        {"id": "preview", "label": "Computer preview", "detail": ""},
        {"id": "pause", "label": "Pause work", "detail": ""},
        {"id": "resume", "label": "Resume work", "detail": ""},
        {"id": "stop", "label": "Stop current task", "detail": ""},
        {"id": "auto_approve", "label": "Auto-approve desktop input", "detail": ""},
    ]
    pending = [a for a in state.get("approvals") or [] if a.get("state") == "pending"]
    for approval in pending:
        key = str(approval.get("id") or "")
        if not key:
            continue
        items.extend(
            [
                {"id": f"approve:{key}", "label": "Approve pending input", "detail": key},
                {"id": f"deny:{key}", "label": "Deny pending input", "detail": key},
            ]
        )
    items.extend(
        [
            {"id": "history", "label": "Task history", "detail": ""},
            {"id": "edit", "label": "Edit settings", "detail": ""},
            {"id": "delete", "label": "Delete teammate", "detail": "Requires its name"},
            {"id": "back", "label": "Back", "detail": ""},
        ]
    )
    return ScreenSpec("list", {"items": items}, str(state.get("name") or "Teammate"))


def teammate_form_spec(state: dict[str, object] | None) -> ScreenSpec:
    """Build the create/edit form for a teammate.

    Args:
        state: Existing status dict, or `None` for creation.

    Returns:
        A `form` spec. Provider choices are ignored when left on Nova's default.
    """
    state = state or {}
    model = state.get("model")
    vision = state.get("vision")
    options = teammate_provider_options()
    return ScreenSpec(
        "form",
        {
            "fields": [
                {"key": "name", "label": "Agent name", "value": str(state.get("name") or "")},
                {"key": "role", "label": "Role", "value": str(state.get("role") or "")},
                _choice("model_provider", "Model provider", _teammate_model_label(model), options),
                {
                    "key": "model",
                    "label": "Model ID",
                    "value": str((model or {}).get("model") or "")
                    if isinstance(model, dict)
                    else "",
                },
                _choice(
                    "vision_provider",
                    "Vision provider",
                    _teammate_model_label(vision),
                    options,
                ),
                {
                    "key": "vision_model",
                    "label": "Vision model ID",
                    "value": str((vision or {}).get("model") or "")
                    if isinstance(vision, dict)
                    else "",
                },
            ]
        },
        "Teammate settings",
    )


def teammate_assign_spec(name: str) -> ScreenSpec:
    """Build the task-assignment form."""
    return ScreenSpec(
        "form",
        {"fields": [{"key": "text", "label": "Task", "value": ""}]},
        f"Assign to {name}",
    )


def teammate_auto_spec(state: dict[str, object]) -> ScreenSpec:
    """Build the auto-approve toggle form."""
    return ScreenSpec(
        "form",
        {
            "fields": [
                _toggle(
                    "auto_approve",
                    "Auto-approve desktop input",
                    bool(state.get("auto_approve")),
                )
            ]
        },
        "Desktop input approvals",
    )


def teammate_delete_spec(name: str) -> ScreenSpec:
    """Build the exact-name delete confirmation."""
    return ScreenSpec(
        "form",
        {"fields": [{"key": "confirm", "label": f"Type {name} to delete", "value": ""}]},
        "Delete teammate",
    )


def teammate_preview_spec(name: str, url: str) -> ScreenSpec:
    """Render the computer preview URL as a copyable detail screen."""
    return ScreenSpec(
        "detail",
        {
            "markdown": f"# {name} · computer preview\n\n{url}\n\n"
            "Open this URL in a browser to watch or take control.",
            "actions": [],
        },
        "Computer",
    )


def room_actions_spec(room: str) -> ScreenSpec:
    """Build the action menu for one room."""
    return ScreenSpec(
        "list",
        {
            "items": [
                {"id": "read", "label": "Read transcript", "detail": ""},
                {"id": "messages", "label": "Message actions", "detail": ""},
                {"id": "send", "label": "Send a message", "detail": ""},
                {"id": "board", "label": "Task board", "detail": ""},
                {"id": "settings", "label": "Channel settings", "detail": ""},
                {"id": "back", "label": "Back", "detail": ""},
            ]
        },
        room,
    )


def room_send_spec(room: str) -> ScreenSpec:
    """Build the owner-message form."""
    return ScreenSpec(
        "form",
        {"fields": [{"key": "text", "label": "Message", "value": ""}]},
        f"Send to {room}",
    )


def room_create_spec() -> ScreenSpec:
    """Build the new-channel name form."""
    return ScreenSpec(
        "form",
        {"fields": [{"key": "name", "label": "Channel name", "value": ""}]},
        "New channel",
    )


def room_settings_spec(room: dict[str, object]) -> ScreenSpec:
    """Build the channel rename/topic form."""
    return ScreenSpec(
        "form",
        {
            "fields": [
                {
                    "key": "name",
                    "label": "Channel name",
                    "value": str(room.get("name") or "").lstrip("#"),
                },
                {"key": "topic", "label": "Topic", "value": str(room.get("topic") or "")},
            ]
        },
        "Channel settings",
    )


def room_board_spec(room: str, board: dict[str, object]) -> ScreenSpec:
    """Render the shared task board and notes for one room."""
    lines = [f"# {room} · task board", ""]
    tasks = list(board.get("tasks") or [])
    lines.extend(["## Tasks", ""])
    if not tasks:
        lines.append("No tasks yet.")
    for task in tasks:
        lines.extend(
            [
                f"### {str(task.get('status') or 'unknown').capitalize()} · {task.get('id', '')}",
                "",
                str(task.get("goal") or "").strip() or "_No goal recorded._",
                "",
            ]
        )
    notes = list(board.get("notes") or [])
    lines.extend(["", "## Notes", ""])
    if not notes:
        lines.append("No shared notes yet.")
    for note in notes:
        lines.extend(
            [
                f"**{note.get('author', 'unknown')}** · {note.get('kind', '')}",
                "",
                str(note.get("body") or "").strip()[:2000],
                "",
            ]
        )
    return ScreenSpec("detail", {"markdown": "\n".join(lines), "actions": []}, room)


REACTIONS: tuple[str, ...] = ("👍", "❤️", "🎉", "👀")


def room_messages_spec(room: str, messages: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build a selectable message table for one room."""
    rows = []
    for message in messages:
        key = str(message.get("id") or "")
        if not key:
            continue
        text = " ".join(str(message.get("text") or "").split())[:80]
        reactions = list(message.get("reactions") or [])
        detail = " ".join(str(reaction.get("emoji") or "") for reaction in reactions)
        rows.append(
            {
                "id": key,
                "message": key,
                "author": str(message.get("author") or ""),
                "text": text,
                "detail": detail,
            }
        )
    return ScreenSpec(
        "table",
        {
            "columns": [
                {"key": "message", "title": "#", "width": 6},
                {"key": "author", "title": "Author", "width": 10},
                {"key": "text", "title": "Message", "width": 60},
            ],
            "rows": rows,
        },
        room,
    )


def room_message_actions_spec(message: dict[str, object]) -> ScreenSpec:
    """Build the action menu for one message."""
    return ScreenSpec(
        "list",
        {
            "items": [
                {"id": "view", "label": "View", "detail": ""},
                {"id": "edit", "label": "Edit text", "detail": "Only your own messages"},
                {"id": "delete", "label": "Delete", "detail": ""},
                {"id": "react", "label": "React", "detail": ""},
                {"id": "pin", "label": "Pin / unpin", "detail": ""},
                {"id": "back", "label": "Back", "detail": ""},
            ]
        },
        f"Message {message.get('id', '')}",
    )


def room_message_spec(room: str, message: dict[str, object]) -> ScreenSpec:
    """Render one message with its reactions and edit marker."""
    key = str(message.get("id") or "")
    lines = [
        f"# {room} · #{key}",
        "",
        f"**{message.get('author', 'unknown')}** · {message.get('created_at', '')}",
        "",
        str(message.get("text") or "").strip() or "_empty message_",
    ]
    if message.get("edited_at"):
        lines.extend(["", f"_edited {message['edited_at']}_"])
    reactions = list(message.get("reactions") or [])
    if reactions:
        lines.extend(["", " ".join(str(reaction.get("emoji") or "") for reaction in reactions)])
    return ScreenSpec("detail", {"markdown": "\n".join(lines), "actions": []}, room)


def room_edit_spec(message: dict[str, object]) -> ScreenSpec:
    """Build the edit form for one owner message."""
    return ScreenSpec(
        "form",
        {"fields": [{"key": "text", "label": "Message", "value": str(message.get("text") or "")}]},
        f"Edit #{message.get('id', '')}",
    )


def room_react_spec() -> ScreenSpec:
    """Build the supported-reaction picker."""
    return ScreenSpec(
        "form",
        {"fields": [_choice("emoji", "Reaction", REACTIONS[0], list(REACTIONS))]},
        "React",
    )


# ── Prime / Deep Work ────────────────────────────────────────────────────
def prime_saved_spec(session: object, identity: str = "") -> ScreenSpec:
    """Build the saved-work browser over `PrimeMemory` and factory records.

    Args:
        session: The session proxy; only `session_directory` is read.
        identity: When set, show one check's evidence instead of the lists.

    Returns:
        A `list` spec of saved tasks and checks, or a `detail` spec of evidence.
    """
    import json

    from novacode_cli.commands.prime_handler import factory_records
    from novacode_cli.prime_memory import PrimeMemory, project_context, session_directory

    memory = PrimeMemory(project_context(session_directory(session)))
    if identity:
        check = memory.get("checks", identity)
        if check is None:
            return ScreenSpec(
                "detail",
                {"markdown": "Prime check not found in this project.", "actions": []},
                "Prime",
            )
        evidence = [memory.get("evidence", key) for key in check.get("evidence_ids", [])]
        return ScreenSpec(
            "detail",
            {
                "markdown": json.dumps(
                    {"check": check, "evidence": evidence}, ensure_ascii=False, indent=2
                ),
                "actions": [],
            },
            f"Check {identity}",
        )
    items: list[dict[str, object]] = [
        {
            "id": str(record.get("id") or ""),
            "label": f"{record.get('id', '')} · factory · {record.get('status', '')}",
            "detail": str(record.get("goal") or "")[:120],
        }
        for record in factory_records(session)
    ]
    for kind, label in (("tasks", "task"), ("checks", "check")):
        for record in memory.records(kind):
            goal = str(record.get("goal") or record.get("purpose") or "")[:120]
            items.append(
                {
                    "id": f"{kind}:{record.get('id', '')}",
                    "label": f"{record.get('id', '')} · {label} · {record.get('status', 'draft')}",
                    "detail": goal,
                }
            )
    if not items:
        items = [{"id": "__none__", "label": "No saved Prime work in this project.", "detail": ""}]
    items.append({"id": "__close__", "label": "Close", "detail": ""})
    return ScreenSpec("list", {"items": items}, "Prime saved work")


def prime_report_spec(report: object) -> ScreenSpec:
    """Render a factory receipt as a detail screen."""
    from novacode_cli.commands.prime_handler import factory_receipt

    if not report:
        return ScreenSpec(
            "detail",
            {"markdown": "No Prime delivery record yet.", "actions": []},
            "Prime report",
        )
    return ScreenSpec(
        "detail",
        {"markdown": f"```\n{factory_receipt(report)}\n```", "actions": []},
        "Prime report",
    )


def token_view_spec(tracker: object) -> ScreenSpec:
    """Build the `/tokens` report: session totals and context share.

    Args:
        tracker: A `TokenTracker`, or `None` when the session has none.

    Returns:
        A `detail` spec. Zero when the tracker has no data yet.
    """
    if tracker is None:
        return ScreenSpec(
            "detail", {"markdown": "No token usage recorded yet.", "actions": []}, "Tokens"
        )

    def count(attribute: str) -> int:
        value = getattr(tracker, attribute, 0)
        return value if isinstance(value, (int, float)) else 0

    window = int(count("context_window_size"))
    current = int(count("current_context"))
    session_total = int(count("session_total_tokens"))
    share = f"{current / window * 100:.1f}%" if window else "unknown"
    lines = [
        "# Token usage",
        "",
        f"- Context now: {current:,} of {window:,} ({share})",
        f"- Session total: {session_total:,} tokens (input + output, monotonic)",
        f"- Last output: {int(count('last_output')):,} tokens",
        f"- Cache read: {int(count('last_cache_read')):,} · cache write: "
        f"{int(count('last_cache_creation')):,}",
    ]
    if not getattr(tracker, "has_api_data", False):
        lines.extend(["", "_Estimated; no provider usage received yet._"])
    return ScreenSpec("detail", {"markdown": "\n".join(lines), "actions": []}, "Tokens")


def worktrees_spec(found: Sequence[tuple[object, str]]) -> ScreenSpec:
    """List the repository's Nova worktrees."""
    items = [{"id": str(path), "label": branch, "detail": str(path)} for path, branch in found]
    if not items:
        items = [
            {"id": "__none__", "label": "No Nova worktrees for this repository.", "detail": ""}
        ]
    return ScreenSpec("list", {"items": items}, "Worktrees")


def worktree_name_spec() -> ScreenSpec:
    """Build the create form: name, optional task."""
    return ScreenSpec(
        "form",
        {
            "fields": [
                {"key": "name", "label": "Worktree name", "value": ""},
                {"key": "task", "label": "Task (optional)", "value": ""},
            ]
        },
        "New worktree",
    )


def tasks_spec(tasks: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build the background-tasks table.

    Args:
        tasks: Rows with `task_id`, `command`/`prompt`, `status`.

    Returns:
        A `table` spec. One row per task; no actions yet.
    """
    columns = [
        {"key": "task", "title": "Task", "width": 10},
        {"key": "status", "title": "Status", "width": 10},
        {"key": "command", "title": "Command", "width": 50},
    ]
    rows = [
        {
            "id": str(task.get("task_id") or ""),
            "task": str(task.get("task_id") or ""),
            "status": str(task.get("status") or ""),
            "command": " ".join(str(task.get("command") or "").split())[:60],
        }
        for task in tasks
        if task.get("task_id")
    ]
    if not rows:
        rows = [{"id": "__none__", "task": "—", "status": "—", "command": "No background tasks."}]
    return ScreenSpec("table", {"columns": columns, "rows": rows}, "Background tasks")


def restore_spec(snapshots: Sequence[tuple[str, object]]) -> ScreenSpec:
    """Build the snapshot picker for `/restore`.

    Args:
        snapshots: `(session_id, SnapshotEntry)` pairs, newest first.

    Returns:
        A `list` spec whose id is the index, matching the Textual picker.
    """
    from datetime import UTC, datetime

    minute, hour, day = 60, 3600, 86400
    items = []
    for index, (_session_id, entry) in enumerate(snapshots):
        reason = str(getattr(entry, "reason", ""))
        timestamp = str(getattr(entry, "timestamp", ""))
        try:
            age = datetime.now(tz=UTC) - datetime.fromisoformat(timestamp)
            seconds = int(age.total_seconds())
            label_age = (
                f"{seconds}s ago"
                if seconds < minute
                else (
                    f"{seconds // minute}m ago"
                    if seconds < hour
                    else (f"{seconds // hour}h ago" if seconds < day else f"{seconds // day}d ago")
                )
            )
        except (TypeError, ValueError):
            label_age = timestamp
        items.append(
            {
                "id": str(index),
                "label": str(getattr(entry, "original_path", "")),
                "detail": f"{reason} ({label_age})",
            }
        )
    return ScreenSpec("list", {"items": items}, "Restore which snapshot?")


# ── artifacts ────────────────────────────────────────────────────────────
def artifacts_spec(artifacts: Sequence[object]) -> ScreenSpec:
    """Build the artifact browser that `/artifacts` opens.

    Args:
        artifacts: Registry entries with `id`, `title`, `type`, `version` and
            `status`; the same list the Textual picker labels.

    Returns:
        A `list` spec whose item id is the artifact id, so the answer names the
        artifact rather than its position.
    """
    items = []
    for artifact in artifacts:
        artifact_id = str(getattr(artifact, "id", "") or "")
        if not artifact_id:
            continue
        label = f"◈ {getattr(artifact, 'title', artifact_id)}"
        detail = (
            f"[{getattr(artifact, 'type', '')}] "
            f"v{getattr(artifact, 'version', '')} · {getattr(artifact, 'status', '')}"
        )
        items.append({"id": artifact_id, "label": label, "detail": detail})
    return ScreenSpec("list", {"items": items}, "Artifacts — open in browser")


# ── usage ────────────────────────────────────────────────────────────────
#: Shown when no provider has a usage endpoint to read.
NO_USAGE = "Nothing to show: no Claude Code, Codex or keyed provider has usage."


def usage_spec(quotas: Sequence[str], report: str, note: str) -> ScreenSpec:
    """Build the `@@/usage@@` report out of the Textual screen's three parts.

    Args:
        quotas: One rendered bar block per provider, already flattened to text
            by `novacode_cli.commands.planner_usage.usage_text`.
        report: This session's observed usage, from `format_usage_report`.
        note: The caption under the bars (`USAGE_NOTE`).

    Returns:
        A `detail` spec: quotas, then the session report, then the note.
    """
    body = "\n\n".join(quotas) if quotas else NO_USAGE
    return ScreenSpec(
        "detail",
        {"markdown": f"{body}\n\n{report}\n\n*{note}*", "actions": []},
        "Usage",
    )


# ── updates ──────────────────────────────────────────────────────────────
def update_spec(status: object, *, install_hint: str) -> ScreenSpec:
    """Build the `@@/update@@` report from one `check_for_update` result.

    Args:
        status: The update status: `current`, `latest`, `available`, `error`
            and the optional `release_title` / `release_version`.
        install_hint: What to do to install, which differs by frontend.

    Returns:
        A `detail` spec whose first line is the verdict.
    """
    error = str(getattr(status, "error", "") or "")
    current = str(getattr(status, "current", "") or "")
    latest = str(getattr(status, "latest", "") or "")
    available = bool(getattr(status, "available", False))
    if error:
        headline = "Could not check for updates."
        detail = f"{error}\n\nCheck your connection, then run /update again."
    elif available:
        title = str(getattr(status, "release_title", "") or latest[:12])
        headline = f"Update available · {title}."
        detail = f"Installed {current[:12] or 'unknown'} · latest {latest[:12]}."
        if str(getattr(status, "release_version", "") or ""):
            detail += f" · release {status.release_version}"
        detail += f"\n\n{install_hint}"
    else:
        headline = "NovaCode is up to date."
        detail = f"Installed {current[:12] or 'current release'}."
    markdown = f"**{headline}**\n\n{detail}"
    return ScreenSpec("detail", {"markdown": markdown, "actions": []}, "Updates")


# ── subagents ────────────────────────────────────────────────────────────
#: Colours the Textual create form offers, in its order.
AGENT_COLORS: tuple[tuple[str, str], ...] = (
    ("Sky Blue", "#0ea5e9"),
    ("Teal", "#14b8a6"),
    ("Violet", "#8b5cf6"),
    ("Amber", "#f59e0b"),
    ("Rose", "#f43f5e"),
    ("Lime", "#84cc16"),
)


def agents_spec(rows: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build the `@@/subagents@@` list from one row per configured agent.

    Args:
        rows: One dict per agent, carrying `name`, `scope`, `description`,
            `runs_async` and `path`; the port reads them off the event loop.

    Returns:
        A `list` spec whose item id is the agent name.
    """
    items = [
        {
            "id": str(row.get("name", "")),
            "label": f"@{row.get('name', '')} "
            + ("async " if row.get("runs_async") else "sync ")
            + f"· {row.get('scope', '')}",
            "detail": str(row.get("description", "") or "(no description)"),
        }
        for row in rows
        if row.get("name")
    ]
    return ScreenSpec(
        "list",
        {
            "items": items,
            "hint": "Select an agent · /subagents create · /subagents delete <name>",
        },
        "Subagents",
    )


def agent_detail_spec(
    name: str,
    scope: str,
    path: str,
    description: str,
    prompt: str,
    *,
    runs_async: bool,
) -> ScreenSpec:
    """Build one agent's detail view.

    Args:
        name: Agent name, without its `@`.
        scope: `global` or `project`.
        path: The agent file the port read.
        description: The frontmatter description.
        runs_async: Whether the agent also runs on the agent server.
        prompt: The agent's system prompt.

    Returns:
        A `detail` spec with the metadata in the first lines.
    """
    runs = "async (background agent server)" if runs_async else "in-process (same turn)"
    head = f"**@{name}** · {scope} · {runs}\n\n`{path}`\n\n{description or '(no description)'}"
    body = f"{head}\n\n---\n\n{prompt or '(empty system prompt)'}"
    return ScreenSpec("detail", {"markdown": body, "actions": []}, f"@{name}")


def agent_create_spec(*, project_available: bool) -> ScreenSpec:
    """Build the form that creates one custom subagent.

    Args:
        project_available: Whether the workspace has a project root, which is
            what makes the project scope offerable.

    Returns:
        A `form` spec whose keys match the answer the port reads back.
    """
    colors = [label for label, _ in AGENT_COLORS]
    fields: list[dict[str, object]] = [
        {
            "key": "name",
            "label": "Name (letters, numbers, hyphens, underscores)",
            "value": "",
        },
        {
            "key": "description",
            "label": "Specialization — e.g. reviews Python for security bugs",
            "value": "",
        },
        {
            "key": "kind",
            "label": "Runs",
            "value": "sync",
            "options": ["sync", "async"],
        },
        {
            "key": "scope",
            "label": "Storage scope",
            "value": "global",
            "options": ["global", "project"] if project_available else ["global"],
        },
        {"key": "color", "label": "Colour", "value": colors[0], "options": colors},
        {
            "key": "system_prompt",
            "label": "System prompt (blank = generate one with the model)",
            "value": "",
        },
    ]
    return ScreenSpec("form", {"fields": fields}, "Create custom subagent")


# ── rewind ───────────────────────────────────────────────────────────────
def rewind_spec(rows: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build the checkpoint list `@@/rewind@@` picks from.

    Args:
        rows: `{"id", "label", "detail"}` dicts, newest first; the id is the
            message index the conversation is rewound to.

    Returns:
        A single-select `list` spec whose chosen id is the cut point.
    """
    items = [
        {
            "id": str(row.get("id") or ""),
            "label": str(row.get("label") or ""),
            "detail": str(row.get("detail") or ""),
        }
        for row in rows
        if str(row.get("id") or "")
    ]
    return ScreenSpec(
        "list",
        {"items": items, "hint": "Rewind to before the message you pick"},
        "Rewind",
    )


def rewind_scope_spec(count: int) -> ScreenSpec:
    """Build the second `@@/rewind@@` question: what to put back.

    Args:
        count: How many files changed after the chosen message.

    Returns:
        A `list` spec answered `both`, `conversation` or `code`.
    """
    items = [
        {"id": "both", "label": "Conversation and code"},
        {"id": "conversation", "label": "Conversation only  (files stay as they are)"},
        {"id": "code", "label": f"Code only  ({count} file(s); the conversation stays)"},
    ]
    return ScreenSpec("list", {"items": items}, "Restore what?")


def tree_spec(rows: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build the session-history list `@@/tree@@` picks from.

    Args:
        rows: `{"id", "label", "detail"}` dicts in conversation order; the id
            is a `tree.jsonl` node id.

    Returns:
        A single-select `list` spec whose chosen id is the continue point.
    """
    items = [
        {
            "id": str(row.get("id") or ""),
            "label": str(row.get("label") or ""),
            "detail": str(row.get("detail") or ""),
        }
        for row in rows
        if str(row.get("id") or "")
    ]
    return ScreenSpec(
        "list",
        {
            "items": items,
            "hint": "Continue from the point you pick; the old branch stays in the tree",
        },
        "Session tree",
    )


def resume_spec(rows: Sequence[dict[str, object]]) -> ScreenSpec:
    """Build the saved-session list `@@/resume@@` picks from.

    Args:
        rows: `{"id", "label", "detail"}` dicts, most recently active first; the
            id is the saved session id the child will continue.

    Returns:
        A single-select `list` spec whose chosen id is the session to resume.
    """
    items = [
        {
            "id": str(row.get("id") or ""),
            "label": str(row.get("label") or ""),
            "detail": str(row.get("detail") or ""),
        }
        for row in rows
        if str(row.get("id") or "")
    ]
    return ScreenSpec(
        "list",
        {"items": items, "hint": "Resume the session you pick in a new tab"},
        "Resume a session",
    )


def command_center_spec(
    rows: Sequence[CommandSession], *, status: str = "All", note: str = ""
) -> ScreenSpec:
    """Build the `@@/command-center@@` list: saved sessions overlaid with live tabs.

    The Textual modal keeps a details pane beside the list; a socket client shows
    one screen at a time, so every fact that pane carried rides on the row itself.

    Args:
        rows: Rows from `novacode_cli.ui.command_center.merge_sessions`.
        status: The filter the list was built with, shown beside the title.
        note: A warning to show above the list, such as unreadable saved metadata.

    Returns:
        A single-select `list` spec whose chosen id is the row key.
    """
    items = [
        {
            "id": row.key,
            "label": f"{'→' if row.current else ' '} {'●' if row.pane_id else '○'} {row.title}",
            "detail": describe(row),
        }
        for row in rows
        if row.key
    ]
    payload: dict[str, object] = {
        "items": items,
        "hint": "Enter opens the tab, or resumes the session in a new tab",
        "counts": [f"{label} {count}" for label, count in status_counts(rows)],
    }
    if note:
        payload["note"] = note
    return ScreenSpec("list", payload, f"Agent command center · {status}")


def share_gist_spec() -> ScreenSpec:
    """Build the `@@/share@@` question before a session is uploaded.

    Returns:
        A two-item `list` spec answered `upload` or anything else to cancel.
    """
    return ScreenSpec(
        "list",
        {
            "items": [
                {"id": "upload", "label": "Upload to a secret GitHub gist"},
                {"id": "cancel", "label": "Cancel"},
            ],
            "hint": (
                "Anyone with the link can read it. Common token formats are masked, "
                "but check for other private data first."
            ),
        },
        "Share this session?",
    )
