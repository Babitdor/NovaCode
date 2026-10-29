"""Bounded paired Caveman evaluation using the exact saved model, without config writes."""

from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from novacode_cli.agents.caveman import CavemanMiddleware
from novacode_cli.config.nova_config import NovaConfig

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


def scenarios() -> dict[str, tuple[str, list]]:
    """Use public synthetic inputs, including repeated observations with exact facts."""
    cases = {
        "mutable_default": (
            "style",
            [
                HumanMessage(
                    "Explain why def add_item(item, bucket=[]): bucket.append(item); return bucket "
                    "shares state. Show the complete corrected add_item function, preserving "
                    "explicit caller-supplied lists. Explain the tradeoff. Do not call tools."
                )
            ],
        ),
        "safety": (
            "style",
            [
                HumanMessage(
                    "I plan to run rm -rf ./data. Explain what to check first and whether "
                    "recovery is guaranteed. Give no executable command. Preserve rm -rf ./data "
                    "verbatim when referring to it. Do not call tools."
                )
            ],
        ),
        "portuguese": (
            "style",
            [
                HumanMessage(
                    "Explique em português como um pool de conexões reduz latência e quais "
                    "limites configurar. Preserve ECONNRESET e o timeout de 30 segundos. "
                    "Não use ferramentas."
                )
            ],
        ),
    }
    payloads = {
        "json_reads": (
            json.dumps(
                {
                    "service": "billing_worker",
                    "max_retries": 7,
                    "timeout_seconds": 30,
                    "records": [{"id": i, "state": "ready"} for i in range(120)],
                },
                indent=4,
            ),
            "read_file",
            {"file_path": "settings.json"},
            "Report exact service, max_retries and timeout_seconds. "
            "Use the observations; no tools.",
        ),
        "repeated_logs": (
            "progress unchanged\n" * 400 + "FAIL tests/parser.py:42\n"
            "AssertionError: expected 17, got 18\nexit code 1",
            "execute",
            {"command": "pytest tests/parser.py"},
            "Report the failing test location, exact assertion and exit code. No tools.",
        ),
    }
    for name, (text, tool, args, question) in payloads.items():
        messages = [HumanMessage("Inspect the supplied observations.")]
        for index in range(3):
            call_id = f"{name}-{index}"
            messages += [
                AIMessage(content="", tool_calls=[{"id": call_id, "name": tool, "args": args}]),
                ToolMessage(content=text, tool_call_id=call_id, name=tool),
            ]
        messages.append(HumanMessage(question))
        cases[name] = ("workflow", messages)
    return cases


def check_function(text: str) -> bool:
    """Execute only a validated tiny list-function AST, never arbitrary model code."""
    allowed = (
        ast.Module,
        ast.If,
        ast.Assign,
        ast.Expr,
        ast.Return,
        ast.Compare,
        ast.Is,
        ast.IsNot,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.List,
        ast.Call,
        ast.Attribute,
    )
    for code in re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL):
        try:
            functions = [
                node
                for node in ast.parse(code).body
                if isinstance(node, ast.FunctionDef) and node.name == "add_item"
            ]
            for original in functions:
                fn = copy.deepcopy(original)
                if (
                    fn.decorator_list
                    or fn.args.posonlyargs
                    or fn.args.kwonlyargs
                    or fn.args.vararg is not None
                    or fn.args.kwarg is not None
                    or [arg.arg for arg in fn.args.args] != ["item", "bucket"]
                    or len(fn.args.defaults) != 1
                    or not isinstance(fn.args.defaults[0], ast.Constant)
                    or fn.args.defaults[0].value is not None
                ):
                    continue
                nodes = list(ast.walk(ast.Module(body=fn.body, type_ignores=[])))
                if any(not isinstance(node, allowed) for node in nodes):
                    continue
                if any(
                    isinstance(node, ast.Attribute)
                    and (
                        node.attr != "append"
                        or not isinstance(node.value, ast.Name)
                        or node.value.id != "bucket"
                    )
                    for node in nodes
                ):
                    continue
                if any(
                    isinstance(node, ast.Call)
                    and not (
                        isinstance(node.func, ast.Attribute)
                        or (isinstance(node.func, ast.Name) and node.func.id == "list")
                    )
                    for node in nodes
                ):
                    continue
                for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
                    arg.annotation = None
                fn.returns = None
                namespace = {"__builtins__": {}, "list": list}
                exec(  # noqa: S102 -- validated tiny AST with no general calls or attributes
                    compile(
                        ast.Module(body=[fn], type_ignores=[]), "<validated-list-function>", "exec"
                    ),
                    namespace,
                )
                function = namespace["add_item"]
                first, second, supplied = function(1), function(2), []
                result = function(3, supplied)
                if first == [1] and second == [2] and result is supplied and supplied == [3]:
                    return True
        except (SyntaxError, TypeError, ValueError, NameError, KeyError):
            continue
    return False


def quality(case: str, text: str) -> bool:
    """Check observable requirements; language and explanation still need human review."""
    if case == "mutable_default":
        return check_function(text)
    if case == "safety":
        return (
            "rm -rf ./data" in text
            and bool(
                re.search(
                    r"(?:not|no|cannot|can't|never|isn't|is not).{0,65}guarantee"
                    r"|guarantee.{0,30}(?:not|no)",
                    text,
                    re.IGNORECASE | re.DOTALL,
                )
            )
            and "```" not in text
        )
    if case == "repeated_logs":
        plain = text.replace("`", "").replace("*", "")
        return (
            "tests/parser.py:42" in text
            and "AssertionError: expected 17, got 18" in text
            and re.search(r"\bexit code\s*[:=|]?\s*1\b", plain, re.IGNORECASE) is not None
        )
    if case == "portuguese":
        plain = text.replace("`", "").replace("*", "")
        return (
            "ECONNRESET" in text
            and re.search(r"\b30\s*(?:segundos\b|s\b)", plain, re.IGNORECASE) is not None
            and ("conex" in text.lower() or "latência" in text.lower())
        )
    expected = {
        "json_reads": ("billing_worker", "7", "30"),
    }
    return all(fact in text for fact in expected[case])


def prepare(
    model: BaseChatModel,
    base: str,
    messages: list,
    config: NovaConfig,
    level: str,
    profile: str,
) -> ModelRequest:
    """Use the real middleware, with preferences copied in memory and routing pinned."""
    request = ModelRequest(
        model=model,
        system_message=SystemMessage(content=base),
        messages=messages,
        state={"messages": messages},
    )
    if profile == "style":
        return CavemanMiddleware.prepare(request, enabled=level != "off", level=level)
    local = copy.deepcopy(config)
    local._config.update(
        compression_mode="off" if level == "off" else "caveman",
        caveman_enabled=level != "off",
        caveman_level=level,
    )
    preferences = local.get_caveman_feature_preferences()
    local._config["caveman_features"] = {**preferences, "auto": False}
    with patch("novacode_cli.agents.caveman.NovaConfig", return_value=local):
        middleware = CavemanMiddleware()
        update = middleware.before_model(request.state, None)
        if update:
            request = request.override(state={**request.state, **update})
        return middleware._prepare_live(request)


def summarize(rows: list[dict]) -> dict:
    """Count provider usage, pairing only completed, requirement-passing observations."""
    result = {}
    for profile in ("style", "workflow"):
        for level in ("full", "ultra"):
            pairs = []
            for row in rows:
                if row["profile"] != profile or row["level"] != level:
                    continue
                baseline = next(
                    (
                        candidate
                        for candidate in rows
                        if candidate["case"] == row["case"]
                        and candidate["repeat"] == row["repeat"]
                        and candidate["level"] == "off"
                    ),
                    None,
                )
                if baseline and all(
                    r.get("complete") and r.get("quality_pass") and r.get("usage")
                    for r in (baseline, row)
                ):
                    pairs.append((baseline, row))
            metrics = {
                "valid_pairs": len(pairs),
                "attempted_pairs": sum(
                    r["profile"] == profile and r["level"] == level for r in rows
                ),
            }
            for token_type in ("input_tokens", "output_tokens", "total_tokens"):
                off = sum(a["usage"][token_type] for a, _ in pairs)
                on = sum(b["usage"][token_type] for _, b in pairs)
                metrics[token_type] = {
                    "off": off,
                    "on": on,
                    "reduction_percent": round(100 * (off - on) / off, 2) if off else None,
                }
            result[f"{profile}:{level}"] = metrics
    return result


def review(output: Path) -> bool:
    """Recheck saved responses offline, retaining the initial grading for audit."""
    data = json.loads(output.read_text(encoding="utf-8"))
    data.setdefault("initial_summary", data["summary"])
    for row in data["results"]:
        row.setdefault("initial_quality_pass", row.get("quality_pass"))
        row["quality_pass"] = quality(row["case"], row.get("text", ""))
    data["quality_gate"] = "Exact required facts; exit-code label is case/Markdown insensitive."
    data["summary"] = summarize(data["results"])
    output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(data["summary"], indent=2), flush=True)  # noqa: T201
    return all(r.get("complete") and r["quality_pass"] and r.get("usage") for r in data["results"])


async def measure(output: Path, repeats: int) -> bool:
    """Persist every bounded attempt and expose incomplete pairs explicitly."""
    from novacode_cli.onboarding import load_secrets_into_env

    load_secrets_into_env()
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.prompts import render_template
    from novacode_cli.security.prompt_injection import GUARDRAIL_INSTRUCTIONS

    config = NovaConfig()
    selected = config.get_model_config()
    if not selected or selected["model"].lower().rsplit("/", 1)[-1] != "deepseek-v4-flash":
        message = "The saved default must be DeepSeek V4 Flash; no fallback model will be used."
        raise RuntimeError(message)
    model = build_chat_model(selected["provider"], selected["model"]).model_copy(
        update={"max_retries": 0, "request_timeout": 120, "temperature": 0}
    )
    base = (
        render_template(
            "core_agent_system.jinja",
            platform=os.name,
            shell_name="none (benchmark)",
            path_sep=os.sep,
            shell_notes=[],
            has_async_agents=False,
        )
        + "\n\n"
        + GUARDRAIL_INSTRUCTIONS
    )
    cases = scenarios()
    rows = []
    output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output.write_text(
            json.dumps(
                {
                    "provider": selected["provider"],
                    "model": selected["model"],
                    "reasoning_effort": config.get("reasoning_effort"),
                    "repeats": repeats,
                    "max_tokens": 4096,
                    "temperature": 0,
                    "routing": "disabled to hold model constant",
                    "workflow_preferences": config.get_caveman_feature_preferences(),
                    "summary": summarize(rows),
                    "results": rows,
                    "limits": (
                        "Synthetic paired sample; provider token counts, not invoice savings. "
                        "Language/explanations require review. No universal guarantee."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # Alternate mode order between repeats to avoid always warming one mode's cache first.
    for repeat in range(repeats):
        for case, (profile, messages) in cases.items():
            levels = ("off", "full", "ultra") if repeat % 2 == 0 else ("ultra", "full", "off")
            for level in levels:
                row = {"case": case, "profile": profile, "repeat": repeat, "level": level}
                started = time.perf_counter()
                try:
                    request = prepare(model, base, messages, config, level, profile)
                    response = await asyncio.wait_for(
                        model.ainvoke([request.system_message, *request.messages], max_tokens=4096),
                        timeout=130,
                    )
                    row.update(
                        text=response.text,
                        usage=response.usage_metadata,
                        finish_reason=response.response_metadata.get("finish_reason"),
                    )
                    row["complete"] = row["finish_reason"] == "stop"
                    row["quality_pass"] = quality(case, response.text)
                except Exception as exc:  # noqa: BLE001 -- safe structural failures only
                    row.update(error_type=type(exc).__name__, complete=False, quality_pass=False)
                row["duration_seconds"] = round(time.perf_counter() - started, 3)
                rows.append(row)
                save()
                print(json.dumps({k: v for k, v in row.items() if k != "text"}), flush=True)  # noqa: T201
                if row.get("error_type") in {"AuthenticationError", "PermissionDeniedError"}:
                    return False
    print(json.dumps(summarize(rows), indent=2), flush=True)  # noqa: T201
    return all(row["complete"] and row["quality_pass"] and row.get("usage") for row in rows)


def main() -> None:
    """Require an explicit live run; never write user preferences or credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--review", type=Path, help="Recheck a saved synthetic run without inference"
    )
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=2)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/caveman-deepseek-v4-flash-benchmark.json")
    )
    args = parser.parse_args()
    if args.review:
        if not review(args.review):
            raise SystemExit(1)
        return
    if not args.live:
        parser.error("Pass --live to authorize the bounded configured-model benchmark")
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    if not asyncio.run(measure(args.output, args.repeats)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
