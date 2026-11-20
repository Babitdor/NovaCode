"""Updater robustness: VCS tool installs, installer locks, deadlines, refusals.

Each test pins one contract from the update review, so a regression names the
behavior it broke rather than "updates feel flaky".
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from novacode_cli import update_guard as G
from novacode_cli import update_ui as UI
from novacode_cli import updates as U


@pytest.fixture(autouse=True)
def isolate_native_update_checks(monkeypatch: pytest.MonkeyPatch):
    from contextlib import nullcontext

    monkeypatch.setattr(U, "installation_lock", lambda **_kw: nullcontext())
    monkeypatch.setattr(U, "check_update_blockers", lambda: None)


def _tool_install(monkeypatch: pytest.MonkeyPatch, kind: str = "uv-tool", revision: str = "") -> Mock:
    monkeypatch.setattr(U.sys, "platform", "linux")
    monkeypatch.setattr(U, "detect_installation", lambda: U.Installation("1.0", kind, revision))
    monkeypatch.setattr(U.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(U, "_pip_command", lambda: ["python", "-m", "pip", "install"])
    run = Mock(return_value="")
    monkeypatch.setattr(U, "_run", run)
    return run


def test_git_tool_install_reinstalls_its_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _tool_install(monkeypatch, revision="a" * 40)
    monkeypatch.setattr(U, "_installed_state", lambda: ("1.0", "a" * 40))
    requirement = f"novacode-cli @ git+{U.REPO_URL}@main"
    monkeypatch.setattr(U, "_vcs_requirement", lambda: requirement)

    U.install_update()

    arguments = run.call_args.args[0]
    assert arguments[:3] == ["uv", "tool", "install"], "uv never advances a VCS requirement"
    assert "--force" in arguments and "--reinstall" in arguments
    assert arguments[-1] == requirement


def test_registry_tool_install_still_upgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _tool_install(monkeypatch)
    monkeypatch.setattr(U, "_installed_state", lambda: ("1.0", ""))
    monkeypatch.setattr(U, "_vcs_requirement", lambda: "")

    U.install_update()

    assert run.call_args.args[0] == [
        "uv", "tool", "upgrade", "novacode-cli", "--compile-bytecode"
    ]


@pytest.mark.parametrize(
    ("url", "revision", "expected"),
    [
        (U.REPO_URL, "main", f"novacode-cli @ git+{U.REPO_URL}@main"),
        (U.REPO_URL, "", f"novacode-cli @ git+{U.REPO_URL}@main"),
        ("https://github.com/someone/else.git", "main", ""),
        (U.REPO_URL, "v1.2.52", ""),
    ],
)
def test_vcs_requirement_only_accepts_the_official_main_branch(
    monkeypatch: pytest.MonkeyPatch, url: str, revision: str, expected: str
) -> None:
    import json

    direct = {"url": url, "vcs_info": {"vcs": "git", "requested_revision": revision}}
    dist = Mock()
    dist.read_text.return_value = json.dumps(direct)
    monkeypatch.setattr(U.importlib.metadata, "distribution", lambda _name: dist)
    assert U._vcs_requirement() == expected


def test_installed_state_without_provenance_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = Mock()
    dist.read_text.return_value = None
    monkeypatch.setattr(U.importlib.metadata, "distribution", lambda _name: dist)
    monkeypatch.setattr(U, "installed_version", lambda _python=None: "1.0")
    assert U._installed_state() == ("1.0", "")


@pytest.mark.parametrize(("after", "changed"), [(("1.0", ""), False), (("1.1", ""), True)])
def test_tool_install_change_is_measured_not_parsed(
    monkeypatch: pytest.MonkeyPatch, after: tuple[str, str], changed: bool
) -> None:
    _tool_install(monkeypatch)
    states = iter([("1.0", ""), after])
    monkeypatch.setattr(U, "_installed_state", lambda: next(states))
    monkeypatch.setattr(U, "_vcs_requirement", lambda: "")

    # uv's wording must not decide whether an update happened.
    assert U.install_update() is changed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
def test_posix_installer_lock_excludes_a_second_installer(tmp_path: Path) -> None:
    with G.installation_lock(exclusive=True, root=tmp_path):
        with pytest.raises(G.UpdateBlockedError, match="using this installation"):
            with G.installation_lock(exclusive=True, root=tmp_path):
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
def test_posix_shared_locks_coexist_and_release(tmp_path: Path) -> None:
    with G.installation_lock(exclusive=False, root=tmp_path):
        with G.installation_lock(exclusive=False, root=tmp_path):
            pass
    with G.installation_lock(exclusive=True, root=tmp_path):
        pass


def test_step_deadline_kills_a_hung_command() -> None:
    started = __import__("time").monotonic()
    with pytest.raises(UI.CommandFailed) as failed:
        UI.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], Mock(), timeout=0.5
        )
    assert "imed out" in str(failed.value.output), "the deadline names itself"
    assert __import__("time").monotonic() - started < 15, "a hung step is not waited out"


def test_interrupt_terminates_the_running_step(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(stdout=iter(()), returncode=-1, pid=1)
    process.wait.side_effect = KeyboardInterrupt
    monkeypatch.setattr(UI.subprocess, "Popen", Mock(return_value=process))

    with pytest.raises(KeyboardInterrupt):
        UI.run_command(["uv", "tool", "upgrade", "novacode-cli"], Mock(), timeout=30)

    process.terminate.assert_called_once()
    process.kill.assert_not_called()


def test_untracked_files_do_not_block_a_source_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(U, "detect_installation", lambda: U.Installation("1.0", "source", "a" * 40, root=tmp_path))
    calls: list[tuple[str, ...]] = []

    def git(_root: Path, *args: str) -> str:
        calls.append(args)
        if args[0] == "rev-parse":
            return "origin/main"
        if args[0] == "rev-list":
            return "0"
        return ""

    monkeypatch.setattr(U, "_git", git)
    monkeypatch.setattr(U.shutil, "which", lambda _name: None)
    monkeypatch.setattr(U.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(U, "_run", Mock())

    U.install_update()

    assert ("status", "--porcelain", "--untracked-files=no") in calls


def test_checkout_ahead_of_upstream_fails_before_fetching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(U, "detect_installation", lambda: U.Installation("1.0", "source", "a" * 40, root=tmp_path))

    def git(_root: Path, *args: str) -> str:
        if args[0] == "rev-parse":
            return "origin/main"
        if args[0] == "rev-list":
            return "2"
        return ""

    monkeypatch.setattr(U, "_git", git)
    run = Mock()
    monkeypatch.setattr(U, "_run", run)

    with pytest.raises(ValueError, match="not on origin/main"):
        U.install_update()
    run.assert_not_called()


def test_handoff_timeout_tells_the_user_how_to_resume(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from novacode_cli.update_guard import UpdateBlockedError

    monkeypatch.setenv(U.HANDOFF_PID_ENV, "1")
    monkeypatch.setenv(U.RESTART_SESSION_ENV, "sess-42")
    monkeypatch.setattr(
        U, "_wait_for_handoff", Mock(side_effect=UpdateBlockedError("previous Nova has not exited"))
    )
    monkeypatch.setattr(U, "_update_with_progress", Mock(return_value=0))

    assert U._update_after_exit() == U.EXIT_UPDATE_BLOCKED
    assert "nova --continue sess-42" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
def test_installer_may_upgrade_the_lock_its_own_runtime_holds(tmp_path: Path) -> None:
    """The TUI updates in-process, inside the shared lock its own launch took."""
    with G.installation_lock(exclusive=False, root=tmp_path):
        with G.installation_lock(exclusive=True, root=tmp_path):
            pass
        with G.installation_lock(exclusive=False, root=tmp_path):
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
def test_posix_lock_excludes_another_process(tmp_path: Path) -> None:
    """The runtime lock must hold against a real second process, not just a listener."""
    import subprocess

    attempt = (
        "import sys; from pathlib import Path\n"
        "from novacode_cli.update_guard import UpdateBlockedError, installation_lock\n"
        "try:\n"
        "    with installation_lock(exclusive=True, root=Path(sys.argv[1])):\n"
        "        print('acquired')\n"
        "except UpdateBlockedError:\n"
        "    print('blocked')\n"
    )
    with G.installation_lock(exclusive=False, root=tmp_path):
        finished = subprocess.run(
            [sys.executable, "-c", attempt, str(tmp_path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    assert finished.stdout.strip() == "blocked"


def test_native_scan_prunes_artifacts(tmp_path: Path) -> None:
    native = tmp_path / "Lib" / "site-packages" / "pkg"
    native.mkdir(parents=True)
    (native / "native.pyd").touch()
    for junk in ("build", "__pycache__", "node_modules"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "junk.dll").touch()

    found = {path.name for path in G._native_files(tmp_path)}

    assert found == {"native.pyd"}
