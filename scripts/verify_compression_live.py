"""Opt-in real-provider checks: uv run python scripts/verify_compression_live.py.

Makes 20 bounded calls using Nova's configured model. Saves synthetic outputs and
provider usage in test-results/compression-live.json; never changes preferences.
"""

# ruff: noqa: T201 -- opt-in command prints test progress.

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage

from novacode_cli.config.model_create import create_model_from_config
from novacode_cli.prompts import TEMPLATES_DIR
from novacode_cli.utils.model_info import get_model_info

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

FACTS = (
    "Explain this failure and give the next check: src/auth.py:42 raised "
    "`ValueError: token expired` after 3 retries. Do not reset credentials. "
    "The exact check is `uv run pytest tests/test_auth.py -q`. Preserve these facts."
)
CODE = (
    "Implement only a pure Python clamp(value, lower, upper) function. "
    "It returns lower below the range, upper above it, otherwise value. "
    "Raise ValueError when lower > upper. No imports or dependencies. "
    "Give one fenced Python block; any explanation goes outside it."
)


def check_code(text: str) -> bool:  # noqa: PLR0911 -- independent fail-closed AST checks.
    """Validate a restricted arithmetic function without running arbitrary code."""
    import ast

    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    if len(blocks) != 1:
        return False
    try:
        tree = ast.parse(blocks[0])
        allowed = (
            ast.Module,
            ast.FunctionDef,
            ast.arguments,
            ast.arg,
            ast.Return,
            ast.If,
            ast.Compare,
            ast.Gt,
            ast.Lt,
            ast.GtE,
            ast.LtE,
            ast.Eq,
            ast.Name,
            ast.Load,
            ast.Constant,
            ast.Call,
            ast.Raise,
            ast.Expr,
            ast.IfExp,
            ast.BoolOp,
            ast.And,
            ast.Or,
            ast.UnaryOp,
            ast.USub,
            ast.JoinedStr,
            ast.FormattedValue,
        )
        if any(not isinstance(node, allowed) for node in ast.walk(tree)):
            return False
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            return False
        fn = tree.body[0]
        if fn.name != "clamp" or fn.decorator_list or fn.args.defaults:
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                not isinstance(node.func, ast.Name)
                or node.func.id not in {"min", "max", "ValueError"}
            ):
                return False
        namespace = {"__builtins__": {"min": min, "max": max, "ValueError": ValueError}}
        exec(compile(tree, "<restricted-clamp>", "exec"), namespace)  # noqa: S102
        clamp = namespace["clamp"]
        if any(clamp(x, 0, 10) != max(0, min(x, 10)) for x in (-2, 0, 4, 10, 15)):
            return False
        try:
            clamp(1, 5, 2)
        except ValueError:
            return True
    except (SyntaxError, TypeError, ValueError, NameError, KeyError):
        return False
    return False


async def main(provider: str, model_name: str, model: BaseChatModel) -> bool:
    """Exercise every mode through production request preparation."""
    from novacode_cli.agents.caveman import CAVEMAN_STYLES, CavemanMiddleware
    from novacode_cli.agents.ponytail import PONYTAIL_LEVELS, ponytail_instructions

    modes = [
        ("off", "full"),
        *(("caveman", n) for n in CAVEMAN_STYLES),
        *(("ponytail", n) for n in PONYTAIL_LEVELS),
    ]
    semaphore = asyncio.Semaphore(2)
    base = (TEMPLATES_DIR / "response_style.jinja").read_text(encoding="utf-8")

    async def run(mode: str, level: str, case: str, prompt: str) -> dict:
        request = ModelRequest(
            model=model, messages=[HumanMessage(prompt)], system_message=SystemMessage(base)
        )
        if mode == "caveman":
            request = CavemanMiddleware.prepare(request, enabled=True, level=level)
        elif mode == "ponytail":
            request = CavemanMiddleware.prepare_instructions(request, ponytail_instructions(level))
        async with semaphore:
            started = time.monotonic()
            try:
                reply = await asyncio.wait_for(
                    model.ainvoke(
                        [request.system_message, *request.messages],
                        max_tokens=2048,
                    ),
                    timeout=60,
                )
                output = reply.text
                passed = (
                    check_code(output)
                    if case == "code"
                    else all(
                        fact in output
                        for fact in (
                            "src/auth.py:42",
                            "ValueError: token expired",
                            "3",
                            "uv run pytest tests/test_auth.py -q",
                        )
                    )
                    and bool(
                        re.search(
                            r"(?:do not|don't|never|no|avoid|without)[^\n.]{0,50}reset",
                            output,
                            re.IGNORECASE,
                        )
                    )
                )
                result = {
                    "mode": mode,
                    "level": level,
                    "case": case,
                    "passed": passed,
                    "usage": reply.usage_metadata or "unknown",
                    "output": output,
                    "visible_chars": len(output),
                }
            except Exception as exc:  # noqa: BLE001 -- record provider failures without secrets
                result = {
                    "mode": mode,
                    "level": level,
                    "case": case,
                    "passed": False,
                    "error": type(exc).__name__,
                    "usage": "unknown",
                }

            result["seconds"] = round(time.monotonic() - started, 2)
            print(f"{mode}/{level}/{case}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
            return result

    results = await asyncio.gather(
        *(
            run(mode, level, case, prompt)
            for mode, level in modes
            for case, prompt in (("facts", FACTS), ("code", CODE))
        )
    )
    destination = Path("test-results/compression-live.json")
    destination.parent.mkdir(exist_ok=True)
    await asyncio.to_thread(
        destination.write_text,
        json.dumps(
            {"provider": provider, "model": model_name, "results": results},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"{sum(r['passed'] for r in results)}/{len(results)} passed; {destination}")
    return all(r["passed"] for r in results)


if __name__ == "__main__":
    provider, model_name, _ = get_model_info()
    model = create_model_from_config(provider, model_name)
    if model is None:
        message = "Configured model unavailable; check Nova provider login."
        raise SystemExit(message)
    raise SystemExit(0 if asyncio.run(main(provider, model_name, model)) else 1)
