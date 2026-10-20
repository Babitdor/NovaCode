"""Real Git evidence for current-session workspace transitions."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from novacode_cli.sessions import worktree as wt
from novacode_cli.tools.worktree_tools import WorktreeSession, WorktreeToolsMiddleware


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        assert wt._git(args, cwd=root)[0] == 0
    (root / "tracked.txt").write_text("baseline", encoding="utf-8")
    assert wt._git(["add", "."], cwd=root)[0] == 0
    assert wt._git(["commit", "-m", "baseline"], cwd=root)[0] == 0
    monkeypatch.setattr(wt, "worktrees_root", lambda: tmp_path / "worktrees")
    return root


def test_enter_exit_preserves_files_and_commits(repo: Path) -> None:
    switched = []
    session = WorktreeSession(repo, switched.append)
    (repo / "original.txt").write_text("stay here", encoding="utf-8")
    entered = session.enter("feature")
    root = Path(entered["path"])
    assert root != repo
    assert switched == [root]
    assert not (root / "original.txt").exists()
    (root / "tracked.txt").write_text("committed change", encoding="utf-8")
    assert wt._git(["add", "."], cwd=root)[0] == 0
    assert wt._git(["commit", "-m", "feature"], cwd=root)[0] == 0
    (root / "dirty.txt").write_text("keep me", encoding="utf-8")
    exited = session.exit()
    assert exited["path"] == str(repo)
    assert switched == [root, repo]
    assert (root / "dirty.txt").read_text(encoding="utf-8") == "keep me"
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "baseline"
    assert wt._git(["log", "-1", "--format=%s"], cwd=root)[1] == "feature"


def test_sessions_with_same_name_are_isolated(repo: Path) -> None:
    first = WorktreeSession(repo, lambda _root: None).enter("feature")
    second = WorktreeSession(repo, lambda _root: None).enter("feature")
    assert first["path"] != second["path"]
    assert first["branch"] != second["branch"]


def test_invalid_transitions_and_creation_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    switched = []
    session = WorktreeSession(repo, switched.append)
    assert "error" in session.exit()
    assert "error" in session.enter(" ")
    entered = session.enter("feature")
    assert "error" in session.enter("nested")
    assert session.root == Path(entered["path"])
    session.exit()

    monkeypatch.setattr(wt, "create_worktree", Mock(side_effect=RuntimeError("git refused")))
    assert "git refused" in session.enter("failure")["error"]
    assert session.root == repo
    assert switched[-1] == repo


def test_requires_committed_repository(tmp_path: Path) -> None:
    switched = []
    session = WorktreeSession(tmp_path, switched.append)
    assert "error" in session.enter("feature")
    assert wt._git(["init"], cwd=tmp_path)[0] == 0
    assert "error" in session.enter("feature")
    assert not switched


def _middleware(repo: Path, *, enabled: bool = True) -> WorktreeToolsMiddleware:
    from deepagents.backends.composite import CompositeBackend

    from novacode_cli.backends.filesystem import OptimizedLocalShellBackend

    return WorktreeToolsMiddleware(
        repo,
        CompositeBackend(
            default=OptimizedLocalShellBackend(root_dir=repo, virtual_mode=True), routes={}
        ),
        [],
        enabled=enabled,
    )


def _call(name: str, *, mixed: bool = False) -> SimpleNamespace:
    from langchain_core.messages import AIMessage

    calls = [{"name": name, "args": {}, "id": "one"}]
    if mixed:
        calls.append({"name": "read_file", "args": {}, "id": "two"})
    return SimpleNamespace(
        tool_call=calls[0], state={"messages": [AIMessage("", tool_calls=calls)]}
    )


def test_transition_rejects_mixed_batch_and_concurrent_tools(repo: Path) -> None:
    middleware = _middleware(repo)
    called = Mock()
    rejected = middleware.wrap_tool_call(_call("EnterWorktreeTool", mixed=True), called)
    assert rejected.status == "error"
    called.assert_not_called()
    with middleware._guard(_call("shell")) as error:
        assert error is None
        rejected = middleware.wrap_tool_call(_call("EnterWorktreeTool"), called)
        assert rejected.status == "error"
    called.assert_not_called()
    with middleware._guard(_call("EnterWorktreeTool")) as error:
        assert error is None
        rejected = middleware.wrap_tool_call(_call("read_file"), called)
        assert rejected.status == "error"
    called.assert_not_called()
    middleware.wrap_tool_call(_call("EnterWorktreeTool"), called)
    called.assert_called_once()


async def test_tool_entry_rebases_files_shell_and_project_memory(repo: Path) -> None:
    from deepagents.backends.filesystem import FilesystemBackend

    from novacode_cli.shell import ShellMiddleware

    middleware = _middleware(repo)
    shell = ShellMiddleware(workspace_root=str(repo))
    middleware._middleware.append(shell)
    plans = FilesystemBackend(root_dir=repo / ".nova/plans", virtual_mode=True)
    memory = FilesystemBackend(root_dir=repo / ".nova", virtual_mode=True)
    middleware._backend.sorted_routes = [("/.nova/plans/", plans), ("/project-memory/", memory)]
    enter, exit_tool = middleware.tools
    entered = await enter.ainvoke({"name": "feature"})
    root = Path(entered["path"])
    assert middleware._backend.default.cwd == root
    assert plans.cwd == root / ".nova/plans"
    assert memory.cwd == root / ".nova"
    assert shell._os_policy.workspace_root == str(root)
    assert shell._workspace_root == str(root)
    result = middleware._backend.default.write("/isolated.txt", "only in worktree")
    assert result.error is None
    assert (root / "isolated.txt").exists()
    assert not (repo / "isolated.txt").exists()
    # Exercise a real shell subprocess, not just the configured cwd attribute.
    import asyncio

    process = await shell._spawn(
        "git rev-parse --show-toplevel",
        None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 0
    assert Path(stdout.decode().strip()).resolve() == root.resolve()
    exited = await exit_tool.ainvoke({})
    assert exited["path"] == str(repo)
    assert middleware._backend.default.cwd == repo
    assert shell._workspace_root == str(repo)
    assert plans.cwd == repo / ".nova/plans"
    assert memory.cwd == repo / ".nova"
    assert (root / "isolated.txt").exists()


def test_remote_context_fails_without_touching_git(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    middleware = _middleware(repo, enabled=False)
    create = Mock()
    monkeypatch.setattr(wt, "create_worktree", create)
    assert "error" in middleware.tools[0].invoke({"name": "feature"})
    create.assert_not_called()


def test_workspace_tools_are_always_bound(repo: Path) -> None:
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import SystemMessage

    from novacode_cli.agents.tool_search import ToolSearchMiddleware
    from novacode_cli.sessions.gateway import SendMessage, list_sessions

    middleware = _middleware(repo)
    tools = [*middleware.tools, SendMessage, list_sessions]
    request = ModelRequest(
        model=None,
        tools=tools,
        messages=[],
        state={},
        runtime=None,
        system_message=SystemMessage("base"),
    )
    search = ToolSearchMiddleware()
    search._frequent = set()
    assert {tool.name for tool in search._apply(request).tools} == {tool.name for tool in tools}
    assert str(repo) in middleware._model_request(request).system_message.content_blocks[-1]["text"]


async def test_async_guard_releases_after_handler_error(repo: Path) -> None:
    middleware = _middleware(repo)

    async def fail(_request: object) -> None:
        raise RuntimeError

    with pytest.raises(RuntimeError):
        await middleware.awrap_tool_call(_call("EnterWorktreeTool"), fail)
    with middleware._guard(_call("shell")) as error:
        assert error is None


async def test_cancelling_transition_keeps_reservation_until_worker_finishes(repo: Path) -> None:
    import asyncio

    middleware = _middleware(repo)
    started, finish = asyncio.Event(), asyncio.Event()

    async def worker(_request: object) -> str:
        started.set()
        await finish.wait()
        return "finished"

    task = asyncio.create_task(middleware.awrap_tool_call(_call("EnterWorktreeTool"), worker))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    called = Mock()
    rejected = middleware.wrap_tool_call(_call("shell"), called)
    assert rejected.status == "error"
    called.assert_not_called()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    middleware.wrap_tool_call(_call("shell"), called)
    called.assert_called_once()


async def test_tools_execute_through_agent_graph(repo: Path) -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, _tools: object, **_kwargs: object) -> "Model":
            return self

    middleware = _middleware(repo)
    model = Model(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": "EnterWorktreeTool", "args": {"name": "graph"}, "id": "enter"}
                ],
            ),
            AIMessage("entered"),
            AIMessage("", tool_calls=[{"name": "ExitWorktreeTool", "args": {}, "id": "exit"}]),
            AIMessage("exited"),
        ]
    )
    graph = create_agent(model, middleware=[middleware])
    result = await graph.ainvoke({"messages": [{"role": "user", "content": "enter"}]})
    assert middleware.session.root != repo
    assert result["messages"][-2].name == "EnterWorktreeTool"
    root = middleware.session.root
    result = await graph.ainvoke({"messages": [{"role": "user", "content": "exit"}]})
    assert middleware.session.root == repo
    assert result["messages"][-2].name == "ExitWorktreeTool"
    assert root.is_dir()
