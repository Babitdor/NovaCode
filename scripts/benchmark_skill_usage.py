"""Opt-in live skill decisions with the saved DeepSeek model and harmless tools only."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage

from novacode_cli.backends import OptimizedFilesystemBackend
from novacode_cli.config.nova_config import NovaConfig
from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware

LIBRARY = {
    "interface-build": (
        "Build website interfaces. Not for reviewing existing sites or terminal apps."
    ),
    "interface-review": "Review existing website usability. Read-only; not for building sites.",
    "terminal-build": "Build Textual terminal apps. Not for websites.",
    "release-notes": "Write release notes. Use when release notes are explicitly requested.",
}
CASES = [
    ("Design a website interface for a bakery. Describe the implementation.", {"interface-build"}),
    ("Review an existing website's usability; give findings only.", {"interface-review"}),
    ("Build keyboard navigation for a Textual terminal app.", {"terminal-build"}),
    ("Please use $interface-review to inspect this website.", {"interface-review"}),
    ("Do not use any skills. Explain what a Python list is.", set()),
    ("What is 2 + 2?", set()),
    ("Explain what $terminal-build does, without activating it.", set()),
    (
        "Design a website interface and write release notes for it.",
        {"interface-build", "release-notes"},
    ),
]


async def measure(output: Path) -> bool:
    """Run bounded actual middleware/tool trajectories, writing no user configuration."""
    from novacode_cli.config.model_create import build_chat_model
    from novacode_cli.onboarding import load_secrets_into_env

    load_secrets_into_env()
    selected = NovaConfig().get_model_config()
    if not selected or selected["model"].lower().rsplit("/", 1)[-1] != "deepseek-v4-flash":
        message = "Saved default must be DeepSeek V4 Flash; no fallback is permitted."
        raise RuntimeError(message)
    model = build_chat_model(selected["provider"], selected["model"]).model_copy(
        update={"max_retries": 0, "request_timeout": 120, "temperature": 0, "max_tokens": 4096}
    )
    rows = []
    with tempfile.TemporaryDirectory(prefix="nova-skill-eval-") as directory:
        root = Path(directory)
        for name, description in LIBRARY.items():
            folder = root / name
            folder.mkdir()
            (folder / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {description}\n---\n"
                f"For this synthetic evaluation, include USED:{name} in your answer. "
                "Describe your work; no external actions are required.",
                encoding="utf-8",
            )
        # BM25 only: avoid extra models/downloads and use an isolated enabled library.
        with (
            patch("novacode_cli.skills.retrieval._model", return_value=None),
            patch("novacode_cli.skills.skills_prefs.effective_disabled", return_value=set()),
        ):
            for question, expected in CASES:
                middleware = RefreshingSkillsMiddleware(
                    backend=OptimizedFilesystemBackend(root_dir=root, virtual_mode=True),
                    sources=["/"],
                    watch_dirs=[root],
                )
                graph = create_agent(
                    model,
                    middleware=[middleware],
                    system_prompt="You are Nova. Help with the user's task using available tools.",
                )
                row = {"question": question, "expected": sorted(expected)}
                try:
                    result = await asyncio.wait_for(
                        graph.ainvoke(
                            {"messages": [HumanMessage(question)]}, {"recursion_limit": 12}
                        ),
                        timeout=180,
                    )
                    messages = result["messages"]
                    loaded = set()
                    for message in messages:
                        snapshot = message.additional_kwargs.get("skill", {})
                        if isinstance(message, ToolMessage) and message.status != "error":
                            artifact = message.artifact
                            if isinstance(artifact, dict):
                                snapshot = artifact.get("skill", {})
                        if snapshot.get("name"):
                            loaded.add(snapshot["name"])
                    answer = messages[-1].text
                    row.update(
                        loaded=sorted(loaded),
                        answer=answer,
                        passed=loaded == expected
                        and all(f"USED:{name}" in answer for name in expected),
                        trajectory=[m.model_dump(mode="json") for m in messages],
                    )
                except Exception as exc:  # noqa: BLE001 - record failed probes, no SDK details.
                    row.update(passed=False, error=type(exc).__name__)
                rows.append(row)
                output.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    output.write_text,
                    json.dumps({"model": selected, "results": rows}, indent=2),
                    encoding="utf-8",
                )
                print(f"{len(rows)}/{len(CASES)} passed={row['passed']}", flush=True)  # noqa: T201
    return all(row["passed"] for row in rows)


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(measure(Path("reports/skill-usage-deepseek.json"))) else 1)
