"""Paired Caveman/Ponytail evaluation using the saved model, without config writes."""

from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import hashlib
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
        "reuse_helper": (
            "style",
            [
                HumanMessage(
                    "An existing stable_unique(values) helper returns a new list of unique "
                    "values in their original order. Fix def unique_nonempty(values): "
                    "return list(set(values)). Reuse stable_unique and use a list comprehension "
                    "to drop only None and empty strings, preserving 0. Do not mutate values. "
                    "Return the complete replacement unique_nonempty function only, in a Python "
                    "code block. No new helpers, imports or dependencies. Do not call tools."
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
        ast.IfExp,
        ast.BoolOp,
        ast.Or,
        ast.And,
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
                    or getattr(fn, "type_params", [])
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
    if case == "reuse_helper":
        return (
            len(re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)) == 1
            and bool(re.fullmatch(r"\s*```(?:python|py)?\s*\n.*?```\s*", text, re.DOTALL))
            and check_reuse(text)
        )
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


def check_reuse(text: str) -> bool:
    """Execute only a tiny helper/list-comprehension contract with no general calls."""
    allowed = (
        ast.Return,
        ast.Expr,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.ListComp,
        ast.comprehension,
        ast.Compare,
        ast.Constant,
        ast.Is,
        ast.IsNot,
        ast.Eq,
        ast.NotEq,
        ast.NotIn,
        ast.In,
        ast.Tuple,
        ast.List,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
    )
    for code in re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL):
        try:
            module = ast.parse(code)
            if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
                continue
            fn = module.body[0]
            if (
                fn.name != "unique_nonempty"
                or fn.decorator_list
                or getattr(fn, "type_params", [])
                or fn.args.defaults
                or fn.args.kw_defaults
                or fn.args.posonlyargs
                or fn.args.kwonlyargs
                or fn.args.vararg
                or fn.args.kwarg
                or [a.arg for a in fn.args.args] != ["values"]
            ):
                continue
            nodes = [node for statement in fn.body for node in ast.walk(statement)]
            calls = [n for n in nodes if isinstance(n, ast.Call)]
            loops = [n for n in nodes if isinstance(n, ast.comprehension)]
            iterator = loops[0].iter if len(loops) == 1 else None
            bounded_iterator = (isinstance(iterator, ast.Name) and iterator.id == "values") or (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Name)
                and iterator.func.id == "stable_unique"
                and len(iterator.args) == 1
                and isinstance(iterator.args[0], ast.Name)
                and iterator.args[0].id == "values"
                and not iterator.keywords
            )
            if (
                any(not isinstance(n, allowed) for n in nodes)
                or len(calls) != 1
                or not isinstance(calls[0].func, ast.Name)
                or calls[0].func.id != "stable_unique"
                or len(loops) != 1
                or loops[0].is_async
                or not bounded_iterator
            ):
                continue
            fn.args.args[0].annotation = None
            fn.returns = None
            namespace = {"__builtins__": {}, "stable_unique": lambda xs: list(dict.fromkeys(xs))}
            exec(compile(module, "<validated-helper-function>", "exec"), namespace)  # noqa: S102
            function = namespace["unique_nonempty"]
            sample = ["b", "a", "b", "", None, 0, "a", 0]
            before = list(sample)
            if (
                function(sample) == ["b", "a", 0]
                and sample == before
                and function([None, "", 0]) == [0]
                and function([]) == []
            ):
                return True
        except (SyntaxError, TypeError, ValueError, NameError, KeyError):
            continue
    return False


def prepare(
    model: BaseChatModel,
    base: str,
    messages: list,
    config: NovaConfig,
    level: str,
    profile: str,
    mode: str = "caveman",
) -> ModelRequest:
    """Use the real middleware, with preferences copied in memory and routing pinned."""
    request = ModelRequest(
        model=model,
        system_message=SystemMessage(content=base),
        messages=messages,
        state={"messages": messages},
    )
    local = copy.deepcopy(config)
    local._config.update(
        compression_mode="off" if level == "off" else mode,
        caveman_enabled=level != "off" and mode == "caveman",
        caveman_level=level,
        ponytail_level=level,
    )
    preferences = local.get_caveman_feature_preferences()
    local._config["caveman_features"] = (
        {name: name == "say" for name in preferences}
        if profile == "style"
        else {**preferences, "auto": False}
    )
    with patch("novacode_cli.agents.caveman.NovaConfig", return_value=local):
        middleware = CavemanMiddleware()
        update = middleware.before_model(request.state, None)
        if update:
            request = request.override(state={**request.state, **update})
        return middleware._prepare_live(request)


def summarize(rows: list[dict]) -> dict:
    """Count provider usage, pairing only completed, requirement-passing observations."""
    result = {}
    for mode, profile in (
        (mode, profile) for mode in ("caveman", "ponytail") for profile in ("style", "workflow")
    ):
        for level in ("full", "ultra"):
            pairs = []
            for row in rows:
                if (
                    row["profile"] != profile
                    or row["level"] != level
                    or row.get("mode", "caveman") != mode
                ):
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
                    r["profile"] == profile
                    and r["level"] == level
                    and r.get("mode", "caveman") == mode
                    for r in rows
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
            key = f"{profile}:{level}" if mode == "caveman" else f"ponytail:{profile}:{level}"
            result[key] = metrics
    return result


def review(output: Path) -> bool:
    """Recheck saved responses offline, retaining the initial grading for audit."""
    data = json.loads(output.read_text(encoding="utf-8"))
    data.setdefault("initial_summary", data["summary"])
    for row in data["results"]:
        row.setdefault("initial_quality_pass", row.get("quality_pass"))
        row["quality_pass"] = quality(row["case"], row.get("text", ""))
    data["quality_gate"] = (
        "Executed bounded function contracts; code-only output shape; exact required facts. "
        "Equivalent helper-first/filter-first implementations accepted. "
        "Exit-code labels are case/Markdown insensitive."
    )
    data["summary"] = summarize(data["results"])
    output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(data["summary"], indent=2), flush=True)  # noqa: T201
    return all(r.get("complete") and r["quality_pass"] and r.get("usage") for r in data["results"])


async def measure(output: Path, repeats: int) -> bool:
    """Persist every bounded attempt and expose incomplete pairs explicitly."""
    from novacode_cli.onboarding import load_secrets_into_env

    load_secrets_into_env()
    from novacode_cli.agents.model_budget import _bounded_model
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.prompts import render_template
    from novacode_cli.security.prompt_injection import GUARDRAIL_INSTRUCTIONS

    config = NovaConfig()
    selected = config.get_model_config()
    if not selected:
        message = "A saved default model is required; no fallback model will be used."
        raise RuntimeError(message)
    model = _bounded_model(build_chat_model(selected["provider"], selected["model"], max_retries=0))
    config_hash = hashlib.sha256(config.config_path.read_bytes()).hexdigest()
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
                    "temperature": getattr(model, "temperature", None),
                    "routing": "disabled to hold model constant",
                    "mode_preferences_unchanged": (
                        hashlib.sha256(config.config_path.read_bytes()).hexdigest() == config_hash
                    ),
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

    # Alternate order: one mode must not always benefit from a warmed provider prefix.
    modes = [
        ("off", "off"),
        ("caveman", "full"),
        ("caveman", "ultra"),
        ("ponytail", "full"),
        ("ponytail", "ultra"),
    ]
    for repeat in range(repeats):
        for case, (profile, messages) in cases.items():
            for mode, level in modes if repeat % 2 == 0 else list(reversed(modes)):
                row = {
                    "case": case,
                    "profile": profile,
                    "repeat": repeat,
                    "level": level,
                    "mode": mode,
                }
                started = time.perf_counter()
                try:
                    originals = [m.model_dump() for m in messages]
                    request = prepare(model, base, messages, config, level, profile, mode)
                    system = request.system_message.text
                    marker = (
                        f'<nova_caveman_style level="{level}">'
                        if mode == "caveman"
                        else f"PONYTAIL MODE ACTIVE — level: {level}"
                    )
                    row["mode_applied"] = (
                        marker in system
                        if mode != "off"
                        else "<nova_caveman_style" not in system
                        and "PONYTAIL MODE ACTIVE" not in system
                    )
                    row["history_unchanged"] = [m.model_dump() for m in messages] == originals
                    if (
                        request.model is not model
                        or not row["mode_applied"]
                        or not row["history_unchanged"]
                    ):
                        message = "Benchmark changed the model/history or failed to apply the mode."
                        raise RuntimeError(message)  # noqa: TRY301 - persist the failed invariant.
                    row["tool_chars_before"] = sum(
                        len(str(m.content)) for m in messages if isinstance(m, ToolMessage)
                    )
                    row["tool_chars_sent"] = sum(
                        len(str(m.content)) for m in request.messages if isinstance(m, ToolMessage)
                    )
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
                    row["code_lines"] = sum(
                        len(block.strip().splitlines())
                        for block in re.findall(
                            r"```(?:python|py)?\s*\n(.*?)```", response.text, re.DOTALL
                        )
                    )
                except Exception as exc:  # noqa: BLE001 -- safe structural failures only
                    row.update(error_type=type(exc).__name__, complete=False, quality_pass=False)
                row["duration_seconds"] = round(time.perf_counter() - started, 3)
                rows.append(row)
                save()
                print(json.dumps({k: v for k, v in row.items() if k != "text"}), flush=True)  # noqa: T201
                if row.get("error_type"):
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
        "--output", type=Path, default=Path("reports/lite-mode-default-model-benchmark.json")
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
