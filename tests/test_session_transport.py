"""Two real processes in different projects use the production JSONL pipes."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from novacode_cli.sessions.messaging import SessionBroker, SessionInfo
from novacode_cli.sessions.supervisor import ChildSession, SessionSupervisor


async def test_shutdown_closes_stdin_before_waiting_for_child(tmp_path):
    class Pipe:
        closed = False

        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            self.closed = True

    pipe = Pipe()

    async def wait():
        assert pipe.closed, "EOF must unblock the child's stdin reader before waiting for exit"
        return 0

    child = ChildSession("a", "a", tmp_path)
    child.proc = SimpleNamespace(stdin=pipe, returncode=None, wait=wait)
    supervisor = SessionSupervisor(AsyncMock())
    supervisor._children["a"] = child
    assert await supervisor.close("a") is child
    assert pipe.closed


@pytest.mark.timeout(60)
async def test_two_project_processes_route_owned_requests_and_close_pipes(tmp_path):
    bus = SessionBroker()
    frames = []

    async def receive(sid, frame):
        frames.append((sid, frame))
        if frame.get("t") != "session_request":
            return
        payload = frame["payload"]
        if frame["method"] == "send":
            message = bus.send(sid, payload["target"], payload["text"])
            bus.start(message.id)
            result = {"id": message.id}
            await supervisor.send_prompt(
                message.recipient, message.id, auto_approve=False, prompt_id=f"peer-{message.id}"
            )
        elif frame["method"] == "reply":
            message = bus.reply(sid, payload["id"], payload["text"])
            result = {"id": message.id, "status": message.status}
        else:
            result = {"error": "unsupported"}
        await supervisor._send(
            supervisor.get(sid), {"t": "session_reply", "id": frame["id"], "result": result}
        )

    supervisor = SessionSupervisor(receive)
    projects = {sid: tmp_path / ("项目-" + sid) for sid in ("a", "b")}
    fixture = Path(__file__).parent / "helpers" / "session_rpc_child.py"
    try:
        for sid, project in projects.items():
            project.mkdir()
            child = ChildSession(sid, sid, project)
            child.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                str(fixture.resolve()),
                cwd=project,
                env=supervisor._child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            supervisor._children[sid] = child
            child.tasks = [
                asyncio.create_task(supervisor._read_stdout(child)),
                asyncio.create_task(supervisor._read_stderr(child)),
                asyncio.create_task(supervisor._on_exit(child)),
            ]
        bus.update_sessions(
            [SessionInfo(sid, sid, str(project)) for sid, project in projects.items()]
        )
        async with asyncio.timeout(40):
            while sum(frame.get("t") == "ready" for _, frame in frames) < 2:
                await asyncio.sleep(0.02)
            assert await supervisor.send_prompt("a", "initiate", auto_approve=False) == "p1"
            while sum(frame.get("t") == "turn_done" for _, frame in frames) < 2:
                await asyncio.sleep(0.02)
        result = bus.read("a")[0]
        assert result["sender"] == "a" and result["recipient"] == "b"
        assert result["text"] == "explicit question"
        assert Path(result["response"]) == projects["b"]
        from novacode_cli.sessions.protocol import decode_event
        from novacode_cli.ui_events import AssistantMessage

        answers = [
            (sid, json.loads(event.text))
            for sid, frame in frames
            if frame.get("t") == "ev" and isinstance(event := decode_event(frame), AssistantMessage)
        ]
        assert len(answers) == 2
        assert all(
            Path(answer["cwd"]) == projects[sid] and answer["approve"] is False
            for sid, answer in answers
        )
    finally:
        children = list(supervisor.list())
        await supervisor.close_all(timeout=3)
        assert all(child.proc.returncode == 0 for child in children)
        assert all(task.done() for child in children for task in child.tasks)
