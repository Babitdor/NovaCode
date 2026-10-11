"""Model-free child used by the real JSONL transport rehearsal."""

import asyncio
import json
import os
from types import SimpleNamespace

from novacode_cli import ui_events as ev
from novacode_cli.sessions import worker as mod
from novacode_cli.sessions.gateway import request


async def stream(text, agent, assistant_id, state, **kwargs):
    if text == "initiate":
        result = await request("send", {"target": "b", "text": "explicit question"})
    else:
        result = await request("reply", {"id": text, "text": os.getcwd()})
    yield ev.AssistantMessage(
        text=json.dumps({"result": result, "cwd": os.getcwd(), "approve": state.auto_approve}),
        agent_name="Nova",
        agent_color="cyan",
    )
    yield ev.Done()


async def main():
    mod.iterate_agent_events = stream
    state = SimpleNamespace(
        session_id="fixture",
        thread_id="fixture",
        auto_approve=False,
        headless_out_fd=None,
        steering_instructions=[],
    )
    worker = mod.SessionWorker(agent=object(), assistant_id=None, session_state=state)

    async def save():
        pass

    worker._save = save
    try:
        return await worker.run()
    finally:
        worker._emit.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
