"""Offline acceptance tests for project room boundaries and restart behavior."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from textual.app import App
from textual.widgets import Input, OptionList, TextArea

from novacode_cli.rooms import cli, workspace
from novacode_cli.rooms.runtime import RoomRuntime
from novacode_cli.rooms.store import RoomStore, canonical_project, recipients
from novacode_cli.tui.rooms import RoomsScreen


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(  # noqa: S603 - Fixed Git init in a temporary project.
        ["git", "init", str(tmp_path)],  # noqa: S607 - Fixed test executable.
        check=True,
        capture_output=True,
    )
    workspace.git(tmp_path, "config", "user.email", "rooms@example.test")
    workspace.git(tmp_path, "config", "user.name", "Room tests")
    (tmp_path / "app.py").write_text("x = 1\n")
    workspace.git(tmp_path, "add", "app.py")
    workspace.git(tmp_path, "commit", "-m", "base")
    monkeypatch.setattr(
        "novacode_cli.sessions.worktree.worktrees_root",
        lambda: tmp_path.parent / (tmp_path.name + "-trees"),
    )
    return tmp_path


@pytest.mark.parametrize(
    ("text", "room", "expected"),
    [
        ("@claude inspect @app.py then @codex", "general", ["claude", "codex"]),
        ("@codex.py @claude/file @nova.json", "general", ["nova"]),
        ("email@claude inspect @app.py", "general", ["nova"]),
        ("@claude hello @claude", "general", ["claude"]),
        ("@claude implement", "dm:codex", ["codex"]),
    ],
)
def test_routing(text: str, room: str, expected: list[str]):
    assert recipients(text, room) == expected


def test_history_authorship_export_and_worktree_root(project: Path):
    store = RoomStore(project)
    store.message("general", "claude", "Public finding")
    store.message("dm:codex", "codex", "Private finding")
    with pytest.raises(ValueError, match="unauthorized"):
        store.history("dm:codex", "claude")
    note = store.publish("general", "claude", "finding", {"text": "Public finding"})
    private = store.publish("dm:codex", "codex", "finding", {"text": "Private finding"})
    output = project / "notes.md"
    with pytest.raises(ValueError, match="channel"):
        store.export([private], output)
    assert not output.exists()
    store.export([note], output)
    assert "Public finding" in output.read_text()
    assert "Private finding" not in output.read_text()
    with pytest.raises(FileExistsError):
        store.export([note], output)
    task = store.propose("general", "Fix x", ["codex"])
    task = workspace.prepare(store, task["id"])
    assert canonical_project(Path(task["worktree"])) == project
    linked = RoomStore(Path(task["worktree"]))
    assert linked.history("general")[0]["author"] == "claude"
    assert "workspace" not in workspace.git(project, "status", "--porcelain")
    linked.close()
    store.close()


def test_concurrent_delivery_and_claims(project: Path):
    stores = [RoomStore(project), RoomStore(project)]

    def deliver(i: int) -> int:
        return stores[i % 2].message("general", "owner", f"message {i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(deliver, range(60)))
    assert len(set(ids)) == 60
    assert len(stores[0].history("general")) == 60
    jobs = [
        stores[i % 2].enqueue("general", a, "Discuss", "runtime")
        for i, a in enumerate(["nova", "claude", "codex", "nova"])
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda i: stores[i % 2].claim(jobs[i]), range(4)))
    assert sum(c is not None for c in claims) == 2
    for store in stores:
        store.close()


def test_task_approval_budget_and_integration_preserve_work(project: Path):
    store = RoomStore(project)
    task = store.propose("general", "Fix x", ["codex", "claude"])
    with pytest.raises(ValueError, match="not approved"):
        store.enqueue("general", "codex", "Write now", "r", task["id"])
    task = workspace.prepare(store, task["id"])
    path = Path(task["worktree"])
    (path / "app.py").write_text("x = 2\n")
    with store.transaction():
        task.update(status="queued")
        store.put_task(task)
    for _ in range(6):
        job = store.enqueue("general", "codex", "Fix x", "r", task["id"])
        assert store.claim(job)
        store.finish(job, "finished")
    assert store.task(task["id"])["turns"] == 6
    with pytest.raises(ValueError, match="exhausted"):
        store.enqueue("general", "claude", "Review", "r", task["id"])
    (project / "owner.txt").write_text("Owner work")
    with pytest.raises(ValueError, match="uncommitted"):
        workspace.integrate(store, task["id"])
    assert (path / "app.py").read_text() == "x = 2\n"
    assert (project / "app.py").read_text() == "x = 1\n"
    # Preserve owner files, commit them to deliberately change the base.
    workspace.git(project, "add", "owner.txt")
    workspace.git(project, "commit", "-m", "owner change")
    with pytest.raises(ValueError, match="base changed"):
        workspace.integrate(store, task["id"])
    assert path.exists()
    store.close()


def test_successful_integration_keeps_worktree(project: Path):
    store = RoomStore(project)
    task = workspace.prepare(store, store.propose("general", "Fix x", ["codex"])["id"])
    path = Path(task["worktree"])
    (path / "new.py").write_text("y = 3\n")
    (path / "new ä file.py").write_text("z = 4\n", encoding="utf-8")
    assert "+z = 4" in workspace.diff(store, task["id"])
    with store.transaction():
        task["status"] = "finished"
        store.put_task(task)
    workspace.integrate(store, task["id"])
    assert (project / "new.py").read_text() == "y = 3\n"
    assert path.exists()
    assert store.task(task["id"])["status"] == "integrated"
    store.close()


def test_restart_pauses_dead_but_preserves_live_runtime(project: Path):
    store = RoomStore(project)
    store.register("live")
    job = store.enqueue("general", "claude", "Discuss", "live")
    store.claim(job)
    store.register("second")
    assert store.rows("SELECT status FROM jobs WHERE id=?", (job,))[0]["status"] == "working"
    with store.transaction():
        store.db.execute("UPDATE runtimes SET born=-1 WHERE id='live'")
    store.register("third")
    assert store.rows("SELECT status FROM jobs WHERE id=?", (job,))[0]["status"] == "paused"
    store.close()


async def test_fake_stream_routing_sessions_cancel_and_bound_tools(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake(
        agent: str, prompt: str, _cwd: Path, session: str | None, **kwargs: Any
    ) -> cli.Turn:
        calls.append((agent, json.loads(prompt), session))
        kwargs["on_session"]("native-" + agent)
        if "wait" in prompt:
            started.set()
            await release.wait()
        return cli.Turn(text="Reply", session="native-" + agent, usage={"input_tokens": 5})

    monkeypatch.setattr(cli, "run", fake)
    store = RoomStore(project)
    runtime = RoomRuntime(store)
    await runtime.open()
    try:
        await runtime.send("dm:codex", "private")
        await asyncio.sleep(0.3)
        await runtime.send("general", "@claude public")
        await asyncio.sleep(0.3)
        assert calls[0][0] == "codex"
        assert "private" not in json.dumps(calls[1][1])
        await runtime.send("general", "@claude followup")
        await asyncio.sleep(0.3)
        assert calls[-1][2] == "native-claude"
        await runtime.send("general", "@codex wait")
        await asyncio.wait_for(started.wait(), 2)
        job = store.rows("SELECT * FROM jobs WHERE status='working'")[0]
        await runtime.tool(
            job, "publish", {"kind": "finding", "text": "Evidence", "author": "owner"}
        )
        assert store.rows("SELECT author FROM notes")[0]["author"] == "codex"
        with pytest.raises(ValueError, match="owner controls"):
            await runtime.tool(job, "start", {})
        await runtime.cancel(job["id"])
        assert (
            store.rows("SELECT status FROM jobs WHERE id=?", (job["id"],))[0]["status"] == "paused"
        )
        with pytest.raises(ValueError, match="not running"):
            await runtime.tool(job, "read", {})
    finally:
        await runtime.close()


def test_cli_argv_native_permissions_and_usage(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli.claude_plan, "executable", lambda: "claude")
    monkeypatch.setattr(cli.codex_plan, "executable", lambda: "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    argv, env = cli.command("codex", "hello", "session-1", implementation=False, bridge={})
    assert argv[:4] == ["codex", "exec", "resume", "session-1"]
    assert 'sandbox_mode="read-only"' in argv
    assert "--skip-git-repo-check" in argv
    assert "OPENAI_API_KEY" not in env
    argv, _ = cli.command("claude", "hello", "session-2", implementation=True, bridge={})
    assert "--resume" in argv
    assert "--strict-mcp-config" in argv
    assert "--dangerously-skip-permissions" not in argv
    turn = cli.Turn()
    ids = []
    cli.consume(turn, "codex", {"type": "thread.started", "thread_id": "id"}, ids.append)
    cli.consume(
        turn,
        "codex",
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer"}},
        ids.append,
    )
    cli.consume(turn, "codex", {"type": "turn.completed", "usage": {"input_tokens": 4}}, ids.append)
    assert turn.text == "Answer"
    assert turn.usage == {"input_tokens": 4}
    assert ids == ["id"]


def test_codex_can_recover_from_failed_checks_but_denied_approval_blocks():
    turn = cli.Turn()
    cli.consume(
        turn,
        "codex",
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "exit_code": 1,
                "status": "completed",
                "aggregated_output": "A regression test failed; investigate and fix it.",
            },
        },
        lambda _: None,
    )
    cli.consume(
        turn,
        "codex",
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Fixed the regression; tests now pass."},
        },
        lambda _: None,
    )
    assert turn.error is None
    cli.consume(
        turn,
        "codex",
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "status": "failed",
                "exit_code": 1,
                "aggregated_output": "Permission denied: this operation requires approval.",
            },
        },
        lambda _: None,
    )
    assert "approval" in turn.error


@pytest.mark.timeout(40)
async def test_sidebar_drafts_board_narrow_and_no_automatic_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def no_call(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Opening rooms must not run a CLI")

    monkeypatch.setattr(cli, "run", no_call)
    app = App()
    async with app.run_test(size=(100, 35)) as pilot:
        screen = RoomsScreen(tmp_path, "test-viewer")
        await app.push_screen(screen)
        await pilot.pause()
        composer = screen.query_one("#room-composer", Input)
        composer.value = "unfinished draft"
        await pilot.pause()
        await screen.switch("dm:claude")
        composer.value = "private draft"
        await pilot.pause()
        await screen.switch("general")
        assert composer.value == "unfinished draft"
        await screen.switch("__board")
        assert screen.query_one("#room-goal", TextArea)
        assert not screen.query_one("#room-compose-row").display
        await pilot.resize_terminal(55, 20)
        await pilot.pause()
        assert not screen.query_one("#room-sidebar").display
        screen.action_sidebar()
        assert screen.query_one("#room-sidebar").display
        await screen.action_sessions()


async def test_bounded_handoffs_retain_scope_worktree_and_pause(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    store = RoomStore(project)
    runtime = RoomRuntime(store)
    await runtime.open()

    async def fake(
        agent: str, _prompt: str, cwd: Path, _session: str | None, **_kwargs: Any
    ) -> cli.Turn:
        row = store.rows("SELECT * FROM jobs WHERE status='working'")[0]
        task = store.task(row["task"])
        payload = {
            "goal": task["goal"],
            "completed": "Inspected app.py",
            "files": "app.py",
            "checks": "pytest",
            "questions": "None",
            "next_owner": "claude" if agent == "codex" else "codex",
        }
        result = await runtime.tool(row, "handoff", payload)
        with pytest.raises(ValueError, match="pending handoff"):
            await runtime.tool(row, "handoff", payload)
        assert cwd == Path(task["worktree"])
        return cli.Turn(text=f"Handed off: {result['status']}")

    monkeypatch.setattr(cli, "run", fake)
    task = workspace.prepare(store, store.propose("general", "Fix x", ["codex", "claude"])["id"])
    try:
        await runtime.start(task["id"])
        with pytest.raises(ValueError, match="Prepare and review"):
            await runtime.start(task["id"])
        for _ in range(40):
            if store.task(task["id"])["status"] == "paused":
                break
            await asyncio.sleep(0.1)
        assert store.task(task["id"])["turns"] == 6
        assert store.task(task["id"])["status"] == "paused"
        assert len(store.rows("SELECT * FROM notes WHERE kind='handoff'")) == 6
        paused = store.rows("SELECT * FROM jobs WHERE status='paused'")[0]
        with pytest.raises(ValueError, match="Extend"):
            await runtime.resume(paused["id"])
        # Pause scheduler so the owner extension can be inspected before execution.
        runtime.pump.cancel()
        await asyncio.gather(runtime.pump, return_exceptions=True)
        await runtime.resume(paused["id"], extend=6)
        assert store.task(task["id"])["limit"] == 12
        assert await asyncio.to_thread(Path(store.task(task["id"])["worktree"]).exists)
    finally:
        await runtime.close()


async def test_actual_subprocess_stream_drains_stderr_and_reaps_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    script = (
        "import sys,json; sys.stderr.write('x'*200000); sys.stderr.flush(); "
        "print(json.dumps({'type':'thread.started','thread_id':'native'}),flush=True); "
        "print(json.dumps({'type':'item.completed',"
        "'item':{'type':'agent_message','text':'Answer'}}),flush=True); "
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7}}),flush=True)"
    )
    monkeypatch.setattr(cli, "command", lambda *_a, **_kw: ([sys.executable, "-c", script], {}))
    ids = []
    turn = await cli.run(
        "codex", "hello", tmp_path, None, implementation=False, bridge={}, on_session=ids.append
    )
    assert turn.text == "Answer"
    assert turn.error is None
    assert ids == ["native"]
    assert turn.usage == {"input_tokens": 7}
    started = asyncio.Event()
    script = (
        "import json,time; "
        "print(json.dumps({'type':'thread.started','thread_id':'waiting'}),flush=True); "
        "time.sleep(60)"
    )
    task = asyncio.create_task(
        cli.run(
            "codex",
            "wait",
            tmp_path,
            None,
            implementation=False,
            bridge={},
            on_session=lambda _: started.set(),
        )
    )
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)


async def test_bridge_rejects_forged_token_and_room_access(tmp_path: Path):
    store = RoomStore(tmp_path)
    runtime = RoomRuntime(store)
    await runtime.open()
    try:
        job = store.enqueue("dm:claude", "claude", "Discuss", runtime.id)
        row = store.claim(job)
        runtime.bindings["bound-token"] = row
        store.message("dm:codex", "codex", "Secret")
        port = runtime.server.sockets[0].getsockname()[1]

        async def invoke(token: str, operation: str, payload: dict) -> dict:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                json.dumps({"token": token, "operation": operation, "payload": payload}).encode()
                + b"\n"
            )
            await writer.drain()
            result = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return result

        assert "error" in await invoke("forged-token", "read", {})
        result = await invoke("bound-token", "read", {"room": "dm:codex", "author": "owner"})
        assert "Secret" not in json.dumps(result)
        assert "error" in await invoke("bound-token", "integrate", {})
        result = await invoke(
            "bound-token", "publish", {"kind": "finding", "text": "Hello", "author": "codex"}
        )
        assert "id" in result
        assert store.rows("SELECT author FROM notes")[0]["author"] == "claude"
    finally:
        await runtime.close()


def test_nova_paths_inside_default_hidden_worktrees(tmp_path: Path):
    from novacode_cli.rooms.nova import destination

    path = tmp_path / ".nova" / "worktrees" / "task"
    path.mkdir(parents=True)
    assert destination(path, "app.py") == path / "app.py"
    for name in ("../outside.txt", ".git", ".nova/workspace/rooms.sqlite3"):
        with pytest.raises(ValueError, match="outside"):
            destination(path, name)


@pytest.mark.timeout(40)
async def test_reopen_draft_unread_file_completion_and_free_scrolling(tmp_path: Path):
    (tmp_path / "app.py").write_text("x=1")
    store = RoomStore(tmp_path)
    store.view("viewer", "general", "saved draft")
    store.message("dm:codex", "codex", "Unread")
    for i in range(35):
        store.message("general", "claude", f"Line {i}\nDetails\nMore")
    store.close()
    app = App()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = RoomsScreen(tmp_path, "viewer")
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#room-composer", Input).value == "saved draft"
        assert "Codex (1)" in screen.navigation
        composer = screen.query_one("#room-composer", Input)
        composer.value = "@c"
        for _ in range(20):
            await pilot.pause()
            if screen.completion_value == "@c":
                break
        assert "claude" in screen.completions
        assert "codex" in screen.completions
        composer.value = "@app"
        for _ in range(20):
            await pilot.pause()
            if screen.completion_value == "@app":
                break
        assert "app.py" in screen.completions
        scroll = screen.query_one("#room-transcript")
        scroll.scroll_home(animate=False)
        await pilot.pause()
        await screen.runtime.db("message", "general", "claude", "Latest")
        await screen.poll()
        await pilot.pause()
        assert scroll.scroll_y == 0
        await screen.action_sessions()


@pytest.mark.timeout(40)
async def test_stale_mention_selection_cannot_replace_new_input(tmp_path: Path):
    app = App()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = RoomsScreen(tmp_path, "viewer")
        await app.push_screen(screen)
        await pilot.pause()
        composer = screen.query_one("#room-composer", Input)
        composer.value = "@c"
        for _ in range(20):
            await pilot.pause()
            if "claude" in screen.completions:
                break
        assert "claude" in screen.completions
        listing = screen.query_one("#room-suggestions", OptionList)
        selected = OptionList.OptionSelected(listing, listing.get_option_at_index(0), 0)
        composer.value = "@app"
        screen.complete(selected)
        assert composer.value == "@app"
        await screen.action_sessions()


@pytest.mark.timeout(40)
async def test_nova_uses_canonical_loop_private_checkpoint_and_readonly_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    seen: set[str] = set()

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools: list, **_kwargs: object) -> Model:
            seen.update(t.name for t in tools)
            return self

    model = Model(responses=[AIMessage(content="Nova room answer")])
    monkeypatch.setattr("novacode_cli.config.model_create.create_model", lambda: model)
    runtime = RoomRuntime(RoomStore(tmp_path))
    await runtime.open()
    try:
        await runtime.send("general", "@nova discuss")
        for _ in range(80):
            rows = runtime.store.rows("SELECT * FROM jobs")
            if rows and rows[0]["status"] in {"finished", "blocked"}:
                break
            await asyncio.sleep(0.1)
        assert rows[0]["status"] == "finished", runtime.store.history("general")
        assert runtime.store.history("general")[-1]["author"] == "nova"
        assert "Nova room answer" in runtime.store.history("general")[-1]["text"]
        assert runtime.store.session(rows[0]).startswith("room-")
        assert {"read_project_file", "publish_note", "handoff"} <= seen
        assert "write_project_file" not in seen
        assert "run_pytest" not in seen
    finally:
        await runtime.close()


def test_artifacts_are_verified_room_scoped_and_context_is_bounded(tmp_path: Path):
    store = RoomStore(tmp_path)
    try:
        note = store.artifact("dm:claude", "claude", "Private report", "Private body")
        assert store.read_artifact("dm:claude", "claude", note) == "Private body"
        with pytest.raises(ValueError, match="unauthorized"):
            store.read_artifact("dm:codex", "codex", note)
        store.create_channel("decisions")
        public = store.artifact("decisions", "codex", "Shared report", "Public\r\nbody")
        store.propose("dm:claude", "Private goal", ["claude"])
        assert store.read_artifact("general", "claude", public) == "Public\r\nbody"
        assert "Shared report" in json.dumps(store.board("general", "claude"))
        assert "Private report" not in json.dumps(store.board("general", "claude"))
        assert "Private goal" not in json.dumps(store.board("general", "claude"))
        for _ in range(5):
            store.message("general", "codex", "中" * 90_000)
            store.publish("general", "codex", "finding", {"text": "中" * 90_000})
        context = store.context("general", "claude")
        assert sum(len(m["text"]) for m in context["messages"]) <= 40_000
        assert sum(len(n["body"]) for n in context["notes"]) <= 20_000
        assert len(json.dumps(context).encode()) < 1_000_000
        body = json.loads(store.rows("SELECT body FROM notes WHERE id=?", (note,))[0]["body"])
        Path(body["artifact"]["path"]).write_text("tampered")
        with pytest.raises(ValueError, match="verification"):
            store.read_artifact("dm:claude", "claude", note)
    finally:
        store.close()


def test_non_git_projects_allow_discussion_and_notes_but_refuse_implementation(tmp_path: Path):
    store = RoomStore(tmp_path)
    try:
        store.message("general", "owner", "Discuss architecture")
        store.publish("general", "nova", "decision", {"text": "Use SQLite"})
        task = store.propose("general", "Implement", ["nova"])
        with pytest.raises(ValueError, match="requires Git"):
            workspace.prepare(store, task["id"])
        assert store.history("general")[0]["text"] == "Discuss architecture"
    finally:
        store.close()


@pytest.mark.timeout(40)
async def test_task_card_start_and_review_are_wired_to_real_runtime(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = []

    async def fake(
        agent: str, _prompt: str, cwd: Path, _session: str | None, **kwargs: Any
    ) -> cli.Turn:
        calls.append((agent, kwargs["implementation"]))
        if kwargs["implementation"]:
            await asyncio.to_thread((cwd / "app.py").write_text, "x = 2\n")
        return cli.Turn(text="Focused checks passed", session="native-task")

    monkeypatch.setattr(cli, "run", fake)
    app = App()
    async with app.run_test(size=(110, 42)) as pilot:
        screen = RoomsScreen(project, "owner")
        await app.push_screen(screen)
        await pilot.pause()
        await screen.switch("__board")
        screen.query_one("#room-goal", TextArea).load_text("Fix x")
        screen.query_one("#room-participants", Input).value = "codex,claude"
        await screen.owner_action("room-propose")
        task = json.loads(screen.runtime.store.rows("SELECT body FROM tasks")[0]["body"])
        assert calls == []
        # A timer may be finishing an older snapshot while preparation returns.
        # Wait for that refresh, then request the current authoritative card.
        while screen.refreshing:
            await pilot.pause()
        await screen.poll()
        button = screen.query_one("#start-" + task["id"])
        button.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#start-" + task["id"])
        await app.workers.wait_for_complete()
        for _ in range(25):
            if screen.runtime.store.task(task["id"])["status"] == "finished":
                break
            await asyncio.sleep(0.1)
        assert calls == [("codex", True)]
        assert (await asyncio.to_thread((project / "app.py").read_text)) == "x = 1\n"
        await screen.owner_action("review-" + task["id"])
        for _ in range(25):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.1)
        assert calls[-1] == ("claude", False)
        await screen.action_sessions()


@pytest.mark.timeout(40)
async def test_closing_app_pauses_foreground_room_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def fake(*_args: object, **_kwargs: object) -> cli.Turn:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return cli.Turn()

    monkeypatch.setattr(cli, "run", fake)
    app = App()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = RoomsScreen(tmp_path, "owner")
        await app.push_screen(screen)
        await pilot.pause()
        runtime = screen.runtime
        await runtime.send("general", "@codex investigate")
        await asyncio.wait_for(started.wait(), 3)
    assert runtime.closed
    assert cancelled.is_set()
    store = RoomStore(tmp_path)
    try:
        assert store.rows("SELECT status FROM jobs")[0]["status"] == "paused"
    finally:
        store.close()


async def test_cancel_between_claim_and_worker_creation_never_launches_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runtime = RoomRuntime(RoomStore(tmp_path))
    claimed, release = asyncio.Event(), asyncio.Event()
    original = runtime.db
    calls = []

    async def gate(method: str, *args: Any, **kwargs: Any) -> object:
        result = await original(method, *args, **kwargs)
        if method == "claim" and result:
            claimed.set()
            await release.wait()
        return result

    async def fake(*_args: object, **_kwargs: object) -> cli.Turn:
        calls.append("launched")
        return cli.Turn(text="Unexpected")

    monkeypatch.setattr(runtime, "db", gate)
    monkeypatch.setattr(cli, "run", fake)
    await runtime.open()
    try:
        await runtime.send("general", "@codex inspect")
        await asyncio.wait_for(claimed.wait(), 2)
        job = runtime.store.rows("SELECT id FROM jobs")[0]["id"]
        await runtime.cancel(job)
        release.set()
        await asyncio.sleep(0.3)
        assert calls == []
        assert runtime.store.rows("SELECT status FROM jobs")[0]["status"] == "paused"
    finally:
        release.set()
        await runtime.close()


async def test_transient_database_contention_does_not_kill_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import sqlite3

    runtime = RoomRuntime(RoomStore(tmp_path))
    original = runtime.db
    failures = []

    async def gate(method: str, *args: Any, **kwargs: Any) -> object:
        if method == "claim" and not failures:
            failures.append("locked")
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)
        return await original(method, *args, **kwargs)

    async def fake(*_args: object, **_kwargs: object) -> cli.Turn:
        return cli.Turn(text="Completed once")

    monkeypatch.setattr(runtime, "db", gate)
    monkeypatch.setattr(cli, "run", fake)
    await runtime.open()
    try:
        await runtime.send("general", "@codex inspect")
        for _ in range(30):
            history = runtime.store.history("general")
            if history[-1]["author"] == "codex":
                break
            await asyncio.sleep(0.1)
        assert failures == ["locked"]
        assert [m["text"] for m in history].count("Completed once") == 1
        assert not runtime.pump.done()
    finally:
        await runtime.close()


@settings(
    max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
)
@given(st.text(min_size=1, max_size=120).filter(lambda value: bool(value.strip())))
def test_unicode_messages_and_artifacts_round_trip(tmp_path: Path, text: str):
    store = RoomStore(tmp_path)
    key = store.message("general", "codex", text)
    note = store.artifact("general", "codex", "Generated report", text)
    store.close()
    reopened = RoomStore(tmp_path)
    try:
        assert reopened.rows("SELECT text FROM messages WHERE id=?", (key,))[0]["text"] == text
        assert reopened.read_artifact("general", "claude", note) == text
    finally:
        reopened.close()


@pytest.mark.timeout(40)
async def test_real_stdio_mcp_bridge_exposes_only_bound_participant_tools(tmp_path: Path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    runtime = RoomRuntime(RoomStore(tmp_path))
    await runtime.open()
    job = runtime.store.enqueue("general", "claude", "Discuss", runtime.id)
    row = runtime.store.claim(job)
    runtime.bindings["test-capability"] = row
    port = runtime.server.sockets[0].getsockname()[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "novacode_cli.rooms.bridge"],
        env={"NOVA_ROOMS_PORT": str(port), "NOVA_ROOMS_TOKEN": "test-capability"},
    )
    try:
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
            assert {t.name for t in listing.tools} == {
                "read_room",
                "read_board",
                "publish_note",
                "publish_artifact",
                "read_artifact",
                "handoff",
            }
            result = await session.call_tool(
                "publish_artifact", {"title": "MCP report", "text": "Unicode 🚀\r\n" * 1000}
            )
            assert not result.isError
            note = runtime.store.rows("SELECT * FROM notes")[0]
            assert note["author"] == "claude"
            assert (
                runtime.store.read_artifact("general", "codex", note["id"])
                == "Unicode 🚀\r\n" * 1000
            )
            result = await session.call_tool("read_board", {})
            assert not result.isError
            assert "MCP report" in str(result.content)
    finally:
        await runtime.close()


@pytest.mark.timeout(40)
async def test_nova_task_tools_edit_only_the_approved_worktree(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, _tools: list, **_kwargs: object) -> Model:
            return self

    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_project_file",
                        "id": "write-1",
                        "args": {"path": "nested/new.py", "content": "x = 2\n"},
                    }
                ],
            ),
            AIMessage(content="Worktree change ready for review."),
        ]
    )
    monkeypatch.setattr("novacode_cli.config.model_create.create_model", lambda: model)
    runtime = RoomRuntime(RoomStore(project))
    await runtime.open()
    task = workspace.prepare(
        runtime.store, runtime.store.propose("general", "Fix x", ["nova"])["id"]
    )
    try:
        await runtime.start(task["id"])
        for _ in range(80):
            current = runtime.store.task(task["id"])
            if current["status"] in {"finished", "blocked"}:
                break
            await asyncio.sleep(0.1)
        assert current["status"] == "finished", runtime.store.history("general")
        target = Path(task["worktree"]) / "nested" / "new.py"
        assert await asyncio.to_thread(target.read_text) == "x = 2\n"
        assert not await asyncio.to_thread((project / "nested").exists)
        assert runtime.store.history("general")[-1]["author"] == "nova"
    finally:
        await runtime.close()
