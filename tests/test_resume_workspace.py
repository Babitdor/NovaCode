"""Directory selection never silently moves tools or grants workspace trust."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli.session.resume_workspace import (
    choose_resume_directory,
    prepare_cli_resume,
    relaunch_resume,
    resume_argv,
)


@pytest.fixture
def preferences(monkeypatch: pytest.MonkeyPatch) -> dict:
    from novacode_cli.config.nova_config import NovaConfig

    values = {}
    monkeypatch.setattr(NovaConfig, "__init__", lambda _self: None)
    monkeypatch.setattr(
        NovaConfig, "get", lambda _self, key, default=None: values.get(key, default)
    )
    monkeypatch.setattr(NovaConfig, "set", lambda _self, key, value: values.__setitem__(key, value))
    return values


@pytest.fixture
def directories(tmp_path: Path) -> tuple[Path, Path]:
    original, current = tmp_path / "saved [project]", tmp_path / "current project"
    original.mkdir()
    current.mkdir()
    return original, current


@pytest.mark.parametrize("choice", [0, 1, 2, 3, -1, None])
async def test_explicit_directory_choices_and_cancellation(
    directories: tuple[Path, Path],
    preferences: dict,
    choice: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, current = directories
    approve = Mock(side_effect=AssertionError("Directory selection cannot grant trust"))
    monkeypatch.setattr("novacode_cli.path_approval.PathApprovalManager.approve_path", approve)
    picker = AsyncMock(return_value=choice)
    result = await choose_resume_directory(
        SimpleNamespace(project_root=str(original)), current, picker
    )
    expected = None if choice in (-1, None) else original if choice in (0, 2) else current
    assert result == expected
    picker.assert_awaited_once_with(original, current)
    assert preferences == (
        {"resume_directory": "session" if choice == 2 else "current"} if choice in (2, 3) else {}
    )
    approve.assert_not_called()


@pytest.mark.parametrize("preference", ["session", "current"])
async def test_always_choices_reuse_the_preference(
    directories: tuple[Path, Path],
    preferences: dict,
    preference: str,
) -> None:
    original, current = directories
    preferences["resume_directory"] = preference
    picker = AsyncMock()
    result = await choose_resume_directory(
        SimpleNamespace(project_root=str(original)), current, picker
    )
    assert result == (original if preference == "session" else current)
    picker.assert_not_awaited()


async def test_unavailable_original_does_not_block_using_current(
    directories: tuple[Path, Path],
    preferences: dict,
) -> None:
    original, current = directories
    original.rmdir()
    meta = SimpleNamespace(project_root=str(original))
    assert await choose_resume_directory(meta, current, AsyncMock(return_value=1)) == current
    with pytest.raises(FileNotFoundError):
        await choose_resume_directory(meta, current, AsyncMock(return_value=2))
    assert preferences == {}


async def test_legacy_and_same_directory_sessions_need_no_question(
    directories: tuple[Path, Path],
    preferences: dict,
) -> None:
    _, current = directories
    picker = AsyncMock()
    for meta in (
        None,
        SimpleNamespace(project_root=None),
        SimpleNamespace(project_root=str(current)),
    ):
        assert await choose_resume_directory(meta, current, picker) == current
    picker.assert_not_awaited()
    assert preferences == {}


async def test_cli_uses_the_shared_directory_choice_and_keeps_context_mode(
    directories: tuple[Path, Path],
    preferences: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, current = directories
    manager = SimpleNamespace(
        load_session_meta=Mock(return_value=SimpleNamespace(project_root=str(original)))
    )
    sessions = AsyncMock(return_value=("saved-id", True))
    directory = AsyncMock(return_value=0)
    monkeypatch.setattr("novacode_cli.session.session_restore.select_session_interactive", sessions)
    monkeypatch.setattr("novacode_cli.tui.pickers.pick_resume_directory_tui", directory)
    result = await prepare_cli_resume(manager, current, resume=True, session_id=None)
    assert result == ("saved-id", True, original)
    sessions.assert_awaited_once_with(manager)
    directory.assert_awaited_once_with(original, current)
    assert preferences == {}


async def test_directory_question_without_a_terminal_cancels_instead_of_changing_workspace(
    directories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual.app import App

    from novacode_cli.tui.pickers import pick_resume_directory_tui

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    run = AsyncMock(side_effect=AssertionError("No interactive picker without a terminal"))
    monkeypatch.setattr(App, "run_async", run)
    assert await pick_resume_directory_tui(*directories) is None
    run.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--resume", "--agent", "nova", "--sandbox", "os"],
        ["--continue", "old-id", "--agent", "nova", "--sandbox", "os"],
        ["--continue=old-id", "--agent", "nova", "--sandbox", "os", "--compact-on-resume"],
    ],
)
def test_relaunch_preserves_cli_options_and_the_compaction_choice(arguments: list[str]) -> None:
    assert resume_argv(arguments, "new-id", compact=True) == [
        "--agent",
        "nova",
        "--sandbox",
        "os",
        "--continue",
        "new-id",
        "--compact-on-resume",
    ]
    assert "--compact-on-resume" not in resume_argv(arguments, "new-id", compact=False)


def test_relaunch_uses_a_new_process_directory_without_changing_the_host(
    directories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, _ = directories
    before = Path.cwd()
    child = Mock()
    child.wait.side_effect = [KeyboardInterrupt, 7]
    launch = Mock(return_value=child)
    monkeypatch.setattr("novacode_cli.session.resume_workspace.subprocess.Popen", launch)
    assert relaunch_resume(original, ["--continue", "saved-id"]) == 7
    launch.assert_called_once_with(
        [sys.executable, "-m", "novacode_cli", "--continue", "saved-id"],
        cwd=original,
    )
    assert Path.cwd() == before


@pytest.mark.parametrize(
    "arguments", [["--resume", "--continue", "saved"], ["--compact-on-resume"]]
)
def test_invalid_resume_arguments_fail_before_any_picker(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novacode_cli.cli_args import parse_args

    monkeypatch.setattr(sys, "argv", ["nova", *arguments])
    with pytest.raises(SystemExit) as error:
        parse_args()
    assert error.value.code == 2


@pytest.mark.parametrize("result", ["success", "failed", "exception", "disabled"])
async def test_child_compacts_before_serving_without_losing_saved_context_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    from novacode_cli.config.config import settings
    from novacode_cli.sessions.worker import run_session_worker

    phases = []
    state = SimpleNamespace(
        compact_on_resume=result != "disabled",
        _model=object(),
        thread_id="restored-thread",
        token_tracker=SimpleNamespace(context_window_size=128000),
    )

    async def compact(**kwargs: object) -> SimpleNamespace:
        phases.append("compact")
        assert kwargs["model"] is state._model
        assert kwargs["thread_id"] == "restored-thread"
        if result == "exception":
            raise RuntimeError
        return SimpleNamespace(success=result == "success", error="provider unavailable")

    async def serve() -> int:
        phases.append("serve")
        return 3

    worker = SimpleNamespace(run=AsyncMock(side_effect=serve), _emit=SimpleNamespace(close=Mock()))
    monkeypatch.setattr("novacode_cli.sessions.worker.SessionWorker", lambda **_kwargs: worker)
    monkeypatch.setattr("novacode_cli.compaction.compact_conversation", compact)
    monkeypatch.setattr(settings, "get_agent_dir", lambda _name: tmp_path)
    assert await run_session_worker(agent=object(), assistant_id="nova", session_state=state) == 3
    assert phases == (["serve"] if result == "disabled" else ["compact", "serve"])
    assert state.compact_on_resume is False
    worker._emit.close.assert_called_once()


@pytest.mark.parametrize("cancel", [False, True])
def test_cli_selects_or_cancels_before_allocating_session_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancel: bool,
) -> None:
    import importlib

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    main = importlib.import_module("novacode_cli.main")
    from novacode_cli.cli_args import parse_args

    monkeypatch.setattr(sys, "argv", ["nova", "--resume", "--no-agent-server"])
    arguments = parse_args()
    monkeypatch.setattr(main, "parse_args", lambda: arguments)
    monkeypatch.setattr(main, "check_cli_dependencies", lambda: None)
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            get_agents_root_dir=lambda: tmp_path,
            get_onboarding_status=lambda: True,
        ),
    )
    monkeypatch.setattr("novacode_cli.config.config.seed_builtin_agents", lambda _path: None)
    monkeypatch.setattr(
        "novacode_cli.plugins.claude_plugins.set_session_plugins", lambda _paths: None
    )
    manager = object()
    monkeypatch.setattr("novacode_cli.session.session_persistence.SessionManager", lambda: manager)
    selection = None if cancel else ("saved-id", True, tmp_path)
    prepare = AsyncMock(return_value=selection)
    restart = Mock(return_value=0)
    state = Mock(side_effect=AssertionError("Runtime allocated before workspace choice"))
    monkeypatch.setattr("novacode_cli.session.resume_workspace.prepare_cli_resume", prepare)
    monkeypatch.setattr("novacode_cli.session.resume_workspace.relaunch_resume", restart)
    monkeypatch.setattr(main, "SessionState", state)
    if cancel:
        main.cli_main()
        restart.assert_not_called()
    else:
        with pytest.raises(SystemExit) as stopped:
            main.cli_main()
        assert stopped.value.code == 0
        restart.assert_called_once_with(
            tmp_path,
            ["--no-agent-server", "--continue", "saved-id", "--compact-on-resume"],
        )
    prepare.assert_awaited_once()
    state.assert_not_called()
