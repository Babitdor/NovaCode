"""Project onboarding: real parsing, shared commands and offline dashboard boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest

from novacode_cli import project_onboard as onboard
from novacode_cli.commands import CommandContext, CommandRegistry
from novacode_cli.commands.onboard_handler import handle_onboard_command, register_commands

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text(
        "from service import greet\ndef main():\n    return greet()\n", encoding="utf-8"
    )
    (tmp_path / "service.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text("# Example\nA greeting service.", encoding="utf-8")
    return tmp_path


def context(root: Path, args: str | None = None, **kwargs: Any) -> CommandContext:
    return CommandContext(
        cmd="onboard",
        cmd_args=args,
        agent=Mock(),
        token_tracker=Mock(),
        assistant_id="test",
        session_state=SimpleNamespace(working_directory=str(root), plan_mode_enabled=False),
        **kwargs,
    )


async def test_real_parsers_persist_graph_and_trace_callers(project: Path) -> None:
    graph = await onboard.analyze(project)
    assert onboard.load_graph(project) == graph
    assert set(graph["fingerprints"]) == {"main.py", "service.py", "README.md"}
    assert any(edge["type"] == "calls" for edge in graph["edges"])
    assert "main()" in onboard.impact(graph, "greet")
    assert "main.py" in onboard.impact(graph, "service.py")
    assert "No exact" in onboard.impact(graph, "unknown")
    assert graph["tour"]
    assert "Guided tour" in (project / ".nova/onboard/guide.md").read_text(encoding="utf-8")
    assert not (project / "graphify-out").exists()


async def test_fingerprints_detect_dirty_new_deleted_inputs(project: Path) -> None:
    graph = await onboard.analyze(project)
    assert not onboard.changed_files(project, graph)
    (project / "service.py").write_text("def greet(): return 'changed'", encoding="utf-8")
    (project / "README.md").unlink()
    (project / "new.py").write_text("x=1", encoding="utf-8")
    assert onboard.changed_files(project, graph) == ["README.md", "new.py", "service.py"]
    refreshed = await onboard.analyze(project)
    assert not onboard.changed_files(project, refreshed)
    assert refreshed["fingerprints"] != graph["fingerprints"]


def test_gitignore_generated_and_secret_inputs_are_excluded(project: Path) -> None:
    subprocess.run(  # noqa: S603 - Fixed Git init in the isolated test directory.
        [shutil.which("git"), "init", str(project)], check=True, capture_output=True
    )
    (project / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    for name in ("ignored.py", ".env.py", "credentials.py"):
        (project / name).write_text("secret=1", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules/dependency.py").write_text("x=1", encoding="utf-8")
    assert set(onboard.fingerprints(project)) == {"main.py", "service.py", "README.md"}


def test_git_failure_never_falls_back_to_reading_ignored_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = subprocess.CalledProcessError(128, "git", stderr=b"unsafe repository")
    monkeypatch.setattr(onboard.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(ValueError, match="safely"):
        onboard.fingerprints(project)
    assert "core.fsmonitor=false" in onboard.subprocess.run.call_args.args[0]


def test_linked_inputs_and_storage_cannot_escape_workspace(project: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    (outside / "private.py").write_text("private=1", encoding="utf-8")
    try:
        (project / "external.py").symlink_to(outside / "private.py")
        (project / ".nova").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks require developer mode on this Windows host")
    assert "external.py" not in onboard.fingerprints(project)
    with pytest.raises(ValueError, match="links"):
        onboard.storage(project)
    assert not (outside / "onboard").exists()


def test_resource_limit_preserves_existing_data(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = onboard.storage(project)
    directory.mkdir(parents=True)
    old = directory / "graph.json"
    old.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(onboard, "MAX_FILE_BYTES", 4)
    with pytest.raises(ValueError, match="input exceeds"):
        onboard.fingerprints(project)
    assert old.read_text(encoding="utf-8") == "previous"


async def test_cancellation_kills_parser_and_preserves_previous_graph(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = onboard.storage(project)
    directory.mkdir(parents=True)
    (directory / "graph.json").write_text("previous", encoding="utf-8")
    started = asyncio.Event()
    waiting = asyncio.Event()
    process = SimpleNamespace(returncode=None)

    async def wait() -> int:
        started.set()
        await waiting.wait()
        return process.returncode

    def kill() -> None:
        process.returncode = -1
        waiting.set()

    process.wait = wait
    process.kill = Mock(side_effect=kill)
    monkeypatch.setattr(onboard.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(onboard.analyze(project))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    assert (directory / "graph.json").read_text(encoding="utf-8") == "previous"


async def test_parser_failure_preserves_graph(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard, "_WORKER", "raise RuntimeError('parser failure')")
    with pytest.raises(ValueError, match="extraction failed"):
        await onboard.analyze(project)
    assert not (project / ".nova").exists()


async def test_parser_timeout_kills_process_without_publishing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard, "_WORKER", "import time; time.sleep(20)")
    monkeypatch.setattr(onboard, "PARSER_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(TimeoutError):
        await onboard.analyze(project)
    assert not (project / ".nova").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DOS short-path aliases")
async def test_parser_preserves_edges_with_short_temp_paths(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes

    with onboard.tempfile.TemporaryDirectory(prefix="nova-long-path-test-") as temporary:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(temporary, buffer, len(buffer))
        if not length or buffer.value.lower() == temporary.lower():
            pytest.skip("Short path aliases are disabled on this volume")
        monkeypatch.setattr(onboard.tempfile, "tempdir", buffer.value)
        graph = await onboard.analyze(project)
        assert any(edge["type"] == "calls" for edge in graph["edges"]), graph
        assert "main()" in onboard.impact(graph, "greet")


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point boundary")
def test_windows_junction_storage_is_rejected(project: Path, tmp_path: Path) -> None:
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(project / ".nova"))
    with pytest.raises(ValueError, match="links"):
        onboard.storage(project)
    assert not (outside / "onboard").exists()


async def test_command_registered_and_build_returns_normal_agent_prompt(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novacode_cli.artifacts import registry, server

    artifacts = registry.ArtifactRegistry()
    monkeypatch.setattr(registry, "get_registry", lambda: artifacts)
    monkeypatch.setattr(server, "artifact_url", lambda identity: f"http://127.0.0.1/{identity}")
    commands = CommandRegistry()
    register_commands(commands)
    result = await commands.get("onboard")(context(project))
    assert "normal file tools and permissions" in result
    assert ".nova/onboard/guide.md" in result
    assert artifacts.list()[0].type == "dashboard"
    assert "__NOVA_ONBOARD_DATA__" not in artifacts.list()[0].content
    assert await handle_onboard_command(context(project, "open")) is True
    question = await handle_onboard_command(context(project, "ask How does greet work?"))
    assert "How does greet work?" in question
    assert "untrusted evidence" in question
    assert len(question) < 20_000
    assert await handle_onboard_command(context(project, "impact greet")) is True
    assert "guided learning tour" in await handle_onboard_command(context(project, "tour"))


@pytest.mark.parametrize("mode", ["plan", "sandbox"])
async def test_build_obeys_plan_and_workspace_boundaries(project: Path, mode: str) -> None:
    ctx = context(project, sandbox_type="docker" if mode == "sandbox" else None)
    ctx.session_state.plan_mode_enabled = mode == "plan"
    assert await handle_onboard_command(ctx) is True
    assert not (project / ".nova").exists()


async def test_corrupt_graph_requires_explicit_refresh(project: Path) -> None:
    directory = onboard.storage(project)
    directory.mkdir(parents=True)
    (directory / "graph.json").write_text('{"version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid onboarding graph"):
        onboard.load_graph(project)


async def test_dashboard_escapes_untrusted_project_text(project: Path) -> None:
    graph = await onboard.analyze(project)
    graph["project"] = "</script><script>alert('host')</script>"
    html = onboard.dashboard(project, graph, ["new.py"])
    assert graph["project"] not in html
    assert "\\u003c/script\\u003e" in html
    assert "connect-src 'none'" in html
    assert "textContent" in html
    assert "new.py" in html
    assert not any(cdn in html for cdn in ("unpkg.com", "jsdelivr.net"))
    payload = html.split("const data = ", 1)[1].split(";\nconst $", 1)[0]
    assert json.loads(payload)["project"] == graph["project"]


async def test_tui_command_and_home_suggest_onboarding() -> None:
    from novacode_cli.tui.app import TUI_COMMANDS
    from novacode_cli.tui.home import HomeIdentity, home_presentation
    from novacode_cli.tui.palette import palette_for
    from novacode_cli.tui.widgets import NOVA_TOKYO_NIGHT

    assert TUI_COMMANDS["onboard"].handler == "_run_onboard"
    identity = HomeIdentity("test", "model", "provider", "project", "session", None)
    presentation = home_presentation("centered", 80, identity, palette_for(NOVA_TOKYO_NIGHT))
    assert "/onboard" in presentation.tip.plain
