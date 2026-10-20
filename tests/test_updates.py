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
    run = Mock(return_value="")
    monkeypatch.setattr(U, "_run", run)
    U.install_update()
    arguments = run.call_args.args[0]
    if kind == "uv-tool":
        expected = ["uv", "tool", "upgrade", "novacode-cli", "--compile-bytecode"]
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
    assert "NovaCode is up to date." in capsys.readouterr().out
    installer.assert_not_called()


def _windows_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uv_output: str, *, rewrites: bool):
    """A uv-tool install on Windows whose launchers live in tmp_path."""
    scripts, bin_dir = tmp_path / "venv" / "Scripts", tmp_path / "bin"
    for folder in (scripts, bin_dir):
        folder.mkdir(parents=True)
        (folder / "nova.exe").write_bytes(b"old")
        (folder / "nova.exe.old-1").write_bytes(b"stale")  # from an earlier update
    monkeypatch.setattr(U.sys, "platform", "win32")
    monkeypatch.setattr(U.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(U, "detect_installation", lambda: U.Installation("1.0", "uv-tool"))
    monkeypatch.setattr(U, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(U.shutil, "which", lambda _name: "uv")
    monkeypatch.setattr(U.subprocess, "check_output", lambda *_a, **_kw: f"{bin_dir}\n")
    entry = Mock(group="console_scripts")
    entry.name = "nova"
    monkeypatch.setattr(U.importlib.metadata, "distribution", lambda _name: Mock(entry_points=[entry]))
    seen = []

    def run(arguments, **_kw):
        # While uv runs, the running launchers are out of its way.
        seen.append(sorted(p.name for p in (*scripts.iterdir(), *bin_dir.iterdir())))
        if rewrites:
            for folder in (scripts, bin_dir):
                (folder / "nova.exe").write_bytes(b"new")
        return uv_output

    monkeypatch.setattr(U, "_run", run)
    return scripts, bin_dir, seen


def test_windows_update_runs_in_the_foreground_with_launchers_moved_aside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scripts, bin_dir, seen = _windows_tool(
        monkeypatch, tmp_path, "Updated novacode-cli v1.0 -> v1.1", rewrites=True
    )
    assert U.install_update() is True
    assert "nova.exe" not in seen[0] and "nova.exe.old-1" not in seen[0], "stale ones removed"
    for folder in (scripts, bin_dir):
        assert (folder / "nova.exe").read_bytes() == b"new"


def test_windows_launchers_come_back_when_uv_writes_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scripts, bin_dir, _seen = _windows_tool(monkeypatch, tmp_path, "Nothing to upgrade", rewrites=False)
    assert U.install_update() is False, "nothing to upgrade is not a change"
    for folder in (scripts, bin_dir):
        assert (folder / "nova.exe").read_bytes() == b"old"
        assert [p.name for p in folder.iterdir()] == ["nova.exe"]


def test_windows_launchers_come_back_when_uv_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scripts, bin_dir, _seen = _windows_tool(monkeypatch, tmp_path, "", rewrites=False)
    monkeypatch.setattr(U, "_run", Mock(side_effect=subprocess.CalledProcessError(2, "uv")))
    with pytest.raises(subprocess.CalledProcessError):
        U.install_update()
    assert (scripts / "nova.exe").read_bytes() == (bin_dir / "nova.exe").read_bytes() == b"old"


def test_windows_locked_entrypoint_is_success_when_uv_installed_the_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _scripts, _bin_dir, _seen = _windows_tool(monkeypatch, tmp_path, "", rewrites=False)
    monkeypatch.setattr(U, "installed_version", lambda _python: "1.1")
    monkeypatch.setattr(
        U,
        "_run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                1,
                "uv tool upgrade",
                output=(
                    "error: Failed to upgrade novacode-cli\n"
                    "Caused by: Failed to install entrypoint\n"
                    "The process cannot access the file because it is being used "
                    "by another process. (os error 32)"
                ),
            )
        ),
    )
    progress = Mock()

    assert U.install_update(progress) is True
    progress.done.assert_any_call("uv installed the package; the running nova.exe is still in use")


def test_windows_locked_entrypoint_is_success_even_when_package_version_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _windows_tool(monkeypatch, tmp_path, "", rewrites=False)
    monkeypatch.setattr(U, "installed_version", lambda _python: "1.0")
    monkeypatch.setattr(
        U,
        "_run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                1,
                "uv tool upgrade",
                output=(
                    "Failed to install entrypoint: The process cannot access the file "
                    "because it is being used by another process. (os error 32)"
                ),
            )
        ),
    )

    progress = Mock()
    assert U.install_update(progress) is True
    progress.done.assert_any_call("uv installed the package; the running nova.exe is still in use")


def test_update_command_reports_success_for_installed_package_with_locked_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _windows_tool(monkeypatch, tmp_path, "", rewrites=False)
    monkeypatch.setattr(
        U,
        "check_for_update",
        lambda **_kwargs: U.UpdateStatus(True, "1.0", "1.1"),
    )
    monkeypatch.setattr(U, "installed_version", lambda _python: "1.0")
    monkeypatch.setattr(
        U,
        "_run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                1,
                "uv tool upgrade",
                output=(
                    "Failed to install entrypoint: The process cannot access the file "
                    "because it is being used by another process. (os error 32)"
                ),
            )
        ),
    )

    assert U.update_main([]) == 0
    captured = capsys.readouterr()
    assert "NovaCode updated" in captured.out
    assert "uv installed the package; the running nova.exe is still in use" in captured.out
    assert "Update failed" not in captured.err


def test_a_failed_step_prints_its_output_then_the_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        U, "check_for_update", lambda **_kw: U.UpdateStatus(available=True, current="a", latest="b")
    )
    command = [sys.executable, "-c", "print('resolving'); print('error: no network'); raise SystemExit(3)"]
    monkeypatch.setattr(U, "install_update", lambda progress: U._run(command, progress=progress))
    assert U.update_main([]) == 1
    err = capsys.readouterr().err
    assert "error: no network" in err and err.index("no network") < err.index("Update failed")
