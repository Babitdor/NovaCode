"""Exercise configured subagent routes with scripted models and no provider calls.

Run with Nova's installed Python, from the checkout: python -m scripts.verify_subagents.
The optional --server check starts and stops an isolated local Agent Server.
"""

# ruff: noqa: BLE001, T201 - A diagnostic reports each failure and continues checking other agents.

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

MARKER = "NOVA_SUBAGENT_SMOKE_OK"


class Model(FakeMessagesListChatModel):
    """Scripted model that never contacts a provider or requests tools."""

    def bind_tools(self, _tools: object, **_kwargs: object) -> Model:
        """Accept the real graph's tool binding without using a provider."""
        return self


def smoke_model(*_args: object, **_kwargs: object) -> Model:
    """Return a deterministic model with no external side effects."""
    return Model(responses=[AIMessage(MARKER)])


def require_marker(content: str, *, exact: bool = False) -> None:
    """Fail a probe when the graph did not deliver its expected result."""
    if (content != MARKER) if exact else (MARKER not in content):
        raise RuntimeError(content)


def offline_models(stack: ExitStack) -> None:
    """Replace model construction and external MCP discovery only."""
    for name in ("build_chat_model", "build_role_model", "build_async_agent_model"):
        stack.enter_context(patch(f"novacode_cli.config.model_create.{name}", smoke_model))
    stack.enter_context(
        patch("novacode_cli.agents.async_agents._specialist.build_async_agent_model", smoke_model)
    )
    stack.enter_context(
        patch("novacode_cli.agents.async_agents._mcp_tools.mcp_tools_for", lambda _declared: [])
    )


def async_definitions() -> tuple[dict[str, Path], dict[str, Path]]:
    """Discover the shipped and current profile's background graph definitions."""
    from novacode_cli.agents.async_agents._config import shipped_graphs
    from novacode_cli.agents.user_async_agents import collect_user_async_agents

    return shipped_graphs(), dict(collect_user_async_agents())


def make_server_graph(name: str) -> Any:  # noqa: ANN401 - Framework graph boundary.
    """Build an actual configured graph using a scripted model in the server."""
    stack = ExitStack()
    offline_models(stack)
    shipped, user = async_definitions()
    if name in shipped:
        graph = runpy.run_path(str(shipped[name]))["graph"]
    else:
        from novacode_cli.agents.user_async_agents import build_user_graph

        graph = build_user_graph(name, user[name])
    # Patches remain installed for the lifetime of this isolated server process.
    return graph


async def local_routes(report: dict) -> list[str]:
    """Invoke each configured synchronous and background graph and fan out dynamically."""
    from novacode_cli.agents.core_agent import _build_subagent_roster

    with ExitStack() as stack:
        offline_models(stack)
        specs = _build_subagent_roster(
            assistant_id="nova-agent", tools=[], plugin_specs=[], skill_sources=[]
        )
        specs = [s for s in specs if "graph_id" not in s and "url" not in s]
        for spec in specs:
            if "runnable" not in spec:
                spec["model"] = smoke_model()
        names = [s["name"] for s in specs]
        responses = []
        for index, name in enumerate(names):
            responses.extend(
                [
                    AIMessage(
                        "",
                        tool_calls=[
                            {
                                "id": f"sync-{index}",
                                "name": "task",
                                "args": {
                                    "subagent_type": name,
                                    "description": "Return the smoke marker.",
                                },
                            }
                        ],
                    ),
                    AIMessage("finished"),
                ]
            )
        graph = create_deep_agent(model=Model(responses=responses), tools=[], subagents=specs)
        for name in names:
            try:
                result = await asyncio.wait_for(
                    graph.ainvoke({"messages": [HumanMessage("smoke")]}), 40
                )
                outputs = [str(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]
                require_marker("\n".join(outputs))
                report["in_process"][name] = "pass"
            except Exception as exc:
                report["in_process"][name] = f"{type(exc).__name__}: {exc}"
        print(f"In-process: {len(names)} configured agents checked", flush=True)

        from langchain_quickjs import CodeInterpreterMiddleware

        code = (
            f"const names = {json.dumps(names)}; "
            "const results = await Promise.all(names.map(name => task({"
            "subagentType:name, description:'Return the smoke marker.', label:name})));"
            "console.log(JSON.stringify(results));"
        )
        parent = Model(
            responses=[
                AIMessage(
                    "", tool_calls=[{"id": "fanout", "name": "eval", "args": {"code": code}}]
                ),
                AIMessage("finished"),
            ]
        )
        try:
            dynamic = create_deep_agent(
                model=parent, tools=[], subagents=specs, middleware=[CodeInterpreterMiddleware()]
            )
            result = await asyncio.wait_for(
                dynamic.ainvoke({"messages": [HumanMessage("fan out")]}), 120
            )
            outputs = [str(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]
            combined = "\n".join(outputs)
            report["dynamic"] = {
                "agents": names,
                "markers": combined.count(MARKER),
                "status": "pass" if combined.count(MARKER) == len(names) else "failed",
                "output": combined,
            }
        except Exception as exc:
            report["dynamic"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

        shipped, user = async_definitions()
        for name in [*shipped, *user]:
            try:
                graph = make_server_graph(name)
                result = await asyncio.wait_for(
                    graph.ainvoke({"messages": [HumanMessage("smoke")]}), 40
                )
                require_marker(str(result["messages"][-1].content), exact=True)
                report["async_graphs"][name] = "pass"
            except Exception as exc:
                report["async_graphs"][name] = f"{type(exc).__name__}: {exc}"
        print(
            f"Async graph execution: {len(report['async_graphs'])} configured graphs checked",
            flush=True,
        )
        return [*shipped, *user]


async def server_routes(report: dict, names: list[str]) -> None:
    """Compare successful real server runs with Nova's context-bound dispatch."""
    from langgraph_sdk import get_client

    from novacode_cli.agents.async_context import NovaAsyncContext, _bind_context
    from novacode_cli.agents.server_launcher import AgentServerProcess

    with tempfile.TemporaryDirectory(prefix="nova-subagent-smoke-") as folder:
        root = Path(folder)
        module = root / "graphs.py"
        module.write_text(
            f"import sys\nsys.path.insert(0, {str(Path.cwd())!r})\n"
            "from scripts.verify_subagents import make_server_graph\n"
            + "\n".join(f"g{i} = make_server_graph({name!r})" for i, name in enumerate(names)),
            encoding="utf-8",
        )
        config = root / "langgraph.json"
        config.write_text(
            json.dumps(
                {
                    "graphs": {name: f"{module.as_posix()}:g{i}" for i, name in enumerate(names)},
                    "dependencies": ["."],
                }
            ),
            encoding="utf-8",
        )
        server = AgentServerProcess(
            config_path=config, cwd=root, graph_names=names, workspace=root, timeout=40
        )
        try:
            print("Starting isolated local Agent Server (scripted models only)", flush=True)
            await server.start()
            for name in names:
                client = get_client(url=server.url)
                try:
                    thread = await client.threads.create(
                        metadata={"parent_thread_id": "smoke-parent"}
                    )
                    config_arg = {
                        "configurable": {"deepagents_parent": {"thread_id": "smoke-parent"}}
                    }
                    # Control: the graph and real server work without conflicting fields.
                    run = await client.runs.create(
                        thread["thread_id"],
                        name,
                        input={"messages": [{"role": "user", "content": "smoke"}]},
                    )
                    await client.runs.join(thread["thread_id"], run["run_id"])
                    state = await client.threads.get_state(thread["thread_id"])
                    require_marker(str(state["values"]["messages"][-1]["content"]), exact=True)
                    control = "pass"
                    bound = _bind_context(get_client(url=server.url))
                    with patch(
                        "novacode_cli.agents.async_context.current_async_context",
                        lambda: NovaAsyncContext(provider="smoke", model="scripted"),
                    ):
                        run = await bound.runs.create(
                            thread["thread_id"],
                            name,
                            input={"messages": [{"role": "user", "content": "smoke"}]},
                            config=config_arg,
                        )
                    await bound.runs.join(thread["thread_id"], run["run_id"])
                    state = await bound.threads.get_state(thread["thread_id"])
                    require_marker(str(state["values"]["messages"][-1]["content"]), exact=True)
                    report["async_dispatch"][name] = {
                        "control": control,
                        "nova_dispatch": "accepted",
                    }
                except Exception as exc:
                    report["async_dispatch"][name] = {
                        "control": locals().get("control", "failed"),
                        "nova_dispatch": f"{type(exc).__name__}: {exc}",
                    }
                finally:
                    control = "failed"
        except Exception as exc:
            report["server_error"] = f"{type(exc).__name__}: {exc}"
            report["server_log"] = server.log_tail(20)
        finally:
            await asyncio.to_thread(server.stop)


async def main() -> int:
    """Write a complete report and fail the command when a checked route fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--output", default=".tmp/subagent-verification.json")
    args = parser.parse_args()
    import importlib.metadata

    report = {
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("deepagents", "langgraph-sdk", "langchain-quickjs")
        },
        "in_process": {},
        "dynamic": {},
        "async_graphs": {},
        "async_dispatch": {},
        "limits": "Scripted models; no provider/MCP calls; no specialist task-quality assessment.",
    }
    try:
        names = await local_routes(report)
        if args.server:
            await server_routes(report, names)
    finally:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report: {target.resolve()}", flush=True)
    failed = (
        any(value != "pass" for value in report["in_process"].values())
        or report["dynamic"].get("status") != "pass"
        or any(value != "pass" for value in report["async_graphs"].values())
        or "server_error" in report
        or any(
            value.get("control") != "pass" or value.get("nova_dispatch") != "accepted"
            for value in report["async_dispatch"].values()
        )
    )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
