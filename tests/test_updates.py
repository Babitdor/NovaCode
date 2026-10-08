"""Update checks use install provenance; updates never overwrite local changes."""

# Fixture arguments establish the installation and cache sandbox.
# ruff: noqa: ARG001

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from novacode_cli import updates as U  # noqa: N812


@pytest.fixture
def update_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    installation = U.Installation("1.0.0", "source", "a" * 40, root=tmp_path)
    monkeypatch.setattr(U, "detect_installation", lambda: installation)
    monkeypatch.setattr(U, "_cache_path", lambda: tmp_path / "cache.json")
    network = Mock(
        side_effect=[
            {"sha": "b" * 40},
            {"status": "ahead", "html_url": "https://example.com/diff"},
        ]
    )
    monkeypatch.setattr(U, "_get_json", network)
    return network


def test_repo_push_detected_without_version_bump_and_cached(update_env: Mock) -> None:
    status = U.check_for_update()
    assert status.available
    assert status.latest == "b" * 40
    assert U.check_for_update() == status
    assert update_env.call_count == 2


@pytest.mark.parametrize("relation", ["identical", "behind"])
def test_local_equal_or_ahead_does_not_notify(update_env: Mock, relation: str) -> None:
    update_env.side_effect = [{"sha": "b" * 40}, {"status": relation}]
    assert not U.check_for_update().available


def test_manual_check_bypasses_cache(update_env: Mock) -> None:
    U.check_for_update()
    update_env.side_effect = [{"sha": "c" * 40}, {"status": "ahead"}]
    assert U.check_for_update(force=True).latest == "c" * 40
    assert update_env.call_count == 4


def test_network_failure_is_quiet_and_retryable(update_env: Mock) -> None:
    update_env.side_effect = OSError("offline")
    result = U.check_for_update()
    assert not result.available
    assert result.error == "offline"
    U.check_for_update()
    assert update_env.call_count == 2


def test_package_check_compares_versions_not_strings(
    update_env: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(U, "detect_installation", lambda: U.Installation("1.9.0", "package"))
    update_env.side_effect = None
    update_env.return_value = {"info": {"version": "1.10.0"}}
    assert U.check_for_update().available


def test_dirty_checkout_never_runs_fetch_or_installer(
    update_env: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(U, "_git", lambda *_args: " M important.py")
    run = Mock()
    monkeypatch.setattr(U, "_run", run)
    with pytest.raises(ValueError, match="local changes"):
        U.install_update()
    run.assert_not_called()


def test_source_update_is_fast_forward_only_and_refreshes_dependencies(
    update_env: Mock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        U, "_git", lambda _root, *args: "" if args[0] == "status" else "origin/main"
    )
    monkeypatch.setattr(U, "_pip_command", lambda: ["python", "-m", "pip", "install"])
    monkeypatch.setattr(U.shutil, "which", lambda _name: None)
    monkeypatch.setattr(U.importlib.util, "find_spec", lambda _name: object())
    run = Mock()
    monkeypatch.setattr(U, "_run", run)
    U.install_update()
    assert run.call_args_list[0].args[0] == ["git", "-C", str(tmp_path), "fetch", "origin", "main"]
    assert run.call_args_list[1].args[0][-3:] == ["merge", "--ff-only", "origin/main"]
    assert run.call_args_list[2].args[0][-3:] == ["--upgrade", "-e", str(tmp_path)]


def test_failed_merge_never_runs_dependency_install(
    update_env: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.setattr(
        U, "_git", lambda _root, *args: "" if args[0] == "status" else "origin/main"
    )
    run = Mock(side_effect=[None, subprocess.CalledProcessError(1, "git merge")])
    monkeypatch.setattr(U, "_run", run)
    with pytest.raises(subprocess.CalledProcessError):
        U.install_update()
    assert run.call_count == 2


@pytest.mark.parametrize("kind", ["package", "git", "uv-tool"])
def test_update_uses_original_manager(
    update_env: Mock,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(U.sys, "platform", "linux")
    monkeypatch.setattr(U, "detect_installation", lambda: U.Installation("1.0", kind))
    monkeypatch.setattr(U.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(U, "_pip_command", lambda: ["python", "-m", "pip", "install"])
    run = Mock()
    monkeypatch.setattr(U, "_run", run)
    U.install_update()
    arguments = run.call_args.args[0]
    if kind == "uv-tool":
        expected = ["uv", "tool", "upgrade", "novacode-cli"]
        if U.sys.platform == "win32":
            expected.extend(["--reinstall-package", "novacode-cli"])
        assert arguments == expected
    elif kind == "git":
        assert "--force-reinstall" in arguments
        assert "git+https://github.com/Babitdor/NovaCode.git@main" in arguments[-1]
    else:
        assert arguments[-2:] == ["--upgrade", "novacode-cli"]


def test_update_entrypoint_does_not_import_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from novacode_cli import entrypoint

    monkeypatch.setattr(entrypoint.sys, "argv", ["nova", "update", "--check"])
    handler = Mock(return_value=0)
    monkeypatch.setattr(U, "update_main", handler)
    with pytest.raises(SystemExit) as exited:
        entrypoint.cli_main()
    assert exited.value.code == 0
    handler.assert_called_once_with(["--check"])


@pytest.mark.parametrize("git_install", [False, True])
def test_detection_uses_distribution_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    git_install: bool,
) -> None:
    import json

    dist = Mock(version="1.2.3")
    direct = (
        {
            "url": U.REPO_URL,
            "vcs_info": {"vcs": "git", "commit_id": "d" * 40, "requested_revision": "main"},
        }
        if git_install
        else {}
    )
    dist.read_text.side_effect = lambda name: (
        json.dumps(direct) if name == "direct_url.json" else None
    )
    monkeypatch.setattr(U.importlib.metadata, "distribution", lambda _name: dist)
    monkeypatch.setattr(U, "__file__", str(tmp_path / "package" / "updates.py"))
    monkeypatch.setattr(U.sys, "prefix", str(tmp_path))
    installed = U.detect_installation()
    assert installed.kind == ("git" if git_install else "package")
    assert installed.revision == ("d" * 40 if git_install else "")


def test_source_detection_does_not_depend_on_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").touch()
    dist = Mock(version="1.0")
    dist.read_text.return_value = None
    monkeypatch.setattr(U.importlib.metadata, "distribution", lambda _name: dist)
    monkeypatch.setattr(U, "__file__", str(tmp_path / "package" / "updates.py"))
    git = Mock(side_effect=[U.REPO_URL, "main", "a" * 40])
    monkeypatch.setattr(U, "_git", git)
    installation = U.detect_installation()
    assert installation.root == tmp_path
    assert installation.kind == "source"
    assert all(call.args[0] == tmp_path for call in git.call_args_list)


def test_windows_update_hands_off_without_touching_launcher(
    update_env: Mock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(U.sys, "platform", "win32")
    monkeypatch.setattr(U, "detect_installation", lambda: U.Installation("1.0", "uv-tool"))
    monkeypatch.setattr(U.shutil, "which", lambda _name: "uv")
    spawn = Mock()
    run = Mock()
    monkeypatch.setattr(U, "_start_windows_update", spawn)
    monkeypatch.setattr(U, "_run", run)
    assert U.install_update() is True
    run.assert_not_called()
    spawn.assert_called_once_with(
        ["uv", "tool", "upgrade", "novacode-cli", "--reinstall-package", "novacode-cli"]
    )


def test_windows_update_uses_independent_interpreter_and_waits_for_caller(
    update_env: Mock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    monkeypatch.setattr(U.sys, "_base_executable", "base-python.exe")
    process = Mock()
    monkeypatch.setattr(U.subprocess, "Popen", process)
    U._start_windows_update(["uv", "tool", "upgrade", "novacode-cli"])
    args = process.call_args.args[0]
    assert args[:2] == ["base-python.exe", "-I"]
    payload = json.loads(args[-1])
    assert payload["caller_pid"] == U.os.getpid()
    assert payload["parent_pid"] == U.os.getppid()
    assert process.call_args.kwargs["creationflags"] == 0
    assert process.call_args.kwargs.get("stdout") is None
    assert process.call_args.kwargs.get("stderr") is None
    assert not list(tmp_path.glob("*.log"))


def test_queued_update_is_not_reported_as_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(
        U,
        "check_for_update",
        lambda **_kw: U.UpdateStatus(available=True, current="old", latest="new"),
    )
    monkeypatch.setattr(U, "install_update", lambda: True)
    assert U.update_main([]) == 0
    output = capsys.readouterr().out
    assert "log" not in output
    assert "Nova updated" not in output


@pytest.mark.parametrize("fails", [False, True])
def test_external_updater_waits_before_installing_and_records_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    *,
    fails: bool,
) -> None:
    import subprocess

    from novacode_cli import _windows_update as helper

    order = []
    monkeypatch.setattr(helper, "wait_for_exit", lambda pid, **_kw: order.append(pid))
    cache = tmp_path / "cache.json"
    cache.write_text("stale", encoding="utf-8")

    def install(arguments: list[str]) -> bool:
        assert order == [123, 456]
        assert arguments == ["uv", "tool", "upgrade", "novacode-cli"]
        if fails:
            raise subprocess.CalledProcessError(1, "uv")
        return True

    monkeypatch.setattr(helper, "run_installer", install)
    result = helper.main(
        {
            "caller_pid": 123,
            "parent_pid": 456,
            "command": ["uv", "tool", "upgrade", "novacode-cli"],
            "cache": str(cache),
        }
    )
    assert result == int(fails)
    assert cache.exists() == fails
    assert ("Update failed" if fails else "Update successful.") in capsys.readouterr().out


def test_external_updater_never_installs_if_launcher_has_not_exited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from novacode_cli import _windows_update as helper

    monkeypatch.setattr(helper, "wait_for_exit", Mock(side_effect=TimeoutError("still running")))
    installer = Mock()
    monkeypatch.setattr(helper, "run_installer", installer)
    assert (
        helper.main(
            {
                "caller_pid": 123,
                "parent_pid": 456,
                "command": ["uv"],
                "cache": str(tmp_path / "cache.json"),
            }
        )
        == 1
    )
    installer.assert_not_called()


def test_up_to_date_displays_message_without_starting_installer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(
        U,
        "check_for_update",
        lambda **_kw: U.UpdateStatus(available=False, current="same", latest="same"),
    )
    installer = Mock()
    monkeypatch.setattr(U, "install_update", installer)
    assert U.update_main([]) == 0
    assert capsys.readouterr().out.strip() == "NovaCode is up to date."
    installer.assert_not_called()


def test_noop_external_update_displays_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    from novacode_cli import _windows_update as helper

    monkeypatch.setattr(helper, "wait_for_exit", Mock())
    monkeypatch.setattr(helper, "run_installer", lambda _command: False)
    assert (
        helper.main(
            {"caller_pid": 1, "parent_pid": 2, "command": ["uv"], "cache": str(tmp_path / "cache")}
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "NovaCode is up to date." in output
    assert "Restart Nova" not in output


@pytest.mark.parametrize(
    ("output", "exit_code", "changed"),
    [
        ("Nothing to upgrade", 0, False),
        ("Updated novacode-cli", 0, True),
        ("Nothing to upgrade", 1, False),
    ],
)
def test_installer_output_reports_no_upgrade_only_on_success(
    output: str, exit_code: int, capsys: pytest.CaptureFixture, *, changed: bool
) -> None:
    from novacode_cli import _windows_update as helper

    command = [
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1]); sys.exit(int(sys.argv[2]))",
        output,
        str(exit_code),
    ]
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError):
            helper.run_installer(command)
    else:
        assert helper.run_installer(command) is changed
    assert output in capsys.readouterr().out
