"""Which model each agent role runs on.

One question, one module: given the saved per-role models, what do the main agent,
the in-process subagents, the remote graphs and the discovered agents each run on?
Nothing here builds a model or touches the TUI, so all of it is directly testable.

Inheritance is the design. A role with nothing saved resolves to ``None``, and every
caller then takes exactly the path it took before per-role models existed.
"""

from __future__ import annotations

from novacode_cli.config.nova_config import ROLE_NAMES, NovaConfig

#: Human wording for the role rows, matching how the roles are described to users.
ROLE_LABELS: dict[str, str] = {
    "main": "Main agent",
    "subagent": "Subagents",
    "async": "Async agents",
    "dynamic": "Dynamic agents",
}

#: One-line explanation per role, shown beside the row in ``/model``.
ROLE_NOTES: dict[str, str] = {
    "main": "the agent you talk to",
    "subagent": "in-process delegation via the task tool",
    "async": "the remote graphs on the LangGraph server",
    "dynamic": "agents discovered in the agent directories",
}


def spec_of(entry: dict[str, str] | None) -> str | None:
    """``provider:model`` for a saved entry, or None when the role inherits.

    That is the form deepagents documents for a subagent's ``model`` field, so the
    rest of Nova only ever has to pass this string through.
    """
    if not entry:
        return None
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    if not provider or not model:
        return None
    return f"{provider}:{model}"


def base_url_of(entry: dict[str, str] | None) -> str | None:
    """The saved endpoint override for a role, if any."""
    if not entry:
        return None
    url = str(entry.get("base_url") or "").strip()
    return url or None


def role_specs(config: NovaConfig | None = None) -> dict[str, str | None]:
    """``provider:model`` per role, None where the role inherits the main agent."""
    cfg = config or NovaConfig()
    return {role: spec_of(cfg.get_role_model(role)) for role in ROLE_NAMES}


def subagent_spec(config: NovaConfig | None = None) -> str | None:
    """The model for in-process subagents, or None to let them inherit."""
    return spec_of((config or NovaConfig()).get_role_model("subagent"))


def dynamic_spec(config: NovaConfig | None = None) -> str | None:
    """The model for discovered agents that do not name one in their own frontmatter.

    Falls back to the ``subagent`` role rather than to None: a discovered agent is a
    subagent, so an unset ``dynamic`` role should follow whatever subagents do.
    """
    cfg = config or NovaConfig()
    return spec_of(cfg.get_role_model("dynamic")) or subagent_spec(cfg)


def async_server_env(config: NovaConfig | None = None) -> dict[str, str]:
    """Environment for a LangGraph server Nova launches itself.

    The graphs read ``ASYNC_AGENT_PROVIDER`` and ``ASYNC_AGENT_MODEL``, so the stored
    ``provider:model`` is split here. An unset async role returns nothing at all,
    which leaves the graphs on their own default (Ollama) exactly as before.
    """
    cfg = config or NovaConfig()
    entry = cfg.get_role_model("async")
    spec = spec_of(entry)
    if not spec:
        return {}
    provider, _, model = spec.partition(":")
    env = {"ASYNC_AGENT_PROVIDER": provider, "ASYNC_AGENT_MODEL": model}
    base_url = base_url_of(entry)
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
    return env


def describe_roles(
    main_provider: str,
    main_model: str,
    config: NovaConfig | None = None,
) -> list[tuple[str, str, str]]:
    """Rows for the picker header: ``(role, label, what it will actually run)``.

    A role that inherits resolves to the model it inherits *from* rather than
    printing a bare "inherit", so the header never leaves the reader guessing which
    model a delegation will use.
    """
    cfg = config or NovaConfig()
    specs = role_specs(cfg)
    main_effective = specs["main"] or f"{main_provider}:{main_model}"
    rows: list[tuple[str, str, str]] = []
    for role in ROLE_NAMES:
        spec = specs[role]
        if role == "main":
            effective = main_effective
        elif role == "async":
            # An unset async role follows the main agent's model, sent with each
            # dispatch (agents/async_context.current_async_context).
            effective = spec or main_effective
        elif role == "dynamic":
            # A discovered agent follows the subagent role when it names no model of
            # its own, so this must resolve through subagents, not straight to main.
            effective = spec or specs["subagent"] or main_effective
        else:
            # The resolved model itself, not a wrapper around it. This field answers
            # "what will it run"; `role_provenance` says where that came from, so
            # "inherit (model)" would state the same fact twice and read long.
            effective = spec or main_effective
        rows.append((role, ROLE_LABELS[role], effective))
    return rows


def role_provenance(role: str, config: NovaConfig | None = None) -> str:
    """Where a role's model comes from, in as few words as fit a list row.

    Empty when the role has a model of its own. Otherwise it names what it is
    following, which is the part a reader cannot guess: a discovered agent follows
    the subagents, and an async role is set for the server rather than inherited.
    """
    cfg = config or NovaConfig()
    if role == "main":
        return ""
    if role == "async":
        return "via config" if spec_of(cfg.get_role_model("async")) else ""
    if cfg.get_role_model(role):
        return ""
    if role == "subagent":
        return "via main"
    return "via subagents" if cfg.get_role_model("subagent") else "via main"


def panel_row_model(
    phase_kind: str,
    session_model: str,
    config: NovaConfig | None = None,
) -> str:
    """What the subagents panel should print in MODEL for one row.

    ``phase_kind`` is the row's provenance: ``direct`` is a blocking ``task`` call
    and ``eval`` a JS fan-out, which dispatches the same configured agents, so both
    resolve to the subagent role. ``async`` rows run on the server, which decides
    their model from its own environment. Anything else keeps the session model,
    which is what every row showed before per-role models existed.

    One imprecision, accepted deliberately: an individual discovered agent carrying
    its own ``model:`` in frontmatter is shown as its role's model, because a row
    knows the agent's name but not its resolved spec, and reading every agent's
    frontmatter per row would be file I/O in the render path.
    """
    cfg = config or NovaConfig()
    if phase_kind == "async":
        # The async role, or the session's model when that role is unset.
        return spec_of(cfg.get_role_model("async")) or session_model
    if phase_kind in ("direct", "eval"):
        return subagent_spec(cfg) or dynamic_spec(cfg) or session_model
    return session_model
