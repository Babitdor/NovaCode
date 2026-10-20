"""Lazily attach opt-in teams to existing graph construction."""

from __future__ import annotations

import contextlib
import hashlib
import threading
import uuid
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

from novacode_cli.teams.coordinator import TeamCoordinator
from novacode_cli.teams.gateway import TEAM_TOOLS, Binding
from novacode_cli.teams.host import TeamHost
from novacode_cli.teams.middleware import TeamMiddleware
from novacode_cli.teams.models import Adapter, Identity, SpawnRequest, TeamError

if TYPE_CHECKING:
    from pathlib import Path

_runtimes: WeakValueDictionary[tuple[str, str], TeamHost] = WeakValueDictionary()
_lock = threading.Lock()


def enabled() -> bool:
    """Read the existing profile boolean without changing personal configuration."""
    from novacode_cli.config.nova_config import NovaConfig

    return NovaConfig().get_agent_team_settings()["enabled"]


def attach(  # noqa: PLR0915 - Ordered runtime assembly.
    model: Any,  # noqa: ANN401 - Dynamic framework boundary.
    assistant_id: str,
    tools: list,
    subagents: list,
    workspace: Path,
    session_id: str | None,
    binding: Binding | None = None,
    *,
    restricted: bool = False,
) -> tuple[list, TeamMiddleware | None, TeamHost | None]:
    """Return unchanged tools while disabled, or a trusted root/child binding."""
    from novacode_cli.config.nova_config import NovaConfig

    if binding is not None:
        names = {getattr(tool, "name", None) for tool in tools}
        return (
            [*tools, *(tool for tool in TEAM_TOOLS if tool.name not in names)],
            TeamMiddleware(binding),
            None,
        )
    if restricted:
        return tools, None, None
    root = session_id or uuid.uuid4().hex
    project = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    if not enabled():
        # Keep ownership/lifecycle references when a live root disables teams.
        # Existing work may finish, but no further swarm tasks are assigned.
        with _lock:
            host = _runtimes.get((project, root))
            if host is not None:
                host.roots[root].swarm_enabled = False
        return tools, None, host
    from novacode_cli.agents.agent_file import ALWAYS_INCLUDED, FilesystemToolFilter
    from novacode_cli.teams.legacy import LegacyMailboxMiddleware

    for spec in subagents:
        if isinstance(spec, dict) and not any(
            key in spec for key in ("runnable", "url", "graph_id")
        ):
            spec["middleware"] = [
                *(spec.get("middleware") or []),
                LegacyMailboxMiddleware(spec["name"]),
            ]
    from novacode_cli.config.config import settings

    profile = NovaConfig()
    base_tools = tuple(tools)
    specs = {
        spec["name"]: spec
        for spec in subagents
        if isinstance(spec, dict)
        and not any(key in spec for key in ("runnable", "url", "graph_id"))
    }

    def blocked_filesystem(spec: dict) -> set[str]:
        blocked = set()
        for middleware in spec.get("middleware") or []:
            if isinstance(middleware, FilesystemToolFilter):
                blocked.update(middleware._blocked)
        if "execute" in blocked:
            blocked.add("shell")
        return blocked

    def role_tools(spec: dict) -> list:
        return list(base_tools if spec.get("tools") is None else spec["tools"])

    def builder(request: SpawnRequest, child_binding: Binding) -> tuple[Any, Any, Any]:
        from novacode_cli.agents.core_agent import create_agent_with_config
        from novacode_cli.states.Session import SessionState

        spec = specs.get(request.agent_type)
        if spec is None:
            msg = f"Unknown local agent type: {request.agent_type}"
            raise TeamError(msg)
        state = SessionState(auto_approve=request.auto_approve)
        state.session_id = state.thread_id = request.identity.agent
        state.workspace_root = str(request.cwd)
        selected_model = spec.get("model") or model
        from novacode_cli.tracking.usage_tree import attach_usage_callback

        attach_usage_callback(selected_model)
        graph, backend = create_agent_with_config(
            selected_model,
            assistant_id,
            role_tools(spec),
            system_prompt=spec.get("system_prompt"),
            workspace_root=str(request.cwd),
            session_id=request.identity.agent,
            auto_approve=True,
            steering_instructions=state.steering_instructions,
            team_binding=child_binding,
            extra_middleware=[
                FilesystemToolFilter(set(ALWAYS_INCLUDED) - blocked_filesystem(spec))
            ],
        )
        # auto_approve=True only suppresses nested graph HITL. TeamMiddleware
        # remains the mandatory per-call parent policy/approval broker.
        return graph, backend, state

    from novacode_cli.compaction import _window_tokens
    from novacode_cli.teams.adapters.inprocess import InProcessAdapter
    from novacode_cli.teams.adapters.process import ProcessAdapter

    common = set(ALWAYS_INCLUDED) | {"shell", "write_todos"}
    capabilities = {
        name: (common | {getattr(tool, "name", "") for tool in role_tools(spec)})
        - blocked_filesystem(spec)
        for name, spec in specs.items()
    }

    def descriptor(selected: Any, spec: dict) -> dict[str, Any]:  # noqa: ANN401 - Dynamic framework boundary.
        metadata = getattr(selected, "metadata", None) or {}
        provider, model_name = metadata.get("nova_provider"), metadata.get("nova_model")
        if not provider:
            # A legacy model object may predate identity metadata. Restore only
            # an exact configured match; never infer a gateway from its class.
            configured = profile.get_role_model("subagent") or profile.get_model_config() or {}
            actual = (
                getattr(selected, "model_name", None)
                or getattr(selected, "model", None)
                or selected
            )
            if configured.get("model") == actual:
                provider, model_name = configured["provider"], configured["model"]
        return {
            "provider": provider,
            "model": model_name,
            "reasoning_effort": profile.get("reasoning_effort"),
            "policy_root": str(workspace),
            "system_prompt": spec.get("system_prompt"),
            "allowed_tools": sorted(capabilities.get(spec.get("name", ""), common)),
            "required_tools": sorted(
                {getattr(tool, "name", "") for tool in role_tools(spec)} - blocked_filesystem(spec)
            ),
            "model_options": {
                key: profile.get(key)
                for key in (
                    "reasoning_effort",
                    "thinking_budget",
                    "max_tokens",
                    "temperature",
                    "top_p",
                    "commandcode_zdr",
                )
                if profile.get(key) is not None
            },
            "base_url": profile.get_model_base_url(),
            "auth_mode": profile.get_openai_auth_mode() if provider == "openai" else None,
        }

    by_agent = {name: descriptor(spec.get("model") or model, spec) for name, spec in specs.items()}
    process_model = {**descriptor(model, specs.get("general-purpose", {})), "by_agent": by_agent}

    adapters: dict[str, Adapter] = {
        "default": InProcessAdapter(builder),
        "fork": ProcessAdapter(process_model),
        "worktree": ProcessAdapter(process_model, mode="worktree"),
    }
    container_profile = profile.get("team_container")
    if isinstance(container_profile, dict):
        from novacode_cli.teams.adapters.container import ContainerAdapter

        with contextlib.suppress(TeamError, ValueError, TypeError):
            adapters["remote"] = ContainerAdapter(process_model, container_profile)

    def factory(root_id: str) -> TeamCoordinator:
        coordinator = TeamCoordinator(
            settings.user_deepagents_dir / "sessions",
            Identity(project, root_id),
            dict(adapters),
            workspace=workspace,
            swarm_enabled=profile.get_agent_team_settings()["swarm"],
            context_budget=max(1, int(_window_tokens(model) * 0.75)),
        )
        coordinator.capabilities = capabilities
        return coordinator

    def register(root_id: str, host: TeamHost | None) -> None:
        with _lock:
            if host is None:
                _runtimes.pop((project, root_id), None)
            else:
                _runtimes[project, root_id] = host

    with _lock:
        host = _runtimes.get((project, root))
        if host is None:
            host = TeamHost(factory(root), factory, register)
            _runtimes[project, root] = host
        else:
            host.factory = factory
            coordinator = host.roots[root]
            coordinator.adapters = dict(adapters)
            coordinator.capabilities = capabilities
            coordinator.swarm_enabled = profile.get_agent_team_settings()["swarm"]

    names = {getattr(tool, "name", None) for tool in tools}
    tools = [*tools, *(tool for tool in TEAM_TOOLS if tool.name not in names)]
    return tools, TeamMiddleware(host.binding(None), host=host), host
