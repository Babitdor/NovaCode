"""Nova team worker entry point: reuse SessionWorker, not a second agent loop."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from novacode_cli.teams.models import Identity, TeamError
from novacode_cli.teams.protocol import MAX_CONTROL_BYTES, VERSION, encode_control


async def serve(bootstrap: dict[str, Any], output_fd: int) -> int:  # noqa: PLR0915 - Ordered runtime assembly.
    """Build exactly the requested configured model and bind tools to the pipe."""
    from novacode_cli._lazy_heavy import install

    install()
    from novacode_cli.agents.core_agent import create_agent_with_config, get_shared_store
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.main import _session_tools
    from novacode_cli.sessions.worker import SessionWorker
    from novacode_cli.states.Session import SessionState
    from novacode_cli.teams.gateway import Binding

    if bootstrap.get("version") != VERSION or await asyncio.to_thread(
        lambda: Path(bootstrap["cwd"]).resolve() != Path.cwd().resolve()
    ):
        msg = "Worker protocol or workspace does not match the host"
        raise TeamError(msg)
    identity = Identity(
        bootstrap["project"], bootstrap["root"], bootstrap["agent"], bootstrap["team"]
    )
    state = SessionState(auto_approve=bootstrap.get("auto_approve") is True)
    state.thread_id = state.session_id = identity.agent
    state.workspace_root = str(Path.cwd())
    state.session_model_provider = bootstrap["provider"]
    state.headless_out_fd = output_fd
    # Read-only, process-local configuration overlay: no personal config writes.
    original_get = NovaConfig.get

    def configured_get(instance: Any, key: str, default: Any = None) -> Any:  # noqa: ANN401 - Dynamic framework boundary.
        if key in bootstrap.get("model_options", {}):
            return bootstrap["model_options"][key]
        if key == "reasoning_effort":
            return bootstrap.get("reasoning_effort")
        if key in {"agent_teams_enabled", "swarm_enabled"}:
            return False
        return original_get(instance, key, default)

    NovaConfig.get = configured_get  # type: ignore[method-assign, assignment]
    original_base_url = NovaConfig.get_model_base_url
    NovaConfig.get_model_base_url = lambda instance: (  # type: ignore[method-assign, assignment]
        bootstrap.get("base_url") or original_base_url(instance)
    )
    if bootstrap.get("auth_mode"):
        NovaConfig.get_openai_auth_mode = lambda _instance: bootstrap["auth_mode"]  # type: ignore[method-assign, assignment]

    class TeamWorker(SessionWorker):
        """Delegated task text cannot invoke the user's CLI command channel."""

        async def _resolve_slash(self, text: str, **_options: object) -> str:
            return text

    worker = TeamWorker(
        agent=None, assistant_id="nova-agent", session_state=state, model_name=bootstrap["model"]
    )
    binding = Binding(identity, worker.session_request)
    model = await asyncio.to_thread(build_chat_model, bootstrap["provider"], bootstrap["model"])
    from novacode_cli.tracking.usage_tree import attach_usage_callback

    attach_usage_callback(model)
    metadata = model.metadata or {}
    from novacode_cli.sessions.worktree import _git

    code, revision, _ = await asyncio.to_thread(_git, ["rev-parse", "HEAD"], cwd=Path.cwd())
    state.team_handshake = {
        "base_revision": revision if code == 0 else None,
        **{
            key: bootstrap[key]
            for key in ("version", "agent", "team", "project", "root", "attempt", "policy")
        },
        "cwd": str(Path.cwd()),
        "provider": metadata.get("nova_provider"),
        "model": metadata.get("nova_model"),
    }
    tools = _session_tools(state)
    if "allowed_tools" in bootstrap:
        allowed = set(bootstrap["allowed_tools"])
        tools = [tool for tool in tools if tool.name in allowed]
    from novacode_cli.agents.agent_file import ALWAYS_INCLUDED, FilesystemToolFilter

    graph, backend = await asyncio.to_thread(
        create_agent_with_config,
        model,
        "nova-agent",
        tools,
        store=get_shared_store(),
        workspace_root=str(Path.cwd()),
        session_id=identity.agent,
        team_binding=binding,
        extra_middleware=[
            FilesystemToolFilter(
                set(bootstrap.get("allowed_tools", ALWAYS_INCLUDED)) & set(ALWAYS_INCLUDED)
            )
        ],
        auto_approve=True,
        steering_instructions=state.steering_instructions,
        system_prompt=bootstrap.get("system_prompt"),
    )
    node = getattr(graph, "nodes", {}).get("tools")
    available = set(getattr(getattr(node, "bound", None), "_tools_by_name", {}))
    missing = set(bootstrap.get("required_tools", [])) - available
    if missing:
        if hasattr(graph, "close_session"):
            await graph.close_session(state.thread_id)
        message = (
            "Worker lacks configured tools; use default mode or configure the worker image/profile"
        )
        raise TeamError(message)
    state.team_handshake["capabilities"] = sorted(available)
    worker.agent, worker.backend = graph, backend
    emit = worker._emit
    deferred: list[dict[str, Any]] = []
    current_tree = None
    streamed_answer = False

    def output(message: dict[str, Any]) -> None:
        nonlocal streamed_answer
        if message.get("t") == "turn_done":
            deferred.append(message)
        else:
            if message.get("t") == "ev" and message.get("c") == "AssistantMessage":
                streamed_answer = True
            emit(message)

    # SessionWorker owns all stdin routing, steering, cancellation and shutdown.
    worker._emit = output  # type: ignore[assignment]  # Worker-local emitter interception.
    original_turn = worker._run_turn

    async def run_turn(*args: Any, **kwargs: Any) -> None:
        nonlocal current_tree, streamed_answer
        from novacode_cli.tracking.usage_tree import UsageTree, set_current_tree

        current_tree = UsageTree(identity.agent)
        set_current_tree(current_tree)
        streamed_answer = False
        before = await graph.aget_state({"configurable": {"thread_id": state.thread_id}})
        old_ids = {message.id for message in before.values.get("messages", [])}
        try:
            await original_turn(*args, **kwargs)
        finally:
            from langchain_core.messages import AIMessage

            if deferred and deferred[0].get("ok"):
                after = await graph.aget_state({"configurable": {"thread_id": state.thread_id}})
                for message in reversed(after.values.get("messages", [])):
                    if (
                        isinstance(message, AIMessage)
                        and message.id not in old_ids
                        and not message.tool_calls
                    ):
                        for index, start in enumerate(range(0, len(message.text), 60_000)):
                            emit(
                                {
                                    "t": "team_output",
                                    "turn_id": str(args[0]),
                                    "index": index,
                                    "text": message.text[start : start + 60_000],
                                }
                            )
                        if not streamed_answer:
                            emit(
                                {
                                    "t": "ev",
                                    "c": "AssistantMessage",
                                    "d": {
                                        "text": message.text,
                                        "agent_name": bootstrap["agent_type"],
                                        "agent_color": "cyan",
                                    },
                                }
                            )
                        break
            emit({"t": "team_usage", "usage": current_tree.to_dict(), "turn_id": str(args[0])})
            for message in deferred:
                emit(message)
            deferred.clear()
            set_current_tree(None)

    worker._run_turn = run_turn  # type: ignore[method-assign]
    try:
        return await worker.run()
    finally:
        if hasattr(graph, "close_session"):
            await graph.close_session(state.thread_id)
        emit.close()


def main() -> int:
    """Read a bounded bootstrap before importing/building the model or graph."""
    output_fd = os.dup(sys.stdout.fileno())
    sys.stdout = sys.stderr
    try:
        line = sys.stdin.buffer.readline(MAX_CONTROL_BYTES + 1)
        if len(line) > MAX_CONTROL_BYTES:
            msg = "Bootstrap limit exceeded"
            raise TeamError(msg)  # noqa: TRY301 - Rollback must include validation failures.
        bootstrap = json.loads(line)
        if not isinstance(bootstrap, dict) or bootstrap.pop("t", None) != "team_bootstrap":
            msg = "Missing team bootstrap"
            raise TeamError(msg)  # noqa: TRY301 - Rollback must include validation failures.
        encode_control(bootstrap)
        return asyncio.run(serve(bootstrap, output_fd))
    except (TeamError, ValueError, KeyError, OSError):
        # Do not expose provider credentials or arbitrary bootstrap values.
        os.write(output_fd, b'{"t":"error","message":"Team worker startup failed","fatal":true}\n')
        return 1
    finally:
        os.close(output_fd)


if __name__ == "__main__":
    raise SystemExit(main())
