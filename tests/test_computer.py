"""Local computers stay opt-in, session-bound, observable, and user-controlled."""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import os
import sys
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from novacode_cli.computer import runtime
from novacode_cli.computer.preview import ComputerPreview
from novacode_cli.tools.computer_tools import ComputerActionInput, build_computer_tools

if TYPE_CHECKING:
    from pathlib import Path

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII="
)


def test_standard_windows_qemu_install_is_discovered_with_stale_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "old-bin"))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    directory = tmp_path / "qemu"
    directory.mkdir()
    for name in ("qemu-system-x86_64.exe", "qemu-img.exe"):
        (directory / name).touch()
    runtime._prepare_qemu_path()
    assert os.environ["PATH"].split(os.pathsep) == [str(tmp_path / "old-bin"), str(directory)]


def test_existing_qemu_path_keeps_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(tmp_path / name))
    runtime._prepare_qemu_path()
    assert os.environ["PATH"] == str(tmp_path)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_incomplete_or_non_windows_install_does_not_change_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "old-bin"))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    directory = tmp_path / "qemu"
    directory.mkdir()
    (directory / "qemu-system-x86_64.exe").touch()
    if platform == "linux":
        (directory / "qemu-img.exe").touch()
    runtime._prepare_qemu_path()
    assert os.environ["PATH"] == str(tmp_path / "old-bin")


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch):
    counter = 0
    capture_threads = []

    async def screenshot(_options: object) -> SimpleNamespace:
        nonlocal counter
        counter += 1
        capture_threads.append(threading.get_ident())
        return SimpleNamespace(
            image=PNG, width=800, height=600, scale=2.0, screenshot_id=f"shot-{counter}"
        )

    desktop = SimpleNamespace(
        screenshot=AsyncMock(side_effect=screenshot),
        click=AsyncMock(),
        double_click=AsyncMock(),
        right_click=AsyncMock(),
        move_to=AsyncMock(),
        drag=AsyncMock(),
        type_text=AsyncMock(),
        hotkey=AsyncMock(),
        scroll=AsyncMock(),
    )
    guest = SimpleNamespace(
        info=lambda: SimpleNamespace(location="local", kind="vm", runtime="qemu"),
        spacesd=AsyncMock(return_value=desktop),
        delete=AsyncMock(),
    )
    boxes = SimpleNamespace(create=AsyncMock(return_value=guest))
    doctor = AsyncMock(
        return_value=SimpleNamespace(
            report_json=json.dumps(
                {"backends": [{"backend": "qemu", "ready": True, "provisionable": False}]}
            )
        )
    )
    client = SimpleNamespace(sandboxes=lambda: boxes, local=lambda: SimpleNamespace(doctor=doctor))
    fake = SimpleNamespace(
        embedded=lambda: client,
        SandboxCreateOptions=SimpleNamespace,
        ScreenshotOptions=SimpleNamespace,
        ImageFormat=SimpleNamespace(PNG="png"),
        desktop=desktop,
        guest=guest,
        boxes=boxes,
        doctor=doctor,
        capture_threads=capture_threads,
    )
    monkeypatch.setitem(sys.modules, "cua", fake)
    yield fake
    runtime.close_computers()


async def test_opt_in_vm_creation_and_session_isolation(sdk: SimpleNamespace):
    first = runtime.get_computer("session-one")
    second = runtime.get_computer("session-two")
    assert first is runtime.get_computer("session-one")
    assert first is not second
    assert first._thread is None
    sdk.boxes.create.assert_not_awaited()
    with pytest.raises(runtime.ComputerError, match="No ready computer"):
        await first.call("screenshot")
    assert (await first.call("start"))["phase"] == "ready"
    options = sdk.boxes.create.await_args.args[0]
    assert (options.on, options.kind, options.runtime, options.os) == (
        "local",
        "vm",
        "qemu",
        "linux",
    )
    assert options.cpus == 2
    assert options.memory_mb == 4096
    assert options.token
    assert options.name.startswith("nova-")
    assert not hasattr(options, "build")
    assert (await second.call("status"))["phase"] == "stopped"
    assert sdk.capture_threads == [first._thread.ident]
    assert sdk.capture_threads[0] != threading.get_ident()
    await first.call("start")
    sdk.boxes.create.assert_awaited_once()
    await first.call("stop")
    sdk.guest.delete.assert_awaited_once()
    assert (await first.call("status"))["phase"] == "stopped"


async def test_observation_input_pause_and_scaled_coordinates(sdk: SimpleNamespace):
    computer = runtime.get_computer("observations")
    await computer.call("start")
    await computer.call("screenshot")
    shot_id = computer._observation.screenshot_id
    await computer.call("frame")  # Viewing must not invalidate the agent's observation.
    blocks = await computer.call("action", action="click", screenshot_id=shot_id, x=300, y=200)
    sdk.desktop.click.assert_awaited_once_with(150, 100)
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    with pytest.raises(runtime.ComputerError, match="fresh"):
        await computer.call("action", action="click", screenshot_id=shot_id, x=10, y=10)
    shot_id = computer._observation.screenshot_id
    with pytest.raises(runtime.ComputerError, match="inside"):
        await computer.call("action", action="click", screenshot_id=shot_id, x=800, y=0)
    await computer.call("pause")
    with pytest.raises(runtime.ComputerError, match="paused"):
        await computer.call("action", action="key", screenshot_id=shot_id, keys=["enter"])
    await computer.call("resume")
    with pytest.raises(runtime.ComputerError, match="fresh"):
        await computer.call("action", action="key", screenshot_id=shot_id, keys=["enter"])
    sdk.desktop.hotkey.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "arguments", "method", "expected"),
    [
        ("double_click", {"x": 100, "y": 200}, "double_click", (50, 100)),
        ("right_click", {"x": 100, "y": 200}, "right_click", (50, 100)),
        ("move", {"x": 100, "y": 200}, "move_to", (50, 100)),
        ("drag", {"x": 100, "y": 200, "end_x": 300, "end_y": 400}, "drag", (50, 100, 150, 200)),
        ("type", {"text": "hello"}, "type_text", ("hello",)),
        ("key", {"keys": ["ctrl", "l"]}, "hotkey", (["ctrl", "l"],)),
        ("scroll", {"dx": 0, "dy": 3}, "scroll", (0, 3)),
    ],
)
async def test_input_primitives_return_new_observations(
    sdk: SimpleNamespace, action: str, arguments: dict, method: str, expected: tuple
):
    computer = runtime.get_computer("inputs")
    await computer.call("start")
    await computer.call("screenshot")
    before = computer._observation.screenshot_id
    await computer.call("action", action=action, screenshot_id=before, **arguments)
    getattr(sdk.desktop, method).assert_awaited_once_with(*expected)
    assert computer._observation.screenshot_id != before


async def test_failed_or_uncertain_input_requires_new_observation(sdk: SimpleNamespace):
    computer = runtime.get_computer("failure")
    await computer.call("start")
    await computer.call("screenshot")
    before = computer._observation.screenshot_id
    sdk.desktop.click.side_effect = RuntimeError("guest connection interrupted")
    with pytest.raises(RuntimeError, match="interrupted"):
        await computer.call("action", action="click", screenshot_id=before)
    assert computer._observation is None
    sdk.desktop.click.assert_awaited_once()


async def test_unexpected_runtime_and_failed_initialization_clean_up(sdk: SimpleNamespace):
    computer = runtime.get_computer("bad-runtime")
    sdk.guest.info = lambda: SimpleNamespace(location="direct", kind="", runtime="")
    with pytest.raises(runtime.ComputerError, match="local QEMU VM"):
        await computer.call("start")
    sdk.guest.delete.assert_awaited_once()
    sdk.guest.spacesd.assert_not_awaited()
    assert computer._desktop is None
    assert computer._guest is None


async def test_missing_qemu_fails_before_downloading_a_guest(sdk: SimpleNamespace):
    sdk.doctor.return_value = SimpleNamespace(
        report_json=json.dumps(
            {
                "backends": [
                    {
                        "backend": "qemu",
                        "ready": False,
                        "provisionable": False,
                        "detail": "qemu-img is missing",
                    }
                ]
            }
        )
    )
    computer = runtime.get_computer("missing-qemu")
    with pytest.raises(runtime.ComputerError, match="qemu-img is missing"):
        await computer.call("start")
    sdk.boxes.create.assert_not_awaited()


async def test_stop_cancels_start_and_deletes_created_guest(sdk: SimpleNamespace):
    computer = runtime.get_computer("cancel")
    entered = threading.Event()

    async def pending_desktop(_timeout: int) -> None:
        entered.set()
        await asyncio.Event().wait()

    sdk.guest.spacesd.side_effect = pending_desktop
    start = asyncio.create_task(computer.call("start"))
    assert await asyncio.to_thread(entered.wait, 2)
    await computer.call("stop")
    with pytest.raises(asyncio.CancelledError):
        await start
    sdk.guest.delete.assert_awaited_once()
    assert (await computer.call("status"))["phase"] == "stopped"


async def test_missing_optional_dependency_is_actionable(monkeypatch: pytest.MonkeyPatch):
    def missing(_name: str) -> None:
        raise ImportError

    monkeypatch.setattr(runtime.importlib, "import_module", missing)
    computer = runtime.Computer()
    try:
        with pytest.raises(runtime.ComputerError, match=r"novacode-cli\[computer\]"):
            await computer.call("start")
    finally:
        await asyncio.to_thread(computer.close)


@pytest.mark.parametrize(
    "fields",
    [
        {"x": -1},
        {"x": True},
        {"dx": 101},
        {"keys": ["a"] * 6},
        {"text": "a" * 10001},
        {"keys": [""]},
        {"keys": []},
        {"unknown": "value"},
    ],
)
def test_input_validation_rejects_unbounded_or_coerced_arguments(fields: dict):
    with pytest.raises(ValidationError):
        ComputerActionInput(action="click", screenshot_id="observed", **fields)


async def test_tools_keep_multimodal_results_and_user_controls_private(sdk: SimpleNamespace):
    from novacode_cli.agents.code_mode import DIRECT_ONLY_TOOLS
    from novacode_cli.hitl.interrupts import get_interrupt_configs
    from novacode_cli.security.policy import get_policy
    from novacode_cli.ui.hitl_approval import check_plan_mode_blocked

    tools = {tool.name: tool for tool in build_computer_tools("tool-session")}
    sdk.boxes.create.assert_not_awaited()
    assert set(tools) == {"computer_status", "computer_screenshot", "computer_action"}
    assert "computer_action" in DIRECT_ONLY_TOOLS
    assert "computer_screenshot" in DIRECT_ONLY_TOOLS
    assert get_policy().evaluate("computer_action", {}).tier == "ask"
    config = get_interrupt_configs()["computer_action"]
    assert "screenshot_id" in config["args_schema"]["properties"]
    assert check_plan_mode_blocked(
        {"action_requests": [{"name": "computer_action"}]}, plan_mode_enabled=True
    )[0]
    assert "No ready computer" in (await tools["computer_screenshot"].ainvoke({}))[0]["text"]
    await runtime.get_computer("tool-session").call("start")
    message = await tools["computer_screenshot"].ainvoke(
        {"type": "tool_call", "id": "screenshot", "name": "computer_screenshot", "args": {}}
    )
    assert message.content[1]["type"] == "image_url"
    status = await tools["computer_status"].ainvoke({})
    assert "token" not in status
    assert "http" not in status


def test_preview_authentication_origin_controls_and_frame_cache(sdk: SimpleNamespace):
    computer = runtime.get_computer("preview-session")
    computer.call_sync("start")
    preview = ComputerPreview(computer)
    parsed = urlsplit(preview.url)
    token = parse_qs(parsed.query)["token"][0]
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)

    def request(
        path: str, *, method: str = "GET", headers: dict | None = None
    ) -> tuple[http.client.HTTPResponse, bytes]:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response, body

    auth = {"X-Nova-Token": token}
    try:
        assert request("/status")[0].status == 403
        assert (
            request("/status", headers={**auth, "Origin": "https://evil.example"})[0].status == 403
        )
        assert request("/status", headers={**auth, "Host": "evil.example"})[0].status == 403
        response, html = request(parsed.path + "?" + parsed.query)
        assert response.status == 200
        assert b"X-Nova-Token" in html
        assert response.getheader("Referrer-Policy") == "no-referrer"
        assert request("/frame", headers=auth)[1] == PNG
        assert request("/frame", headers=auth)[1] == PNG
        sdk.desktop.screenshot.assert_awaited_once()  # Startup frame is reused within a second.
        assert request("/pause", method="POST", headers=auth)[0].status == 403
        assert not computer.call_sync("status")["paused"]
        control = {**auth, "X-Nova-Request": "1"}
        assert request("/pause", method="POST", headers=control)[0].status == 200
        assert computer.call_sync("status")["paused"]
        assert request("/resume", method="POST", headers=control)[0].status == 200
        assert request("/start", method="POST", headers=control)[0].status == 404
        assert request("/stop", method="POST", headers=control)[0].status == 200
        assert computer.call_sync("status")["phase"] == "stopped"
    finally:
        connection.close()
        preview.close()


async def test_failed_stop_preserves_handle_for_retry(sdk: SimpleNamespace):
    computer = runtime.get_computer("cleanup-retry")
    await computer.call("start")
    sdk.guest.delete.side_effect = RuntimeError("temporarily unavailable")
    with pytest.raises(runtime.ComputerError, match="Retry /computer stop"):
        await computer.call("stop")
    assert computer._guest is sdk.guest
    assert (await computer.call("status"))["phase"] == "error"
    sdk.guest.delete.side_effect = None
    await computer.call("stop")
    assert computer._guest is None


@pytest.mark.timeout(30)
async def test_tui_computer_commands_open_private_preview_and_clean_up(
    sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    import webbrowser

    from novacode_cli.tui.widgets import PromptInput
    from tests.test_tui_home_preferences import _app

    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setenv("NOVA_DISABLE_UPDATE_CHECK", "1")
    app = _app()
    entered = threading.Event()
    release = asyncio.Event()

    async def pending_desktop(_timeout: int) -> SimpleNamespace:
        entered.set()
        await release.wait()
        return sdk.desktop

    sdk.guest.spacesd.side_effect = pending_desktop
    async with app.run_test(size=(90, 35)) as pilot:
        await pilot.pause()
        app._sandbox_id = "remote-sandbox"
        await app._run_computer("/computer start").wait()
        sdk.boxes.create.assert_not_awaited()
        assert not opened
        app._sandbox_id = None
        start = app._run_computer("/computer start")
        assert await asyncio.to_thread(entered.wait, 2)
        await app._run_computer("/computer status").wait()
        await app._run_computer("/computer preview").wait()
        computer = runtime.get_computer(app.session_state.session_id)
        assert (await computer.call("status"))["phase"] == "starting"
        sdk.guest.delete.assert_not_awaited()
        assert computer._loop is not None
        computer._loop.call_soon_threadsafe(release.set)
        await start.wait()
        assert (await computer.call("status"))["phase"] == "ready"
        assert opened == [computer.preview_url(), computer.preview_url()]
        await pilot.press("h", "i")
        assert app.query_one("#prompt", PromptInput).text == "hi"
        await app._run_computer("/computer pause").wait()
        assert (await computer.call("status"))["paused"]
        await app._run_computer("/computer resume").wait()
        assert not (await computer.call("status"))["paused"]
    sdk.guest.delete.assert_awaited_once()
    assert computer._closed
