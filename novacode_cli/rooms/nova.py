"""Nova room participant using the shared event loop and a private checkpoint."""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING

from novacode_cli.rooms.cli import INSTRUCTIONS, Turn, stop_process

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig

    from novacode_cli.rooms.runtime import RoomRuntime
    from novacode_cli.rooms.store import Record


def destination(cwd: Path, name: str) -> Path:
    """Reject traversal, symlinks outside the worktree, and coordination state."""
    path = (cwd / name).resolve()
    if not path.is_relative_to(cwd.resolve()):
        msg = "Path is outside the permitted project files"
        raise ValueError(msg)
    if {".git", ".nova"}.intersection(path.relative_to(cwd.resolve()).parts):
        msg = "Path is outside the permitted project files"
        raise ValueError(msg)
    return path


async def run(  # noqa: PLR0915 - One isolated graph with explicitly scoped tools.
    runtime: RoomRuntime, row: Record, prompt: str, cwd: Path, session: str | None
) -> Turn:
    """Use Nova's selected model with only room-bound, scope-specific tools."""
    from langchain.tools import tool
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from novacode_cli.agents.core_agent import create_agent_with_config
    from novacode_cli.config.model_create import create_model
    from novacode_cli.core.agent_loop import iterate_agent_events
    from novacode_cli.ui_events import (
        AssistantMessage,
        Cancelled,
        Error,
        InterruptRequest,
        ToolResult,
    )

    @tool
    async def read_project_file(path: str) -> str:
        """Read a UTF-8 project file (relative path)."""

        def read() -> str:
            return destination(cwd, path).read_text(encoding="utf-8")[:100_000]

        return await asyncio.to_thread(read)

    @tool
    async def list_project_files(path: str = ".") -> list[str]:
        """List one directory of project files."""

        def listing() -> list[str]:
            return [
                str(p.relative_to(cwd))
                for p in destination(cwd, path).iterdir()
                if p.name not in {".git", ".nova", ".venv", "node_modules"}
            ][:1000]

        return await asyncio.to_thread(listing)

    @tool
    async def read_room() -> dict:
        """Read shared context for this room only."""
        return await runtime.tool(row, "read", {})

    @tool
    async def read_board() -> dict:
        """Read published project tasks and memory, without other rooms' DMs."""
        return await runtime.tool(row, "board", {})

    @tool
    async def publish_note(kind: str, text: str) -> dict:
        """Publish a decision or finding under Nova's identity."""
        return await runtime.tool(row, "publish", {"kind": kind, "text": text})

    @tool
    async def publish_artifact(title: str, text: str) -> dict:
        """Publish a shared text artifact under Nova's identity in this room."""
        return await runtime.tool(row, "artifact", {"title": title, "text": text})

    @tool
    async def read_artifact(note: str) -> dict:
        """Read a verified artifact by note ID, restricted to this room."""
        return await runtime.tool(row, "read_artifact", {"note": note})

    @tool
    async def handoff(
        goal: str, completed: str, files: str, checks: str, questions: str, next_owner: str
    ) -> dict:
        """Hand off to an approved participant, preserving scope and worktree."""
        return await runtime.tool(
            row,
            "handoff",
            {
                "goal": goal,
                "completed": completed,
                "files": files,
                "checks": checks,
                "questions": questions,
                "next_owner": next_owner,
            },
        )

    @tool
    async def write_project_file(path: str, content: str) -> str:
        """Write a UTF-8 file inside this approved task worktree."""

        def write() -> None:
            target = destination(cwd, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        await asyncio.to_thread(write)
        return "Written " + path

    @tool
    async def run_pytest(path: str = "tests") -> str:
        """Run Python pytest for a relative test file/directory; no arbitrary shell."""
        target = await asyncio.to_thread(destination, cwd, path)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            str(target),
            "-q",
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def output() -> str:
            tail = b""
            if process.stdout:
                while data := await process.stdout.read(8192):
                    tail = (tail + data)[-50_000:]
            await process.wait()
            return f"Exit {process.returncode}\n" + tail.decode(errors="replace")

        try:
            return await asyncio.wait_for(output(), 120)
        finally:
            await stop_process(process)

    tools = [
        read_project_file,
        list_project_files,
        read_room,
        read_board,
        publish_note,
        publish_artifact,
        read_artifact,
        handoff,
    ]
    if row["task"] and not row.get("readonly"):
        tools += [write_project_file, run_pytest]
    thread = session or "room-" + uuid.uuid4().hex
    await runtime.db("session", row, thread)
    state = SimpleNamespace(
        thread_id=thread, active_goal=None, plan_mode_enabled=False, auto_approve=False
    )
    model = await asyncio.to_thread(create_model)
    path = str(runtime.store.directory / "nova-checkpoints.sqlite3")
    turn = Turn(session=thread)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph, _ = await asyncio.to_thread(
            create_agent_with_config,
            model,
            "nova-agent",
            tools,
            system_prompt=INSTRUCTIONS,
            checkpointer=saver,
            teammate_only=True,
        )
        config: RunnableConfig = {"configurable": {"thread_id": thread}}
        before = await graph.aget_state(config)
        old_ids = {message.id for message in before.values.get("messages", [])}
        async for event in iterate_agent_events(
            prompt, graph, "nova-agent", state, seen_message_ids=set(), skip_file_mentions=True
        ):
            if isinstance(event, AssistantMessage):
                turn.text += event.text + "\n"
            elif isinstance(event, Cancelled):
                raise asyncio.CancelledError
            elif isinstance(event, Error):
                turn.error = event.message
            elif isinstance(event, ToolResult):
                turn.details.append(event.full_output[:20_000])
                if event.is_error:
                    turn.error = "Tool failed; inspect details before resuming."
            elif isinstance(event, InterruptRequest):
                turn.error = "Unsupported approval request; task blocked for owner decision."
                if not event.future.done():
                    event.future.set_result({})
                break
        # Match the teammate adapter: some models checkpoint a complete answer
        # without streaming token events. Recover only this turn's final AI.
        from langchain_core.messages import AIMessage

        after = await graph.aget_state(config)
        fresh = [m for m in after.values.get("messages", []) if m.id not in old_ids]
        if not turn.text and not turn.error:
            turn.text = next(
                (m.text for m in reversed(fresh) if isinstance(m, AIMessage) and not m.tool_calls),
                "",
            )
        usage = [m.usage_metadata for m in fresh if isinstance(m, AIMessage) and m.usage_metadata]
        if usage:
            turn.usage = {
                key: sum(item.get(key, 0) for item in usage)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            }
        if not turn.text and not turn.error:
            turn.error = "Nova returned no answer; inspect model setup before resuming."
    return turn
