"""Bounded, isolated live Code Mode comparison; never writes user preferences."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool, tool

from novacode_cli.agents import code_runtime
from novacode_cli.agents.caveman import CavemanMiddleware
from novacode_cli.agents.code_mode import CodeModeMiddleware
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.mcp.middleware import MCPMiddleware
from novacode_cli.security.delegated_approval import HardDenyMiddleware

TASKS = {
    "native_discovery": 'Find the native checksum tool in the catalog and call it with value="nova". Return {"checksum":436}.',
    "mcp_composition": 'Discover the fixture MCP tools. Get its numbers, then pass them to its total tool. Return {"total":42}.',
    "exact_reads": "Read /a.txt, /b.txt and /c.txt. Return their exact contents as JSON keys a, b, c.",
    "exact_edit": 'Read /edit.txt, replace only enabled=false with enabled=true using edit_file, then read to verify. Return {"verified":true}.',
    "partial_failure": 'Call record_write once, then controlled_failure, then read /writes.txt to recover. Do not repeat record_write. Return {"writes":1}.',
    "oversized_recovery": 'Call oversized once and select its COMPLETE result as the tool response, without filtering or storing a subset. Interpreter state will then be cleared. Recover the result without calling oversized again, and return its exact final line as {"tail":string}.',
}


def fixtures(root: Path):
    """Only deterministic fixture operations; all file access stays inside root."""
    state = {"writes": 0, "large_calls": 0, "calls": []}
    facts = {"a": "alpha=17\n", "b": "beta=Berlin\n", "c": "gamma=λ\n"}
    for key, value in facts.items():
        (root / f"{key}.txt").write_text(value, encoding="utf-8")
    (root / "edit.txt").write_text("# exact\nenabled=false\nkeep=λ\n", encoding="utf-8")

    def resolve(file_path):
        path = (root / file_path.lstrip("/")).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Outside fixture workspace")
        return path

    @tool
    def read_file(file_path: str, offset: int = 0, limit: int = 100) -> str:
        """Read exact UTF-8 text lines from a workspace virtual path."""
        state["calls"].append("read_file")
        return "".join(
            resolve(file_path)
            .read_text(encoding="utf-8")
            .splitlines(keepends=True)[offset : offset + limit]
        )

    @tool
    def edit_file(file_path: str, old_string: str, new_string: str) -> str:
        """Replace exactly one literal occurrence in a workspace file."""
        state["calls"].append("edit_file")
        path = resolve(file_path)
        source = path.read_text(encoding="utf-8")
        if source.count(old_string) != 1:
            raise ValueError("Expected exactly one match")
        path.write_text(source.replace(old_string, new_string), encoding="utf-8")
        return "Updated"

    @tool
    def native_checksum(value: str) -> int:
        """Calculate the native character checksum of a string."""
        state["calls"].append("native_checksum")
        return sum(map(ord, value))

    @tool
    def record_write() -> str:
        """Append one irreversible fixture write; call exactly once."""
        state["calls"].append("record_write")
        state["writes"] += 1
        (root / "writes.txt").write_text(str(state["writes"]), encoding="utf-8")
        return "Write completed"

    @tool
    def controlled_failure(runtime: ToolRuntime) -> ToolMessage:
        """Always fail after the separate completed write."""
        state["calls"].append("controlled_failure")
        return ToolMessage(
            content="Controlled fixture failure; inspect completed outcomes",
            tool_call_id=runtime.tool_call_id,
            status="error",
        )

    @tool
    def oversized() -> str:
        """One-shot large text; cannot be retrieved a second time."""
        state["calls"].append("oversized")
        state["large_calls"] += 1
        if state["large_calls"] != 1:
            raise ValueError("One-shot result expired")
        return "public fixture record\n" * 900 + "exact-tail-7f94λ"

    @tool("fixture_numbers")
    def numbers() -> dict:
        """Get deterministic numbers for composition with total."""
        state["calls"].append("fixture_numbers")
        return {"values": [17, 25]}

    @tool("fixture_total")
    def total(values: list[int]) -> int:
        """Sum the numbers returned by numbers."""
        state["calls"].append("fixture_total")
        return sum(values)

    config = root / "mcp.json"
    config.write_text('{"mcpServers":{}}', encoding="utf-8")
    mcp = MCPMiddleware(config)
    mcp._tools_discovered = True
    for target, alias in [(numbers, "numbers"), (total, "total")]:
        target.metadata = {"nova_mcp_server": "fixture", "nova_mcp_tool": alias}
        mcp.tools.append(target)
        mcp._tools_cache.append(
            {
                "server": "fixture",
                "skill_alias": alias,
                "name": target.name,
                "description": target.description,
            }
        )

    def unused(value: str = "") -> str:
        return value

    tools = [read_file, edit_file, native_checksum, record_write, controlled_failure, oversized]
    tools += [
        StructuredTool.from_function(
            unused,
            name=f"catalog_entry_{i:03}",
            description=f"Synthetic unrelated catalog capability {i}",
        )
        for i in range(80)
    ]
    return tools, mcp, state, facts


class Measurements(AgentMiddleware):
    def __init__(self):
        super().__init__()
        self.rounds = []
        self.tools_seen = []
        self.dispatches = []

    async def awrap_model_call(self, request, handler):
        if len(self.rounds) >= 20:
            raise RuntimeError("20 model turn limit")
        row = {"complete": False}
        self.rounds.append(row)
        response = await handler(request)
        row.update(
            complete=True,
            messages=[m.model_dump(mode="json") for m in response.result],
            usage=[m.usage_metadata for m in response.result if isinstance(m, AIMessage)],
        )
        return response

    async def awrap_tool_call(self, request, handler):
        self.tools_seen.append(request.tool_call["name"])
        result = await handler(request)
        if request.tool_call["name"] in {"eval", "MCPCode"} and isinstance(result, ToolMessage):
            self.dispatches.append(json.loads(result.content))
        return result


def baseline():
    """Load the captured sources, including their original bootstrap and MCP descriptions."""
    modules = {}
    for name in ("code_runtime", "generic", "code_mode"):
        loader = SourceFileLoader(
            f"_nova_baseline_{name}",
            str(
                Path(__file__).resolve().parent / "fixtures" / "code_mode_baseline" / f"{name}.txt"
            ),
        )
        module = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader)
        )
        sys.modules[loader.name] = module
        loader.exec_module(module)
        modules[name] = module
    modules["code_mode"].build_mcp_tool = modules["generic"].build_mcp_tool
    return modules["code_mode"].CodeModeMiddleware, modules["code_runtime"].run_script


def passed(task, answer, state, root, facts):
    try:
        text = (
            answer.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
        value = json.loads(text)
    except (ValueError, AttributeError):
        return False
    expected = {
        "native_discovery": {"checksum": 436},
        "mcp_composition": {"total": 42},
        "exact_reads": facts,
        "exact_edit": {"verified": True},
        "partial_failure": {"writes": 1},
        "oversized_recovery": {"tail": "exact-tail-7f94λ"},
    }
    if value != expected[task]:
        return False
    if task == "native_discovery":
        return "native_checksum" in state["calls"]
    if task == "mcp_composition":
        return all(name in state["calls"] for name in ("fixture_numbers", "fixture_total"))
    if task == "exact_reads":
        return state["calls"].count("read_file") >= 3
    if task == "exact_edit":
        return (root / "edit.txt").read_text(
            encoding="utf-8"
        ) == "# exact\nenabled=true\nkeep=λ\n" and state["calls"].count("read_file") >= 2
    if task == "partial_failure":
        return (
            state["writes"] == 1
            and "controlled_failure" in state["calls"]
            and "read_file" in state["calls"]
        )
    return state["large_calls"] == 1


def summary(rows):
    output = {}
    for variant in sorted({r["variant"] for r in rows}):
        output[variant] = {}
        for task in TASKS:
            group = [r for r in rows if r["variant"] == variant and r["task"] == task]
            output[variant][task] = {
                "successes": sum(r["success"] for r in group),
                "trials": len(group),
            }
            for label, subset in [
                ("successful", [r for r in group if r["success"]]),
                ("failed", [r for r in group if not r["success"]]),
            ]:
                output[variant][task][label] = {
                    key: sum(r.get(key, 0) for r in subset)
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "round_trips",
                        "nested_calls",
                        "retries",
                        "elapsed",
                    )
                }
    return output


async def run(variants, output):
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.onboarding import load_secrets_into_env
    from novacode_cli.security.policy import get_policy

    if output.exists():
        raise FileExistsError("Choose a fresh --output path; existing measurements are preserved")
    load_secrets_into_env()
    config = NovaConfig()
    selected = config.get_model_config()
    if not selected:
        raise RuntimeError("A saved default model is required")
    digest = hashlib.sha256(config.config_path.read_bytes()).hexdigest()
    model = build_chat_model(selected["provider"], selected["model"], max_retries=0)
    old_mode, old_script = baseline()
    data = {"results": []}
    data.update(
        provider=selected["provider"],
        model=selected["model"],
        reasoning=config.get("reasoning_effort"),
        compression_mode=config.get_compression_mode(),
        lite_features=config.get_caveman_feature_preferences(),
        effective_lite_features=config.get_caveman_features(),
        ponytail_level=config.get_ponytail_level(),
        temperature=getattr(model, "temperature", None),
        config_sha256=digest,
        bounds={"model_turns": 20, "seconds": 120},
        pricing="Unavailable; no monetary cost estimated",
        task_prompts=TASKS,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    for repeat in range(2):
        for task, prompt in TASKS.items():
            for variant in variants:
                with tempfile.TemporaryDirectory(prefix="nova-code-bench-") as directory:
                    root = Path(directory)
                    tools, mcp, state, facts = fixtures(root)
                    meter = Measurements()
                    selected_mode = old_mode if variant == "current" else CodeModeMiddleware

                    class StateLossCodeMode(selected_mode):
                        async def _execute(self, request, handler, code):
                            from langchain_quickjs.middleware import _resolve_thread_id

                            result = await super()._execute(request, handler, code)
                            await self._registry.aevict(
                                _resolve_thread_id(self._fallback_thread_id)
                            )
                            return result

                    mode_class = (
                        StateLossCodeMode if task == "oversized_recovery" else selected_mode
                    )
                    kwargs = {"offload_dir": root / "cleared"} if variant == "improved" else {}
                    mode = mode_class(mcp, auto_approve=True, **kwargs)
                    original_tiers = dict(get_policy().tool_tiers)
                    get_policy().tool_tiers.update({t.name: "allow" for t in [*tools, *mcp.tools]})
                    middleware = ([mode] if variant != "direct" else []) + [
                        meter,
                        CavemanMiddleware(),
                        mcp,
                        HardDenyMiddleware(),
                    ]
                    graph = create_agent(
                        model,
                        tools=tools,
                        middleware=middleware,
                        system_prompt="Complete the fixture task using tools. Return only the requested JSON object. "
                        "Use virtual paths. Tool errors are recoverable; inspect completed work before retrying.",
                    )
                    row = {"variant": variant, "task": task, "repeat": repeat, "success": False}
                    started = time.monotonic()
                    try:
                        with (
                            patch.object(
                                NovaConfig,
                                "get_code_mode_enabled",
                                return_value=variant != "direct",
                            ),
                            patch.object(
                                code_runtime,
                                "run_script",
                                old_script if variant == "current" else code_runtime.run_script,
                            ),
                        ):
                            result = await asyncio.wait_for(
                                graph.ainvoke(
                                    {"messages": [HumanMessage(prompt)]}, {"recursion_limit": 85}
                                ),
                                timeout=120,
                            )
                        row["answer"] = result["messages"][-1].content
                        row["success"] = passed(task, row["answer"], state, root, facts)
                        row["tool_results"] = [
                            m.model_dump(mode="json")
                            for m in result["messages"]
                            if isinstance(m, ToolMessage)
                        ]
                    except Exception as exc:
                        row["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                    finally:
                        get_policy().tool_tiers.clear()
                        get_policy().tool_tiers.update(original_tiers)
                        await mode._registry.aevict(mode._fallback_thread_id)
                    row.update(
                        elapsed=time.monotonic() - started,
                        rounds=meter.rounds,
                        round_trips=len(meter.rounds),
                        native_calls=state["calls"],
                        tools=meter.tools_seen,
                    )
                    usages = [u for r in meter.rounds for u in r.get("usage", []) if u]
                    row.update(
                        input_tokens=sum(u.get("input_tokens", 0) for u in usages),
                        output_tokens=sum(u.get("output_tokens", 0) for u in usages),
                        cached_tokens=sum(
                            u.get("input_token_details", {}).get("cache_read", 0) for u in usages
                        ),
                        usage_available=bool(usages),
                        usage_complete=bool(usages) and all(r["complete"] for r in meter.rounds),
                        cached_usage_available=any(
                            "cache_read" in u.get("input_token_details", {}) for u in usages
                        ),
                        nested_calls=sum(d.get("calls", 0) for d in meter.dispatches),
                        dispatches=meter.dispatches,
                        retries=sum(
                            max(0, state["calls"].count(n) - 1)
                            for n in set(state["calls"])
                            if n != "read_file"
                        ),
                    )
                    data["results"].append(row)
                    data["summary"] = summary(data["results"])
                    data["preferences_unchanged"] = (
                        hashlib.sha256(config.config_path.read_bytes()).hexdigest() == digest
                    )
                    output.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    print(
                        variant, task, repeat, row["success"], round(row["elapsed"], 1), flush=True
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--variants", nargs="+", choices=["direct", "current", "improved"], required=True
    )
    parser.add_argument("--output", type=Path, default=Path("reports/code-mode-benchmark.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for configured-provider API usage")
    asyncio.run(run(args.variants, args.output))
