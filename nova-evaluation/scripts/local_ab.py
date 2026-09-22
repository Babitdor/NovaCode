"""Run Nova in-process on a local nova-task; report outcome, model calls, tokens.

No Docker, no Harbor: the task's ``environment/`` is staged into a temp dir that
becomes the workspace root, Nova's own agent (full middleware stack, the user's
configured model) runs the instruction with approvals off, then the task's own
pytest suite grades the result. Point ``--nova-root`` at two checkouts (e.g. a
worktree of the baseline commit and the working tree) to A/B a change.

    python local_ab.py --nova-root <checkout> --task fix-data-processing-bug

Only tasks whose files ship in ``environment/`` work here; ones that build
state in their Dockerfile (git-bisect-regression, ...) need the Harbor run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

TASKS_DIR = Path(__file__).resolve().parents[1] / "nova-tasks"


def _stage(task: str, root: Path) -> str:
    """Copy the task's files into ``root`` and return the adapted instruction."""
    shutil.copytree(
        TASKS_DIR / task / "environment", root, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("Dockerfile"),
    )
    # The tests hard-code the container layout: /app and a `python3` binary.
    for test in (root / "tests").rglob("*.py"):
        text = test.read_text(encoding="utf-8")
        text = text.replace('"/app', f'"{root.as_posix()}').replace('"python3"', "sys.executable")
        if "sys.executable" in text and "import sys" not in text:
            text = "import sys\n" + text
        test.write_text(text, encoding="utf-8")
    # In the instruction /app is the project root, which is "/" to Nova.
    instruction = (TASKS_DIR / task / "instruction.md").read_text(encoding="utf-8")
    return re.sub(r"/app/?", "/", instruction)


async def _run(nova_root: Path, task: str) -> dict:
    sys.path.insert(0, str(nova_root))
    from langchain_core.messages import AIMessage
    from novacode_cli.agents.core_agent import create_agent_with_config
    from novacode_cli.config.model_create import create_model
    from novacode_cli.memory.store import get_async_durable_store
    from novacode_cli.onboarding import load_secrets_into_env

    load_secrets_into_env()
    work = Path(tempfile.mkdtemp(prefix=f"nova-ab-{task}-"))
    instruction = _stage(task, work)

    agent, _ = create_agent_with_config(
        model=create_model(),
        assistant_id="nova-ab-eval",  # one memory dir for all eval runs
        tools=[],
        auto_approve=True,
        workspace_root=str(work),
        store=await get_async_durable_store(),  # as the Harbor wrapper does
    )
    started = time.monotonic()
    error = None
    config = {"recursion_limit": 200, "configurable": {"thread_id": uuid.uuid4().hex}}
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": instruction}]}, config=config
        )
        messages = result["messages"]
    except Exception as exc:  # noqa: BLE001 — a crashed run is a result, not a harness error
        error = f"{type(exc).__name__}: {exc}"
        # Still count what it spent: the checkpointer holds the partial run.
        messages = (await agent.aget_state(config)).values.get("messages", [])
    elapsed = time.monotonic() - started

    ai = [m for m in messages if isinstance(m, AIMessage)]
    usage = [m.usage_metadata or {} for m in ai]
    graded = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=work, capture_output=True, text=True, timeout=300, check=False,
    )
    return {
        "task": task,
        "nova_root": str(Path(sys.modules["novacode_cli"].__file__).parent.parent),
        "passed": graded.returncode == 0,
        "tests": (graded.stdout.strip().splitlines() or [""])[-1],
        "model_calls": len(ai),
        "tool_calls": sum(len(m.tool_calls or []) for m in ai),
        "wrote_todos": any(tc["name"] == "write_todos" for m in ai for tc in m.tool_calls or []),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "seconds": round(elapsed, 1),
        "error": error,
        "workdir": str(work),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nova-root", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_run(args.nova_root.resolve(), args.task))
    line = json.dumps(report)
    print(line)
    if args.out:
        with args.out.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


if __name__ == "__main__":
    main()
