"""Opt-in paired inference check; reads saved model settings without saving changes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


async def measure(output: Path, selected_cases: list[str] | None, levels: list[str]) -> bool:
    from novacode_cli.onboarding import load_secrets_into_env

    load_secrets_into_env()
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage

    from novacode_cli.agents.caveman import CavemanMiddleware
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.prompts import render_template
    from novacode_cli.security.prompt_injection import GUARDRAIL_INSTRUCTIONS

    base_prompt = (
        render_template(
            "core_agent_system.jinja",
            platform=os.name,
            shell_name="none (text-only check)",
            path_sep=os.sep,
            shell_notes=[],
            has_async_agents=False,
        )
        + "\n\n"
        + GUARDRAIL_INSTRUCTIONS
    )

    config = NovaConfig().get_model_config()
    if not config:
        raise RuntimeError("No saved main model is configured")
    model = build_chat_model(config["provider"], config["model"])
    updates = {}
    if hasattr(model, "max_retries"):
        updates["max_retries"] = 0
    if hasattr(model, "request_timeout"):
        updates["request_timeout"] = 90
    model = model.model_copy(update=updates)
    cases = {
        "mutable_default": (
            "Explain why def add_item(item, bucket=[]): bucket.append(item); return bucket "
            "shares state between calls. Show a complete corrected function and explain "
            "the tradeoff. Keep add_item exact. Do not call tools."
        ),
        "clarity": (
            "I plan to run rm -rf ./data. Explain what I should check first and whether "
            "recovery is guaranteed. Give no executable command. Preserve rm -rf ./data "
            "verbatim when referring to it. Do not call tools."
        ),
        "portuguese": (
            "Explique em português como um pool de conexões reduz latência e quais "
            "limites configurar. Preserve ECONNRESET e o timeout de 30 segundos. "
            "Não use ferramentas."
        ),
    }
    semaphore = asyncio.Semaphore(2)

    async def call(case: str, level: str) -> dict:
        request = CavemanMiddleware.prepare(
            ModelRequest(
                model=model,
                system_message=SystemMessage(content=base_prompt),
                messages=[HumanMessage(content=cases[case])],
            ),
            enabled=level != "off",
            level=level,
        )
        messages = [request.system_message, *request.messages]
        async with semaphore:
            try:
                response = await asyncio.wait_for(
                    model.ainvoke(messages, max_tokens=1200), timeout=100
                )
                return {
                    "case": case,
                    "level": level,
                    "text": response.text,
                    "usage": response.usage_metadata,
                    "finish_reason": response.response_metadata.get("finish_reason"),
                }
            except Exception as exc:  # noqa: BLE001 -- record bounded failures, never credentials.
                return {"case": case, "level": level, "error_type": type(exc).__name__}

    results = await asyncio.gather(
        *(call(case, level) for case in (selected_cases or cases) for level in levels)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "provider": config["provider"],
                "model": config["model"],
                "prompt": "packaged core prompt + security boundaries + selected style",
                "results": results,
                "scope": "Synthetic paired probes; correctness needs review; no universal savings claim.",
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for row in results:
        print(json.dumps({k: v for k, v in row.items() if k != "text"}, ensure_ascii=False))
    print(f"Responses and provider usage saved to {output}")
    return not any("error_type" in row for row in results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize nine real model calls")
    parser.add_argument(
        "--case", action="append", choices=["mutable_default", "clarity", "portuguese"]
    )
    parser.add_argument(
        "--levels", nargs="+", choices=["off", "full", "ultra"], default=["off", "full", "ultra"]
    )
    parser.add_argument("--output", type=Path, default=Path("reports/caveman-style-live.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to make nine inference calls using your saved main model")
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    if not asyncio.run(measure(args.output, args.case, args.levels)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
